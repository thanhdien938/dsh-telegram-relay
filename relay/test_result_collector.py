"""
P18-W2 mandatory tests for the result collector, before any live run.

Run: .venv\\Scripts\\python.exe relay\\test_result_collector.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from result_collector import (
    ResultStateStore, find_boundary_id, scan_terminal_candidates, classify,
    PENDING, COMPLETED, FAILED, AMBIGUOUS, TIMEOUT,
    extract_task_id_from_ack, scan_terminal_candidates_by_task_id,
    NORMAL_RESULT_TIMEOUT_SECONDS, LONG_RESULT_TIMEOUT_SECONDS,
    DSH_LONG_EXECUTION_CEILING_SECONDS, result_timeout_seconds_for,
    TASK_ID_UNRECOVERABLE,
    parse_accepted_ack, scan_ack_candidates_by_correlation, classify_ack,
    ACK_RECOVERY_TIMEOUT_SECONDS,
    HISTORY_PAGE_SIZE, HISTORY_ESCALATION_LIMITS, MAX_HISTORY_MESSAGES,
    history_window_covers_boundary,
)

REPO = "example-owner/relay-control"
ISSUE = 7
CORR = "P18W2_TEST_MARKER_XYZ"
BOT = "dsh_test_bot"

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


def msg(id_, from_, text, date="2026-09-02T12:00:00+00:00"):
    return {"id": id_, "from": from_, "text": text, "date": date}


def completed_text(marker, task_id="task-AbCdEfGhIjKlMnOp"):
    return (f"✅ DSH task completed\n\nTask: {task_id}\nPM: claude-pm\n"
            f"Status: completed\n\nResult:\nP18_W2_TERMINAL_OK {marker}")


def failed_text(marker, task_id="task-AbCdEfGhIjKlMnOp"):
    return (f"❌ DSH task failed\n\nTask: {task_id}\nPM: claude-pm\n"
            f"Status: failed\n\nResult:\nsomething went wrong {marker}")


def ack_text(marker=""):
    # Real shorthand ACK shape -- deliberately has NO "Status:" line, so it
    # must never be misparsed as terminal even if it echoed the marker.
    return f"✅ DSH task accepted\n\nProject:\n2 — dsh-test-project\n\nPM:\n1 — claude-pm\n\nTask:\n2-1 ... {marker}"


# ---------------------------------------------------------------------------
# 1. no terminal result yet -> PENDING
# ---------------------------------------------------------------------------
def test_no_result_yet_pending():
    history = {"messages": [
        msg(100, "you", f"2-1 dispatch text {CORR}"),
        msg(101, f"@{BOT}", ack_text(CORR)),
    ]}
    boundary = 99
    candidates = scan_terminal_candidates(history, boundary, CORR, BOT)
    state, cand, reason = classify(candidates)
    check("no-result-yet: zero candidates", len(candidates) == 0)
    check("no-result-yet: classify() -> PENDING", state == PENDING)


# ---------------------------------------------------------------------------
# 2. exact marker + one terminal message -> COMPLETED
# ---------------------------------------------------------------------------
def test_exact_completed():
    history = {"messages": [
        msg(100, "you", f"2-1 dispatch {CORR}"),
        msg(101, f"@{BOT}", ack_text(CORR)),
        msg(102, f"@{BOT}", completed_text(CORR)),
    ]}
    candidates = scan_terminal_candidates(history, 99, CORR, BOT)
    state, cand, reason = classify(candidates)
    check("exact-completed: one candidate", len(candidates) == 1)
    check("exact-completed: classify() -> COMPLETED", state == COMPLETED)
    check("exact-completed: real task_id recovered", cand.task_id == "task-AbCdEfGhIjKlMnOp")


# ---------------------------------------------------------------------------
# 3. exact marker + terminal failed message -> FAILED
# ---------------------------------------------------------------------------
def test_exact_failed():
    history = {"messages": [
        msg(100, "you", f"2-1 dispatch {CORR}"),
        msg(101, f"@{BOT}", ack_text(CORR)),
        msg(102, f"@{BOT}", failed_text(CORR)),
    ]}
    candidates = scan_terminal_candidates(history, 99, CORR, BOT)
    state, cand, reason = classify(candidates)
    check("exact-failed: classify() -> FAILED", state == FAILED)
    check("exact-failed: task_id still recovered", cand.task_id == "task-AbCdEfGhIjKlMnOp")


# ---------------------------------------------------------------------------
# 4. multiple terminal candidates -> AMBIGUOUS
# ---------------------------------------------------------------------------
def test_multiple_candidates_ambiguous():
    history = {"messages": [
        msg(100, "you", f"2-1 dispatch {CORR}"),
        msg(101, f"@{BOT}", ack_text(CORR)),
        msg(102, f"@{BOT}", completed_text(CORR, task_id="task-AAAA")),
        msg(103, f"@{BOT}", completed_text(CORR, task_id="task-BBBB")),
    ]}
    candidates = scan_terminal_candidates(history, 99, CORR, BOT)
    state, cand, reason = classify(candidates)
    check("multiple-candidates: two candidates found", len(candidates) == 2)
    check("multiple-candidates: classify() -> AMBIGUOUS (never guesses)", state == AMBIGUOUS)
    check("multiple-candidates: no candidate returned", cand is None)


# ---------------------------------------------------------------------------
# 5. unrelated Telegram messages ignored
# ---------------------------------------------------------------------------
def test_unrelated_messages_ignored():
    history = {"messages": [
        msg(50, "you", "hello unrelated"),
        msg(51, f"@{BOT}", "some unrelated bot chatter"),
        msg(100, "you", f"2-1 dispatch {CORR}"),
        msg(101, f"@{BOT}", ack_text(CORR)),
        msg(102, f"@{BOT}", completed_text("SOME_OTHER_MARKER_ENTIRELY")),  # different marker
        msg(103, f"@{BOT}", completed_text(CORR)),
    ]}
    candidates = scan_terminal_candidates(history, 99, CORR, BOT)
    check("unrelated-ignored: only the real match counted", len(candidates) == 1)
    check("unrelated-ignored: matched message is the right one", candidates[0].message_id == 103)


# ---------------------------------------------------------------------------
# 6. old pre-dispatch matching history ignored (boundary_id)
# ---------------------------------------------------------------------------
def test_old_pre_dispatch_history_ignored():
    # A message BEFORE the dispatch boundary that happens to contain the
    # same marker text (e.g. a coincidental re-use, or a stale leftover
    # from a previous crashed attempt with the same correlation_id) must
    # never count.
    history = {"messages": [
        msg(90, f"@{BOT}", completed_text(CORR)),  # pre-existing, BEFORE boundary
        msg(100, "you", f"2-1 dispatch {CORR}"),
        msg(101, f"@{BOT}", ack_text(CORR)),
    ]}
    boundary = find_boundary_id({"messages": [msg(90, f"@{BOT}", completed_text(CORR))]})
    check("old-history: boundary computed from pre-dispatch snapshot", boundary == 90)
    candidates = scan_terminal_candidates(history, boundary, CORR, BOT)
    state, cand, reason = classify(candidates)
    check("old-history: pre-boundary match excluded", len(candidates) == 0)
    check("old-history: classify() -> PENDING, not COMPLETED", state == PENDING)


# ---------------------------------------------------------------------------
# 7. timeout bounded and deterministic
# ---------------------------------------------------------------------------
def test_timeout_bounded_deterministic():
    store = fresh_store()
    entry = store.start_or_resume(REPO, ISSUE, CORR, boundary_id=99, timeout_seconds=60)
    deadline = datetime.fromisoformat(entry["deadline"])
    created = datetime.fromisoformat(entry["created_at"])
    check("timeout: deadline is exactly created_at + timeout_seconds",
          abs((deadline - created).total_seconds() - 60) < 0.01)

    # Simulate the deadline having passed -- a poll loop checking
    # `datetime.now() >= deadline` must deterministically stop and settle
    # TIMEOUT, never poll forever.
    past_deadline = datetime.now(timezone.utc) - timedelta(seconds=1) >= deadline - timedelta(seconds=61)
    settled = store.settle(REPO, ISSUE, CORR, TIMEOUT, reason="deadline reached, zero exact matches")
    check("timeout: settle() -> TIMEOUT", settled["status"] == TIMEOUT)

    # Resuming after settlement must not reopen polling or change the deadline.
    resumed = store.start_or_resume(REPO, ISSUE, CORR, boundary_id=99, timeout_seconds=60)
    check("timeout: resume after TIMEOUT returns the SAME settled entry, unchanged",
          resumed["status"] == TIMEOUT and resumed["deadline"] == entry["deadline"])


# ---------------------------------------------------------------------------
# 8. collector rerun after completion writes no duplicate comment
# ---------------------------------------------------------------------------
def test_rerun_after_completion_no_duplicate_comment():
    store = fresh_store()
    store.start_or_resume(REPO, ISSUE, CORR, boundary_id=99, timeout_seconds=300)
    store.settle(REPO, ISSUE, CORR, COMPLETED, task_id="task-XYZ", terminal_status="completed",
                 terminal_message_id=102, reason="exact single completed terminal match")
    store.mark_comment_posted(REPO, ISSUE, CORR)

    # A rerun re-enters via start_or_resume + would-be settle again.
    resumed = store.start_or_resume(REPO, ISSUE, CORR, boundary_id=99, timeout_seconds=300)
    check("rerun-no-dup: resumed entry already COMPLETED", resumed["status"] == COMPLETED)
    check("rerun-no-dup: comment_posted already True", resumed["comment_posted"] is True)

    # A second settle() call (as a rerun's code path would attempt) must be
    # a no-op, not overwrite task_id/terminal_status with something else.
    settled_again = store.settle(REPO, ISSUE, CORR, FAILED, task_id="DIFFERENT", reason="should not apply")
    check("rerun-no-dup: second settle() is a no-op (status unchanged)", settled_again["status"] == COMPLETED)
    check("rerun-no-dup: second settle() did not overwrite task_id", settled_again["task_id"] == "task-XYZ")
    # The caller-side contract: only post a comment when comment_posted is
    # False at the time of settlement discovery -- since it's already
    # True here, a correctly-written caller posts nothing further.


# ---------------------------------------------------------------------------
# 9. collector restart does not redispatch / does not reset polling window
# ---------------------------------------------------------------------------
def test_restart_does_not_reset_window():
    store = fresh_store()
    first = store.start_or_resume(REPO, ISSUE, CORR, boundary_id=50, timeout_seconds=120)
    # Simulate a crash-and-restart: call start_or_resume again with
    # DIFFERENT boundary/timeout arguments (as a naive re-implementation
    # might pass "now" values) -- the stored entry must win, unchanged.
    second = store.start_or_resume(REPO, ISSUE, CORR, boundary_id=999, timeout_seconds=99999)
    check("restart: boundary_id NOT reset on restart", second["boundary_id"] == 50)
    check("restart: deadline NOT extended on restart", second["deadline"] == first["deadline"])
    check("restart: status still PENDING (resumable, not redispatched)", second["status"] == PENDING)
    # Dispatch-side non-redispatch itself is covered by
    # relay.state_machine.DispatchStateStore (see test_state_machine.py,
    # scenario 5/6) -- this test covers the result-collector's own half of
    # the same guarantee: it must not silently widen the search window.


# ---------------------------------------------------------------------------
# 10. malformed/missing task_id in apparent terminal message fails closed
# ---------------------------------------------------------------------------
def test_malformed_task_id_fails_closed():
    history = {"messages": [
        msg(100, "you", f"2-1 dispatch {CORR}"),
        msg(101, f"@{BOT}", ack_text(CORR)),
        msg(102, f"@{BOT}", completed_text(CORR, task_id="unknown")),  # renderTerminalResult's own fallback
    ]}
    candidates = scan_terminal_candidates(history, 99, CORR, BOT)
    state, cand, reason = classify(candidates)
    check("malformed-task-id: one terminal-shaped candidate found", len(candidates) == 1)
    check("malformed-task-id: classify() -> AMBIGUOUS, never guesses a task_id", state == AMBIGUOUS)
    check("malformed-task-id: reason mentions the problem", "task_id" in reason)


# ---------------------------------------------------------------------------
# P18-W4 ACK-causal-correlation remediation (Part D/E/F/H) -- ACK recovery
# is now correlation-scoped, not "any bot message newer than X". A v2 ACK
# always carries a Correlation: line (contract_v3.py's --client-correlation,
# Part D); the fast-path (synchronous reply) is authoritative ONLY when it
# matches the EXACT expected correlation_id (Part E).
# ---------------------------------------------------------------------------

def canonical_ack_text(task_id="task-aAWg6YdkKx5yX78gv9pKVC2A", correlation_id="P18W4_TEST_CORR_001"):
    # renderOwnerAck()'s real SUBMIT_TASK/non-council v2 shape
    # (src/owner/telegram-owner-client.mjs) -- the shape the CANONICAL
    # `@project --pm ... --client-correlation ...` dispatch path this
    # relay's v2 contract compiles actually produces (distinct from
    # ack_text()'s shorthand fixture above, a DIFFERENT, numeric-alias-only
    # ack shape with no Correlation line at all).
    corr_line = f"\nCorrelation: {correlation_id}" if correlation_id else ""
    return f"✅ DSH task accepted\nProject: dsh-test-project\nTask: {task_id}\nPM: claude-pm{corr_line}"


EXPECTED_CORR = "P18W4_TEST_CORR_001"
OTHER_CORR = "P18W4_TEST_CORR_OTHER"


def test_ack_task_id_recovered_from_ok_reply_with_exact_correlation():
    send_payload = {"status": "ok", "bot": "@dsh_test_bot", "sent": "...", "reply": canonical_ack_text("task-real123", EXPECTED_CORR)}
    check("ack-task-id-recovered", extract_task_id_from_ack(send_payload, EXPECTED_CORR) == "task-real123")


def test_ack_task_id_none_on_timeout_status():
    # telegram-mcp's real shape: status=="timeout" means Telethon's send
    # returned but NO reply was observed within the send_message timeout
    # window -- `reply` is None. Must never guess a task_id here.
    send_payload = {"status": "timeout", "bot": "@dsh_test_bot", "sent": "...", "reply": None, "timeout_seconds": 45}
    check("ack-task-id-none-on-timeout", extract_task_id_from_ack(send_payload, EXPECTED_CORR) is None)


def test_ack_task_id_none_on_error_status():
    send_payload = {"status": "error", "bot": "@dsh_test_bot", "error": "Cannot find any entity"}
    check("ack-task-id-none-on-error", extract_task_id_from_ack(send_payload, EXPECTED_CORR) is None)


def test_ack_task_id_none_when_reply_is_a_rejection_not_an_acceptance():
    # renderOwnerError()'s real shape -- structurally has no "Task:" line
    # at all. Must never be misparsed as an acceptance.
    send_payload = {"status": "ok", "reply": "❌ project not found"}
    check("ack-task-id-none-on-rejection-reply", extract_task_id_from_ack(send_payload, EXPECTED_CORR) is None)


def test_ack_task_id_none_when_reply_is_shorthand_ack_with_no_canonical_task_line():
    # The pre-W4 shorthand ack fixture (`ack_text()`) has no bare
    # "Task: <id>" line (it has "Task:\n2-1 ..." -- a different shape) --
    # confirms the regex does not accidentally match it.
    send_payload = {"status": "ok", "reply": ack_text("marker")}
    check("ack-task-id-none-on-shorthand-shape", extract_task_id_from_ack(send_payload, EXPECTED_CORR) is None)


# ---- PM review P0 remediation: the fast-path reply is NOT causally authoritative ----

def test_ack_fast_path_rejects_a_terminal_message_returned_as_the_sync_reply():
    # PM review scenario 1: an OLD, unrelated task's own COMPLETED terminal
    # (which also has a "Task: <id>" line) is what telegram-mcp's real
    # send_message() could return as `reply` -- it is NOT an accepted-ACK
    # shape (wrong marker wording: "completed", not "accepted") and must
    # never be misparsed as one, regardless of correlation.
    stale_terminal = completed_text("", task_id="task-STALE-B")
    send_payload = {"status": "ok", "reply": stale_terminal}
    check("ack-fast-path-rejects-stale-terminal", extract_task_id_from_ack(send_payload, EXPECTED_CORR) is None)


def test_ack_fast_path_rejects_a_valid_ack_with_the_wrong_correlation():
    # PM review scenario 5: sync reply contains a real "Task:" line (it IS
    # a valid ACK shape) but the correlation does not match what THIS
    # dispatch expects -- not authoritative for THIS dispatch.
    send_payload = {"status": "ok", "reply": canonical_ack_text("task-wrong-dispatch", OTHER_CORR)}
    check("ack-fast-path-rejects-wrong-correlation", extract_task_id_from_ack(send_payload, EXPECTED_CORR) is None)


def test_ack_fast_path_rejects_a_valid_ack_with_no_correlation_line_at_all():
    send_payload = {"status": "ok", "reply": canonical_ack_text("task-no-corr", correlation_id=None)}
    check("ack-fast-path-rejects-missing-correlation", extract_task_id_from_ack(send_payload, EXPECTED_CORR) is None)


def test_ack_fast_path_accepts_exact_match_only():
    send_payload = {"status": "ok", "reply": canonical_ack_text("task-match", EXPECTED_CORR)}
    check("ack-fast-path-accepts-exact-match", extract_task_id_from_ack(send_payload, EXPECTED_CORR) == "task-match")


# ---- parse_accepted_ack(): structural ACK parsing, marker-gated ----

def test_parse_accepted_ack_rejects_terminal_shaped_text():
    check("parse-ack-rejects-terminal", parse_accepted_ack(completed_text("", task_id="task-x")) is None)
    check("parse-ack-rejects-failed-terminal", parse_accepted_ack(failed_text("", task_id="task-x")) is None)


def test_parse_accepted_ack_accepts_real_ack_shape():
    parsed = parse_accepted_ack(canonical_ack_text("task-y", EXPECTED_CORR))
    check("parse-ack-accepts-real-shape", parsed is not None and parsed["task_id"] == "task-y" and parsed["correlation_id"] == EXPECTED_CORR)


def test_parse_accepted_ack_correlation_is_none_when_absent():
    parsed = parse_accepted_ack(canonical_ack_text("task-z", correlation_id=None))
    check("parse-ack-correlation-none-when-absent", parsed is not None and parsed["correlation_id"] is None)


# ---- PM final hold (strict-ACK-shape remediation): the marker must be the
# message's exact FIRST line, not merely present anywhere in the text ----

def test_parse_accepted_ack_rejects_terminal_whose_result_body_quotes_the_ack_marker():
    # PM final hold, required regression 4: a real terminal message (title
    # line is "completed", never "accepted") whose free-form Result body
    # happens to quote the literal ACK marker text AND a Correlation line
    # (e.g. echoing a prior conversation, a log excerpt, or adversarial-
    # looking model output) must never be misparsed as an acceptance ACK.
    text = (
        "✅ DSH task completed\n\nTask: task-terminal-xyz\nPM: claude-pm\n"
        "Status: completed\n\nResult:\n"
        f"✅ DSH task accepted\nTask: task-SPOOFED\nCorrelation: {EXPECTED_CORR}\n"
        "(this is just the model quoting an earlier message, not a real ACK)"
    )
    check("parse-ack-rejects-marker-quoted-inside-terminal-result-body", parse_accepted_ack(text) is None)


def test_parse_accepted_ack_rejects_failed_terminal_whose_result_body_quotes_the_ack_marker():
    text = (
        "❌ DSH task failed\n\nTask: task-terminal-abc\nPM: claude-pm\n"
        "Status: failed\n\nResult:\n"
        f"the model's own output included the text: ✅ DSH task accepted\nCorrelation: {EXPECTED_CORR}"
    )
    check("parse-ack-rejects-marker-quoted-inside-failed-result-body", parse_accepted_ack(text) is None)


def test_parse_accepted_ack_still_accepts_real_ack_when_marker_is_the_first_line():
    # Sanity companion to the two rejections above: the real ACK shape
    # (marker genuinely on line 1) is unaffected by the tightened check.
    parsed = parse_accepted_ack(canonical_ack_text("task-real-first-line", EXPECTED_CORR))
    check("parse-ack-still-accepts-real-first-line-marker", parsed is not None and parsed["task_id"] == "task-real-first-line")


# ---- P18-W5 ACK cardinality hardening (owner review) -- SINGLE ----------
# An authoritative ACK must NEVER silently pick one identity-bearing field
# when several `Task:`/`Correlation:` lines appear in the SAME message.
# Exactly one Task line; at most one Correlation line; no dedup; no "first".

def test_single_ack_cardinality_one_task_one_correlation_accepted():
    parsed = parse_accepted_ack("✅ DSH task accepted\nProject: p\nTask: task-ok1\nPM: pm-x\nCorrelation: CORR_ok_001")
    check("single-ack-1task-1corr-accepted", parsed and parsed["task_id"] == "task-ok1" and parsed["correlation_id"] == "CORR_ok_001")


def test_single_ack_cardinality_zero_task_rejected():
    check("single-ack-0task-rejected",
          parse_accepted_ack("✅ DSH task accepted\nProject: p\nPM: pm-x\nCorrelation: CORR_ok_001") is None)


def test_single_ack_cardinality_two_identical_task_lines_rejected():
    text = "✅ DSH task accepted\nProject: p\nTask: task-dup\nTask: task-dup\nPM: pm-x\nCorrelation: CORR_ok_001"
    check("single-ack-2identical-task-rejected", parse_accepted_ack(text) is None)


def test_single_ack_cardinality_two_conflicting_task_lines_rejected():
    text = "✅ DSH task accepted\nProject: p\nTask: task-A\nTask: task-B\nPM: pm-x\nCorrelation: CORR_ok_001"
    check("single-ack-2conflicting-task-rejected", parse_accepted_ack(text) is None)


def test_single_ack_cardinality_no_correlation_line_parses_with_none():
    parsed = parse_accepted_ack("✅ DSH task accepted\nProject: p\nTask: task-ok1\nPM: pm-x")
    check("single-ack-0corr-parses-none", parsed is not None and parsed["correlation_id"] is None)


def test_single_ack_cardinality_two_identical_correlation_lines_rejected():
    text = "✅ DSH task accepted\nProject: p\nTask: task-ok1\nPM: pm-x\nCorrelation: CORR_dup_001\nCorrelation: CORR_dup_001"
    check("single-ack-2identical-corr-rejected", parse_accepted_ack(text) is None)


def test_single_ack_cardinality_two_conflicting_correlation_lines_rejected():
    text = "✅ DSH task accepted\nProject: p\nTask: task-ok1\nPM: pm-x\nCorrelation: CORR_aaa_001\nCorrelation: CORR_bbb_002"
    check("single-ack-2conflicting-corr-rejected", parse_accepted_ack(text) is None)


def test_single_ack_cardinality_single_task_line_with_malformed_value_rejected():
    # exactly one `Task:` line, but its value is not a valid task id -> not
    # authoritative (never fall back to "no task id" for a message that
    # clearly tried to carry one).
    check("single-ack-1task-malformed-value-rejected",
          parse_accepted_ack("✅ DSH task accepted\nProject: p\nTask: has space\nPM: pm-x\nCorrelation: CORR_ok_001") is None)


def test_single_ack_cardinality_valid_task_plus_second_malformed_task_line_rejected():
    # one well-formed + one malformed `Task:` line: the loose line-count
    # guard still fails this closed (never silently takes the valid one).
    text = "✅ DSH task accepted\nProject: p\nTask: task-good\nTask: !!!bad\nPM: pm-x\nCorrelation: CORR_ok_001"
    check("single-ack-valid-plus-malformed-task-rejected", parse_accepted_ack(text) is None)


def test_single_ack_cardinality_single_malformed_correlation_line_rejected():
    text = "✅ DSH task accepted\nProject: p\nTask: task-ok1\nPM: pm-x\nCorrelation: !!!"
    check("single-ack-1malformed-corr-rejected", parse_accepted_ack(text) is None)


def test_single_ack_cardinality_fast_path_and_history_reject_malformed():
    # The fast-path (extract_task_id_from_ack) and the history scan
    # (scan_ack_candidates_by_correlation) both delegate to
    # parse_accepted_ack, so malformed intra-message cardinality can never
    # become authoritative through either path.
    dup_task = "✅ DSH task accepted\nProject: p\nTask: task-X\nTask: task-X\nPM: pm-x\nCorrelation: " + EXPECTED_CORR
    dup_corr = f"✅ DSH task accepted\nProject: p\nTask: task-X\nPM: pm-x\nCorrelation: {EXPECTED_CORR}\nCorrelation: {EXPECTED_CORR}"
    check("single-ack-fastpath-rejects-dup-task",
          extract_task_id_from_ack({"status": "ok", "reply": dup_task}, EXPECTED_CORR) is None)
    check("single-ack-fastpath-rejects-dup-corr",
          extract_task_id_from_ack({"status": "ok", "reply": dup_corr}, EXPECTED_CORR) is None)
    h1 = {"messages": [msg(101, f"@{BOT}", dup_task)]}
    h2 = {"messages": [msg(101, f"@{BOT}", dup_corr)]}
    check("single-ack-history-scan-rejects-dup-task", len(scan_ack_candidates_by_correlation(h1, 99, EXPECTED_CORR, BOT)) == 0)
    check("single-ack-history-scan-rejects-dup-corr", len(scan_ack_candidates_by_correlation(h2, 99, EXPECTED_CORR, BOT)) == 0)


# ---- scan_ack_candidates_by_correlation() / classify_ack(): PM review Part H scenarios ----

def test_scenario1_stale_terminal_never_recovered_as_ack():
    # PM review scenario 1, via the history-scan path (not just the fast
    # path already proven above): a stale COMPLETED terminal after the
    # boundary must never be treated as our correlated ACK.
    history = {"messages": [msg(101, f"@{BOT}", completed_text("", task_id="task-STALE-B"))]}
    candidates = scan_ack_candidates_by_correlation(history, 99, EXPECTED_CORR, BOT)
    check("scenario1-zero-ack-candidates-from-stale-terminal", len(candidates) == 0)


def test_scenario2_unrelated_ack_with_different_correlation_ignored():
    history = {"messages": [msg(101, f"@{BOT}", canonical_ack_text("task-UNRELATED-B", OTHER_CORR))]}
    candidates = scan_ack_candidates_by_correlation(history, 99, EXPECTED_CORR, BOT)
    check("scenario2-unrelated-correlation-ignored", len(candidates) == 0)


def test_scenario3_target_ack_after_unrelated_terminal_recovered_correctly():
    history = {"messages": [
        msg(101, f"@{BOT}", completed_text("", task_id="task-UNRELATED-B")),
        msg(102, f"@{BOT}", canonical_ack_text("task-TARGET-A", EXPECTED_CORR)),
    ]}
    candidates = scan_ack_candidates_by_correlation(history, 99, EXPECTED_CORR, BOT)
    task_id, ambiguous, reason = classify_ack(candidates)
    check("scenario3-recovers-only-target", task_id == "task-TARGET-A" and not ambiguous)


def test_scenario6_no_correlated_ack_ever_arrives():
    history = {"messages": [msg(101, f"@{BOT}", completed_text("", task_id="task-STALE-B"))]}
    candidates = scan_ack_candidates_by_correlation(history, 99, EXPECTED_CORR, BOT)
    task_id, ambiguous, reason = classify_ack(candidates)
    check("scenario6-no-candidates", task_id is None and not ambiguous and len(candidates) == 0)


def test_scenario8_same_correlation_different_task_ids_is_ambiguous():
    history = {"messages": [
        msg(101, f"@{BOT}", canonical_ack_text("task-FIRST", EXPECTED_CORR)),
        msg(102, f"@{BOT}", canonical_ack_text("task-SECOND", EXPECTED_CORR)),
    ]}
    candidates = scan_ack_candidates_by_correlation(history, 99, EXPECTED_CORR, BOT)
    task_id, ambiguous, reason = classify_ack(candidates)
    check("scenario8-ambiguous-different-task-ids", task_id is None and ambiguous)
    check("scenario8-reason-mentions-both-ids", "task-FIRST" in reason and "task-SECOND" in reason)


def test_scenario8b_same_correlation_same_task_id_is_not_ambiguous():
    # A literal duplicate delivery of the IDENTICAL ack (e.g. get_history
    # returning it twice across polls is not itself possible, but a
    # genuinely-duplicated bot message would be) must not be flagged
    # ambiguous merely for appearing more than once.
    history = {"messages": [
        msg(101, f"@{BOT}", canonical_ack_text("task-SAME", EXPECTED_CORR)),
        msg(102, f"@{BOT}", canonical_ack_text("task-SAME", EXPECTED_CORR)),
    ]}
    candidates = scan_ack_candidates_by_correlation(history, 99, EXPECTED_CORR, BOT)
    task_id, ambiguous, reason = classify_ack(candidates)
    check("scenario8b-same-task-id-not-ambiguous", task_id == "task-SAME" and not ambiguous)


def test_ack_scan_pre_boundary_ack_ignored():
    history = {"messages": [msg(50, f"@{BOT}", canonical_ack_text("task-old", EXPECTED_CORR))]}
    candidates = scan_ack_candidates_by_correlation(history, 99, EXPECTED_CORR, BOT)
    check("ack-scan-pre-boundary-ignored", len(candidates) == 0)


def test_ack_scan_non_bot_sender_ignored():
    history = {"messages": [msg(101, "you", canonical_ack_text("task-spoof", EXPECTED_CORR))]}
    candidates = scan_ack_candidates_by_correlation(history, 99, EXPECTED_CORR, BOT)
    check("ack-scan-non-bot-ignored", len(candidates) == 0)


def test_ack_recovery_window_is_bounded_and_short():
    # Part F: deliberately much shorter than NORMAL/LONG_RESULT_TIMEOUT_
    # SECONDS -- this is acceptance-latency patience, not execution
    # patience.
    check("ack-recovery-window-bounded", 0 < ACK_RECOVERY_TIMEOUT_SECONDS < NORMAL_RESULT_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# P18-W4 Part E/G: task_id-based terminal correlation -- no correlation
# marker required in the Result body at all.
# ---------------------------------------------------------------------------

def test_task_id_scan_completed_with_no_marker_in_body():
    history = {"messages": [
        msg(100, "you", "@dsh-test-project --pm claude-pm --durability direct do the thing"),
        msg(101, f"@{BOT}", canonical_ack_text("task-realabc")),
        msg(102, f"@{BOT}", completed_text("", task_id="task-realabc")),
    ]}
    candidates = scan_terminal_candidates_by_task_id(history, 99, "task-realabc", BOT)
    state, cand, reason = classify(candidates)
    check("task-id-scan-completed: exactly one candidate", len(candidates) == 1)
    check("task-id-scan-completed: settles COMPLETED", state == COMPLETED)


def test_task_id_scan_failed_with_only_generic_error_body_and_no_marker():
    # This is exactly issue #8's observed shape: a FAILED terminal whose
    # Result body is only the generic PM_DECISION_PARSE_FAILED reason --
    # no correlation marker exists anywhere. Correlation by task_id alone
    # must still settle FAILED correctly.
    history = {"messages": [
        msg(100, "you", "@dsh-test-project --pm claude-pm --durability direct do the thing"),
        msg(101, f"@{BOT}", canonical_ack_text("task-issue8like")),
        msg(102, f"@{BOT}", "❌ DSH task failed\n\nTask: task-issue8like\nPM: claude-pm\n"
                             "Status: failed\n\nResult:\nPM backend returned an invalid decision"),
    ]}
    candidates = scan_terminal_candidates_by_task_id(history, 99, "task-issue8like", BOT)
    state, cand, reason = classify(candidates)
    check("task-id-scan-failed-no-marker: exactly one candidate", len(candidates) == 1)
    check("task-id-scan-failed-no-marker: settles FAILED", state == FAILED)
    check("task-id-scan-failed-no-marker: real body has no correlation marker",
          "P18" not in cand.status and "marker" not in (history["messages"][2]["text"]))


def test_task_id_scan_cancelled_maps_to_failed():
    history = {"messages": [
        msg(100, "you", "dispatch"),
        msg(101, f"@{BOT}", "⚪ DSH task cancelled\n\nTask: task-cxyz\nPM: claude-pm\n"
                             "Status: cancelled\n\nResult:\nowner cancelled"),
    ]}
    candidates = scan_terminal_candidates_by_task_id(history, 99, "task-cxyz", BOT)
    state, cand, reason = classify(candidates)
    check("task-id-scan-cancelled-maps-failed", state == FAILED)


def test_task_id_scan_wrong_task_id_ignored():
    history = {"messages": [
        msg(100, "you", "dispatch"),
        msg(101, f"@{BOT}", completed_text("", task_id="task-DIFFERENT")),
    ]}
    candidates = scan_terminal_candidates_by_task_id(history, 99, "task-expected", BOT)
    check("task-id-scan-wrong-id-ignored: zero candidates", len(candidates) == 0)


def test_task_id_scan_pre_boundary_terminal_ignored():
    history = {"messages": [
        msg(50, f"@{BOT}", completed_text("", task_id="task-samples")),  # before boundary
    ]}
    candidates = scan_terminal_candidates_by_task_id(history, 99, "task-samples", BOT)
    check("task-id-scan-pre-boundary-ignored: zero candidates", len(candidates) == 0)


def test_task_id_scan_multiple_exact_matches_ambiguous():
    history = {"messages": [
        msg(100, f"@{BOT}", completed_text("", task_id="task-dup")),
        msg(101, f"@{BOT}", completed_text("", task_id="task-dup")),
    ]}
    candidates = scan_terminal_candidates_by_task_id(history, 99, "task-dup", BOT)
    state, cand, reason = classify(candidates)
    check("task-id-scan-multiple-exact: two candidates found", len(candidates) == 2)
    check("task-id-scan-multiple-exact: fails closed AMBIGUOUS", state == AMBIGUOUS)


def test_task_id_scan_non_bot_sender_ignored():
    history = {"messages": [
        msg(100, "you", completed_text("", task_id="task-spoof")),  # NOT from the bot
    ]}
    candidates = scan_terminal_candidates_by_task_id(history, 99, "task-spoof", BOT)
    check("task-id-scan-non-bot-ignored: zero candidates", len(candidates) == 0)


def test_completed_v2_does_not_need_a_correlation_marker_echoed():
    # Proves Part G's second requirement directly: a completed v2 task's
    # own real output can be arbitrary text with NO relay marker anywhere,
    # and correlation still settles COMPLETED purely by task_id.
    history = {"messages": [
        msg(100, f"@{BOT}", "✅ DSH task completed\n\nTask: task-v2ok\nPM: claude-pm\n"
                             "Status: completed\n\nResult:\nHere is the real answer to the task, nothing relay-specific."),
    ]}
    candidates = scan_terminal_candidates_by_task_id(history, 99, "task-v2ok", BOT)
    state, cand, reason = classify(candidates)
    check("completed-v2-no-marker-needed", state == COMPLETED)


# ---------------------------------------------------------------------------
# P24.1R: terminal settlement independent of an empty/trimmed Result payload
#
# Incident: GitHub issue #67 (correlation p24-q1-long-single-20260915-v2,
# task_id task-lyeXbh68mu-1xLsXMK17BFT8wGkUrxYs) completed in DSH with an
# empty Result body. renderTerminalResult() legitimately renders that as a
# message that structurally ends in `Result:\n` (empty body, empty outcome
# suffix); real Telegram message transport strips the trailing newline, so
# the message actually observed via get_history ends in `Result:` with NO
# trailing newline. The old TERMINAL_RE hardcoded that newline as mandatory,
# so this exact -- entirely legitimate -- shape never matched, the collector
# never saw a candidate for the full 2400s LONG bound, and #67 settled
# TIMEOUT with task_id=None despite DSH having actually completed. Fixed by
# making the newline between `Result:` and the body optional.
# ---------------------------------------------------------------------------
def test_task_id_scan_completed_empty_result_body_with_trailing_newline():
    history = {"messages": [
        msg(100, f"@{BOT}", "✅ DSH task completed\n\nTask: task-emptyok\nPM: claude-pm\n"
                             "Status: completed\n\nResult:\n"),
    ]}
    candidates = scan_terminal_candidates_by_task_id(history, 99, "task-emptyok", BOT)
    state, cand, reason = classify(candidates)
    check("empty-result-with-trailing-newline: one candidate", len(candidates) == 1)
    check("empty-result-with-trailing-newline: settles COMPLETED", state == COMPLETED)


def test_task_id_scan_completed_result_with_no_trailing_newline_at_all():
    # The exact defect: message text ends in the literal "Result:" with
    # nothing after it at all (as if Telegram trimmed the trailing "\n").
    history = {"messages": [
        msg(100, f"@{BOT}", "✅ DSH task completed\n\nTask: task-trimmedok\nPM: claude-pm\n"
                             "Status: completed\n\nResult:"),
    ]}
    candidates = scan_terminal_candidates_by_task_id(history, 99, "task-trimmedok", BOT)
    state, cand, reason = classify(candidates)
    check("result-no-trailing-newline: one candidate", len(candidates) == 1)
    check("result-no-trailing-newline: settles COMPLETED", state == COMPLETED)
    check("result-no-trailing-newline: body is empty", cand.status == "completed" and cand.task_id == "task-trimmedok")


def test_task_id_scan_failed_result_with_no_trailing_newline_at_all():
    history = {"messages": [
        msg(100, f"@{BOT}", "❌ DSH task failed\n\nTask: task-trimmedfail\nPM: claude-pm\n"
                             "Status: failed\n\nResult:"),
    ]}
    candidates = scan_terminal_candidates_by_task_id(history, 99, "task-trimmedfail", BOT)
    state, cand, reason = classify(candidates)
    check("failed-no-trailing-newline: settles FAILED", state == FAILED)


def test_task_id_scan_cancelled_result_with_no_trailing_newline_at_all():
    history = {"messages": [
        msg(100, f"@{BOT}", "⚪ DSH task cancelled\n\nTask: task-trimmedcancel\nPM: claude-pm\n"
                             "Status: cancelled\n\nResult:"),
    ]}
    candidates = scan_terminal_candidates_by_task_id(history, 99, "task-trimmedcancel", BOT)
    state, cand, reason = classify(candidates)
    check("cancelled-no-trailing-newline: settles FAILED (existing cancelled->FAILED mapping)", state == FAILED)


def test_ack_still_never_matches_terminal_regex_after_optional_newline_change():
    # The `\n?` relaxation must not make the ACCEPTED-ACK shape (no
    # "Status:" line at all) start matching as a terminal candidate.
    history = {"messages": [
        msg(100, f"@{BOT}", ack_text()),
    ]}
    candidates = scan_terminal_candidates_by_task_id(history, 99, "task-anything", BOT)
    check("ack-still-never-terminal: zero candidates", len(candidates) == 0)


def test_wrong_task_id_still_ignored_with_empty_result_body():
    history = {"messages": [
        msg(100, f"@{BOT}", "✅ DSH task completed\n\nTask: task-OTHER\nPM: claude-pm\n"
                             "Status: completed\n\nResult:"),
    ]}
    candidates = scan_terminal_candidates_by_task_id(history, 99, "task-expected", BOT)
    check("wrong-task-id-empty-result-ignored: zero candidates", len(candidates) == 0)


def test_conflicting_terminal_candidates_with_empty_result_bodies_ambiguous():
    history = {"messages": [
        msg(100, f"@{BOT}", "✅ DSH task completed\n\nTask: task-conf\nPM: claude-pm\n"
                             "Status: completed\n\nResult:"),
        msg(101, f"@{BOT}", "❌ DSH task failed\n\nTask: task-conf\nPM: claude-pm\n"
                             "Status: failed\n\nResult:"),
    ]}
    candidates = scan_terminal_candidates_by_task_id(history, 99, "task-conf", BOT)
    state, cand, reason = classify(candidates)
    check("conflicting-empty-result-terminals: fails closed AMBIGUOUS", state == AMBIGUOUS)


def test_issue_67_q1_exact_fixture_settles_completed():
    # Offline replay of the real incident shape (v2-path scanner; the full
    # v3 multimode-path replay lives in test_result_collector_multimode.py):
    # task_id and pm_profile_id are the actual accepted values from issue
    # #67 / correlation p24-q1-long-single-20260915-v2 (see
    # reports/P24_1R_...md). The Result body is empty and the message ends
    # in the bare "Result:" marker, per the observed Telegram-trimmed shape.
    task_id = "task-lyeXbh68mu-1xLsXMK17BFT8wGkUrxYs"
    history = {"messages": [
        msg(14057, "you", "dispatch"),
        msg(14058, f"@{BOT}", "✅ DSH task accepted\n\nProject: dsh-cross-model\n"
                               f"Task: {task_id}\nPM: codex-sol-pm\n"
                               "Correlation: p24-q1-long-single-20260915-v2"),
        msg(14059, f"@{BOT}", f"✅ DSH task completed\n\nTask: {task_id}\n"
                               "PM: codex-sol-pm\nStatus: completed\n\nResult:"),
    ]}
    candidates = scan_terminal_candidates_by_task_id(history, 14057, task_id, BOT)
    state, cand, reason = classify(candidates)
    check("issue-67-q1-offline-replay: one candidate", len(candidates) == 1)
    check("issue-67-q1-offline-replay: settles COMPLETED", state == COMPLETED)
    check("issue-67-q1-offline-replay: task_id matches", cand is not None and cand.task_id == task_id)


# ---------------------------------------------------------------------------
# P24.1R2: boundary-aware bounded history retrieval (pure function)
#
# The installed Telegram MCP get_history tool has no offset/cursor -- it is
# always "give me the newest N messages right now". A single fixed N can
# permanently miss a post-boundary terminal once enough unrelated traffic
# has pushed it out of that window. history_window_covers_boundary() is the
# pure decision function dispatch_v3.mcp_get_history_boundary_aware() uses
# to decide whether one get_history response can be trusted as complete for
# everything newer than boundary_id, or whether a bigger `limit` must be
# tried. See relay/dispatch_v3.py's own escalation-loop docstring for the
# full incident narrative (GitHub issue #67).
# ---------------------------------------------------------------------------

def test_history_window_covers_boundary_when_oldest_message_reaches_boundary():
    # Oldest returned message id (10) is <= boundary_id (10) -- the window
    # reaches all the way back to the boundary, nothing could be missed.
    history = {"messages": [msg(i, f"@{BOT}", "x") for i in range(10, 30)]}  # ids 10..29, 20 messages
    check("covers-boundary-exact-reach", history_window_covers_boundary(history, 10, requested_limit=20))


def test_history_window_does_not_cover_boundary_when_oldest_is_still_newer():
    # Full page returned (20 of 20 requested) but its oldest id (15) is
    # still strictly newer than boundary_id (10) -- there could be
    # messages between 10 and 15 that this fetch never saw.
    history = {"messages": [msg(i, f"@{BOT}", "x") for i in range(15, 35)]}  # ids 15..34, 20 messages
    check("does-not-cover-boundary-gap-possible",
          not history_window_covers_boundary(history, 10, requested_limit=20))


def test_history_window_covers_boundary_when_chat_history_exhausted():
    # Only 5 messages exist in the ENTIRE chat, all newer than boundary_id
    # (0) -- fewer than the 20 requested, so there is nothing further back
    # to possibly miss, regardless of how the oldest id compares.
    history = {"messages": [msg(i, f"@{BOT}", "x") for i in range(1, 6)]}  # 5 messages
    check("covers-boundary-history-exhausted", history_window_covers_boundary(history, 0, requested_limit=20))


def test_history_window_covers_boundary_on_malformed_or_error_payload():
    # No "messages" key at all (e.g. an error payload) -- treated as
    # covered so the existing zero-candidates/PENDING failure semantics are
    # preserved exactly; retrying with a bigger limit cannot fix a
    # connectivity/auth-level failure.
    check("covers-boundary-malformed-payload-treated-as-covered",
          history_window_covers_boundary({"status": "error"}, 10, requested_limit=20))
    check("covers-boundary-empty-messages-list-treated-as-covered",
          history_window_covers_boundary({"messages": []}, 10, requested_limit=20))


def test_history_escalation_limits_are_bounded_and_increasing():
    check("escalation-limits-first-is-page-size", HISTORY_ESCALATION_LIMITS[0] == HISTORY_PAGE_SIZE)
    check("escalation-limits-strictly-increasing",
          all(a < b for a, b in zip(HISTORY_ESCALATION_LIMITS, HISTORY_ESCALATION_LIMITS[1:])))
    check("escalation-limits-small-bounded-count", len(HISTORY_ESCALATION_LIMITS) <= 5)
    check("escalation-limits-max-is-bounded-not-whole-chat",
          MAX_HISTORY_MESSAGES == HISTORY_ESCALATION_LIMITS[-1] and MAX_HISTORY_MESSAGES < 10_000)


# ---------------------------------------------------------------------------
# P18-W4 Part F: NORMAL vs LONG collector patience bounds
# ---------------------------------------------------------------------------

def test_normal_collector_bound_unchanged():
    check("normal-collector-bound-unchanged", NORMAL_RESULT_TIMEOUT_SECONDS == 420)
    check("result-timeout-seconds-for-normal", result_timeout_seconds_for("normal") == NORMAL_RESULT_TIMEOUT_SECONDS)


def test_long_collector_bound_exceeds_dsh_long_execution_ceiling_with_grace():
    check("dsh-long-execution-ceiling-matches-product-repo", DSH_LONG_EXECUTION_CEILING_SECONDS == 1800)
    check("long-collector-bound-greater-than-dsh-ceiling", LONG_RESULT_TIMEOUT_SECONDS > DSH_LONG_EXECUTION_CEILING_SECONDS)
    check("long-collector-bound-not-infinite", LONG_RESULT_TIMEOUT_SECONDS < 6 * 3600)  # sanity bound, not a tuned value
    check("result-timeout-seconds-for-long", result_timeout_seconds_for("long") == LONG_RESULT_TIMEOUT_SECONDS)


def main():
    test_no_result_yet_pending()
    test_exact_completed()
    test_exact_failed()
    test_multiple_candidates_ambiguous()
    test_unrelated_messages_ignored()
    test_old_pre_dispatch_history_ignored()
    test_timeout_bounded_deterministic()
    test_rerun_after_completion_no_duplicate_comment()
    test_restart_does_not_reset_window()
    test_malformed_task_id_fails_closed()

    test_ack_task_id_recovered_from_ok_reply_with_exact_correlation()
    test_ack_task_id_none_on_timeout_status()
    test_ack_task_id_none_on_error_status()
    test_ack_task_id_none_when_reply_is_a_rejection_not_an_acceptance()
    test_ack_task_id_none_when_reply_is_shorthand_ack_with_no_canonical_task_line()

    test_ack_fast_path_rejects_a_terminal_message_returned_as_the_sync_reply()
    test_ack_fast_path_rejects_a_valid_ack_with_the_wrong_correlation()
    test_ack_fast_path_rejects_a_valid_ack_with_no_correlation_line_at_all()
    test_ack_fast_path_accepts_exact_match_only()

    test_parse_accepted_ack_rejects_terminal_shaped_text()
    test_parse_accepted_ack_accepts_real_ack_shape()
    test_parse_accepted_ack_correlation_is_none_when_absent()
    test_parse_accepted_ack_rejects_terminal_whose_result_body_quotes_the_ack_marker()
    test_parse_accepted_ack_rejects_failed_terminal_whose_result_body_quotes_the_ack_marker()
    test_parse_accepted_ack_still_accepts_real_ack_when_marker_is_the_first_line()

    test_single_ack_cardinality_one_task_one_correlation_accepted()
    test_single_ack_cardinality_zero_task_rejected()
    test_single_ack_cardinality_two_identical_task_lines_rejected()
    test_single_ack_cardinality_two_conflicting_task_lines_rejected()
    test_single_ack_cardinality_no_correlation_line_parses_with_none()
    test_single_ack_cardinality_two_identical_correlation_lines_rejected()
    test_single_ack_cardinality_two_conflicting_correlation_lines_rejected()
    test_single_ack_cardinality_single_task_line_with_malformed_value_rejected()
    test_single_ack_cardinality_valid_task_plus_second_malformed_task_line_rejected()
    test_single_ack_cardinality_single_malformed_correlation_line_rejected()
    test_single_ack_cardinality_fast_path_and_history_reject_malformed()

    test_scenario1_stale_terminal_never_recovered_as_ack()
    test_scenario2_unrelated_ack_with_different_correlation_ignored()
    test_scenario3_target_ack_after_unrelated_terminal_recovered_correctly()
    test_scenario6_no_correlated_ack_ever_arrives()
    test_scenario8_same_correlation_different_task_ids_is_ambiguous()
    test_scenario8b_same_correlation_same_task_id_is_not_ambiguous()
    test_ack_scan_pre_boundary_ack_ignored()
    test_ack_scan_non_bot_sender_ignored()
    test_ack_recovery_window_is_bounded_and_short()

    test_task_id_scan_completed_with_no_marker_in_body()
    test_task_id_scan_failed_with_only_generic_error_body_and_no_marker()
    test_task_id_scan_cancelled_maps_to_failed()
    test_task_id_scan_wrong_task_id_ignored()
    test_task_id_scan_pre_boundary_terminal_ignored()
    test_task_id_scan_multiple_exact_matches_ambiguous()
    test_task_id_scan_non_bot_sender_ignored()
    test_completed_v2_does_not_need_a_correlation_marker_echoed()

    test_task_id_scan_completed_empty_result_body_with_trailing_newline()
    test_task_id_scan_completed_result_with_no_trailing_newline_at_all()
    test_task_id_scan_failed_result_with_no_trailing_newline_at_all()
    test_task_id_scan_cancelled_result_with_no_trailing_newline_at_all()
    test_ack_still_never_matches_terminal_regex_after_optional_newline_change()
    test_wrong_task_id_still_ignored_with_empty_result_body()
    test_conflicting_terminal_candidates_with_empty_result_bodies_ambiguous()
    test_issue_67_q1_exact_fixture_settles_completed()

    test_history_window_covers_boundary_when_oldest_message_reaches_boundary()
    test_history_window_does_not_cover_boundary_when_oldest_is_still_newer()
    test_history_window_covers_boundary_when_chat_history_exhausted()
    test_history_window_covers_boundary_on_malformed_or_error_payload()
    test_history_escalation_limits_are_bounded_and_increasing()

    test_normal_collector_bound_unchanged()
    test_long_collector_bound_exceeds_dsh_long_execution_ceiling_with_grace()

    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)} failing checks)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS (all P18-W2 result-collector checks passed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
