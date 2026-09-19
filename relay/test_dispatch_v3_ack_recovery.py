"""
P18-W4 ACK-causal-correlation remediation -- integration-level proof for
`recover_authoritative_task_id()` (relay/dispatch_v3.py), specifically the
PM review scenarios that require the ASYNC recovery procedure itself (not
just its pure-function building blocks, already covered in
relay/test_result_collector.py): fast-path success/rejection, the bounded
post-boundary scan succeeding with NO fresh send_payload at all (exactly
what a RESTARTED/resumed run does -- Part F), and the bounded window
genuinely expiring.

Run: .venv\\Scripts\\python.exe relay\\test_dispatch_v3_ack_recovery.py

Note: `dispatch_v3.py` itself has no pre-existing unit-test module in this
repo (every other *_v3.py entrypoint is exercised live, per the repo's own
established convention -- see docs/P18_RELAY_PRODUCTION_GENERALIZATION_REPORT.md).
This file is a narrow, additive exception scoped ONLY to
`recover_authoritative_task_id()`, because Part F of the PM's own remediation
request explicitly requires proving the restart-safety property with tests,
and that property is only observable at this async level (a fake MCP
session standing in for the real `ClientSession.call_tool()` telegram-mcp
uses -- no live Telegram, no live GitHub, no real provider quota).

P18-W4 FAST-PATH-UNIQUENESS remediation (PM final hold, follow-up P0): an
independent source audit found that the original version of this function
returned a valid fast-path task_id immediately, WITHOUT ever consulting
get_history -- so a second, conflicting exact-correlation ACK already
sitting in history could never be detected (scenario 4's own old
assertion, `fast-path-never-calls-get-history`, directly encoded this
bug as an intended property). The `test_fastpath_uniqueness_*` functions
below are the exact regression scenarios from that review: a fast-path
candidate is now always merged with an IMMEDIATE (not delayed) history
scan and classified through the SAME `classify_ack()` every purely
history-derived finding goes through, so it can reduce latency but can
never bypass the "two distinct task_ids sharing one correlation_id ->
AMBIGUOUS" uniqueness invariant.
"""
import asyncio
import sys
import json
import os
from types import SimpleNamespace
os.environ.setdefault("DSH_RELAY_TELEGRAM_TARGET", "dsh_test_bot")

from dispatch_v3 import recover_authoritative_task_id, PINNED_BOT
from result_collector import ACK_RECOVERY_TIMEOUT_SECONDS

failures = []


def check(name, condition):
    if condition:
        print(f"PASS: {name}")
    else:
        print(f"FAIL: {name}")
        failures.append(name)


CORR = "P18W4_ASYNC_TEST_CORR_001"
OTHER_CORR = "P18W4_ASYNC_TEST_CORR_OTHER"


def ack_text(task_id, correlation_id=CORR):
    corr_line = f"\nCorrelation: {correlation_id}" if correlation_id else ""
    return f"✅ DSH task accepted\nProject: dsh-test-project\nTask: {task_id}\nPM: claude-pm{corr_line}"


def terminal_text(task_id, status="completed"):
    icon = {"completed": "✅", "failed": "❌", "cancelled": "⚪"}[status]
    return f"{icon} DSH task {status}\n\nTask: {task_id}\nPM: claude-pm\nStatus: {status}\n\nResult:\nsome real output, no relay marker anywhere"


class FakeSession:
    """Duck-typed stand-in for mcp.ClientSession -- only the one method
    mcp_get_history() actually calls. `history_pages` is a list of
    successive get_history payloads; each call to call_tool() advances to
    the next page (staying on the last one once exhausted), so a test can
    simulate "the ACK is not there yet on the first poll, but is by the
    second" without any real waiting."""

    def __init__(self, history_pages):
        self.history_pages = history_pages
        self.calls = 0

    async def call_tool(self, name, args):
        assert name == "get_history"
        page = self.history_pages[min(self.calls, len(self.history_pages) - 1)]
        self.calls += 1
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(page))])


def history_payload(messages):
    return {"messages": messages}


def msg(id_, from_, text):
    return {"id": id_, "from": from_, "text": text}


# ---- PM review scenario 4: fast-path recovery succeeds, no history scan needed ----

async def _scenario4():
    session = FakeSession([history_payload([])])  # empty history: fast candidate alone resolves it
    send_payload = {"status": "ok", "reply": ack_text("task-fastpath", CORR)}
    task_id, ambiguous, reason = await recover_authoritative_task_id(session, 99, CORR, fresh_send_payload=send_payload)
    check("scenario4-fast-path-succeeds", task_id == "task-fastpath" and not ambiguous)
    # P18-W4 FAST-PATH-UNIQUENESS remediation: the old assertion here was
    # `session.calls == 0` ("fast path never calls get_history") -- that
    # was the exact bug an independent audit caught (it meant a
    # conflicting exact-correlation ACK already in history could never be
    # detected). The correct property is the opposite: the fast path DOES
    # consult history -- exactly ONCE, immediately, never waiting out the
    # bounded window -- proving it reduces recovery latency without
    # bypassing the uniqueness check.
    check("scenario4-fast-path-confirmed-via-exactly-one-immediate-history-scan-not-bypassed", session.calls == 1)


def test_scenario4_fast_path_recovery_succeeds():
    asyncio.run(_scenario4())


# ---- PM review scenario 5 (async level): fast path present but wrong correlation -> falls through to scan ----

async def _scenario5_falls_through_to_scan():
    # The synchronous reply is a real ACK but for the WRONG correlation
    # (e.g. another concurrent dispatch's own ACK) -- not authoritative;
    # the bounded scan must still run and find OUR real ACK.
    session = FakeSession([history_payload([
        msg(101, f"@{PINNED_BOT}", ack_text("task-real", CORR)),
    ])])
    send_payload = {"status": "ok", "reply": ack_text("task-someone-else", OTHER_CORR)}
    task_id, ambiguous, reason = await recover_authoritative_task_id(session, 99, CORR, fresh_send_payload=send_payload)
    check("scenario5-falls-through-to-scan-and-recovers", task_id == "task-real" and not ambiguous)
    check("scenario5-scan-was-actually-attempted", session.calls >= 1)


def test_scenario5_wrong_correlation_fast_path_falls_through_to_scan():
    asyncio.run(_scenario5_falls_through_to_scan())


# ---- PM review scenario 7: crash/restart -- NO fresh send_payload at all, recovery from history alone ----

async def _scenario7_resume_with_no_fast_path_available():
    # Exactly what a RESUMED run looks like: fresh_send_payload=None (no
    # synchronous reply available this invocation -- the process that sent
    # it already exited). Recovery must succeed using ONLY the already-
    # durable boundary_id + correlation_id, scanning fresh get_history.
    session = FakeSession([history_payload([
        msg(101, f"@{PINNED_BOT}", ack_text("task-recovered-after-restart", CORR)),
    ])])
    task_id, ambiguous, reason = await recover_authoritative_task_id(session, 99, CORR, fresh_send_payload=None)
    check("scenario7-recovers-after-simulated-restart-with-no-fast-path", task_id == "task-recovered-after-restart")
    check("scenario7-not-ambiguous", not ambiguous)


def test_scenario7_restart_recovers_from_history_alone_no_redispatch():
    asyncio.run(_scenario7_resume_with_no_fast_path_available())


async def _scenario7_takes_a_few_polls_before_the_ack_appears():
    # Simulates the ACK not being visible on the very first history read
    # (e.g. a real network/replication delay) but appearing shortly after
    # -- still within the bounded window, still without ever redispatching
    # (this function never sends anything).
    session = FakeSession([
        history_payload([]),  # poll 1: not there yet
        history_payload([]),  # poll 2: still not there
        history_payload([msg(101, f"@{PINNED_BOT}", ack_text("task-eventually", CORR))]),  # poll 3: found
    ])
    task_id, ambiguous, reason = await recover_authoritative_task_id(
        session, 99, CORR, fresh_send_payload=None,
        timeout_seconds=1.0, poll_interval_seconds=0.01,  # tiny bounds -- this test must not take 60 real seconds
    )
    check("scenario7-multi-poll-eventually-recovers", task_id == "task-eventually" and not ambiguous)
    check("scenario7-multiple-polls-were-actually-made", session.calls >= 3)


def test_scenario7_recovery_survives_a_few_empty_polls_before_the_ack_appears():
    asyncio.run(_scenario7_takes_a_few_polls_before_the_ack_appears())


# ---- PM review scenario 6: correlated ACK never arrives within the bounded window ----

async def _scenario6_never_arrives():
    session = FakeSession([history_payload([])])  # never has our ACK, ever
    task_id, ambiguous, reason = await recover_authoritative_task_id(
        session, 99, CORR, fresh_send_payload=None,
        timeout_seconds=0.2, poll_interval_seconds=0.05,  # tiny bound -- must not take a real 60s
    )
    check("scenario6-never-arrives-returns-none", task_id is None)
    check("scenario6-never-arrives-not-ambiguous", not ambiguous)
    check("scenario6-reason-mentions-bounded-window", "bounded recovery window" in reason)


def test_scenario6_bounded_window_expires_without_a_match():
    asyncio.run(_scenario6_never_arrives())


# ---- PM review scenario 8 (async level): ambiguous result surfaces through the full async path ----

async def _scenario8_ambiguous():
    session = FakeSession([history_payload([
        msg(101, f"@{PINNED_BOT}", ack_text("task-A", CORR)),
        msg(102, f"@{PINNED_BOT}", ack_text("task-B", CORR)),
    ])])
    task_id, ambiguous, reason = await recover_authoritative_task_id(session, 99, CORR, fresh_send_payload=None)
    check("scenario8-async-level-ambiguous", task_id is None and ambiguous)


def test_scenario8_ambiguous_surfaces_through_full_async_recovery():
    asyncio.run(_scenario8_ambiguous())


# ---- PM final hold (fast-path-uniqueness remediation): the fast path must
# never bypass classify_ack()'s uniqueness check. Numbered exactly as the
# PM review's own 6-item regression list. ----

async def _fastpath_uniqueness_1_conflicting_history_is_ambiguous():
    # 1. fresh_send_payload = ACK correlation C task A; history = ACK C
    # task A + ACK C task B -> AMBIGUOUS. The fast path must NOT suppress
    # the conflicting history candidate just because it agrees with one
    # of the two.
    session = FakeSession([history_payload([
        msg(101, f"@{PINNED_BOT}", ack_text("task-A", CORR)),
        msg(102, f"@{PINNED_BOT}", ack_text("task-B", CORR)),
    ])])
    send_payload = {"status": "ok", "reply": ack_text("task-A", CORR)}
    task_id, ambiguous, reason = await recover_authoritative_task_id(session, 99, CORR, fresh_send_payload=send_payload)
    check("fastpath-uniqueness-1-ambiguous-despite-matching-fast-path-reply", task_id is None and ambiguous)


def test_fastpath_uniqueness_1_conflicting_history_is_ambiguous():
    asyncio.run(_fastpath_uniqueness_1_conflicting_history_is_ambiguous())


async def _fastpath_uniqueness_2_unrelated_correlation_in_history_ignored():
    # 2. fresh_send_payload = ACK C task A; history = an unrelated ACK for
    # a DIFFERENT correlation D, plus ACK C task A -> recovers task A. The
    # unrelated D-correlation message is filtered out by
    # scan_ack_candidates_by_correlation()'s own correlation match, same
    # as it always was -- the merge does not change that.
    session = FakeSession([history_payload([
        msg(101, f"@{PINNED_BOT}", ack_text("task-owned-by-D", OTHER_CORR)),
        msg(102, f"@{PINNED_BOT}", ack_text("task-A", CORR)),
    ])])
    send_payload = {"status": "ok", "reply": ack_text("task-A", CORR)}
    task_id, ambiguous, reason = await recover_authoritative_task_id(session, 99, CORR, fresh_send_payload=send_payload)
    check("fastpath-uniqueness-2-recovers-task-a-past-unrelated-correlation", task_id == "task-A" and not ambiguous)


def test_fastpath_uniqueness_2_unrelated_correlation_in_history_ignored():
    asyncio.run(_fastpath_uniqueness_2_unrelated_correlation_in_history_ignored())


async def _fastpath_uniqueness_3_first_history_read_has_no_ack_yet():
    # 3. fresh_send_payload = ACK C task A; the (only) history read has no
    # ACK at all yet (get_history has not indexed it). Must NOT be treated
    # as an incorrect failure and must NOT force waiting out the bounded
    # window -- the fast candidate alone, unopposed, resolves immediately.
    session = FakeSession([history_payload([])])
    send_payload = {"status": "ok", "reply": ack_text("task-A", CORR)}
    task_id, ambiguous, reason = await recover_authoritative_task_id(session, 99, CORR, fresh_send_payload=send_payload)
    check("fastpath-uniqueness-3-resolves-despite-empty-history-read", task_id == "task-A" and not ambiguous)
    check("fastpath-uniqueness-3-resolved-on-first-poll-bounded-semantics-intact", session.calls == 1)


def test_fastpath_uniqueness_3_first_history_read_has_no_ack_yet():
    asyncio.run(_fastpath_uniqueness_3_first_history_read_has_no_ack_yet())


async def _fastpath_uniqueness_5_valid_exact_correlation_ack_is_accepted():
    # 5. A valid DSH accepted ACK with the exact expected correlation_id,
    # confirmed unopposed by an immediate history scan -> accepted.
    session = FakeSession([history_payload([
        msg(101, f"@{PINNED_BOT}", ack_text("task-A", CORR)),
    ])])
    send_payload = {"status": "ok", "reply": ack_text("task-A", CORR)}
    task_id, ambiguous, reason = await recover_authoritative_task_id(session, 99, CORR, fresh_send_payload=send_payload)
    check("fastpath-uniqueness-5-exact-correlation-ack-accepted", task_id == "task-A" and not ambiguous)


def test_fastpath_uniqueness_5_valid_exact_correlation_ack_is_accepted():
    asyncio.run(_fastpath_uniqueness_5_valid_exact_correlation_ack_is_accepted())


async def _fastpath_uniqueness_6_duplicate_delivery_same_task_id_not_ambiguous():
    # 6. Same-correlation duplicate deliveries of the SAME task_id (the
    # fast-path reply and a real history sighting of the identical ACK)
    # must not be flagged ambiguous merely for appearing twice -- they are
    # deduplicated by task_id before classify_ack() ever sees them.
    session = FakeSession([history_payload([
        msg(101, f"@{PINNED_BOT}", ack_text("task-A", CORR)),
    ])])
    send_payload = {"status": "ok", "reply": ack_text("task-A", CORR)}
    task_id, ambiguous, reason = await recover_authoritative_task_id(session, 99, CORR, fresh_send_payload=send_payload)
    check("fastpath-uniqueness-6-duplicate-same-task-id-not-ambiguous", task_id == "task-A" and not ambiguous)


def test_fastpath_uniqueness_6_duplicate_delivery_same_task_id_not_ambiguous():
    asyncio.run(_fastpath_uniqueness_6_duplicate_delivery_same_task_id_not_ambiguous())


# ---- production defaults are the real 60s/5s bounds, never silently swapped in tests ----

def test_default_bounds_match_production_constants():
    import inspect
    sig = inspect.signature(recover_authoritative_task_id)
    check("default-timeout-is-real-production-constant", sig.parameters["timeout_seconds"].default == ACK_RECOVERY_TIMEOUT_SECONDS)


def main():
    test_scenario4_fast_path_recovery_succeeds()
    test_scenario5_wrong_correlation_fast_path_falls_through_to_scan()
    test_scenario7_restart_recovers_from_history_alone_no_redispatch()
    test_scenario7_recovery_survives_a_few_empty_polls_before_the_ack_appears()
    test_scenario6_bounded_window_expires_without_a_match()
    test_scenario8_ambiguous_surfaces_through_full_async_recovery()

    test_fastpath_uniqueness_1_conflicting_history_is_ambiguous()
    test_fastpath_uniqueness_2_unrelated_correlation_in_history_ignored()
    test_fastpath_uniqueness_3_first_history_read_has_no_ack_yet()
    test_fastpath_uniqueness_5_valid_exact_correlation_ack_is_accepted()
    test_fastpath_uniqueness_6_duplicate_delivery_same_task_id_not_ambiguous()

    test_default_bounds_match_production_constants()

    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)} failing checks)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS (all P18-W4 ACK-causal-correlation async recovery checks passed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
