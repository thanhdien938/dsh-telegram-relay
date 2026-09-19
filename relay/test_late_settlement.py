"""
P18-W4R4 mandatory tests for post-timeout terminal settlement, before any
live recovery run against issue #4.

Run: .venv\\Scripts\\python.exe relay\\test_late_settlement.py
"""
import os
import sys
import tempfile
from pathlib import Path

from result_collector import ResultStateStore, TIMEOUT, COMPLETED, COMPLETED_LATE, FAILED_LATE, PENDING
from late_settlement import (
    attempt_late_settlement, build_late_settlement_comment,
    NO_ENTRY, ALREADY_LATE_SETTLED, NOT_ELIGIBLE, STILL_PENDING, STILL_AMBIGUOUS,
    TASK_ID_MISMATCH, SETTLED,
)

REPO = "example-owner/relay-control"
ISSUE = 4
CORR = "p18-w4-real-20260902a"
BOT = "dsh_test_bot"
TASK_ID = "task-i5OogBxj2B486tdwFqC0z4Tw6Kisz4Es"

failures = []


def check(name, condition):
    if condition:
        print(f"PASS: {name}")
    else:
        print(f"FAIL: {name}")
        failures.append(name)


def fresh_store() -> ResultStateStore:
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    tmp = Path(name)
    tmp.unlink()
    return ResultStateStore(tmp)


def msg(id_, from_, text, date="2026-09-02T15:00:00+00:00"):
    return {"id": id_, "from": from_, "text": text, "date": date}


def completed_text(marker, task_id=TASK_ID):
    return (f"✅ DSH task completed\n\nTask: {task_id}\nPM: claude-pm\n"
            f"Status: completed\n\nResult:\nP18_W4_TERMINAL_OK {marker}")


def failed_text(marker, task_id=TASK_ID):
    return (f"❌ DSH task failed\n\nTask: {task_id}\nPM: claude-pm\n"
            f"Status: failed\n\nResult:\nsomething went wrong {marker}")


def timed_out_store(boundary_id=13021):
    store = fresh_store()
    store.start_or_resume(REPO, ISSUE, CORR, boundary_id=boundary_id, timeout_seconds=420)
    store.settle(REPO, ISSUE, CORR, TIMEOUT, reason="deadline reached after 49 poll(s), zero exact matches")
    store.mark_comment_posted(REPO, ISSUE, CORR)  # original TIMEOUT comment already posted
    return store


# ---------------------------------------------------------------------------
# 1. no entry at all -> NO_ENTRY
# ---------------------------------------------------------------------------
def test_no_entry():
    store = fresh_store()
    outcome, entry, reason = attempt_late_settlement(store, REPO, ISSUE, CORR, {"messages": []}, BOT)
    check("no-entry: outcome NO_ENTRY", outcome == NO_ENTRY)
    check("no-entry: entry is None", entry is None)


# ---------------------------------------------------------------------------
# 2. TIMEOUT + later exact single completed match -> SETTLED / COMPLETED_LATE
# ---------------------------------------------------------------------------
def test_settle_completed_late():
    store = timed_out_store()
    history = {"messages": [
        msg(13030, f"@{BOT}", completed_text(CORR)),
    ]}
    outcome, entry, reason = attempt_late_settlement(store, REPO, ISSUE, CORR, history, BOT,
                                                       expected_task_id=TASK_ID)
    check("settle-completed: outcome SETTLED", outcome == SETTLED)
    check("settle-completed: status COMPLETED_LATE", entry["status"] == COMPLETED_LATE)
    check("settle-completed: real task_id recovered", entry["task_id"] == TASK_ID)
    check("settle-completed: terminal_status completed", entry["terminal_status"] == "completed")
    check("settle-completed: terminal_message_id recorded", entry["terminal_message_id"] == 13030)
    check("settle-completed: late_comment_posted starts False", entry["late_comment_posted"] is False)
    # The ORIGINAL TIMEOUT comment flag must be untouched by late settlement.
    check("settle-completed: original comment_posted flag untouched", entry["comment_posted"] is True)


# ---------------------------------------------------------------------------
# 3. TIMEOUT + later exact single failed match -> SETTLED / FAILED_LATE
# ---------------------------------------------------------------------------
def test_settle_failed_late():
    store = timed_out_store()
    history = {"messages": [msg(13030, f"@{BOT}", failed_text(CORR))]}
    outcome, entry, reason = attempt_late_settlement(store, REPO, ISSUE, CORR, history, BOT)
    check("settle-failed: outcome SETTLED", outcome == SETTLED)
    check("settle-failed: status FAILED_LATE", entry["status"] == FAILED_LATE)


# ---------------------------------------------------------------------------
# 4. still nothing in history -> STILL_PENDING, no state change
# ---------------------------------------------------------------------------
def test_still_pending():
    store = timed_out_store()
    history = {"messages": [msg(13022, f"@{BOT}", "unrelated bot chatter")]}
    outcome, entry, reason = attempt_late_settlement(store, REPO, ISSUE, CORR, history, BOT)
    check("still-pending: outcome STILL_PENDING", outcome == STILL_PENDING)
    check("still-pending: status remains TIMEOUT", entry["status"] == TIMEOUT)


# ---------------------------------------------------------------------------
# 5. multiple terminal candidates -> STILL_AMBIGUOUS, never guesses, no change
# ---------------------------------------------------------------------------
def test_still_ambiguous():
    store = timed_out_store()
    history = {"messages": [
        msg(13030, f"@{BOT}", completed_text(CORR, task_id="task-AAAA")),
        msg(13031, f"@{BOT}", completed_text(CORR, task_id="task-BBBB")),
    ]}
    outcome, entry, reason = attempt_late_settlement(store, REPO, ISSUE, CORR, history, BOT)
    check("still-ambiguous: outcome STILL_AMBIGUOUS", outcome == STILL_AMBIGUOUS)
    check("still-ambiguous: status remains TIMEOUT (never guesses)", entry["status"] == TIMEOUT)


# ---------------------------------------------------------------------------
# 6. exact single match but wrong expected task_id -> TASK_ID_MISMATCH
# ---------------------------------------------------------------------------
def test_task_id_mismatch():
    store = timed_out_store()
    history = {"messages": [msg(13030, f"@{BOT}", completed_text(CORR, task_id="task-DIFFERENT"))]}
    outcome, entry, reason = attempt_late_settlement(store, REPO, ISSUE, CORR, history, BOT,
                                                       expected_task_id=TASK_ID)
    check("task-id-mismatch: outcome TASK_ID_MISMATCH", outcome == TASK_ID_MISMATCH)
    check("task-id-mismatch: status remains TIMEOUT (refuses to settle a different task)",
          entry["status"] == TIMEOUT)


# ---------------------------------------------------------------------------
# 7. entry not TIMEOUT (e.g. ordinary COMPLETED) -> NOT_ELIGIBLE, untouched
# ---------------------------------------------------------------------------
def test_not_eligible_when_not_timeout():
    store = fresh_store()
    store.start_or_resume(REPO, ISSUE, CORR, boundary_id=99, timeout_seconds=420)
    store.settle(REPO, ISSUE, CORR, COMPLETED, task_id="task-ORIGINAL", terminal_status="completed",
                 terminal_message_id=100, reason="exact single completed terminal match")
    history = {"messages": [msg(200, f"@{BOT}", completed_text(CORR, task_id="task-LATER"))]}
    outcome, entry, reason = attempt_late_settlement(store, REPO, ISSUE, CORR, history, BOT)
    check("not-eligible: outcome NOT_ELIGIBLE", outcome == NOT_ELIGIBLE)
    check("not-eligible: original task_id never overwritten", entry["task_id"] == "task-ORIGINAL")


# ---------------------------------------------------------------------------
# 8. idempotent rerun after late settlement -> ALREADY_LATE_SETTLED, no dup
# ---------------------------------------------------------------------------
def test_rerun_after_late_settlement_idempotent():
    store = timed_out_store()
    history = {"messages": [msg(13030, f"@{BOT}", completed_text(CORR))]}
    first_outcome, first_entry, _ = attempt_late_settlement(store, REPO, ISSUE, CORR, history, BOT)
    store.mark_late_comment_posted(REPO, ISSUE, CORR)

    second_outcome, second_entry, reason = attempt_late_settlement(store, REPO, ISSUE, CORR, history, BOT)
    check("rerun: first call SETTLED", first_outcome == SETTLED)
    check("rerun: second call ALREADY_LATE_SETTLED", second_outcome == ALREADY_LATE_SETTLED)
    check("rerun: status/task_id unchanged across rerun", second_entry["task_id"] == first_entry["task_id"])
    check("rerun: late_comment_posted stays True (caller posts nothing further)",
          second_entry["late_comment_posted"] is True)

    # A malicious/naive rerun that tried to settle a DIFFERENT candidate
    # must not be able to overwrite the already-late-settled entry.
    other_history = {"messages": [msg(13040, f"@{BOT}", failed_text(CORR, task_id="task-SHOULD-NOT-APPLY"))]}
    third_outcome, third_entry, _ = attempt_late_settlement(store, REPO, ISSUE, CORR, other_history, BOT)
    check("rerun: still ALREADY_LATE_SETTLED even with new conflicting history",
          third_outcome == ALREADY_LATE_SETTLED)
    check("rerun: task_id still the original late-settled value", third_entry["task_id"] == TASK_ID)


# ---------------------------------------------------------------------------
# 9. settle_late() itself refuses non-TIMEOUT starting states
# ---------------------------------------------------------------------------
def test_settle_late_refuses_non_timeout():
    store = fresh_store()
    store.start_or_resume(REPO, ISSUE, CORR, boundary_id=99, timeout_seconds=420)  # still PENDING
    raised = False
    try:
        store.settle_late(REPO, ISSUE, CORR, COMPLETED_LATE, task_id=TASK_ID,
                           terminal_status="completed", terminal_message_id=1, reason="should refuse")
    except ValueError:
        raised = True
    check("settle-late-refuses: ValueError raised for PENDING origin", raised)
    entry = store.get(REPO, ISSUE, CORR)
    check("settle-late-refuses: entry still PENDING, untouched", entry["status"] == PENDING)


# ---------------------------------------------------------------------------
# 10. comment builder: required fields present, optional evidence gated
# ---------------------------------------------------------------------------
def test_comment_builder_shape():
    entry = {
        "status": COMPLETED_LATE, "task_id": TASK_ID, "terminal_status": "completed",
        "terminal_message_id": 13030,
    }
    minimal = build_late_settlement_comment(CORR, entry)
    check("comment-minimal: has status line", "P18-W4 COMPLETED_LATE" in minimal)
    check("comment-minimal: has correlation_id", f"correlation_id: {CORR}" in minimal)
    check("comment-minimal: has task_id", f"task_id: {TASK_ID}" in minimal)
    check("comment-minimal: has terminal_status", "terminal_status: completed" in minimal)
    check("comment-minimal: no task_branch line when not supplied", "task_branch:" not in minimal)
    check("comment-minimal: preserves-original-comment note present", "preserved as historical evidence" in minimal)

    full = build_late_settlement_comment(
        CORR, entry,
        task_branch="dsh/task-task-i5OogBxj2B486tdwFqC0z4Tw6Kisz4Es",
        result_commit="1698f8cbfa5d522a49d7f044ab40b7fb9f7c7492",
        published_head="2bb5d290b89909f9db3114e3aa4645134132aabc",
    )
    check("comment-full: has task_branch", "task_branch: dsh/task-task-i5OogBxj2B486tdwFqC0z4Tw6Kisz4Es" in full)
    check("comment-full: has result_commit", "result_commit: 1698f8cbfa5d522a49d7f044ab40b7fb9f7c7492" in full)
    check("comment-full: has published_head", "published_head: 2bb5d290b89909f9db3114e3aa4645134132aabc" in full)


def main():
    test_no_entry()
    test_settle_completed_late()
    test_settle_failed_late()
    test_still_pending()
    test_still_ambiguous()
    test_task_id_mismatch()
    test_not_eligible_when_not_timeout()
    test_rerun_after_late_settlement_idempotent()
    test_settle_late_refuses_non_timeout()
    test_comment_builder_shape()

    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)} failing checks)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS (all P18-W4R4 late-settlement checks passed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
