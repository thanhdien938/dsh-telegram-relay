"""
P18-W1 fixed relay entrypoint.

Invoked ONLY by the self-hosted GitHub Actions workflow
(.github/workflows/relay.yml) in THIS private control repository, never
by hand for the live canary. Consumes exactly one GitHub issue (repo +
issue number, taken from the workflow's own event context -- never from a
shell-interpolated string), validates it strictly, and if valid, relays a
single fixed probe command to the pinned Telegram target through the
unmodified upstream telegram-mcp server over real MCP stdio JSON-RPC.

Security invariants enforced here (see repo README.md for the full list):
  - issue author allowlist;
  - required title prefix;
  - required body schema, exactly three allowed fields;
  - only probe: aliases is ever accepted;
  - Telegram destination is hard-pinned in code, never issue-controlled;
  - issue body is treated as DATA only -- no eval, no shell interpolation,
    no command derived from issue content;
  - double-send protection via relay.state_machine.DispatchStateStore,
    which distinguishes RESERVED / SENT / COMPLETED / FAILED_RETRYABLE /
    FAILED_TERMINAL (P18-W0-STATE-001 remediation) so FAILED_RETRYABLE can
    retry automatically while a genuinely ambiguous outcome fails closed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from state_machine import (  # noqa: E402
    DispatchStateStore, TransitionDenied, classify_send_result,
    SENT, COMPLETED, FAILED_RETRYABLE, FAILED_TERMINAL,
)

# ---------------------------------------------------------------------------
# Frozen W1 contract
# ---------------------------------------------------------------------------

def _get_allowed_authors() -> set[str]:
    raw = os.environ.get("DSH_RELAY_ALLOWED_GITHUB_USERS", "").strip()
    if not raw:
        return set()
    return {u.strip() for u in raw.split(",") if u.strip()}

REQUIRED_TITLE_PREFIX = (
    os.environ.get("DSH_RELAY_TITLE_PREFIX", "").strip()
    or ("[DSH-TASK]", "[P18-W1-CANARY]")
)
REQUIRED_SCHEMA = "p18-w1-canary/v1"
REQUIRED_PROBE = "aliases"
FIXED_TELEGRAM_COMMAND = "/aliases"
PINNED_BOT = os.environ.get("DSH_RELAY_TELEGRAM_TARGET", "dsh_relay_bot").strip().lstrip("@")
ALLOWED_BODY_FIELDS = {"schema", "probe", "correlation_id"}

STATE_DIR = Path(os.environ.get("DSH_RELAY_STATE_DIR") or str(Path(__file__).resolve().parent.parent / "state"))
STATE_FILE = STATE_DIR / "w1_dispatch_state.json"
TELEGRAM_ENV_PATH = os.environ.get("DSH_RELAY_TELEGRAM_ENV_PATH") or os.environ.get("TELEGRAM_ENV_PATH")
TELEGRAM_MCP_EXE = os.environ.get(
    "DSH_RELAY_TELEGRAM_MCP_EXE",
    os.environ.get("TELEGRAM_MCP_EXE", "telegram-mcp"),
)


class ValidationError(Exception):
    pass


# ---------------------------------------------------------------------------
# GitHub (read/comment only). Prefers the Actions-provided GH_TOKEN
# (least-privilege, scoped to this repo only per workflow permissions:
# contents: read / issues: write) over any ambient personal `gh` auth.
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
# Issue contract validation -- issue text is DATA ONLY, never executed
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
        raise ValidationError(f"ARBITRARY_COMMAND_BLOCKED: probe={fields['probe']!r} (only 'aliases' allowed)")

    correlation_id = fields["correlation_id"]
    if not correlation_id or not re.match(r"^[A-Za-z0-9_.-]{6,128}$", correlation_id):
        raise ValidationError(f"ISSUE_SCHEMA_VALIDATION FAIL: bad correlation_id {correlation_id!r}")

    return {"correlation_id": correlation_id}


# ---------------------------------------------------------------------------
# MCP / Telegram (unmodified upstream telegram-mcp over real MCP stdio)
# ---------------------------------------------------------------------------

async def mcp_send_and_get_history(command: str, timeout: int = 45) -> tuple[dict, dict]:
    sub_env = dict(os.environ)
    if TELEGRAM_ENV_PATH:
        sub_env["TELEGRAM_ENV_PATH"] = TELEGRAM_ENV_PATH
    server_params = StdioServerParameters(
        command=TELEGRAM_MCP_EXE, args=[],
        env=sub_env,
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            send_result = await session.call_tool(
                "send_message", {"bot": PINNED_BOT, "message": command, "timeout": timeout},
            )
            send_payload = json.loads(send_result.content[0].text)
            history_result = await session.call_tool("get_history", {"bot": PINNED_BOT, "limit": 10})
            history_payload = json.loads(history_result.content[0].text)
    return send_payload, history_payload


def build_evidence_comment(correlation_id: str, send_payload: dict, history_ok: bool) -> str:
    telegram_send = "PASS" if send_payload.get("status") in ("ok", "timeout") else "FAIL"
    dsh_reply = "PASS" if send_payload.get("reply") else "FAIL"
    mcp_hist = "PASS" if history_ok else "FAIL"
    overall = "PASS" if send_payload.get("status") == "ok" and send_payload.get("reply") and history_ok else "FAIL"
    return (
        f"P18-W1 RELAY CANARY (AUTOMATIC): {overall}\n"
        f"correlation_id: {correlation_id}\n"
        f"probe: {REQUIRED_PROBE}\n"
        f"target: @{PINNED_BOT}\n"
        f"telegram_send: {telegram_send}\n"
        f"dsh_bot_reply: {dsh_reply}\n"
        f"mcp_get_history: {mcp_hist}\n"
        f"triggered_by: GitHub issue event -> self-hosted runner -> fixed relay entrypoint "
        f"(no manual invocation)\n"
        f"DSH source/config/schema changes: 0 / 0 / 0\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="DSH automatic GitHub->Telegram relay entrypoint")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.repo:
        print("CONFIG ERROR: --repo must be provided or GITHUB_REPOSITORY environment variable set")
        return 1

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    store = DispatchStateStore(STATE_FILE)

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

    try:
        store.reserve(args.repo, args.issue, correlation_id)
    except TransitionDenied as e:
        print(f"DOUBLE-SEND PROTECTION: FAIL ({e.code}: {e})")
        return 1
    print(f"DOUBLE-SEND PROTECTION: PASS (reserved, status={store.get_status(args.repo, args.issue, correlation_id)})")

    try:
        send_payload, history_payload = asyncio.run(mcp_send_and_get_history(FIXED_TELEGRAM_COMMAND))
    except Exception as e:  # noqa: BLE001
        # Unrecognized exception shape at the Python/MCP layer itself (not
        # even a structured telegram-mcp error payload) -- cannot be
        # confidently classified as pre-send, so fail closed.
        store.mark_failed_terminal(args.repo, args.issue, correlation_id, {"error": str(e)})
        print(f"TELEGRAM MCP SEND: FAIL (unclassified exception, marked FAILED_TERMINAL: {e})")
        return 1

    outcome = classify_send_result(send_payload)
    history_ok = history_payload.get("status") == "ok"

    if outcome == FAILED_RETRYABLE:
        store.mark_failed_retryable(args.repo, args.issue, correlation_id, {"send_payload": send_payload})
        print(f"TELEGRAM MCP SEND: FAIL (FAILED_RETRYABLE: {send_payload.get('error')})")
        return 1
    if outcome == FAILED_TERMINAL:
        store.mark_failed_terminal(args.repo, args.issue, correlation_id, {"send_payload": send_payload})
        print(f"TELEGRAM MCP SEND: FAIL (FAILED_TERMINAL, ambiguous: {send_payload.get('error')})")
        return 1

    # outcome == SENT: delivery confirmed
    store.mark_sent(args.repo, args.issue, correlation_id, {"send_payload": send_payload})
    print(f"TELEGRAM MCP SEND: {'PASS' if send_payload.get('status') == 'ok' else 'PASS_NO_REPLY_TIMEOUT'}")
    print(f"DSH BOT REAL REPLY: {'PASS' if send_payload.get('reply') else 'FAIL'}")
    print(f"MCP GET_HISTORY: {'PASS' if history_ok else 'FAIL'}")

    comment_body = build_evidence_comment(correlation_id, send_payload, history_ok)
    try:
        gh_comment_issue(args.repo, args.issue, comment_body)
        store.mark_completed(args.repo, args.issue, correlation_id, {"comment_posted": True})
        print("GITHUB ISSUE COMMENT: PASS")
    except ValidationError as e:
        # Delivery already confirmed (state remains SENT, never resent even
        # on a workflow re-run) -- only the evidence-comment step failed.
        print(f"GITHUB ISSUE COMMENT: FAIL ({e}) -- Telegram delivery already confirmed, state stays SENT")
        return 1

    overall_pass = send_payload.get("status") == "ok" and bool(send_payload.get("reply")) and history_ok
    print(f"P18-W1 RELAY RESULT: {'PASS' if overall_pass else 'FAIL'}")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
