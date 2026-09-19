"""
P18-W2 fixed relay entrypoint: ChatGPT-created GitHub issue -> existing W1
automatic dispatch -> Telegram owner -> DSH -> bounded async result
correlation -> GitHub issue terminal-result comment.

Invoked ONLY by .github/workflows/relay-async.yml in this private control
repository, never by hand for the live canary.

Reuses, rather than rediscovers, the accepted P17-W1 async correlation
method (see docs/phase17/P17_W1_ASYNC_RESULT_CORRELATION_REPORT.md in the
DSH repo): dispatch via the bot's own numeric shorthand
(`2-1 <task text>`, pinned to the test identities
`dsh-test-project` / `test-pm`), capture the ACK, then bounded-poll
`get_history` for the one terminal message carrying the caller-generated
correlation marker. See relay/result_collector.py for the correlation
logic itself.

W2 issue contract (frozen, narrower than W1's already-narrow contract):
  schema: p18-w2-canary/v1
  probe: async-task
  correlation_id: <unique>
No destination, Telegram command, shell command, project/profile, or
execution flag is ever accepted from the issue -- the dispatched task
text is a single fixed template with only the (already-validated)
correlation_id substituted in.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from state_machine import (  # noqa: E402
    DispatchStateStore, TransitionDenied, classify_send_result, FAILED_RETRYABLE, FAILED_TERMINAL,
)
from result_collector import (  # noqa: E402
    ResultStateStore, find_boundary_id, scan_terminal_candidates, classify,
    PENDING, COMPLETED, FAILED, AMBIGUOUS, TIMEOUT,
)

# ---------------------------------------------------------------------------
# Frozen W2 contract
# ---------------------------------------------------------------------------

def _get_allowed_authors() -> set[str]:
    raw = os.environ.get("DSH_RELAY_ALLOWED_GITHUB_USERS", "").strip()
    if not raw:
        return set()
    return {u.strip() for u in raw.split(",") if u.strip()}

REQUIRED_TITLE_PREFIX = (
    os.environ.get("DSH_RELAY_TITLE_PREFIX", "").strip()
    or ("[DSH-TASK]", "[P18-W2-CANARY]")
)
REQUIRED_SCHEMA = "p18-w2-canary/v1"
REQUIRED_PROBE = "async-task"
ALLOWED_BODY_FIELDS = {"schema", "probe", "correlation_id"}

PINNED_BOT = os.environ.get("DSH_RELAY_TELEGRAM_TARGET", "dsh_relay_bot").strip().lstrip("@")
PROJECT_ALIAS = os.environ.get("DSH_RELAY_PROJECT_ALIAS", "2")
PM_ALIAS = os.environ.get("DSH_RELAY_PM_ALIAS", "1")

POLL_INTERVAL_SECONDS = 8
RESULT_TIMEOUT_SECONDS = 420

STATE_DIR = Path(os.environ.get("DSH_RELAY_STATE_DIR") or str(Path(__file__).resolve().parent.parent / "state"))
DISPATCH_STATE_FILE = STATE_DIR / "w2_dispatch_state.json"
RESULT_STATE_FILE = STATE_DIR / "w2_result_state.json"
TELEGRAM_ENV_PATH = os.environ.get("DSH_RELAY_TELEGRAM_ENV_PATH") or os.environ.get("TELEGRAM_ENV_PATH")
TELEGRAM_MCP_EXE = os.environ.get(
    "DSH_RELAY_TELEGRAM_MCP_EXE",
    os.environ.get("TELEGRAM_MCP_EXE", "telegram-mcp"),
)


class ValidationError(Exception):
    pass


def task_template(correlation_id: str) -> str:
    """Fixed template -- only correlation_id (already validated against
    ^[A-Za-z0-9_.-]{6,128}$) is substituted. No issue-supplied free text
    ever reaches this string."""
    return (
        f"{PROJECT_ALIAS}-{PM_ALIAS} P18-W2 ASYNC RESULT CORRELATION CANARY.\n\n"
        f"Correlation marker:\n{correlation_id}\n\n"
        "This is a read-only transport/lifecycle canary via the GitHub relay.\n\n"
        "Do not modify files.\n"
        "Do not create commits.\n"
        "Do not create a council.\n"
        "Do not perform unrelated work.\n\n"
        "Return exactly this single line and nothing else:\n\n"
        f"P18_W2_TERMINAL_OK {correlation_id}"
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
        raise ValidationError(f"GITHUB_ISSUE_READ FAIL: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def gh_comment_issue(repo: str, number: int, body: str) -> None:
    proc = subprocess.run(
        ["gh", "issue", "comment", str(number), "--repo", repo, "--body-file", "-"],
        input=body, capture_output=True, text=True, check=False, env=_gh_env(),
    )
    if proc.returncode != 0:
        raise ValidationError(f"GITHUB_ISSUE_COMMENT FAIL: {proc.stderr.strip()}")


# ---------------------------------------------------------------------------
# Issue contract validation
# ---------------------------------------------------------------------------

def parse_body_fields(body: str) -> dict:
    fields: dict[str, str] = {}
    for raw_line in (body or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$", line)
        if not m:
            raise ValidationError(f"ISSUE_SCHEMA_VALIDATION FAIL: unparsable line: {line!r}")
        key, value = m.group(1), m.group(2).strip().strip('"').strip("'")
        if key in fields:
            raise ValidationError(f"ISSUE_SCHEMA_VALIDATION FAIL: duplicate field {key!r}")
        fields[key] = value
    return fields


def validate_issue(issue: dict, repo: str) -> dict:
    allowed_authors = _get_allowed_authors()
    if not allowed_authors or "*" in allowed_authors:
        raise ValidationError("No authorized GitHub users configured")
    author = (issue.get("author") or {}).get("login")
    if not author or author not in allowed_authors:
        raise ValidationError(f"ISSUE_AUTHOR_VALIDATION FAIL: author={author!r}")
    title = issue.get("title") or ""
    if not title.startswith(REQUIRED_TITLE_PREFIX):
        raise ValidationError(f"ISSUE_SCHEMA_VALIDATION FAIL: title prefix mismatch: {title!r}")
    fields = parse_body_fields(issue.get("body") or "")
    unknown = set(fields) - ALLOWED_BODY_FIELDS
    if unknown:
        raise ValidationError(f"ARBITRARY_COMMAND_BLOCKED: unknown field(s) {sorted(unknown)}")
    missing = ALLOWED_BODY_FIELDS - set(fields)
    if missing:
        raise ValidationError(f"ISSUE_SCHEMA_VALIDATION FAIL: missing field(s) {sorted(missing)}")
    if fields["schema"] != REQUIRED_SCHEMA:
        raise ValidationError(f"ISSUE_SCHEMA_VALIDATION FAIL: schema={fields['schema']!r}")
    if fields["probe"] != REQUIRED_PROBE:
        raise ValidationError(f"ARBITRARY_COMMAND_BLOCKED: probe={fields['probe']!r} (only 'async-task' allowed)")
    correlation_id = fields["correlation_id"]
    if not correlation_id or not re.match(r"^[A-Za-z0-9_.-]{6,128}$", correlation_id):
        raise ValidationError(f"ISSUE_SCHEMA_VALIDATION FAIL: bad correlation_id {correlation_id!r}")
    return {"correlation_id": correlation_id}


# ---------------------------------------------------------------------------
# MCP / Telegram -- one session held open across boundary + send + polling
# ---------------------------------------------------------------------------

class TelegramSession:
    def __init__(self):
        self._cm = None
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


async def mcp_get_history(session: ClientSession, limit: int = 20) -> dict:
    result = await session.call_tool("get_history", {"bot": PINNED_BOT, "limit": limit})
    return json.loads(result.content[0].text)


async def mcp_send_message(session: ClientSession, message: str, timeout: int = 45) -> dict:
    result = await session.call_tool("send_message", {"bot": PINNED_BOT, "message": message, "timeout": timeout})
    return json.loads(result.content[0].text)


# ---------------------------------------------------------------------------
# Comment builders
# ---------------------------------------------------------------------------

def build_dispatched_comment(correlation_id: str) -> str:
    return (
        "P18-W2 DISPATCHED\n"
        f"correlation_id: {correlation_id}\n"
        f"target: @{PINNED_BOT}\n"
        f"project/pm: {PROJECT_ALIAS}-{PM_ALIAS}\n"
        "DSH source/config/schema changes: 0 / 0 / 0\n"
    )


def build_terminal_comment(correlation_id: str, result_entry: dict) -> str:
    status = result_entry["status"]
    task_id = result_entry.get("task_id")
    terminal_status = result_entry.get("terminal_status")
    msg_id = result_entry.get("terminal_message_id")
    reason = result_entry.get("reason", "")
    lines = [f"P18-W2 {status}", f"correlation_id: {correlation_id}"]
    if task_id:
        lines.append(f"task_id: {task_id}")
    if terminal_status:
        lines.append(f"terminal_status: {terminal_status}")
    if msg_id:
        lines.append(f"evidence: matched via Telegram MCP get_history, message id {msg_id} (from @{PINNED_BOT})")
    else:
        lines.append("evidence: no exact single terminal match found via Telegram MCP get_history "
                      f"within the {RESULT_TIMEOUT_SECONDS}s bound" if status == TIMEOUT
                      else "evidence: Telegram MCP get_history scan did not settle to a single exact match")
    if reason:
        lines.append(f"reason: {reason}")
    lines.append("DSH source/config/schema changes: 0 / 0 / 0")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(repo: str, issue_number: int, correlation_id: str, dispatch_store: DispatchStateStore,
               result_store: ResultStateStore) -> int:
    async with TelegramSession() as session:
        boundary_id: int | None = None
        need_send = True
        try:
            dispatch_store.reserve(repo, issue_number, correlation_id)
        except TransitionDenied as e:
            if e.code == "ALREADY_DELIVERED":
                need_send = False
                entry = dispatch_store.get_entry(repo, issue_number, correlation_id)
                boundary_id = (entry.get("detail") or {}).get("boundary_id")
                print(f"DOUBLE-SEND PROTECTION: PASS (already {entry['status']}, resuming result "
                      f"collection with stored boundary_id={boundary_id}, no redispatch)")
            else:
                print(f"DOUBLE-SEND PROTECTION: FAIL ({e.code}: {e})")
                return 1
        else:
            print("DOUBLE-SEND PROTECTION: PASS (reserved)")

        if need_send:
            history_before = await mcp_get_history(session, limit=20)
            boundary_id = find_boundary_id(history_before)
            print(f"PRE-DISPATCH BOUNDARY: message id {boundary_id}")

            task_text = task_template(correlation_id)
            try:
                send_payload = await mcp_send_message(session, task_text, timeout=45)
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
            dispatch_store.mark_sent(repo, issue_number, correlation_id,
                                      {"boundary_id": boundary_id, "send_payload": send_payload})

            try:
                gh_comment_issue(repo, issue_number, build_dispatched_comment(correlation_id))
                print("GITHUB DISPATCHED COMMENT: PASS")
            except ValidationError as e:
                print(f"GITHUB DISPATCHED COMMENT: FAIL ({e}) -- Telegram already delivered, state stays SENT")
                return 1

            dispatch_store.mark_completed(repo, issue_number, correlation_id,
                                           {"boundary_id": boundary_id, "dispatched_comment_posted": True})

        # -------------------------------------------------------------
        # Bounded result collection
        # -------------------------------------------------------------
        result_entry = result_store.start_or_resume(repo, issue_number, correlation_id,
                                                      boundary_id=boundary_id, timeout_seconds=RESULT_TIMEOUT_SECONDS)

        if result_entry["status"] != PENDING:
            print(f"RESULT ALREADY SETTLED: {result_entry['status']} (idempotent, resuming report only)")
        else:
            from datetime import datetime, timezone
            deadline = datetime.fromisoformat(result_entry["deadline"])
            poll_count = 0
            while True:
                history = await mcp_get_history(session, limit=20)
                candidates = scan_terminal_candidates(history, boundary_id, correlation_id, PINNED_BOT)
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
                                                         reason=f"deadline reached after {poll_count} poll(s), zero exact matches")
                    print(f"RESULT COLLECTOR: TIMEOUT after {poll_count} poll(s)")
                    break
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

        if not result_entry.get("comment_posted"):
            gh_comment_issue(repo, issue_number, build_terminal_comment(correlation_id, result_entry))
            result_store.mark_comment_posted(repo, issue_number, correlation_id)
            print("GITHUB TERMINAL COMMENT: PASS")
        else:
            print("GITHUB TERMINAL COMMENT: SKIPPED (already posted, duplicate-comment guard held)")

        print(f"P18-W2 RELAY RESULT: {result_entry['status']}")
        return 0 if result_entry["status"] == COMPLETED else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="DSH automatic GitHub->Telegram async result-correlation entrypoint")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.repo:
        print("CONFIG ERROR: --repo must be provided or GITHUB_REPOSITORY environment variable set")
        return 1

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    dispatch_store = DispatchStateStore(DISPATCH_STATE_FILE)
    result_store = ResultStateStore(RESULT_STATE_FILE)

    print(f"TARGET_PINNED: @{PINNED_BOT}")
    try:
        issue = gh_view_issue(args.repo, args.issue)
        print("GITHUB ISSUE READ: PASS")
    except ValidationError as e:
        print(f"GITHUB ISSUE READ: FAIL ({e})")
        return 1

    try:
        parsed = validate_issue(issue, args.repo)
    except ValidationError as e:
        print(f"ISSUE VALIDATION: FAIL ({e})")
        return 1

    correlation_id = parsed["correlation_id"]
    print("ISSUE VALIDATION: PASS")
    print(f"CORRELATION_ID: {correlation_id}")
    print(f"ISSUE: {args.repo}#{args.issue}")

    if args.dry_run:
        print("DRY_RUN: no Telegram message sent, no comment posted")
        return 0

    return asyncio.run(run(args.repo, args.issue, correlation_id, dispatch_store, result_store))


if __name__ == "__main__":
    sys.exit(main())
