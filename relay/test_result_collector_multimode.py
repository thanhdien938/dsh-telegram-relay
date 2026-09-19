"""
P18-W5 multimode result-collector tests: ACK task_id recovery and terminal
task_id correlation for SINGLE / COUNCIL / DEBATE.

FIXTURES are byte-exact output of the DSH renderers `renderOwnerAck()` /
`renderTerminalResult()` / `renderLongTaskStarted()` / `renderInteraction()`
in src/owner/telegram-owner-client.mjs at authority
4c547dbffd9a4154e98880ca9d8f6b435fcad0c8, captured once and inlined here
so the relay's parsers are pinned to the ACTUAL strings DSH emits (there is
no other channel -- P18-W5 correlation works purely by parsing this text).

Run: .venv\\Scripts\\python.exe relay\\test_result_collector_multimode.py
"""
import re
import sys

from result_collector import (
    parse_accepted_ack, scan_ack_candidates_by_correlation, classify_ack, AckCandidate,
    parse_council_terminal_message, scan_terminal_candidates_by_task_id_multimode,
    scan_terminal_candidates_by_task_id, classify,
    result_timeout_seconds_for_v3,
    ACK_ACCEPTED_MARKER, ACK_COUNCIL_ACCEPTED_MARKER,
    COUNCIL_RESULT_TIMEOUT_SECONDS, DEBATE_RESULT_TIMEOUT_SECONDS,
    DSH_COUNCIL_EXECUTION_CEILING_SECONDS, DSH_DEBATE_EXECUTION_CEILING_SECONDS,
    MULTIMODE_RESULT_GRACE_SECONDS, NORMAL_RESULT_TIMEOUT_SECONDS,
    LONG_RESULT_TIMEOUT_SECONDS, COMPLETED, FAILED, PENDING, AMBIGUOUS,
)

BOT = "dsh_test_bot"
BOT_FROM = f"@{BOT}"

# --- byte-exact DSH @ 4c547db renderer output ----------------------------
FX = {
 "ack_single_corr": "✅ DSH task accepted\nProject: proj-a\nTask: task-SINGLE01\nPM: pm-x\nCorrelation: relW5single001",
 "ack_single_nocorr": "✅ DSH task accepted\nProject: proj-a\nTask: task-SINGLE01\nPM: pm-x",
 "ack_council_corr": "✅ DSH council accepted\nProject: proj-a\n\nChair:\nchair-x\n\nParticipants:\np1\np2\np3\n\nRounds:\n2\n\nTask: task-COUNCIL01\nCorrelation: relW5council01",
 "ack_council_nocorr": "✅ DSH council accepted\nProject: proj-a\n\nChair:\nchair-x\n\nParticipants:\np1\np2\n\nRounds:\n2\n\nTask: task-COUNCIL02",
 "ack_debate_corr_impl": "✅ DSH council accepted\nProject: proj-a\n\nChair:\nchair-x\n\nParticipants:\np1\np2\n\nRounds:\n2\n\nDebate:\nenabled, max 2 round(s)\n\nImplementation participant:\np1\n\nTask: task-DEBATE01\nCorrelation: relW5debate001",
 "ack_debate_corr_r1": "✅ DSH council accepted\nProject: proj-a\n\nChair:\nchair-x\n\nParticipants:\np1\np2\n\nRounds:\n2\n\nDebate:\nenabled, max 1 round(s)\n\nTask: task-DEBATE02\nCorrelation: relW5debate002",
 "term_single_completed": "✅ DSH task completed\n\nTask: task-SINGLE01\nPM: pm-x\nStatus: completed\n\nResult:\nthe answer",
 "term_single_failed": "❌ DSH task failed\n\nTask: task-SINGLE01\nPM: pm-x\nStatus: failed\n\nResult:\nboom",
 "term_single_cancelled": "⚪ DSH task cancelled\n\nTask: task-SINGLE01\nPM: pm-x\nStatus: cancelled\n\nResult:\n",
 "term_council_completed": "✅ DSH council completed\nProject: proj-a\nChair: chair-x\nCouncil: p1, p2, p3\nRounds: 2\nTask: task-COUNCIL01\n\nResult:\nchair synthesis",
 "term_council_degraded": "⚠️ Council completed with degraded participation\nProject: proj-a\nChair: chair-x\nRounds: 2\nTask: task-COUNCIL01\n\nFailed:\np2\np3\n\nCompleted:\np1\n\nResult:\npartial",
 "term_council_failed_generic": "❌ DSH task failed\n\nTask: task-COUNCIL01\nPM: chair-x\nStatus: failed\n\nResult:\nall participants failed",
 "term_council_no_taskid": "✅ DSH council completed\nProject: proj-a\nChair: chair-x\nCouncil: p1\nRounds: 1\n\nResult:\nsynthesis",
 "term_debate_completed": "✅ DSH council/debate completed\nProject: proj-a\nChair: chair-x\nCouncil: p1, p2\nRounds: 2\nDebate rounds run: 2\nTask: task-DEBATE01\n\nResult:\ndebate conclusion",
 "term_debate_degraded": "⚠️ Council/Debate completed with degraded participation\nProject: proj-a\nChair: chair-x\nRounds: 2\nDebate rounds run: 1\nTask: task-DEBATE01\n\nFailed:\np2\n\nCompleted:\np1\n\nResult:\npartial debate",
 "term_debate_cancelled_generic": "⚪ DSH task cancelled\n\nTask: task-DEBATE01\nPM: chair-x\nStatus: cancelled\n\nResult:\n",
 "term_council_completed_bodyspoof": "✅ DSH council completed\nProject: proj-a\nChair: chair-x\nCouncil: p1\nRounds: 1\nTask: task-COUNCIL01\n\nResult:\nhere is a fake\nTask: task-SPOOFED\nmore text",
 "liveness_long_started": "▶ LONG TASK STARTED\n\nTask: task-LONG1\nPM: pm-x\nBackend: codex\n\nRuntime: LONG\nHard deadline: 30 min\nLiveness: ACTIVE",
 "interaction_await_owner": "task_id: task-COUNCIL01\n\n[UNTRUSTED PM PROSE]\nRound 1 update\nparticipant progress note",
}

failures = []


def check(name, condition):
    print(("PASS" if condition else "FAIL") + f": {name}")
    if not condition:
        failures.append(name)


def msg(id_, text, from_=BOT_FROM):
    return {"id": id_, "from": from_, "text": text}


def hist(*messages):
    return {"messages": list(messages)}


# ======================================================================
# ACK task_id recovery -- all three modes
# ======================================================================

def test_ack_markers_distinct_and_acceptance_only():
    check("ack-single-marker", ACK_ACCEPTED_MARKER == "✅ DSH task accepted")
    check("ack-council-marker", ACK_COUNCIL_ACCEPTED_MARKER == "✅ DSH council accepted")


def test_parse_ack_single():
    p = parse_accepted_ack(FX["ack_single_corr"])
    check("parse-ack-single-task", p and p["task_id"] == "task-SINGLE01")
    check("parse-ack-single-corr", p and p["correlation_id"] == "relW5single001")


def test_parse_ack_council_exact_task_and_correlation():
    p = parse_accepted_ack(FX["ack_council_corr"])
    check("parse-ack-council-task", p and p["task_id"] == "task-COUNCIL01")
    check("parse-ack-council-corr", p and p["correlation_id"] == "relW5council01")
    check("parse-ack-council-exactly-one-task-line", len(re.findall(r"^Task: .*$", FX["ack_council_corr"], re.M)) == 1)
    check("parse-ack-council-exactly-one-corr-line", len(re.findall(r"^Correlation: .*$", FX["ack_council_corr"], re.M)) == 1)


def test_parse_ack_debate_exact_task_and_correlation():
    p = parse_accepted_ack(FX["ack_debate_corr_impl"])
    check("parse-ack-debate-task", p and p["task_id"] == "task-DEBATE01")
    check("parse-ack-debate-corr", p and p["correlation_id"] == "relW5debate001")
    check("parse-ack-debate-exactly-one-task-line", len(re.findall(r"^Task: .*$", FX["ack_debate_corr_impl"], re.M)) == 1)
    p1 = parse_accepted_ack(FX["ack_debate_corr_r1"])
    check("parse-ack-debate-rounds1-task", p1 and p1["task_id"] == "task-DEBATE02")


def test_parse_ack_council_missing_correlation_is_none():
    p = parse_accepted_ack(FX["ack_council_nocorr"])
    check("parse-ack-council-nocorr-task", p and p["task_id"] == "task-COUNCIL02")
    check("parse-ack-council-nocorr-corr-none", p and p["correlation_id"] is None)


def test_parse_ack_rejects_missing_task_line():
    # A council-accepted first line but no Task: line at all -> None.
    text = "✅ DSH council accepted\nProject: proj-a\n\nChair:\nchair-x"
    check("parse-ack-council-missing-task-none", parse_accepted_ack(text) is None)


def test_parse_ack_rejects_terminal_shaped_text_all_modes():
    for k in ("term_single_completed", "term_single_failed", "term_council_completed",
              "term_council_degraded", "term_debate_completed", "term_council_failed_generic"):
        check(f"parse-ack-rejects-terminal[{k}]", parse_accepted_ack(FX[k]) is None)


def test_scan_ack_candidates_council_bot_boundary_correlation():
    h = hist(
        msg(50, FX["ack_council_corr"], from_="@someone_else"),          # wrong bot
        msg(90, FX["ack_council_corr"]),                                  # pre-boundary (<=100)
        msg(110, FX["ack_council_corr"].replace("relW5council01", "OTHERCORR")),  # wrong correlation
        msg(120, FX["ack_council_nocorr"].replace("task-COUNCIL02", "task-COUNCIL01")),  # no Correlation line
        msg(130, FX["ack_council_corr"]),                                 # the real one
    )
    cands = scan_ack_candidates_by_correlation(h, 100, "relW5council01", BOT)
    check("scan-ack-council-one-candidate", len(cands) == 1)
    check("scan-ack-council-right-task", cands and cands[0].task_id == "task-COUNCIL01")


def test_scan_ack_candidates_debate():
    h = hist(msg(200, FX["ack_debate_corr_impl"]))
    cands = scan_ack_candidates_by_correlation(h, 100, "relW5debate001", BOT)
    check("scan-ack-debate-one-candidate", len(cands) == 1 and cands[0].task_id == "task-DEBATE01")


# ======================================================================
# P18-W5 ACK cardinality hardening (owner review) -- COUNCIL / DEBATE
# An authoritative ACK must never silently pick one identity-bearing field
# when several `Task:` / `Correlation:` lines appear in the SAME message.
# Duplicates are NOT deduplicated inside one message; the first is NOT
# selected. (History-level dedup of SEPARATE agreeing messages via
# classify_ack() is a different concern and unchanged.)
# ======================================================================

def _council_ack(task_lines, corr_lines, chair="chair-x", participants=("p1", "p2")):
    parts = ["✅ DSH council accepted", "Project: proj-a", "", "Chair:", chair, "",
             "Participants:", *participants, "", "Rounds:", "2", ""]
    parts += [f"Task: {t}" for t in task_lines]
    parts += [f"Correlation: {c}" for c in corr_lines]
    return "\n".join(parts)


def test_council_ack_cardinality_one_task_one_correlation_accepted():
    p = parse_accepted_ack(_council_ack(["task-C-ok"], ["CORR_council_ok"]))
    check("council-ack-1task-1corr-accepted", p and p["task_id"] == "task-C-ok" and p["correlation_id"] == "CORR_council_ok")


def test_council_ack_cardinality_duplicate_identical_task_rejected():
    check("council-ack-dup-identical-task-rejected",
          parse_accepted_ack(_council_ack(["task-C-dup", "task-C-dup"], ["CORR_council_ok"])) is None)


def test_council_ack_cardinality_conflicting_task_rejected():
    check("council-ack-conflicting-task-rejected",
          parse_accepted_ack(_council_ack(["task-C-A", "task-C-B"], ["CORR_council_ok"])) is None)


def test_council_ack_cardinality_duplicate_identical_correlation_rejected():
    check("council-ack-dup-identical-corr-rejected",
          parse_accepted_ack(_council_ack(["task-C-ok"], ["CORR_council_dup", "CORR_council_dup"])) is None)


def test_council_ack_cardinality_conflicting_correlation_rejected():
    check("council-ack-conflicting-corr-rejected",
          parse_accepted_ack(_council_ack(["task-C-ok"], ["CORR_council_a", "CORR_council_b"])) is None)


def test_debate_ack_cardinality_normal_accepted():
    # Real debate ACK shape (with Debate: / Implementation participant: blocks).
    p = parse_accepted_ack(FX["ack_debate_corr_impl"])
    check("debate-ack-normal-accepted", p and p["task_id"] == "task-DEBATE01" and p["correlation_id"] == "relW5debate001")


def test_debate_ack_cardinality_duplicate_task_rejected():
    text = FX["ack_debate_corr_impl"].replace("\nTask: task-DEBATE01\n", "\nTask: task-DEBATE01\nTask: task-DEBATE01\n")
    check("debate-ack-dup-task-rejected", parse_accepted_ack(text) is None)


def test_debate_ack_cardinality_duplicate_correlation_rejected():
    text = FX["ack_debate_corr_impl"] + "\nCorrelation: relW5debate001"
    check("debate-ack-dup-corr-rejected", parse_accepted_ack(text) is None)


def test_history_scan_rejects_malformed_cardinality_ack_council():
    dup_task = _council_ack(["task-C-X", "task-C-X"], ["relW5council01"])
    dup_corr = _council_ack(["task-C-X"], ["relW5council01", "relW5council01"])
    check("history-scan-council-rejects-dup-task",
          len(scan_ack_candidates_by_correlation(hist(msg(200, dup_task)), 100, "relW5council01", BOT)) == 0)
    check("history-scan-council-rejects-dup-corr",
          len(scan_ack_candidates_by_correlation(hist(msg(200, dup_corr)), 100, "relW5council01", BOT)) == 0)


def test_history_scan_recovers_valid_council_ack_after_a_malformed_one():
    h = hist(
        msg(200, _council_ack(["task-C-BAD", "task-C-BAD"], ["relW5council01"])),  # malformed -> ignored
        msg(201, _council_ack(["task-C-REAL"], ["relW5council01"])),               # valid -> the candidate
    )
    cands = scan_ack_candidates_by_correlation(h, 100, "relW5council01", BOT)
    check("history-scan-recovers-valid-council-after-malformed", len(cands) == 1 and cands[0].task_id == "task-C-REAL")


def test_classify_ack_dedups_and_fails_closed():
    same = [AckCandidate(1, "task-COUNCIL01", "c"), AckCandidate(2, "task-COUNCIL01", "c")]
    tid, amb, _ = classify_ack(same)
    check("classify-ack-dedup-same-task", tid == "task-COUNCIL01" and not amb)
    conflict = [AckCandidate(1, "task-A", "c"), AckCandidate(2, "task-B", "c")]
    tid2, amb2, _ = classify_ack(conflict)
    check("classify-ack-conflict-ambiguous", tid2 is None and amb2)


# ======================================================================
# COUNCIL / DEBATE terminal recognition (parse_council_terminal_message)
# ======================================================================

def test_parse_council_terminal_completed_shapes():
    for k, tid in [
        ("term_council_completed", "task-COUNCIL01"),
        ("term_council_degraded", "task-COUNCIL01"),
        ("term_debate_completed", "task-DEBATE01"),
        ("term_debate_degraded", "task-DEBATE01"),
    ]:
        p = parse_council_terminal_message(FX[k])
        check(f"parse-council-terminal[{k}]-task", p and p["task_id"] == tid)
        check(f"parse-council-terminal[{k}]-status-completed", p and p["terminal_status"] == "completed")


def test_parse_council_terminal_rejects_non_council_completed():
    for k in ("term_single_completed", "term_council_failed_generic", "term_debate_cancelled_generic",
              "ack_council_corr", "liveness_long_started", "interaction_await_owner"):
        check(f"parse-council-terminal-rejects[{k}]", parse_council_terminal_message(FX[k]) is None)


def test_parse_council_terminal_no_header_taskid_is_none():
    check("parse-council-terminal-no-taskid-none", parse_council_terminal_message(FX["term_council_no_taskid"]) is None)


def test_parse_council_terminal_body_task_line_does_not_contaminate_identity():
    p = parse_council_terminal_message(FX["term_council_completed_bodyspoof"])
    check("parse-council-terminal-bodyspoof-uses-header-task", p and p["task_id"] == "task-COUNCIL01")
    check("parse-council-terminal-bodyspoof-not-spoofed", p and p["task_id"] != "task-SPOOFED")


# ======================================================================
# Multimode terminal scan (by exact recovered task_id)
# ======================================================================

def _one(cands):
    return len(cands) == 1


def test_multimode_scan_single_completed_failed_cancelled():
    for k, status in [("term_single_completed", "completed"), ("term_single_failed", "failed"),
                       ("term_single_cancelled", "cancelled")]:
        c = scan_terminal_candidates_by_task_id_multimode(hist(msg(10, FX[k])), 5, "task-SINGLE01", BOT)
        check(f"multimode-scan-single[{k}]", _one(c) and c[0].status == status)


def test_multimode_scan_council_completed_by_task_id():
    c = scan_terminal_candidates_by_task_id_multimode(hist(msg(10, FX["term_council_completed"])), 5, "task-COUNCIL01", BOT)
    check("multimode-scan-council-completed", _one(c) and c[0].status == "completed" and c[0].task_id == "task-COUNCIL01")
    state, cand, _ = classify(c)
    check("multimode-scan-council-classify-completed", state == COMPLETED)


def test_multimode_scan_council_degraded_completed():
    c = scan_terminal_candidates_by_task_id_multimode(hist(msg(10, FX["term_council_degraded"])), 5, "task-COUNCIL01", BOT)
    check("multimode-scan-council-degraded", _one(c) and c[0].status == "completed")


def test_multimode_scan_council_failed_generic_by_task_id():
    c = scan_terminal_candidates_by_task_id_multimode(hist(msg(10, FX["term_council_failed_generic"])), 5, "task-COUNCIL01", BOT)
    check("multimode-scan-council-failed", _one(c) and c[0].status == "failed")
    state, _, _ = classify(c)
    check("multimode-scan-council-failed-classify", state == FAILED)


def test_multimode_scan_debate_completed_and_cancelled():
    cc = scan_terminal_candidates_by_task_id_multimode(hist(msg(10, FX["term_debate_completed"])), 5, "task-DEBATE01", BOT)
    check("multimode-scan-debate-completed", _one(cc) and cc[0].status == "completed")
    cx = scan_terminal_candidates_by_task_id_multimode(hist(msg(10, FX["term_debate_cancelled_generic"])), 5, "task-DEBATE01", BOT)
    check("multimode-scan-debate-cancelled", _one(cx) and cx[0].status == "cancelled")
    state, _, _ = classify(cx)
    check("multimode-scan-debate-cancelled-classify-failed", state == FAILED)


def test_multimode_scan_wrong_task_id_ignored():
    c = scan_terminal_candidates_by_task_id_multimode(hist(msg(10, FX["term_council_completed"])), 5, "task-OTHER", BOT)
    check("multimode-scan-wrong-taskid-ignored", len(c) == 0)


def test_multimode_scan_boundary_and_bot_enforced():
    pre = scan_terminal_candidates_by_task_id_multimode(hist(msg(3, FX["term_council_completed"])), 5, "task-COUNCIL01", BOT)
    check("multimode-scan-pre-boundary-ignored", len(pre) == 0)
    wrongbot = scan_terminal_candidates_by_task_id_multimode(
        hist(msg(10, FX["term_council_completed"], from_="@impostor")), 5, "task-COUNCIL01", BOT)
    check("multimode-scan-wrong-bot-ignored", len(wrongbot) == 0)


def test_multimode_scan_intermediate_messages_never_settle():
    h = hist(
        msg(10, FX["ack_council_corr"]),
        msg(11, FX["liveness_long_started"]),
        msg(12, FX["interaction_await_owner"]),
        msg(13, FX["term_council_completed"]),  # the ONLY real terminal
    )
    c = scan_terminal_candidates_by_task_id_multimode(h, 5, "task-COUNCIL01", BOT)
    check("multimode-scan-only-real-terminal-settles", _one(c) and c[0].message_id == 13)
    state, _, _ = classify(c)
    check("multimode-scan-intermediate-classify-completed", state == COMPLETED)


def test_multimode_scan_conflicting_candidates_ambiguous():
    # council completed + generic failed for the SAME task_id -> AMBIGUOUS.
    h = hist(msg(10, FX["term_council_completed"]), msg(11, FX["term_council_failed_generic"]))
    c = scan_terminal_candidates_by_task_id_multimode(h, 5, "task-COUNCIL01", BOT)
    state, cand, _ = classify(c)
    check("multimode-scan-conflict-completed-vs-failed-ambiguous", len(c) == 2 and state == AMBIGUOUS and cand is None)
    # two council-completed deliveries for the same task_id -> also AMBIGUOUS (fail closed).
    h2 = hist(msg(20, FX["term_council_completed"]), msg(21, FX["term_council_completed"]))
    state2, _, _ = classify(scan_terminal_candidates_by_task_id_multimode(h2, 5, "task-COUNCIL01", BOT))
    check("multimode-scan-duplicate-council-terminal-ambiguous", state2 == AMBIGUOUS)


def test_multimode_scan_v2_single_path_unchanged():
    # The multimode scanner's generic half must match the v2 scanner
    # exactly for a SINGLE terminal (no regression).
    h = hist(msg(10, FX["term_single_completed"]))
    a = scan_terminal_candidates_by_task_id(h, 5, "task-SINGLE01", BOT)
    b = scan_terminal_candidates_by_task_id_multimode(h, 5, "task-SINGLE01", BOT)
    check("multimode-scan-matches-v2-for-single",
          [(x.status, x.task_id, x.message_id) for x in a] == [(x.status, x.task_id, x.message_id) for x in b])


# ======================================================================
# P24.1R: v3 SINGLE LONG terminal settlement, empty/Telegram-trimmed
# Result body -- offline replay of GitHub issue #67 (correlation
# p24-q1-long-single-20260915-v2). See relay/test_result_collector.py's
# own P24.1R section for the full incident narrative.
# ======================================================================

def test_multimode_scan_single_completed_empty_trimmed_result():
    text = "✅ DSH task completed\n\nTask: task-SINGLE01\nPM: pm-x\nStatus: completed\n\nResult:"
    c = scan_terminal_candidates_by_task_id_multimode(hist(msg(10, text)), 5, "task-SINGLE01", BOT)
    state, cand, reason = classify(c)
    check("multimode-empty-trimmed-result: one candidate", len(c) == 1)
    check("multimode-empty-trimmed-result: settles COMPLETED", state == COMPLETED)


def test_issue_67_q1_offline_replay_v3_single_long_path():
    # Exact production path for a v3 SINGLE LONG dispatch:
    # scan_terminal_candidates_by_task_id_multimode(), the same function
    # dispatch_v3.py's poll loop calls. task_id/pm_profile_id are the real
    # accepted values from issue #67.
    task_id = "task-lyeXbh68mu-1xLsXMK17BFT8wGkUrxYs"
    terminal_text = (f"✅ DSH task completed\n\nTask: {task_id}\n"
                      "PM: codex-sol-pm\nStatus: completed\n\nResult:")
    h = hist(
        msg(14058, "✅ DSH task accepted\nProject: dsh-cross-model\n"
                    f"Task: {task_id}\nPM: codex-sol-pm\n"
                    "Correlation: p24-q1-long-single-20260915-v2"),
        msg(14059, terminal_text),
    )
    candidates = scan_terminal_candidates_by_task_id_multimode(h, 14057, task_id, BOT)
    state, cand, reason = classify(candidates)
    check("q1-v3-replay: one candidate", len(candidates) == 1)
    check("q1-v3-replay: settles COMPLETED", state == COMPLETED)
    check("q1-v3-replay: task_id matches accepted value", cand is not None and cand.task_id == task_id)


# ======================================================================
# Mode-aware collector timeout policy
# ======================================================================

def test_v3_timeout_selection():
    check("v3-timeout-council", result_timeout_seconds_for_v3("council") == COUNCIL_RESULT_TIMEOUT_SECONDS)
    check("v3-timeout-debate", result_timeout_seconds_for_v3("debate") == DEBATE_RESULT_TIMEOUT_SECONDS)
    check("v3-timeout-single-normal", result_timeout_seconds_for_v3("single") == NORMAL_RESULT_TIMEOUT_SECONDS)
    check("v3-timeout-single-long", result_timeout_seconds_for_v3("single", runtime_class="long") == LONG_RESULT_TIMEOUT_SECONDS)


def test_v3_timeout_bounds_are_reasonable_and_finite():
    check("council-timeout-exceeds-dsh-council-ceiling", COUNCIL_RESULT_TIMEOUT_SECONDS > DSH_COUNCIL_EXECUTION_CEILING_SECONDS)
    check("debate-timeout-exceeds-dsh-debate-ceiling", DEBATE_RESULT_TIMEOUT_SECONDS > DSH_DEBATE_EXECUTION_CEILING_SECONDS)
    check("council-timeout-exceeds-normal-single", COUNCIL_RESULT_TIMEOUT_SECONDS > NORMAL_RESULT_TIMEOUT_SECONDS)
    check("debate-timeout-exceeds-council", DEBATE_RESULT_TIMEOUT_SECONDS > COUNCIL_RESULT_TIMEOUT_SECONDS)
    check("multimode-timeouts-finite", COUNCIL_RESULT_TIMEOUT_SECONDS < 24 * 3600 and DEBATE_RESULT_TIMEOUT_SECONDS < 24 * 3600)
    check("multimode-grace-matches-long-single-path", MULTIMODE_RESULT_GRACE_SECONDS == 600)
    check("council-timeout-is-ceiling-plus-grace",
          COUNCIL_RESULT_TIMEOUT_SECONDS == DSH_COUNCIL_EXECUTION_CEILING_SECONDS + MULTIMODE_RESULT_GRACE_SECONDS)
    check("debate-timeout-is-ceiling-plus-grace",
          DEBATE_RESULT_TIMEOUT_SECONDS == DSH_DEBATE_EXECUTION_CEILING_SECONDS + MULTIMODE_RESULT_GRACE_SECONDS)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)} failing checks)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"RESULT: PASS (all P18-W5 multimode result-collector checks passed, {len(tests)} test functions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
