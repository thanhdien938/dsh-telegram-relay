"""
P18-W1 mandatory pre-remediation tests for P18-W0-STATE-001.

Covers exactly the six scenarios required before any live automation:
  1. event replay before send
  2. event replay after successful send
  3. failure before Telegram delivery
  4. retry after FAILED_RETRYABLE
  5. duplicate workflow invocation
  6. process crash boundary around send

Run: .venv\\Scripts\\python.exe test_state_machine.py
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from state_machine import (
    DispatchStateStore, TransitionDenied, classify_send_result,
    RESERVED, SENT, COMPLETED, FAILED_RETRYABLE, FAILED_TERMINAL,
    STALE_RESERVATION_SECONDS,
)

REPO = "example-owner/relay-control"
ISSUE = 42
CORR = "P18W1_TEST_CORR"

failures = []


def check(name, condition):
    if condition:
        print(f"PASS: {name}")
    else:
        print(f"FAIL: {name}")
        failures.append(name)


def fresh_store() -> DispatchStateStore:
    fd, name = tempfile.mkstemp(suffix=".json")
    import os as _os
    _os.close(fd)
    tmp = Path(name)
    tmp.unlink()  # start with no file
    return DispatchStateStore(tmp)


# ---------------------------------------------------------------------------
# 1. event replay before send: a duplicate arrives while the first is still
#    RESERVED (fresh) -- must refuse, never send twice concurrently.
# ---------------------------------------------------------------------------
def test_replay_before_send():
    store = fresh_store()
    store.reserve(REPO, ISSUE, CORR)
    check("replay-before-send: first reserve succeeds",
          store.get_status(REPO, ISSUE, CORR) == RESERVED)
    try:
        store.reserve(REPO, ISSUE, CORR)
        check("replay-before-send: second reserve refused", False)
    except TransitionDenied as e:
        check("replay-before-send: second reserve refused with REPLAY_BEFORE_SEND",
              e.code == "REPLAY_BEFORE_SEND")
    check("replay-before-send: status still RESERVED (unchanged by refusal)",
          store.get_status(REPO, ISSUE, CORR) == RESERVED)


# ---------------------------------------------------------------------------
# 2. event replay after successful send: duplicate arrives once COMPLETED.
# ---------------------------------------------------------------------------
def test_replay_after_success():
    store = fresh_store()
    store.reserve(REPO, ISSUE, CORR)
    store.mark_sent(REPO, ISSUE, CORR, {"telegram_status": "ok"})
    store.mark_completed(REPO, ISSUE, CORR, {"github_comment": "posted"})
    check("replay-after-success: status COMPLETED",
          store.get_status(REPO, ISSUE, CORR) == COMPLETED)
    try:
        store.reserve(REPO, ISSUE, CORR)
        check("replay-after-success: reserve refused", False)
    except TransitionDenied as e:
        check("replay-after-success: reserve refused with ALREADY_DELIVERED",
              e.code == "ALREADY_DELIVERED")
    check("replay-after-success: status still COMPLETED",
          store.get_status(REPO, ISSUE, CORR) == COMPLETED)


# ---------------------------------------------------------------------------
# 3. failure before Telegram delivery: must land FAILED_RETRYABLE, never
#    COMPLETED (this is the literal P18-W0-STATE-001 defect).
# ---------------------------------------------------------------------------
def test_failure_before_delivery_never_completed():
    store = fresh_store()
    store.reserve(REPO, ISSUE, CORR)
    payload = {"status": "error", "error": "Missing TELEGRAM_API_ID or TELEGRAM_API_HASH. Set them..."}
    outcome = classify_send_result(payload)
    check("pre-delivery failure classified FAILED_RETRYABLE", outcome == FAILED_RETRYABLE)
    store.mark_failed_retryable(REPO, ISSUE, CORR, {"error": payload["error"]})
    status = store.get_status(REPO, ISSUE, CORR)
    check("pre-delivery failure: status is FAILED_RETRYABLE", status == FAILED_RETRYABLE)
    check("pre-delivery failure: status is NEVER COMPLETED", status != COMPLETED)
    # mark_completed must be structurally impossible from FAILED_RETRYABLE
    try:
        store.mark_completed(REPO, ISSUE, CORR, {})
        check("pre-delivery failure: mark_completed from FAILED_RETRYABLE refused", False)
    except TransitionDenied as e:
        check("pre-delivery failure: mark_completed from FAILED_RETRYABLE refused (BAD_TRANSITION)",
              e.code == "BAD_TRANSITION")


# ---------------------------------------------------------------------------
# 4. retry after FAILED_RETRYABLE: must be allowed automatically (no human
#    note required -- that was the W0 workaround, W1 must not need it).
# ---------------------------------------------------------------------------
def test_retry_after_failed_retryable():
    store = fresh_store()
    store.reserve(REPO, ISSUE, CORR)
    store.mark_failed_retryable(REPO, ISSUE, CORR, {"error": "Missing TELEGRAM_API_ID"})
    check("retry-after-failed-retryable: pre-state is FAILED_RETRYABLE",
          store.get_status(REPO, ISSUE, CORR) == FAILED_RETRYABLE)
    store.reserve(REPO, ISSUE, CORR)  # should NOT raise
    check("retry-after-failed-retryable: auto-retry reserve succeeds -> RESERVED",
          store.get_status(REPO, ISSUE, CORR) == RESERVED)
    store.mark_sent(REPO, ISSUE, CORR, {"telegram_status": "ok"})
    store.mark_completed(REPO, ISSUE, CORR, {"github_comment": "posted"})
    check("retry-after-failed-retryable: eventually reaches COMPLETED",
          store.get_status(REPO, ISSUE, CORR) == COMPLETED)
    # history preserved, not discarded
    import json
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    entry = raw[store._key(REPO, ISSUE, CORR)]
    check("retry-after-failed-retryable: prior attempt archived in history",
          len(entry["attempts"]) >= 1 and entry["attempts"][0]["status"] == FAILED_RETRYABLE)


# ---------------------------------------------------------------------------
# 5. duplicate workflow invocation: same repo+issue+correlation fired twice
#    "simultaneously" by two separate runner invocations -- second must lose.
# ---------------------------------------------------------------------------
def test_duplicate_workflow_invocation():
    store = fresh_store()
    store.reserve(REPO, ISSUE, CORR)  # invocation A
    try:
        store.reserve(REPO, ISSUE, CORR)  # invocation B (duplicate workflow run)
        check("duplicate-workflow-invocation: invocation B refused", False)
    except TransitionDenied as e:
        check("duplicate-workflow-invocation: invocation B refused (REPLAY_BEFORE_SEND)",
              e.code == "REPLAY_BEFORE_SEND")
    # invocation A completes normally
    store.mark_sent(REPO, ISSUE, CORR, {"telegram_status": "ok"})
    store.mark_completed(REPO, ISSUE, CORR, {"github_comment": "posted"})
    # a third, later duplicate run (e.g. manual "Re-run job") must also be refused
    try:
        store.reserve(REPO, ISSUE, CORR)
        check("duplicate-workflow-invocation: rerun-after-completion refused", False)
    except TransitionDenied as e:
        check("duplicate-workflow-invocation: rerun-after-completion refused (ALREADY_DELIVERED)",
              e.code == "ALREADY_DELIVERED")


# ---------------------------------------------------------------------------
# 6. process crash boundary around send: the process dies while status is
#    still RESERVED and nothing else was ever recorded (we cannot know if
#    Telethon's send actually reached Telegram). Must fail closed, not
#    silently retry, once the reservation is stale enough that no legitimate
#    in-flight attempt could still be running.
# ---------------------------------------------------------------------------
def test_crash_boundary_around_send():
    store = fresh_store()
    store.reserve(REPO, ISSUE, CORR)
    # Simulate the crash: back-date the reservation past the staleness
    # window without ever calling mark_sent/mark_failed_*.
    import json
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    key = store._key(REPO, ISSUE, CORR)
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=STALE_RESERVATION_SECONDS + 5)).isoformat()
    raw[key]["updated_at"] = old_ts
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    try:
        store.reserve(REPO, ISSUE, CORR)  # replay/retry after the "crash"
        check("crash-boundary: stale RESERVED replay refused", False)
    except TransitionDenied as e:
        check("crash-boundary: stale RESERVED replay refused (STALE_RESERVATION_AMBIGUOUS)",
              e.code == "STALE_RESERVATION_AMBIGUOUS")
    check("crash-boundary: demoted to FAILED_TERMINAL (never silently retried)",
          store.get_status(REPO, ISSUE, CORR) == FAILED_TERMINAL)
    # And FAILED_TERMINAL itself refuses further automatic retry.
    try:
        store.reserve(REPO, ISSUE, CORR)
        check("crash-boundary: FAILED_TERMINAL refuses further auto-retry", False)
    except TransitionDenied as e:
        check("crash-boundary: FAILED_TERMINAL refuses further auto-retry (TERMINAL_REQUIRES_MANUAL_OVERRIDE)",
              e.code == "TERMINAL_REQUIRES_MANUAL_OVERRIDE")


# ---------------------------------------------------------------------------
# Extra: "timeout" status (message WAS delivered, no reply seen) must never
# be classified as retryable -- that would double-send a delivered message.
# ---------------------------------------------------------------------------
def test_timeout_is_delivered_not_retryable():
    outcome = classify_send_result({"status": "timeout"})
    check("timeout status classified SENT (delivered), not retryable", outcome == SENT)

    outcome_unknown = classify_send_result({"status": "error", "error": "FloodWaitError: wait 30 seconds"})
    check("unrecognized error text classified FAILED_TERMINAL (ambiguous, not blindly retried)",
          outcome_unknown == FAILED_TERMINAL)


def main():
    test_replay_before_send()
    test_replay_after_success()
    test_failure_before_delivery_never_completed()
    test_retry_after_failed_retryable()
    test_duplicate_workflow_invocation()
    test_crash_boundary_around_send()
    test_timeout_is_delivered_not_retryable()

    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)} failing checks)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS (all P18-W0-STATE-001 remediation checks passed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
