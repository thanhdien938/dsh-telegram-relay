"""
P18-W4R4 -- post-timeout terminal settlement.

Fixes the gap found on the first real W4 canary: the bounded result
collector (relay/result_collector.py, RESULT_TIMEOUT_SECONDS = 420s)
settled issue #4's entry TIMEOUT and exited its process while the SAME
DSH task (task-i5OogBxj2B486tdwFqC0z4Tw6Kisz4Es, correlation_id
p18-w4-real-20260902a) was still parked at AWAIT_OWNER. The owner's later
RETRY on that SAME task_id then completed successfully, but no process
was still polling `get_history` to see it -- the terminal Telegram
message exists, but the durable result entry (and the GitHub issue)
never advanced past TIMEOUT.

This module is the minimum idempotent recovery: given a caller-supplied
fresh `get_history` snapshot (read by relay/recover_late_v3.py), re-run
the EXACT SAME classification the original bounded collector used
(scan_terminal_candidates + classify -- no new/looser matching rule), and
if it now finds exactly one terminal candidate for the SAME
correlation_id, move the durable entry from TIMEOUT to a typed
COMPLETED_LATE/FAILED_LATE state -- once, ever, per
repo+issue+correlation_id.

Guarantees (P18-W4R4 requirements):
  - no redispatch / no second Telegram task: this module never imports or
    calls send_message; relay/recover_late_v3.py's Telegram wrapper does
    not even define a send_message method.
  - no issue edit as command channel: the issue body is never re-read or
    re-parsed here -- only the durable correlation_id already recorded by
    dispatch_v3.py and a fresh get_history snapshot are consulted.
  - no guessing task_id: reuses classify()'s existing fail-closed rules
    (AMBIGUOUS on multiple candidates, AMBIGUOUS on a missing/malformed
    task_id) verbatim, plus an optional caller-supplied expected_task_id
    exact-match gate.
  - exact correlation marker required / exact single terminal match
    required: identical to the original collector's contract.
  - reuses existing durable identity: the SAME ResultStateStore entry,
    keyed by repo+issue+correlation_id -- no new store, no new key shape.
  - append at most one later terminal settlement comment: gated by the
    entry's own `late_comment_posted` flag, independent of the original
    TIMEOUT comment's `comment_posted` flag -- so the original TIMEOUT
    comment is never touched, edited, or replaced.
  - transitions only from TIMEOUT: ResultStateStore.settle_late() refuses
    any other starting status and is itself idempotent once already late.
"""
from __future__ import annotations

from typing import Optional
import os

from result_collector import (
    scan_terminal_candidates, classify,
    PENDING, COMPLETED, FAILED, AMBIGUOUS, TIMEOUT,
    COMPLETED_LATE, FAILED_LATE, LATE_TERMINAL_STATES,
)
from target import get_required_telegram_target, TelegramTargetError


def _get_pinned_bot() -> str:
    return get_required_telegram_target()


def __getattr__(name: str):
    if name == "PINNED_BOT":
        return get_required_telegram_target()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


NO_ENTRY = "NO_ENTRY"
ALREADY_LATE_SETTLED = "ALREADY_LATE_SETTLED"
NOT_ELIGIBLE = "NOT_ELIGIBLE"
STILL_PENDING = "STILL_PENDING"
STILL_AMBIGUOUS = "STILL_AMBIGUOUS"
TASK_ID_MISMATCH = "TASK_ID_MISMATCH"
SETTLED = "SETTLED"


def attempt_late_settlement(result_store, repo: str, issue: int, correlation_id: str,
                             history_payload: dict, bot_username: Optional[str] = None, *,
                             expected_task_id: Optional[str] = None):
    if bot_username is None:
        bot_username = get_required_telegram_target()
    """Returns (outcome, entry_or_None, reason).

    outcome is one of: NO_ENTRY, ALREADY_LATE_SETTLED, NOT_ELIGIBLE,
    STILL_PENDING, STILL_AMBIGUOUS, TASK_ID_MISMATCH, SETTLED.

    Every "nothing to do yet" case returns normally; this never raises
    for ordinary inputs (the two ValueErrors ResultStateStore.settle_late
    can raise are pre-empted by the status checks in this function before
    it is ever called)."""
    entry = result_store.get(repo, issue, correlation_id)
    if entry is None:
        return NO_ENTRY, None, "no prior dispatch/result entry exists for this repo/issue/correlation_id"

    if entry["status"] in LATE_TERMINAL_STATES:
        return ALREADY_LATE_SETTLED, entry, (
            f"already settled late (status={entry['status']}); idempotent no-op, "
            "history not rescanned, no comment reconsidered"
        )

    if entry["status"] != TIMEOUT:
        return NOT_ELIGIBLE, entry, (
            f"status={entry['status']!r} is not TIMEOUT; late settlement only ever "
            "corrects a TIMEOUT entry, never re-derives a different outcome for an "
            "already-COMPLETED/FAILED/AMBIGUOUS/PENDING entry"
        )

    boundary_id = entry["boundary_id"]
    candidates = scan_terminal_candidates(history_payload, boundary_id, correlation_id, bot_username)
    state, cand, reason = classify(candidates)

    if state == PENDING:
        return STILL_PENDING, entry, reason
    if state == AMBIGUOUS:
        return STILL_AMBIGUOUS, entry, reason

    if expected_task_id is not None and cand.task_id != expected_task_id:
        return TASK_ID_MISMATCH, entry, (
            f"found exact single {cand.status} terminal match but its task_id={cand.task_id!r} "
            f"does not equal the caller-expected task_id={expected_task_id!r}; refusing to settle "
            "a different task under this correlation_id"
        )

    late_status = COMPLETED_LATE if state == COMPLETED else FAILED_LATE
    settled = result_store.settle_late(
        repo, issue, correlation_id, late_status,
        task_id=cand.task_id, terminal_status=cand.status, terminal_message_id=cand.message_id,
        reason=f"post-timeout terminal settlement: {reason}",
    )
    return SETTLED, settled, f"transitioned TIMEOUT -> {late_status}"


def build_late_settlement_comment(correlation_id: str, entry: dict, *,
                                   task_branch: Optional[str] = None,
                                   result_commit: Optional[str] = None,
                                   published_head: Optional[str] = None,
                                   bot_username: Optional[str] = None) -> str:
    if bot_username is None:
        bot_username = get_required_telegram_target()
    """task_branch/result_commit/published_head are OPTIONAL and must be
    independently verified by the caller from real DSH/Git state before
    being passed in -- this function never parses them out of Telegram
    text or any model-authored prose, and simply omits each line when its
    value is not supplied."""
    status = entry["status"]
    task_id = entry.get("task_id")
    terminal_status = entry.get("terminal_status")
    msg_id = entry.get("terminal_message_id")
    lines = [f"P18-W4 {status}", f"correlation_id: {correlation_id}"]
    if task_id:
        lines.append(f"task_id: {task_id}")
    if terminal_status:
        lines.append(f"terminal_status: {terminal_status}")
    if task_branch:
        lines.append(f"task_branch: {task_branch}")
    if result_commit:
        lines.append(f"result_commit: {result_commit}")
    if published_head:
        lines.append(f"published_head: {published_head}")
    if msg_id:
        lines.append(f"evidence: matched via Telegram MCP get_history, message id {msg_id} (from @{bot_username})")
    lines.append(
        "note: the original TIMEOUT comment above is preserved as historical evidence; "
        "this is a later terminal settlement of the SAME correlation_id/task, not a redispatch"
    )
    lines.append("DSH source/config/schema changes: 0 / 0 / 0")
    return "\n".join(lines) + "\n"
