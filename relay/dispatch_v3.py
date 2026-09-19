"""
P18-W3 fixed relay entrypoint: production-shaped SINGLE-task GitHub
dispatch contract -> canonical Telegram command -> existing W1/W2
automatic relay -> Telegram owner -> OwnerControlService -> DSH -> async
terminal result -> GitHub.

Invoked ONLY by .github/workflows/relay-v3.yml in this private control
repository, never by hand for the live canary.

Layering:
  relay/contract_v3.py    strict parse/validate + canonical-ID allowlist
                           + canonical Telegram command compiler
  relay/issue_identity.py immutable issue identity (repo+issue_number ->
                           correlation_id + payload digest), independent
                           of and layered UNDER the per-correlation_id
                           dispatch/result guards below
  relay/state_machine.py  DispatchStateStore -- send idempotency
                           (RESERVED/SENT/COMPLETED/FAILED_RETRYABLE/
                           FAILED_TERMINAL), same module W1/W2 use
  relay/result_collector.py  ResultStateStore -- bounded async result
                           correlation (PENDING/COMPLETED/FAILED/
                           AMBIGUOUS/TIMEOUT), same module W2 uses

DSH remains the sole final authority; every check here is an additive
fail-closed layer, never a replacement for OwnerControlService.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from state_machine import (  # noqa: E402
    DispatchStateStore, TransitionDenied, classify_send_result, FAILED_RETRYABLE, FAILED_TERMINAL,
)
from target import get_required_telegram_target, TelegramTargetError  # noqa: E402
from result_collector import (  # noqa: E402
    ResultStateStore, find_boundary_id, scan_terminal_candidates, classify,
    PENDING, COMPLETED, FAILED, AMBIGUOUS, TIMEOUT,
    extract_task_id_from_ack, scan_terminal_candidates_by_task_id,
    result_timeout_seconds_for, TASK_ID_UNRECOVERABLE,
    scan_ack_candidates_by_correlation, classify_ack, AckCandidate,
    ACK_RECOVERY_TIMEOUT_SECONDS, ACK_RECOVERY_POLL_INTERVAL_SECONDS,
    # P18-W5 multimode:
    result_timeout_seconds_for_v3, scan_terminal_candidates_by_task_id_multimode,
    # P24.1R2 boundary-aware history retrieval:
    HISTORY_ESCALATION_LIMITS, history_window_covers_boundary,
)
from issue_identity import (  # noqa: E402
    IssueIdentityStore, NEW, RESUME, PAYLOAD_MUTATED as IDENTITY_PAYLOAD_MUTATED,
    CORRELATION_CHANGED,
)
from contract_v3 import (  # noqa: E402
    parse_and_validate, compile_telegram_command, ContractError,
    ALREADY_DELIVERED as CONTRACT_ALREADY_DELIVERED, PAYLOAD_MUTATED as CONTRACT_PAYLOAD_MUTATED,
    AMBIGUOUS_RESULT as CONTRACT_AMBIGUOUS_RESULT,
)

def _get_allowed_authors() -> set[str]:
    json_raw = os.environ.get("DSH_RELAY_ALLOWED_GITHUB_USERS_JSON", "").strip()
    if json_raw:
        try:
            parsed = json.loads(json_raw)
            if isinstance(parsed, list):
                return {str(u).strip() for u in parsed if str(u).strip()}
        except Exception:
            return set()
        return set()

    csv_raw = os.environ.get("DSH_RELAY_ALLOWED_GITHUB_USERS", "").strip()
    if not csv_raw:
        return set()
    if csv_raw.startswith("[") and csv_raw.endswith("]"):
        try:
            parsed = json.loads(csv_raw)
            if isinstance(parsed, list):
                return {str(u).strip() for u in parsed if str(u).strip()}
        except Exception:
            return set()
    return {u.strip() for u in csv_raw.split(",") if u.strip()}

REQUIRED_TITLE_PREFIX = (
    os.environ.get("DSH_RELAY_TITLE_PREFIX", "").strip()
    or ("[DSH-TASK]", "[P18-W3-CANARY]")
)

def _get_pinned_bot() -> str:
    return get_required_telegram_target()


def __getattr__(name: str):
    if name == "PINNED_BOT":
        return get_required_telegram_target()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


POLL_INTERVAL_SECONDS = 8
RESULT_TIMEOUT_SECONDS = 420

STATE_DIR = Path(os.environ.get("DSH_RELAY_STATE_DIR") or str(Path(__file__).resolve().parent.parent / "state"))
DISPATCH_STATE_FILE = STATE_DIR / "w3_dispatch_state.json"
RESULT_STATE_FILE = STATE_DIR / "w3_result_state.json"
IDENTITY_STATE_FILE = STATE_DIR / "w3_issue_identity.json"

TELEGRAM_ENV_PATH = os.environ.get("DSH_RELAY_TELEGRAM_ENV_PATH") or os.environ.get("TELEGRAM_ENV_PATH")
TELEGRAM_MCP_EXE = os.environ.get(
    "DSH_RELAY_TELEGRAM_MCP_EXE",
    os.environ.get("TELEGRAM_MCP_EXE", "telegram-mcp"),
)


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def _gh_env() -> dict:
    env = dict(os.environ)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        env["GH_TOKEN"] = token
    return env


def gh_view_issue(repo: str, number: int) -> dict:
    proc = subprocess.run(
        ["gh", "issue", "view", str(number), "--repo", repo,
         "--json", "number,title,body,author,url,createdAt,state"],
        capture_output=True, text=True, check=False, env=_gh_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"GITHUB_ISSUE_READ FAIL: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def gh_comment_issue(repo: str, number: int, body: str) -> None:
    proc = subprocess.run(
        ["gh", "issue", "comment", str(number), "--repo", repo, "--body-file", "-"],
        input=body, capture_output=True, text=True, check=False, env=_gh_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"GITHUB_ISSUE_COMMENT FAIL: {proc.stderr.strip()}")


def build_rejection_comment(code: str, sanitized_detail: str) -> str:
    # Sanitized and useful: the typed code plus a short, safe explanation.
    # NEVER includes a raw exception/stack trace, a session/credential
    # path, or any value read from a secret-bearing file -- only the
    # typed code and (for schema/field errors) the field name involved.
    return (
        f"P18-W3 REJECTED\n"
        f"code: {code}\n"
        f"detail: {sanitized_detail}\n"
        "DSH source/config/schema changes: 0 / 0 / 0\n"
    )


# ---------------------------------------------------------------------------
# MCP / Telegram
# ---------------------------------------------------------------------------

class TelegramSession:
    def __init__(self):
        self.session: ClientSession | None = None

    async def __aenter__(self) -> ClientSession:
        sub_env = dict(os.environ)
        if TELEGRAM_ENV_PATH:
            sub_env["TELEGRAM_ENV_PATH"] = TELEGRAM_ENV_PATH
        server_params = StdioServerParameters(
            command=TELEGRAM_MCP_EXE, args=[],
            env=sub_env,
        )
        self._stdio_cm = stdio_client(server_params)
        read, write = await self._stdio_cm.__aenter__()
        self._session_cm = ClientSession(read, write)
        self.session = await self._session_cm.__aenter__()
        await self.session.initialize()
        return self.session

    async def __aexit__(self, *exc):
        await self._session_cm.__aexit__(*exc)
        await self._stdio_cm.__aexit__(*exc)


async def mcp_get_history(session: ClientSession, limit: int = 20, bot: str | None = None) -> dict:
    target_bot = bot or get_required_telegram_target()
    result = await session.call_tool("get_history", {"bot": target_bot, "limit": limit})
    return json.loads(result.content[0].text)


# P24.1R2: used ONLY by the terminal-result poll loop (main()'s while-True
# below) -- the one call site that runs unattended for up to
# LONG_RESULT_TIMEOUT_SECONDS/COUNCIL/DEBATE_RESULT_TIMEOUT_SECONDS, where a
# single fixed-size get_history(limit=20) call has been proven (P24.1R2
# incident #67 finding) unable to ever recover a terminal message that
# unrelated chat traffic has pushed past the newest 20. `recover_
# authoritative_task_id()`'s ACK scan and the one-time pre-dispatch boundary
# snapshot both keep calling plain `mcp_get_history(session, limit=20)`
# directly, unchanged -- ACK recovery is bounded to
# ACK_RECOVERY_TIMEOUT_SECONDS (60s) and the incident evidence shows it
# already resolves via the fast path with no observed defect, and the
# boundary snapshot only needs the single highest message id, which the
# newest-N response always contains regardless of N.
async def mcp_get_history_boundary_aware(session: ClientSession, boundary_id: int, bot: str | None = None) -> dict:
    """Escalates through HISTORY_ESCALATION_LIMITS (each one single
    get_history call) until the returned window can be PROVEN to cover
    every message newer than boundary_id, or the bounded escalation
    sequence is exhausted -- whichever comes first. Returns the LAST
    fetched payload either way; callers apply the existing boundary_id/
    task_id/bot-identity filtering and classify() unchanged -- this
    function only decides how big a single get_history call to make, never
    which messages are candidates."""
    history: dict = {"messages": []}
    for limit in HISTORY_ESCALATION_LIMITS:
        history = await mcp_get_history(session, limit=limit, bot=bot)
        if history_window_covers_boundary(history, boundary_id, limit):
            break
    return history


async def mcp_send_message(session: ClientSession, message: str, timeout: int = 45, bot: str | None = None) -> dict:
    target_bot = bot or get_required_telegram_target()
    result = await session.call_tool("send_message", {"bot": target_bot, "message": message, "timeout": timeout})
    return json.loads(result.content[0].text)


def build_dispatched_comment(payload, compiled_header_only: str, task_id: str | None = None, bot: str | None = None) -> str:
    target_bot = bot or get_required_telegram_target()
    # P18-W4 Part D: `task_id` is None for every v1 dispatch (v1 never
    # attempts ACK recovery -- unchanged behavior) and for a v2 dispatch
    # where the ACK's task_id could not yet be recovered at comment-post
    # time; only ever included once authoritatively known.
    schema_version = getattr(payload, "schema_version", "v1")
    mode = getattr(payload, "mode", "single")
    lines = [
        "P18-W3 DISPATCHED",
        f"correlation_id: {payload.correlation_id}",
        f"target: @{target_bot}",
        f"schema: {schema_version}",
        f"mode: {mode}",
        f"project_id: {payload.project_id}",
    ]
    # P18-W5: for council/debate `pm_profile_id` is the chair; label it so.
    if mode in ("council", "debate"):
        lines.append(f"chair_profile_id: {payload.pm_profile_id}")
        lines.append(f"participants: {','.join(payload.participants)}")
    else:
        lines.append(f"pm_profile_id: {payload.pm_profile_id}")
    if mode == "debate":
        lines.append(f"debate_rounds: {payload.debate_rounds}")
        if payload.implementation_profile_id:
            lines.append(f"implementation_profile_id: {payload.implementation_profile_id}")
    lines.append(f"durability: {payload.durability}")
    # runtime_class is meaningful for SINGLE only (v2, or v3 single).
    if (schema_version == "v2") or (schema_version == "v3" and mode == "single"):
        lines.append(f"runtime_class: {payload.runtime_class}")
    if task_id:
        lines.append(f"task_id: {task_id}")
    lines += [
        f"git: commit={payload.git_commit} push={payload.git_push} remote={payload.git_remote}",
        f"review_requested: {payload.review_requested}",
        f"compiled_command_header: {compiled_header_only}",
        "DSH source/config/schema changes: 0 / 0 / 0",
    ]
    return "\n".join(lines) + "\n"


def build_terminal_comment(correlation_id: str, result_entry: dict, timeout_seconds_used: int = RESULT_TIMEOUT_SECONDS,
                            mode: str | None = None, bot: str | None = None) -> str:
    target_bot = bot or get_required_telegram_target()
    status = result_entry["status"]
    task_id = result_entry.get("task_id")
    terminal_status = result_entry.get("terminal_status")
    msg_id = result_entry.get("terminal_message_id")
    reason = result_entry.get("reason", "")
    lines = [f"P18-W3 {status}", f"correlation_id: {correlation_id}"]
    # P18-W5: `mode` is echoed when durably known (a v3 dispatch resumed
    # after issue identity was registered). Never guessed.
    if mode:
        lines.append(f"mode: {mode}")
    if task_id:
        lines.append(f"task_id: {task_id}")
    if terminal_status:
        lines.append(f"terminal_status: {terminal_status}")
    if msg_id:
        lines.append(f"evidence: matched via Telegram MCP get_history, message id {msg_id} (from @{target_bot})")
    elif status == TIMEOUT:
        lines.append("evidence: no exact single terminal match found via Telegram MCP get_history "
                      f"within the {timeout_seconds_used}s collector bound (observation/correlation "
                      "patience only -- this is not DSH's own execution timeout)")
    elif status == TASK_ID_UNRECOVERABLE:
        # P18-W4 Part D: delivery to Telegram was confirmed (the send
        # itself succeeded), but this relay could not recover DSH's
        # task_id from the synchronous ACK reply, so there is nothing to
        # correlate a terminal result against. Never guessed via a
        # timestamp/window heuristic -- fails closed here instead.
        lines.append("evidence: Telegram delivery confirmed, but no DSH task_id could be recovered from the "
                      "synchronous send_message ACK reply -- no ambiguous timestamp-window fallback was "
                      "attempted; this dispatch's real DSH outcome is unknown to this relay")
    else:
        lines.append("evidence: Telegram MCP get_history scan did not settle to a single exact match")
    if reason:
        lines.append(f"reason: {reason}")
    lines.append("DSH source/config/schema changes: 0 / 0 / 0")
    return "\n".join(lines) + "\n"


def compiled_header_only(payload) -> str:
    """The flags-only prefix of the compiled command (for the DISPATCHED
    comment) -- never the task body, so the evidence comment cannot leak
    an unexpectedly long/odd task back as a false "this is what was sent"
    claim beyond the structured fields already echoed."""
    # P18-W5: v3 derives the header from the REAL compiler (minus the
    # verbatim task body) rather than a parallel flag-emitter that could
    # drift from _compile_v3_command().
    if getattr(payload, "schema_version", "v1") == "v3":
        from dataclasses import replace
        return compile_telegram_command(replace(payload, task="")).rstrip()
    parts = [f"@{payload.project_id}", "--pm", payload.pm_profile_id, "--durability", payload.durability]
    if getattr(payload, "runtime_class", "normal") == "long":
        parts.append("--long")
    if getattr(payload, "schema_version", "v1") == "v2":
        parts.extend(["--client-correlation", payload.correlation_id])
    if payload.git_commit:
        parts.append("--commit")
    if payload.git_push:
        parts.append("--push")
    if payload.git_remote:
        parts.extend(["--remote", payload.git_remote])
    if payload.review_requested:
        parts.append("--review")
    return " ".join(parts)


# P18-W4 FAST-PATH-UNIQUENESS remediation (PM review, follow-up P0): no
# real Telegram message id is available for a fast-path candidate -- only
# the reply text was parsed, synchronously, before any get_history call.
# classify_ack() never reads message_id (only task_id/correlation_id
# matter to it), so this sentinel exists purely to let the fast-path
# candidate be merged into the SAME AckCandidate list/classify_ack() call
# as real history candidates, rather than short-circuiting outside it.
FAST_PATH_SENTINEL_MESSAGE_ID = -1


# ---------------------------------------------------------------------------
# P18-W4 ACK-causal-correlation remediation (Part E/F): the ONE
# authoritative task_id recovery procedure every v2 call site uses --
# fresh dispatch and restart/resume alike. `fresh_send_payload` is only
# ever non-None on the SAME invocation that just sent the message (a
# resumed run has no synchronous reply to try, and correctly skips
# straight to the bounded scan). Driven entirely by `boundary_id` +
# `correlation_id`, both already durable via dispatch_store/the payload
# itself -- nothing new needs to be cached across a restart for this to
# be safely re-run in full (Part F): a crash at any point before this
# returns simply means the NEXT invocation re-runs the identical bounded
# scan against the same window, never redispatching.
#
# P18-W4 FAST-PATH-UNIQUENESS remediation (PM review, follow-up P0): the
# original version of this function returned the fast-path task_id the
# instant it was found, WITHOUT ever calling get_history -- so two exact-
# correlation ACKs disagreeing on task_id (the accepted AMBIGUOUS/fail-
# closed invariant, classify_ack()) could never be detected when the
# first of the two happened to also be the synchronous reply. A
# synchronous reply is a LATENCY optimization only; it must never bypass
# the same uniqueness check a purely history-derived finding goes
# through. Fixed: a valid fast-path reply becomes an AckCandidate that is
# MERGED into the very first (immediate, no artificial delay) history
# scan's candidate list, and the merged list is classified exactly once
# via the same classify_ack() every other path uses -- so a conflicting
# exact-correlation history candidate is never suppressed by the fast
# reply, and a genuine collision still resolves to AMBIGUOUS. Because
# that merge always makes the candidate list non-empty whenever a fast
# candidate exists, this resolves (uniquely or ambiguously) on the FIRST
# poll -- DSH's acceptance ACK is near-instant, so this does not require
# waiting out the full bounded window before returning.
# ---------------------------------------------------------------------------

async def recover_authoritative_task_id(session, boundary_id: int, correlation_id: str,
                                         fresh_send_payload: dict | None = None, *,
                                         timeout_seconds: float = ACK_RECOVERY_TIMEOUT_SECONDS,
                                         poll_interval_seconds: float = ACK_RECOVERY_POLL_INTERVAL_SECONDS,
                                         bot: str | None = None) -> tuple[str | None, bool, str]:
    target_bot = bot or get_required_telegram_target()
    """Returns (task_id_or_None, ambiguous, reason). `timeout_seconds`/
    `poll_interval_seconds` default to the real production bounds --
    overridable only so tests can exercise the "never found within the
    window" path without a real 60s wait; production call sites (main()
    below) never override them."""
    fast_candidate: AckCandidate | None = None
    if fresh_send_payload is not None:
        fast_task_id = extract_task_id_from_ack(fresh_send_payload, correlation_id)
        if fast_task_id:
            fast_candidate = AckCandidate(
                message_id=FAST_PATH_SENTINEL_MESSAGE_ID,
                task_id=fast_task_id,
                correlation_id=correlation_id,
            )

    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    poll_count = 0
    while True:
        history = await mcp_get_history(session, limit=20, bot=target_bot)
        candidates = scan_ack_candidates_by_correlation(history, boundary_id, correlation_id, target_bot)
        if fast_candidate is not None and not any(c.task_id == fast_candidate.task_id for c in candidates):
            # Merge, never suppress: a real history sighting of the SAME
            # task_id is just a duplicate delivery of the identical ACK
            # (dedup by task_id, not appended a second time) -- but a
            # DIFFERENT task_id already visible in history is kept in the
            # list alongside the fast candidate, so classify_ack() still
            # sees both and fails closed on the collision instead of
            # trusting the fast reply alone.
            candidates = candidates + [fast_candidate]
        task_id, ambiguous, reason = classify_ack(candidates)
        poll_count += 1
        if ambiguous:
            return None, True, reason
        if task_id:
            if fast_candidate is not None:
                reason = (f"{reason}; fast-path candidate confirmed unique via an immediate "
                          f"post-boundary history observation (no bounded wait required)")
            else:
                reason = f"{reason} (bounded scan, {poll_count} poll(s))"
            return task_id, False, reason
        if datetime.now(timezone.utc) >= deadline:
            return None, False, f"no exact correlated ACK found within {timeout_seconds}s bounded recovery window ({poll_count} poll(s))"
        await asyncio.sleep(poll_interval_seconds)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(repo: str, issue_number: int, payload, dispatch_store: DispatchStateStore,
               result_store: ResultStateStore, bot: str | None = None) -> int:
    target_bot = bot or get_required_telegram_target()
    correlation_id = payload.correlation_id
    # P18-W4: v1 is completely unaffected below -- `is_v2` gates every new
    # behavior (task_id ACK recovery, task_id-based terminal scanning, the
    # LONG collector bound). A v1 payload's `schema_version` is always
    # "v1" (contract_v3.py's own default), so every `if is_v2:` branch
    # below is a no-op for it, byte-for-byte the pre-W4 flow.
    is_v2 = getattr(payload, "schema_version", "v1") == "v2"
    # P18-W5: v3 (single|council|debate) reuses the SAME task_id ACK-
    # recovery + task_id terminal-correlation machinery v2 established --
    # `needs_taskid_correlation` gates it for both. v1 stays on its
    # correlation-marker-in-body path, byte-for-byte unchanged.
    is_v3 = getattr(payload, "schema_version", "v1") == "v3"
    needs_taskid_correlation = is_v2 or is_v3
    v3_mode = getattr(payload, "mode", "single") if is_v3 else None
    if is_v3:
        timeout_seconds = result_timeout_seconds_for_v3(payload.mode, runtime_class=payload.runtime_class)
    elif is_v2:
        timeout_seconds = result_timeout_seconds_for(payload.runtime_class)
    else:
        timeout_seconds = RESULT_TIMEOUT_SECONDS
    async with TelegramSession() as session:
        boundary_id: int | None = None
        fresh_send_payload: dict | None = None
        need_send = True
        try:
            dispatch_store.reserve(repo, issue_number, correlation_id)
        except TransitionDenied as e:
            if e.code == "ALREADY_DELIVERED":
                need_send = False
                entry = dispatch_store.get_entry(repo, issue_number, correlation_id)
                detail = entry.get("detail") or {}
                boundary_id = detail.get("boundary_id")
                print(f"DOUBLE-SEND PROTECTION: PASS (already {entry['status']}, resuming result "
                      f"collection with stored boundary_id={boundary_id}, no redispatch)")
            else:
                print(f"DOUBLE-SEND PROTECTION: FAIL ({e.code}: {e})")
                gh_comment_issue(repo, issue_number, build_rejection_comment(
                    CONTRACT_ALREADY_DELIVERED if e.code == "ALREADY_DELIVERED" else "AMBIGUOUS_SEND_STATE",
                    "a prior dispatch attempt for this exact issue/correlation is unresolved or ambiguous",
                ))
                return 1
        else:
            print("DOUBLE-SEND PROTECTION: PASS (reserved)")

        if need_send:
            history_before = await mcp_get_history(session, limit=20, bot=target_bot)
            boundary_id = find_boundary_id(history_before)
            print(f"PRE-DISPATCH BOUNDARY: message id {boundary_id}")

            command_text = compile_telegram_command(payload)
            try:
                send_payload = await mcp_send_message(session, command_text, timeout=45, bot=target_bot)
            except Exception as e:  # noqa: BLE001
                dispatch_store.mark_failed_terminal(repo, issue_number, correlation_id, {"error": str(e)})
                print(f"TELEGRAM MCP SEND: FAIL (unclassified exception, FAILED_TERMINAL: {e})")
                return 1

            outcome = classify_send_result(send_payload)
            if outcome == FAILED_RETRYABLE:
                dispatch_store.mark_failed_retryable(repo, issue_number, correlation_id, {"send_payload": send_payload})
                print(f"TELEGRAM MCP SEND: FAIL (FAILED_RETRYABLE: {send_payload.get('error')})")
                return 1
            if outcome == FAILED_TERMINAL:
                dispatch_store.mark_failed_terminal(repo, issue_number, correlation_id, {"send_payload": send_payload})
                print(f"TELEGRAM MCP SEND: FAIL (FAILED_TERMINAL, ambiguous: {send_payload.get('error')})")
                return 1

            print(f"TELEGRAM MCP SEND: PASS (ACK reply={'yes' if send_payload.get('reply') else 'no'})")
            fresh_send_payload = send_payload

            # P18-W4 ACK-causal-correlation remediation (Part F): task_id
            # is deliberately NOT determined or persisted here anymore --
            # only `boundary_id` (already durable, unchanged) is needed for
            # a later/resumed run to independently and authoritatively
            # recover it (below). This is exactly what makes the recovery
            # step itself restart-safe: it is recomputed fresh from durable
            # facts every time, never cached mid-flight.
            dispatch_store.mark_sent(repo, issue_number, correlation_id,
                                      {"boundary_id": boundary_id, "send_payload": send_payload})

            gh_comment_issue(repo, issue_number, build_dispatched_comment(payload, compiled_header_only(payload), bot=target_bot))
            print("GITHUB DISPATCHED COMMENT: PASS")
            dispatch_store.mark_completed(repo, issue_number, correlation_id,
                                           {"boundary_id": boundary_id, "dispatched_comment_posted": True})

        result_entry = result_store.start_or_resume(repo, issue_number, correlation_id,
                                                      boundary_id=boundary_id, timeout_seconds=timeout_seconds)

        # P18-W4 ACK-causal-correlation remediation (Part E/F): for v2,
        # authoritative task_id recovery happens HERE, still gated on the
        # overall result not already being terminal (idempotent resume),
        # and re-run in full on every invocation -- fresh dispatch OR a
        # resumed/restarted one -- until it settles. See
        # recover_authoritative_task_id()'s own docstring for why this is
        # safe and restart-proof without caching task_id anywhere.
        task_id: str | None = None
        if result_entry["status"] == PENDING and needs_taskid_correlation:
            task_id, ambiguous, ack_reason = await recover_authoritative_task_id(
                session, boundary_id, correlation_id, fresh_send_payload=fresh_send_payload, bot=target_bot,
            )
            print(f"ACK TASK_ID RECOVERY: {'PASS (task_id=' + task_id + ')' if task_id else ('AMBIGUOUS' if ambiguous else 'FAIL')} ({ack_reason})")
            if ambiguous:
                result_entry = result_store.settle(repo, issue_number, correlation_id, AMBIGUOUS, reason=ack_reason)
            elif not task_id:
                result_entry = result_store.settle(repo, issue_number, correlation_id, TASK_ID_UNRECOVERABLE, reason=ack_reason)

        if result_entry["status"] != PENDING:
            print(f"RESULT ALREADY SETTLED: {result_entry['status']} (idempotent, resuming report only)")
        else:
            deadline = datetime.fromisoformat(result_entry["deadline"])
            poll_count = 0
            while True:
                # P24.1R2: boundary-aware escalating retrieval, not a fixed
                # limit=20 -- see mcp_get_history_boundary_aware()'s own
                # docstring for why this exact call site (and no other) is
                # the one that needed it.
                history = await mcp_get_history_boundary_aware(session, boundary_id, bot=target_bot)
                # P18-W4 Part E: v2 scans by exact task_id (no correlation
                # marker required anywhere in the Result body -- this is
                # what lets a FAILED/CANCELLED terminal with only a
                # generic error body still correlate correctly, closing
                # the exact gap issue #8 exposed). v1 keeps scanning by
                # correlation_marker-in-body, unchanged.
                # P18-W5: v3 uses the multimode task_id scan (generic
                # SINGLE/council-failed/debate-failed terminal shape UNION
                # the council/debate COMPLETED structured shape); v2 uses
                # the generic task_id scan; v1 keeps the correlation-marker
                # scan, all unchanged.
                if is_v3:
                    candidates = scan_terminal_candidates_by_task_id_multimode(history, boundary_id, task_id, target_bot)
                elif is_v2:
                    candidates = scan_terminal_candidates_by_task_id(history, boundary_id, task_id, target_bot)
                else:
                    candidates = scan_terminal_candidates(history, boundary_id, correlation_id, target_bot)
                state, cand, reason = classify(candidates)
                poll_count += 1
                now = datetime.now(timezone.utc)
                if state != PENDING:
                    result_entry = result_store.settle(
                        repo, issue_number, correlation_id, state,
                        task_id=(cand.task_id if cand else None),
                        terminal_status=(cand.status if cand else None),
                        terminal_message_id=(cand.message_id if cand else None),
                        reason=reason,
                    )
                    print(f"RESULT COLLECTOR: settled {state} after {poll_count} poll(s) ({reason})")
                    break
                if now >= deadline:
                    result_entry = result_store.settle(repo, issue_number, correlation_id, TIMEOUT,
                                                         reason=f"deadline reached after {poll_count} poll(s) "
                                                                f"({timeout_seconds}s collector bound), zero exact matches")
                    print(f"RESULT COLLECTOR: TIMEOUT after {poll_count} poll(s)")
                    break
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

        if not result_entry.get("comment_posted"):
            gh_comment_issue(repo, issue_number, build_terminal_comment(correlation_id, result_entry, timeout_seconds_used=timeout_seconds, mode=v3_mode, bot=target_bot))
            result_store.mark_comment_posted(repo, issue_number, correlation_id)
            print("GITHUB TERMINAL COMMENT: PASS")
        else:
            print("GITHUB TERMINAL COMMENT: SKIPPED (already posted, duplicate-comment guard held)")

        if result_entry["status"] == AMBIGUOUS:
            print(f"P18-W3 RELAY RESULT: {CONTRACT_AMBIGUOUS_RESULT}")
        else:
            print(f"P18-W3 RELAY RESULT: {result_entry['status']}")
        return 0 if result_entry["status"] == COMPLETED else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="DSH production-shaped dispatch relay entrypoint")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.repo:
        print("CONFIG ERROR: --repo must be provided or GITHUB_REPOSITORY environment variable set")
        return 1

    try:
        get_required_telegram_target()
    except TelegramTargetError as e:
        print(f"CONFIG ERROR: {e}")
        return 1

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    dispatch_store = DispatchStateStore(DISPATCH_STATE_FILE)
    result_store = ResultStateStore(RESULT_STATE_FILE)
    identity_store = IssueIdentityStore(IDENTITY_STATE_FILE)

    try:
        issue = gh_view_issue(args.repo, args.issue)
        print("GITHUB ISSUE READ: PASS")
    except RuntimeError as e:
        print(f"GITHUB ISSUE READ: FAIL ({e})")
        return 1

    title = issue.get("title") or ""
    body = issue.get("body") or ""
    author = (issue.get("author") or {}).get("login")

    allowed_authors = _get_allowed_authors()
    try:
        payload = parse_and_validate(title, body, required_title_prefix=REQUIRED_TITLE_PREFIX,
                                      author=author, expected_author=allowed_authors)
    except ContractError as e:
        print(f"CONTRACT VALIDATION: FAIL ({e.code}: {e.message})")
        gh_comment_issue(args.repo, args.issue, build_rejection_comment(e.code, e.message))
        return 1

    print("CONTRACT VALIDATION: PASS")
    print(f"CORRELATION_ID: {payload.correlation_id}")
    print(f"PROJECT_ID: {payload.project_id}")
    print(f"PM_PROFILE_ID: {payload.pm_profile_id}")

    digest = payload.payload_digest()
    identity_outcome, record = identity_store.register_or_verify(args.repo, args.issue, payload.correlation_id, digest)

    if identity_outcome == NEW:
        print(f"ISSUE IDENTITY: NEW (first acceptance, correlation_id={payload.correlation_id})")
    elif identity_outcome == RESUME:
        print("ISSUE IDENTITY: RESUME (matches original accepted payload, idempotent)")
    elif identity_outcome in (IDENTITY_PAYLOAD_MUTATED, CORRELATION_CHANGED):
        reason = ("the issue title/body no longer matches what was originally accepted"
                  if identity_outcome == IDENTITY_PAYLOAD_MUTATED
                  else f"correlation_id changed from the originally accepted {record.correlation_id!r}")
        print(f"ISSUE IDENTITY: FAIL ({identity_outcome}: {reason})")
        gh_comment_issue(args.repo, args.issue, build_rejection_comment(
            CONTRACT_PAYLOAD_MUTATED,
            f"{reason}. Original correlation_id={record.correlation_id!r} accepted at "
            f"{record.first_accepted_at}. Never redispatched merely because the issue was edited; "
            "open a NEW issue for a new dispatch.",
        ))
        return 1

    if args.dry_run:
        print("DRY_RUN: no Telegram message sent, no comment posted")
        return 0

    return asyncio.run(run(args.repo, args.issue, payload, dispatch_store, result_store))


if __name__ == "__main__":
    sys.exit(main())
