"""
P18-W5 multimode dispatch-level tests: authoritative ACK task_id recovery
for COUNCIL/DEBATE through recover_authoritative_task_id(), restart/resume
safety (no second send, no redispatch), v3 issue-identity mutation guard
(mode-specific), and the multimode evidence-comment builders.

Follows the same narrow, additive convention as
test_dispatch_v3_ack_recovery.py -- a fake MCP session, no live Telegram /
GitHub / provider quota. The full run() loop remains exercised live per the
repo's established convention.

Run: .venv\\Scripts\\python.exe relay\\test_dispatch_v3_multimode.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from dispatch_v3 import (
    recover_authoritative_task_id, PINNED_BOT,
    build_dispatched_comment, build_terminal_comment, compiled_header_only,
    mcp_get_history_boundary_aware,
)
from contract_v3 import parse_and_validate
from issue_identity import IssueIdentityStore, NEW, RESUME, PAYLOAD_MUTATED, CORRELATION_CHANGED
from state_machine import DispatchStateStore, TransitionDenied
from result_collector import (
    ResultStateStore, PENDING, COMPLETED, AMBIGUOUS,
    HISTORY_ESCALATION_LIMITS, scan_terminal_candidates_by_task_id_multimode, classify,
)

TP = "[P18-W3-CANARY]"
AUTHOR = "alice"
failures = []


def check(name, condition):
    print(("PASS" if condition else "FAIL") + f": {name}")
    if not condition:
        failures.append(name)


def parse(body):
    return parse_and_validate(f"{TP} x", body, required_title_prefix=TP, author=AUTHOR, expected_author=AUTHOR)


COUNCIL_BODY = """\
schema: p18-dsh-dispatch/v3
mode: council
correlation_id: relW5_disp_council01
project_id: dsh-test-project
pm_profile_id: chair-pm
participants:
  - alpha-pm
  - beta-pm
  - gamma-pm
durability: direct
git:
  commit: false
  push: false
  remote: null
review:
  requested: false
task: |
  Review the architecture.
"""

DEBATE_BODY = """\
schema: p18-dsh-dispatch/v3
mode: debate
correlation_id: relW5_disp_debate01
project_id: dsh-test-project
pm_profile_id: chair-pm
participants:
  - alpha-pm
  - beta-pm
debate_rounds: 2
implementation_profile_id: alpha-pm
durability: local
git:
  commit: true
  push: false
  remote: null
review:
  requested: false
task: |
  Decide the migration.
"""


# --- byte-exact DSH @ 4c547db council/debate acceptance ACKs -------------
def council_ack(task_id, corr):
    return (f"✅ DSH council accepted\nProject: proj-a\n\nChair:\nchair-pm\n\n"
            f"Participants:\nalpha-pm\nbeta-pm\ngamma-pm\n\nRounds:\n2\n\n"
            f"Task: {task_id}\nCorrelation: {corr}")


def debate_ack(task_id, corr):
    return (f"✅ DSH council accepted\nProject: proj-a\n\nChair:\nchair-pm\n\n"
            f"Participants:\nalpha-pm\nbeta-pm\n\nRounds:\n2\n\n"
            f"Debate:\nenabled, max 2 round(s)\n\nImplementation participant:\nalpha-pm\n\n"
            f"Task: {task_id}\nCorrelation: {corr}")


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    async def call_tool(self, name, args):
        assert name == "get_history"
        page = self.pages[min(self.calls, len(self.pages) - 1)]
        self.calls += 1
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(page))])


def h(*messages):
    return {"messages": [{"id": i, "from": f, "text": t} for (i, f, t) in messages]}


BOT = f"@{PINNED_BOT}"
CORR_C = "relW5_disp_council01"
CORR_D = "relW5_disp_debate01"


# ======================================================================
# ACK task_id recovery -- COUNCIL / DEBATE
# ======================================================================

def test_council_ack_taskid_recovered_fast_path():
    async def go():
        s = FakeSession([h()])
        send = {"status": "ok", "reply": council_ack("task-C-1", CORR_C)}
        tid, amb, _ = await recover_authoritative_task_id(s, 10, CORR_C, fresh_send_payload=send)
        check("council-ack-fastpath-taskid", tid == "task-C-1" and not amb)
        check("council-ack-fastpath-confirmed-via-one-history-scan", s.calls == 1)
    asyncio.run(go())


def test_debate_ack_taskid_recovered_from_history():
    async def go():
        s = FakeSession([h((101, BOT, debate_ack("task-D-1", CORR_D)))])
        tid, amb, _ = await recover_authoritative_task_id(s, 10, CORR_D, fresh_send_payload=None)
        check("debate-ack-history-taskid", tid == "task-D-1" and not amb)
    asyncio.run(go())


def test_council_ack_wrong_correlation_ignored():
    async def go():
        s = FakeSession([h((101, BOT, council_ack("task-OTHER", "relW5_disp_councilXX")),
                           (102, BOT, council_ack("task-C-9", CORR_C)))])
        tid, amb, _ = await recover_authoritative_task_id(s, 10, CORR_C, fresh_send_payload=None)
        check("council-ack-wrong-corr-ignored", tid == "task-C-9" and not amb)
    asyncio.run(go())


def test_council_ack_conflicting_taskids_fail_closed():
    async def go():
        s = FakeSession([h((101, BOT, council_ack("task-C-A", CORR_C)),
                           (102, BOT, council_ack("task-C-B", CORR_C)))])
        tid, amb, reason = await recover_authoritative_task_id(s, 10, CORR_C, fresh_send_payload=None)
        check("council-ack-conflict-ambiguous", tid is None and amb)
    asyncio.run(go())


def test_council_ack_wrong_bot_and_pre_boundary_ignored():
    async def go():
        s = FakeSession([h((5, BOT, council_ack("task-PRE", CORR_C)),               # pre-boundary
                           (101, "@impostor", council_ack("task-IMP", CORR_C)),     # wrong bot
                           (102, BOT, council_ack("task-C-REAL", CORR_C)))])
        tid, amb, _ = await recover_authoritative_task_id(s, 10, CORR_C, fresh_send_payload=None)
        check("council-ack-bot-boundary-enforced", tid == "task-C-REAL" and not amb)
    asyncio.run(go())


def test_fast_path_malformed_council_ack_duplicate_task_is_ignored():
    # Case 16: a synchronous fast-path reply that is a council-accepted
    # shape but carries TWO Task: lines -> not authoritative; recovery
    # falls through to the (here empty) bounded scan and returns None.
    async def go():
        bad = council_ack("task-C-X", CORR_C).replace("\nTask: task-C-X\n", "\nTask: task-C-X\nTask: task-C-X\n")
        s = FakeSession([h(), h()])
        tid, amb, _ = await recover_authoritative_task_id(
            s, 10, CORR_C, fresh_send_payload={"status": "ok", "reply": bad},
            timeout_seconds=0.2, poll_interval_seconds=0.02)
        check("fastpath-malformed-council-dup-task-ignored", tid is None and not amb)
    asyncio.run(go())


def test_fast_path_malformed_council_ack_duplicate_correlation_is_ignored():
    # Case 17.
    async def go():
        bad = council_ack("task-C-X", CORR_C) + f"\nCorrelation: {CORR_C}"
        s = FakeSession([h(), h()])
        tid, amb, _ = await recover_authoritative_task_id(
            s, 10, CORR_C, fresh_send_payload={"status": "ok", "reply": bad},
            timeout_seconds=0.2, poll_interval_seconds=0.02)
        check("fastpath-malformed-council-dup-corr-ignored", tid is None and not amb)
    asyncio.run(go())


def test_history_malformed_council_ack_not_returned_by_scan():
    # Cases 18/19 at the recovery level: a malformed-cardinality council
    # ACK sitting in history is not a candidate, but a VALID one right
    # after it still recovers the authoritative task id (case 20).
    async def go():
        dup_task = council_ack("task-C-BAD", CORR_C).replace("\nTask: task-C-BAD\n", "\nTask: task-C-BAD\nTask: task-C-BAD\n")
        s = FakeSession([h((101, BOT, dup_task), (102, BOT, council_ack("task-C-GOOD", CORR_C)))])
        tid, amb, _ = await recover_authoritative_task_id(s, 10, CORR_C, fresh_send_payload=None)
        check("history-malformed-council-ack-skipped-valid-recovered", tid == "task-C-GOOD" and not amb)
    asyncio.run(go())


def test_valid_history_council_ack_after_malformed_sync_reply():
    # Case 20 explicitly: malformed synchronous reply -> fall through ->
    # valid history ACK -> authoritative task id still recovered.
    async def go():
        bad_sync = council_ack("task-C-SYNC", CORR_C) + f"\nCorrelation: {CORR_C}"  # dup correlation
        s = FakeSession([h((101, BOT, council_ack("task-C-HIST", CORR_C)))])
        tid, amb, _ = await recover_authoritative_task_id(
            s, 10, CORR_C, fresh_send_payload={"status": "ok", "reply": bad_sync})
        check("valid-history-council-after-malformed-sync", tid == "task-C-HIST" and not amb)
    asyncio.run(go())


def test_valid_fast_and_history_same_council_task_still_unique():
    # Case 21: existing unique-recovery behavior is unchanged by the patch.
    async def go():
        s = FakeSession([h((101, BOT, council_ack("task-C-1", CORR_C)))])
        tid, amb, _ = await recover_authoritative_task_id(
            s, 10, CORR_C, fresh_send_payload={"status": "ok", "reply": council_ack("task-C-1", CORR_C)})
        check("valid-fast-and-history-same-council-task-unique", tid == "task-C-1" and not amb)
    asyncio.run(go())


def test_valid_fast_council_task_conflicting_history_still_ambiguous():
    # Case 22: existing AMBIGUOUS fail-closed behavior is unchanged.
    async def go():
        s = FakeSession([h((101, BOT, council_ack("task-C-HIST", CORR_C)))])
        tid, amb, _ = await recover_authoritative_task_id(
            s, 10, CORR_C, fresh_send_payload={"status": "ok", "reply": council_ack("task-C-FAST", CORR_C)})
        check("valid-fast-council-vs-conflicting-history-ambiguous", tid is None and amb)
    asyncio.run(go())


def test_restart_recovers_council_taskid_with_no_fresh_send():
    # Exactly a resumed run: fresh_send_payload=None, recovery from the
    # durable boundary_id + correlation_id alone, scanning fresh history.
    async def go():
        s = FakeSession([h(), h(), h((101, BOT, council_ack("task-C-RESUMED", CORR_C)))])
        tid, amb, _ = await recover_authoritative_task_id(
            s, 10, CORR_C, fresh_send_payload=None, timeout_seconds=1.0, poll_interval_seconds=0.01)
        check("restart-council-recovers-no-redispatch", tid == "task-C-RESUMED" and not amb)
        check("restart-council-multi-poll", s.calls >= 3)
    asyncio.run(go())


# ======================================================================
# Restart / resume safety via the durable stores (schema-agnostic keys,
# pinned here for v3 council)
# ======================================================================

def _stores():
    d = Path(tempfile.mkdtemp())
    return (DispatchStateStore(d / "disp.json"),
            ResultStateStore(d / "res.json"),
            IssueIdentityStore(d / "ident.json"))


def test_v3_no_second_send_after_delivery():
    disp, _, _ = _stores()
    repo, issue, corr = "r", 7, CORR_C
    disp.reserve(repo, issue, corr)
    disp.mark_sent(repo, issue, corr, {"boundary_id": 42})
    try:
        disp.reserve(repo, issue, corr)
        check("v3-no-second-send", False)
    except TransitionDenied as e:
        check("v3-no-second-send", e.code == "ALREADY_DELIVERED")


def test_v3_result_resume_is_idempotent():
    _, res, _ = _stores()
    repo, issue, corr = "r", 7, CORR_C
    e1 = res.start_or_resume(repo, issue, corr, boundary_id=42, timeout_seconds=1860)
    e2 = res.start_or_resume(repo, issue, corr, boundary_id=999, timeout_seconds=99999)
    check("v3-resume-keeps-boundary", e2["boundary_id"] == 42)
    check("v3-resume-keeps-deadline", e2["deadline"] == e1["deadline"])
    res.settle(repo, issue, corr, COMPLETED, task_id="task-C-1", terminal_status="completed", terminal_message_id=5)
    e3 = res.settle(repo, issue, corr, "FAILED", task_id="other")
    check("v3-settle-idempotent", e3["status"] == COMPLETED and e3["task_id"] == "task-C-1")


def test_v3_issue_identity_mode_specific_mutation_guard():
    _, _, ident = _stores()
    repo, issue = "r", 7
    base = parse(DEBATE_BODY)
    outcome, _ = ident.register_or_verify(repo, issue, base.correlation_id, base.payload_digest())
    check("v3-identity-new", outcome == NEW)
    # exact same body -> RESUME
    outcome2, _ = ident.register_or_verify(repo, issue, base.correlation_id, parse(DEBATE_BODY).payload_digest())
    check("v3-identity-resume", outcome2 == RESUME)
    # participant reorder -> PAYLOAD_MUTATED
    reordered = parse(DEBATE_BODY.replace("  - alpha-pm\n  - beta-pm\n", "  - beta-pm\n  - alpha-pm\n"))
    o_r, _ = ident.register_or_verify(repo, issue, reordered.correlation_id, reordered.payload_digest())
    check("v3-identity-participant-reorder-mutated", o_r == PAYLOAD_MUTATED)
    # chair change -> PAYLOAD_MUTATED
    chair = parse(DEBATE_BODY.replace("pm_profile_id: chair-pm", "pm_profile_id: chair-two")
                  .replace("implementation_profile_id: alpha-pm", "implementation_profile_id: alpha-pm"))
    o_c, _ = ident.register_or_verify(repo, issue, chair.correlation_id, chair.payload_digest())
    check("v3-identity-chair-change-mutated", o_c == PAYLOAD_MUTATED)
    # rounds change -> PAYLOAD_MUTATED
    rounds = parse(DEBATE_BODY.replace("debate_rounds: 2", "debate_rounds: 1"))
    o_ro, _ = ident.register_or_verify(repo, issue, rounds.correlation_id, rounds.payload_digest())
    check("v3-identity-rounds-change-mutated", o_ro == PAYLOAD_MUTATED)
    # correlation change -> CORRELATION_CHANGED
    newcorr = parse(DEBATE_BODY.replace("relW5_disp_debate01", "relW5_disp_debate99"))
    o_cc, _ = ident.register_or_verify(repo, issue, newcorr.correlation_id, newcorr.payload_digest())
    check("v3-identity-correlation-change-failclosed", o_cc == CORRELATION_CHANGED)


def test_v3_issue_identity_council_independent_of_debate():
    _, _, ident = _stores()
    c = parse(COUNCIL_BODY)
    o1, _ = ident.register_or_verify("r", 1, c.correlation_id, c.payload_digest())
    # same council, adding a participant -> mutated
    c2 = parse(COUNCIL_BODY.replace("  - gamma-pm\n", "  - gamma-pm\n  - delta-pm\n"))
    o2, _ = ident.register_or_verify("r", 1, c2.correlation_id, c2.payload_digest())
    check("v3-identity-council-add-participant-mutated", o1 == NEW and o2 == PAYLOAD_MUTATED)


# ======================================================================
# Evidence comments (safe multimode audit trail)
# ======================================================================

def test_dispatched_comment_council_shape():
    p = parse(COUNCIL_BODY)
    body = build_dispatched_comment(p, compiled_header_only(p), task_id="task-C-1")
    for needle in ("mode: council", "chair_profile_id: chair-pm",
                   "participants: alpha-pm,beta-pm,gamma-pm", "schema: v3",
                   "task_id: task-C-1", "compiled_command_header: @dsh-test-project --pm chair-pm --debate alpha-pm,beta-pm,gamma-pm"):
        check(f"dispatched-council-has[{needle}]", needle in body)
    check("dispatched-council-no-runtime-class", "runtime_class:" not in body)
    check("dispatched-council-header-no-task-body", "Review the architecture" not in body)


def test_dispatched_comment_debate_shape():
    p = parse(DEBATE_BODY)
    body = build_dispatched_comment(p, compiled_header_only(p))
    for needle in ("mode: debate", "debate_rounds: 2", "implementation_profile_id: alpha-pm",
                   "--debate-extend --debate-rounds 2 --implementation alpha-pm"):
        check(f"dispatched-debate-has[{needle}]", needle in body)


def test_dispatched_comment_single_v3_has_runtime_class():
    p = parse(COUNCIL_BODY.replace(
        "mode: council\n", "mode: single\n").replace(
        "participants:\n  - alpha-pm\n  - beta-pm\n  - gamma-pm\n", "runtime_class: long\n"))
    body = build_dispatched_comment(p, compiled_header_only(p))
    check("dispatched-v3-single-runtime-class", "runtime_class: long" in body)
    check("dispatched-v3-single-pm-profile-id", "pm_profile_id: chair-pm" in body)


def test_terminal_comment_includes_mode_when_known():
    entry = {"status": COMPLETED, "task_id": "task-C-1", "terminal_status": "completed", "terminal_message_id": 12}
    body = build_terminal_comment(CORR_C, entry, timeout_seconds_used=1860, mode="council")
    check("terminal-comment-mode", "mode: council" in body)
    check("terminal-comment-taskid", "task_id: task-C-1" in body)
    check("terminal-comment-terminal-status", "terminal_status: completed" in body)
    # mode omitted entirely when not known (never guessed)
    body2 = build_terminal_comment(CORR_C, entry, timeout_seconds_used=1860, mode=None)
    check("terminal-comment-mode-omitted-when-unknown", "mode:" not in body2)


# ======================================================================
# P24.1R2: boundary-aware history retrieval for the terminal poll loop
#
# The installed Telegram MCP get_history tool has no offset/cursor -- every
# call is "give me the newest N messages right now". These tests drive
# mcp_get_history_boundary_aware() (the escalation wrapper the terminal
# poll loop now calls instead of a bare mcp_get_history(session,
# limit=20)) against a FakeSession that records exactly which `limit` each
# call requested, so the escalation behavior itself -- not just the final
# outcome -- is verified.
# ======================================================================

class LimitRecordingFakeSession:
    """Like FakeSession above, but records the `limit` argument of every
    get_history call (in order) so a test can assert exactly how many
    escalation attempts happened and at what sizes -- not just the final
    result."""

    def __init__(self, pages_by_limit: dict):
        # pages_by_limit maps a requested `limit` value -> the payload to
        # return for a call requesting exactly that limit. A limit with no
        # entry falls back to the largest configured limit's page (mirrors
        # "the chat has this many messages total" once escalation reaches
        # a limit larger than the whole chat).
        self.pages_by_limit = pages_by_limit
        self.requested_limits: list[int] = []

    async def call_tool(self, name, args):
        assert name == "get_history"
        limit = args["limit"]
        self.requested_limits.append(limit)
        page = self.pages_by_limit.get(limit, self.pages_by_limit[max(self.pages_by_limit)])
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(page))])


TASK_X = "task-X"
BOUNDARY = 100


def test_history_escalation_ordinary_newest_page_path_unchanged():
    # 1 & 16: terminal already inside the newest page (limit=20) whose
    # oldest id already reaches the boundary -- exactly one get_history
    # call, unchanged cost from the pre-P24.1R2 ordinary path.
    page20 = h((101, BOT, "accepted"), (110, BOT, terminal_text_single(TASK_X)))
    session = LimitRecordingFakeSession({20: page20})

    async def go():
        return await mcp_get_history_boundary_aware(session, BOUNDARY)
    history = asyncio.run(go())
    candidates = scan_terminal_candidates_by_task_id_multimode(history, BOUNDARY, TASK_X, PINNED_BOT)
    state, cand, reason = classify(candidates)
    check("escalation-ordinary-path-one-call", session.requested_limits == [20])
    check("escalation-ordinary-path-settles-completed", state == COMPLETED)


def test_history_escalation_recovers_terminal_pushed_off_the_newest_page():
    # 2, 7: the terminal (id 110) is older than the newest 20-message page
    # (ids 112..131, 20 unrelated messages) but newer than boundary (100).
    # The limit=20 fetch cannot see it and does not reach the boundary
    # either (oldest id 112 > 100) -- must escalate to limit=100, whose
    # page (ids 101..131, 31 messages, fewer than the 100 requested) both
    # contains the terminal AND proves full coverage (history exhausted).
    unrelated_new = [(i, BOT, "unrelated liveness/chatter") for i in range(112, 132)]
    page20 = h(*unrelated_new)  # ids 112..131 -- terminal NOT visible, gap to boundary unresolved
    full_chat = h((101, BOT, "accepted"), (110, BOT, terminal_text_single(TASK_X)), *unrelated_new)
    session = LimitRecordingFakeSession({20: page20, 100: full_chat})

    async def go():
        return await mcp_get_history_boundary_aware(session, BOUNDARY)
    history = asyncio.run(go())
    candidates = scan_terminal_candidates_by_task_id_multimode(history, BOUNDARY, TASK_X, PINNED_BOT)
    state, cand, reason = classify(candidates)
    check("escalation-recovers-pushed-off-terminal-two-calls", session.requested_limits == [20, 100])
    check("escalation-recovers-pushed-off-terminal-settles-completed", state == COMPLETED)
    check("escalation-recovers-pushed-off-terminal-correct-message-id", cand is not None and cand.message_id == 110)


def test_history_escalation_terminal_exactly_just_after_boundary():
    # 3: terminal is the very first post-boundary message (id 101,
    # boundary 100) -- found on the very first (limit=20) fetch.
    page20 = h((101, BOT, terminal_text_single(TASK_X)))
    session = LimitRecordingFakeSession({20: page20})

    async def go():
        return await mcp_get_history_boundary_aware(session, BOUNDARY)
    history = asyncio.run(go())
    state, cand, reason = classify(scan_terminal_candidates_by_task_id_multimode(history, BOUNDARY, TASK_X, PINNED_BOT))
    check("escalation-terminal-just-after-boundary-found", state == COMPLETED)


def test_history_escalation_terminal_at_or_before_boundary_ignored():
    # 4: a terminal-shaped message exists, but AT the boundary id itself
    # (100) -- must still be ignored (pre-existing boundary semantics,
    # untouched by escalation).
    page20 = h((100, BOT, terminal_text_single(TASK_X)))
    session = LimitRecordingFakeSession({20: page20})

    async def go():
        return await mcp_get_history_boundary_aware(session, BOUNDARY)
    history = asyncio.run(go())
    candidates = scan_terminal_candidates_by_task_id_multimode(history, BOUNDARY, TASK_X, PINNED_BOT)
    check("escalation-at-boundary-terminal-ignored", len(candidates) == 0)


def test_history_escalation_wrong_task_id_on_older_page_ignored():
    # 5: escalation recovers an older page, but its terminal is for a
    # DIFFERENT task_id -- still ignored, exactly like the ordinary path.
    unrelated_new = [(i, BOT, "unrelated") for i in range(112, 132)]
    page20 = h(*unrelated_new)
    full_chat = h((101, BOT, "accepted"), (110, BOT, terminal_text_single("task-OTHER")), *unrelated_new)
    session = LimitRecordingFakeSession({20: page20, 100: full_chat})

    async def go():
        return await mcp_get_history_boundary_aware(session, BOUNDARY)
    history = asyncio.run(go())
    candidates = scan_terminal_candidates_by_task_id_multimode(history, BOUNDARY, TASK_X, PINNED_BOT)
    check("escalation-wrong-task-id-older-page-ignored", len(candidates) == 0)


def test_history_escalation_wrong_bot_source_on_older_page_ignored():
    # 6: escalation recovers an older page, but the terminal-shaped message
    # is from "you" (the relay's own outgoing command), not the pinned
    # bot -- still ignored.
    unrelated_new = [(i, BOT, "unrelated") for i in range(112, 132)]
    page20 = h(*unrelated_new)
    full_chat = h((101, BOT, "accepted"), (110, "you", terminal_text_single(TASK_X)), *unrelated_new)
    session = LimitRecordingFakeSession({20: page20, 100: full_chat})

    async def go():
        return await mcp_get_history_boundary_aware(session, BOUNDARY)
    history = asyncio.run(go())
    candidates = scan_terminal_candidates_by_task_id_multimode(history, BOUNDARY, TASK_X, PINNED_BOT)
    check("escalation-wrong-bot-source-older-page-ignored", len(candidates) == 0)


def test_history_escalation_found_on_last_allowed_page():
    # 8: terminal only recoverable at the LARGEST configured escalation
    # size (HISTORY_ESCALATION_LIMITS[-1]) -- both smaller sizes report an
    # unresolved gap (oldest id still newer than boundary, full page).
    biggest = HISTORY_ESCALATION_LIMITS[-1]
    middle = HISTORY_ESCALATION_LIMITS[1]
    unrelated_small = [(i, BOT, "u") for i in range(BOUNDARY + biggest - 19, BOUNDARY + biggest + 1)]
    unrelated_middle = [(i, BOT, "u") for i in range(BOUNDARY + biggest - 99, BOUNDARY + biggest + 1)]
    full_chat = h((BOUNDARY + 1, BOT, terminal_text_single(TASK_X)), *unrelated_middle)
    session = LimitRecordingFakeSession({
        HISTORY_ESCALATION_LIMITS[0]: h(*unrelated_small),
        middle: h(*unrelated_middle),
        biggest: full_chat,
    })

    async def go():
        return await mcp_get_history_boundary_aware(session, BOUNDARY)
    history = asyncio.run(go())
    state, cand, reason = classify(scan_terminal_candidates_by_task_id_multimode(history, BOUNDARY, TASK_X, PINNED_BOT))
    check("escalation-found-on-last-allowed-page-all-sizes-tried", session.requested_limits == list(HISTORY_ESCALATION_LIMITS))
    check("escalation-found-on-last-allowed-page-completed", state == COMPLETED)


def test_history_escalation_terminal_beyond_bounded_scan_limit_stays_pending():
    # 9: the terminal exists, but even the LARGEST configured escalation
    # page cannot reach back to it (unresolved gap persists through every
    # size) -- must NOT be guessed; stays PENDING, exactly like today's
    # ordinary TIMEOUT-eventually behavior for a message outside the
    # bounded retrieval policy.
    pages = {limit: h(*[(BOUNDARY + limit - k, BOT, "unrelated") for k in range(limit)])
             for limit in HISTORY_ESCALATION_LIMITS}
    session = LimitRecordingFakeSession(pages)

    async def go():
        return await mcp_get_history_boundary_aware(session, BOUNDARY)
    history = asyncio.run(go())
    state, cand, reason = classify(scan_terminal_candidates_by_task_id_multimode(history, BOUNDARY, TASK_X, PINNED_BOT))
    check("escalation-beyond-bound-tries-every-size", session.requested_limits == list(HISTORY_ESCALATION_LIMITS))
    check("escalation-beyond-bound-stays-pending-not-guessed", state == PENDING)


def test_history_escalation_conflicting_candidates_ambiguous():
    # 10: two conflicting terminal candidates for the same task_id recovered
    # via escalation -- still fails closed AMBIGUOUS, unchanged.
    unrelated_new = [(i, BOT, "unrelated") for i in range(112, 132)]  # exactly 20 -- must NOT look exhausted
    page20 = h(*unrelated_new)
    full_chat = h(
        (101, BOT, "accepted"),
        (105, BOT, terminal_text_single(TASK_X, status="completed")),
        (110, BOT, terminal_text_single(TASK_X, status="failed")),
        *unrelated_new,
    )
    session = LimitRecordingFakeSession({20: page20, 100: full_chat})

    async def go():
        return await mcp_get_history_boundary_aware(session, BOUNDARY)
    history = asyncio.run(go())
    state, cand, reason = classify(scan_terminal_candidates_by_task_id_multimode(history, BOUNDARY, TASK_X, PINNED_BOT))
    check("escalation-conflicting-candidates-ambiguous", state == AMBIGUOUS)


def test_history_escalation_no_post_boundary_messages_zero_candidates():
    # 12: nothing at all has happened since the boundary -- every
    # escalation size returns an empty (or all-pre-boundary) chat; must
    # terminate (not loop forever) and report zero candidates.
    session = LimitRecordingFakeSession({limit: {"messages": []} for limit in HISTORY_ESCALATION_LIMITS})

    async def go():
        return await mcp_get_history_boundary_aware(session, BOUNDARY)
    history = asyncio.run(go())
    candidates = scan_terminal_candidates_by_task_id_multimode(history, BOUNDARY, TASK_X, PINNED_BOT)
    check("escalation-empty-chat-terminates-one-call", session.requested_limits == [HISTORY_ESCALATION_LIMITS[0]])
    check("escalation-empty-chat-zero-candidates", len(candidates) == 0)


def test_history_escalation_small_static_chat_stops_after_first_call():
    # 13: a quiet/small chat where every escalation size would return the
    # identical (exhausted) set of messages -- must stop after the FIRST
    # call (len(messages) < requested_limit proves exhaustion immediately),
    # never treated as a "non-progressing cursor" that needs special
    # infinite-loop protection -- the bounded `for limit in ...` loop
    # structurally cannot loop forever regardless.
    small_chat = h((101, BOT, "accepted"), (103, BOT, terminal_text_single(TASK_X)))
    session = LimitRecordingFakeSession({20: small_chat, 100: small_chat, 500: small_chat})

    async def go():
        return await mcp_get_history_boundary_aware(session, BOUNDARY)
    history = asyncio.run(go())
    state, cand, reason = classify(scan_terminal_candidates_by_task_id_multimode(history, BOUNDARY, TASK_X, PINNED_BOT))
    check("escalation-small-static-chat-one-call-only", session.requested_limits == [20])
    check("escalation-small-static-chat-settles-completed", state == COMPLETED)


def test_history_escalation_error_payload_preserves_existing_failure_semantics():
    # 14: a get_history call errors (no "messages" key at all) -- must not
    # crash, must not endlessly retry, and must fall through to the same
    # zero-candidates/PENDING outcome the pre-P24.1R2 code already had for
    # an error/malformed payload.
    session = LimitRecordingFakeSession({20: {"status": "error", "error": "boom"}})

    async def go():
        return await mcp_get_history_boundary_aware(session, BOUNDARY)
    history = asyncio.run(go())
    candidates = scan_terminal_candidates_by_task_id_multimode(history, BOUNDARY, TASK_X, PINNED_BOT)
    state, cand, reason = classify(candidates)
    check("escalation-error-payload-one-call-no-crash", session.requested_limits == [20])
    check("escalation-error-payload-pending-not-crashed", state == PENDING)


def test_history_escalation_p24_1r_empty_result_terminal_on_older_page():
    # 15: combines both incident fixes -- the P24.1R empty/Telegram-trimmed
    # Result body shape, AND recovering it from a page older than the
    # newest 20 via P24.1R2 escalation.
    unrelated_new = [(i, BOT, "unrelated") for i in range(112, 132)]
    page20 = h(*unrelated_new)
    trimmed_terminal = f"✅ DSH task completed\n\nTask: {TASK_X}\nPM: pm-x\nStatus: completed\n\nResult:"
    full_chat = h((101, BOT, "accepted"), (110, BOT, trimmed_terminal), *unrelated_new)
    session = LimitRecordingFakeSession({20: page20, 100: full_chat})

    async def go():
        return await mcp_get_history_boundary_aware(session, BOUNDARY)
    history = asyncio.run(go())
    state, cand, reason = classify(scan_terminal_candidates_by_task_id_multimode(history, BOUNDARY, TASK_X, PINNED_BOT))
    check("escalation-p24-1r-empty-result-on-older-page-completed", state == COMPLETED)


def terminal_text_single(task_id, status="completed"):
    icon = {"completed": "✅", "failed": "❌", "cancelled": "⚪"}[status]
    return f"{icon} DSH task {status}\n\nTask: {task_id}\nPM: pm-x\nStatus: {status}\n\nResult:\nreal output"


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
    print(f"RESULT: PASS (all P18-W5 multimode dispatch-level checks passed, {len(tests)} test functions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
