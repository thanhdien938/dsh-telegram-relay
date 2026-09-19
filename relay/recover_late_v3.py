"""
P18-W4R4 bounded post-timeout terminal settlement recovery pass.

Structurally read-only with respect to Telegram: `ReadOnlyTelegramSession`
below exposes ONLY get_history. There is no send_message method anywhere
in this file, so this entrypoint cannot redispatch or open a second
Telegram task regardless of caller mistake or future edit.

Reuses the exact durable identity relay/dispatch_v3.py already
established for an issue -- this entrypoint never re-reads or re-parses
the GitHub issue body (no `gh issue view`, no contract_v3 revalidation).
The only inputs are:
  - the correlation_id already recorded for this issue (caller must
    supply it -- it is never re-derived or guessed here);
  - the durable relay/result_collector.py ResultStateStore entry that
    dispatch_v3.py already created and settled to TIMEOUT;
  - a fresh Telegram get_history snapshot.

Invoked manually/bounded for now (P18-W4R4 is a narrow remediation, not
new automation) -- not wired into any GitHub Actions workflow trigger.

Usage (issue #4 recovery):
    .venv\\Scripts\\python.exe relay\\recover_late_v3.py \\
        --issue 4 \\
        --correlation-id p18-w4-real-20260902a \\
        --expect-task-id task-i5OogBxj2B486tdwFqC0z4Tw6Kisz4Es \\
        --task-branch dsh/task-task-i5OogBxj2B486tdwFqC0z4Tw6Kisz4Es \\
        --result-commit 1698f8cbfa5d522a49d7f044ab40b7fb9f7c7492 \\
        --published-head 2bb5d290b89909f9db3114e3aa4645134132aabc
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from result_collector import ResultStateStore, TIMEOUT, LATE_TERMINAL_STATES  # noqa: E402
from late_settlement import attempt_late_settlement, build_late_settlement_comment, SETTLED, STILL_PENDING  # noqa: E402

PINNED_BOT = os.environ.get("DSH_RELAY_TELEGRAM_TARGET", "dsh_relay_bot").strip().lstrip("@")

STATE_DIR = Path(os.environ.get("DSH_RELAY_STATE_DIR") or str(Path(__file__).resolve().parent.parent / "state"))
RESULT_STATE_FILE = STATE_DIR / "w3_result_state.json"
TELEGRAM_ENV_PATH = os.environ.get("DSH_RELAY_TELEGRAM_ENV_PATH") or os.environ.get("TELEGRAM_ENV_PATH")
TELEGRAM_MCP_EXE = os.environ.get(
    "DSH_RELAY_TELEGRAM_MCP_EXE",
    os.environ.get("TELEGRAM_MCP_EXE", "telegram-mcp"),
)


def _gh_env() -> dict:
    env = dict(os.environ)
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        env["GH_TOKEN"] = token
    return env


def gh_comment_issue(repo: str, number: int, body: str) -> None:
    proc = subprocess.run(
        ["gh", "issue", "comment", str(number), "--repo", repo, "--body-file", "-"],
        input=body, capture_output=True, text=True, check=False, env=_gh_env(),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"GITHUB_ISSUE_COMMENT FAIL: {proc.stderr.strip()}")


class ReadOnlyTelegramSession:
    """Deliberately narrower than dispatch_v3.py's TelegramSession: this
    class has no send_message wrapper at all. A post-timeout recovery
    pass must NOT be able to dispatch -- omitting the method (rather than
    merely not calling it) makes that a structural fact, not a discipline
    someone has to remember to keep."""

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


async def mcp_get_history(session: ClientSession, limit: int = 50) -> dict:
    result = await session.call_tool("get_history", {"bot": PINNED_BOT, "limit": limit})
    return json.loads(result.content[0].text)


async def run(repo: str, issue: int, correlation_id: str, result_store: ResultStateStore,
              expected_task_id: str | None, history_limit: int,
              task_branch: str | None, result_commit: str | None, published_head: str | None) -> int:
    entry = result_store.get(repo, issue, correlation_id)
    if entry is None:
        print(f"LATE SETTLEMENT: NO_ENTRY (no prior dispatch/result state for {repo}#{issue}#{correlation_id})")
        return 1
    if entry["status"] in LATE_TERMINAL_STATES:
        print(f"LATE SETTLEMENT: ALREADY_SETTLED (status={entry['status']}, idempotent no-op -- "
              "no Telegram call made, zero duplicate comments, zero Telegram sends)")
        return 0
    if entry["status"] != TIMEOUT:
        print(f"LATE SETTLEMENT: NOT_ELIGIBLE (status={entry['status']!r} is not TIMEOUT; "
              "late settlement only ever applies to a TIMEOUT entry)")
        return 1

    async with ReadOnlyTelegramSession() as session:
        history = await mcp_get_history(session, limit=history_limit)

    outcome, updated_entry, reason = attempt_late_settlement(
        result_store, repo, issue, correlation_id, history, PINNED_BOT,
        expected_task_id=expected_task_id,
    )
    print(f"LATE SETTLEMENT: {outcome} ({reason})")

    if outcome != SETTLED:
        return 0 if outcome == STILL_PENDING else 1

    if not updated_entry.get("late_comment_posted"):
        comment = build_late_settlement_comment(
            correlation_id, updated_entry,
            task_branch=task_branch, result_commit=result_commit, published_head=published_head,
        )
        gh_comment_issue(repo, issue, comment)
        result_store.mark_late_comment_posted(repo, issue, correlation_id)
        print("GITHUB LATE SETTLEMENT COMMENT: PASS")
    else:
        print("GITHUB LATE SETTLEMENT COMMENT: SKIPPED (already posted, duplicate-comment guard held)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="DSH post-timeout terminal settlement recovery pass")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--correlation-id", required=True,
                     help="exact correlation_id already recorded for this issue by dispatch_v3.py")
    ap.add_argument("--expect-task-id", default=None,
                     help="if given, settlement is refused unless the found terminal task_id matches exactly")
    ap.add_argument("--history-limit", type=int, default=50)
    ap.add_argument("--task-branch", default=None,
                     help="independently-verified evidence only; never parsed from Telegram text or model prose")
    ap.add_argument("--result-commit", default=None,
                     help="independently-verified evidence only; never parsed from Telegram text or model prose")
    ap.add_argument("--published-head", default=None,
                     help="independently-verified evidence only; never parsed from Telegram text or model prose")
    args = ap.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    result_store = ResultStateStore(RESULT_STATE_FILE)

    return asyncio.run(run(args.repo, args.issue, args.correlation_id, result_store,
                            args.expect_task_id, args.history_limit,
                            args.task_branch, args.result_commit, args.published_head))


if __name__ == "__main__":
    sys.exit(main())
