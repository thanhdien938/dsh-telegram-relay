"""
P18-W2 bounded result collector.

Reuses the exact P17-W1 accepted async correlation method (see
docs/phase17/P17_W1_ASYNC_RESULT_CORRELATION_REPORT.md in the DSH repo):
dispatch via the bot's own numeric shorthand, capture the ACK (no
task_id), then bounded-poll `get_history` for the one terminal message
that (a) comes from the bot, (b) is newer than a pre-dispatch boundary
message id, and (c) contains the caller-generated unique correlation
marker in its `Result:` body. Correlation level 1: task_id + marker, both
recovered from the same terminal message, exactly as P17-W1 proved.

Terminal message shape is read (not modified, not guessed) from
`renderTerminalResult()` in `src/owner/telegram-owner-client.mjs`:

    <icon> DSH task <status>

    Task: <task_id>
    PM: <profile>
    Status: <status>

    Result:
    <body>

`status` is one of completed / failed / cancelled; `icon` is
[U+2705]/[U+274C]/[U+26AA] respectively. This is structurally distinct
from the shorthand ACK (`renderShorthandAck`), which never has a
`Status:` line -- so an ACK can never be misparsed as a terminal result.

Result states (exactly the five required): PENDING, COMPLETED, FAILED,
AMBIGUOUS, TIMEOUT. Never guesses a task_id: a matching-but-malformed
terminal message (task_id missing/"unknown"), or more than one plausible
terminal candidate, settles AMBIGUOUS rather than picking one.

P18-W4 ACK-causal-correlation remediation (PM review, P0): the original
v2 `extract_task_id_from_ack()` trusted telegram-mcp's `send_message()`
synchronous `reply` field as THE causal ACK for this exact dispatch. The
installed/public `send_message()` implementation does not actually
establish that -- it returns `bot_replies[0].text`, the first bot message
newer than the sent message's id, with no `reply_to_msg_id` validation,
no causal linkage, no request nonce. A previously-running, unrelated
task's own terminal message completing at the wrong moment could
therefore be returned as `reply` and misparsed as this dispatch's own
ACK (both shapes contain a `Task: <id>` line).

Fix: the synchronous reply is now used ONLY as a FAST PATH, and only when
it is BOTH (a) a valid DSH accepted-ACK shape (the literal marker
`✅ DSH task accepted`, never a terminal message's own `completed`/
`failed`/`cancelled` wording) AND (b) carries the EXACT expected
`Correlation: <correlation_id>` line (Part D: v2 always compiles
`--client-correlation <correlation_id>`, echoed back verbatim by DSH's
own `renderOwnerAck()` -- never by the model). Anything else (wrong
shape, missing/mismatched correlation, no reply observed) is NOT
authoritative and falls through to a bounded, restart-safe post-boundary
`get_history` scan for the same exact shape+correlation
(`scan_ack_candidates_by_correlation()`), which is the only path this
module treats as ground truth. No "first ACK after timestamp"/"nearest
project profile"/"first bot reply"/"first Task: line" fallback exists
anywhere in this module.

P18-W4 FAST-PATH-UNIQUENESS remediation (PM review, follow-up P0): the
functions in this module (`extract_task_id_from_ack()`,
`scan_ack_candidates_by_correlation()`, `classify_ack()`) are pure
building blocks -- they do not themselves decide whether a fast-path
reply is trusted in isolation or merged with a history observation
first. That decision lives in `dispatch_v3.recover_authoritative_task_id()`,
which now ALWAYS merges any valid fast-path `AckCandidate` into the same
list an immediate `get_history` scan produces before calling
`classify_ack()` once -- so a fast-path reply can no longer bypass the
same "two distinct task_ids sharing one correlation_id -> AMBIGUOUS"
uniqueness check a purely history-derived finding already goes through.
See that function's own docstring for the full rationale.

P18-W4 FAST-PATH-UNIQUENESS remediation (PM review, strict-ACK-shape
follow-up): `parse_accepted_ack()` now requires the ACK marker to be the
message's exact FIRST line (not merely present anywhere in the text) --
see that function's own docstring for why.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

PENDING = "PENDING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
AMBIGUOUS = "AMBIGUOUS"
TIMEOUT = "TIMEOUT"

# P18-W4 Part D: v2-only. Delivery to Telegram was confirmed (state_machine
# already moved to SENT), but the synchronous send_message reply did not
# contain a recoverable `Task: <task_id>` line (either no reply was
# observed within the send timeout, or the reply text was not DSH's own
# task-accepted ACK shape -- e.g. a rejection). There is nothing to scan
# get_history for, so the relay fails closed here rather than guessing
# (Part D explicitly forbids a "first accepted message" heuristic). This
# is a TERMINAL outcome for the RESULT side only -- the Telegram send
# itself already succeeded; the underlying DSH task may or may not exist
# and may or may not have actually run.
TASK_ID_UNRECOVERABLE = "TASK_ID_UNRECOVERABLE"

TERMINAL_STATES = {COMPLETED, FAILED, AMBIGUOUS, TIMEOUT, TASK_ID_UNRECOVERABLE}

# P18-W4 Part F: two named collector-patience bounds, replacing the single
# RESULT_TIMEOUT_SECONDS constant dispatch_v3.py used to hardcode
# everywhere. This is OBSERVATION/CORRELATION patience only -- it never
# touches DSH's own execution timeout (pm-execution-timeout-policy.mjs),
# it only decides how long THIS relay process keeps polling get_history
# before giving up and settling TIMEOUT.
#
# NORMAL_RESULT_TIMEOUT_SECONDS (420s) is the pre-W4 value, unchanged --
# still correct for a v1 dispatch (always NORMAL) and a v2 dispatch with
# runtime_class=normal.
#
# DSH_LONG_EXECUTION_CEILING_SECONDS mirrors the DSH product repo's own
# LONG_TASK_HARD_DEADLINE_MS (src/pm/pm-execution-timeout-policy.mjs,
# 1_800_000ms = 1800s) -- named and commented here specifically so a
# future reader can verify the two repos are still in sync by comparing
# this one number, rather than re-deriving "30 minutes" from scratch.
# LONG_RESULT_TIMEOUT_SECONDS adds a bounded (not infinite) 600s/10-minute
# grace margin on top of it for GitHub/Telegram network round-trips and
# DSH's own settlement/claim-completion writes (all near-instant in
# practice, per the DSH repo's own P12/P13 durability work -- this margin
# is deliberately generous, not tightly tuned) -- the relay collector must
# never declare TIMEOUT before DSH's own legitimate LONG execution ceiling
# could still be running.
NORMAL_RESULT_TIMEOUT_SECONDS = 420
DSH_LONG_EXECUTION_CEILING_SECONDS = 1800
LONG_RESULT_TIMEOUT_SECONDS = DSH_LONG_EXECUTION_CEILING_SECONDS + 600  # 2400s = 40 minutes

# P18-W5: mode-aware collector patience for COUNCIL / DEBATE.
#
# Council/Debate participants and debate rounds execute SEQUENTIALLY inside
# one outer DSH task, so a legitimate multimodel task's wall-clock latency
# is materially higher than a NORMAL SINGLE task -- blindly reusing the
# 420s NORMAL SINGLE bound would false-timeout real work. These ceilings
# are the SUM of DSH's own per-stage execution-timeout-policy constants
# (src/pm/pm-execution-timeout-policy.mjs @ 4c547db) for the WORST-CASE
# shape the relay v3 contract can actually produce, then + a bounded grace
# margin identical to the LONG SINGLE path (600s, for GitHub/Telegram
# round-trips and DSH settlement writes). Each summand assumes every step
# burns its FULL timeout (which never happens in a healthy run), so these
# are deliberately generous upper bounds, not tight tuning.
#
# DSH per-stage constants used (milliseconds -> seconds):
#   chair_plan / participant_report / participant_critique
#     / debate_brief / debate_response  = 120_000 ms  -> 120 s
#   chair_synthesis / debate_synthesis  = 180_000 ms  -> 180 s
#   council_implementation_participant  = 1_800_000 ms -> 1800 s
#     (== LONG_TASK_HARD_DEADLINE_MS; the ONE selected debate implementation
#      participant's own report turn)
#   COUNCIL_MAX_PARTICIPANTS = 4 ; report+critique rounds = 2 ;
#   DEBATE_MAX_ROUNDS = 2
#
# COUNCIL worst case (v3 council FORBIDS an implementation participant, so
# every step is <=180 s):
#   chair_plan(120) + 4*participant_report(120) + 4*participant_critique(120)
#     + chair_synthesis(180)                                = 1260 s
#
# DEBATE worst case (v3 debate MAY carry one implementation participant):
#   council portion: chair_plan(120) + 3*report(120) + 1*impl_report(1800)
#     + 4*critique(120) + chair_synthesis(180)              = 2940 s
#   debate portion : 2 rounds * [ brief(120) + 4*response(120)
#     + debate_synthesis(180) ]                             = 1560 s
#   total                                                    = 4500 s
DSH_COUNCIL_EXECUTION_CEILING_SECONDS = 1260
DSH_DEBATE_EXECUTION_CEILING_SECONDS = 4500
MULTIMODE_RESULT_GRACE_SECONDS = 600
COUNCIL_RESULT_TIMEOUT_SECONDS = DSH_COUNCIL_EXECUTION_CEILING_SECONDS + MULTIMODE_RESULT_GRACE_SECONDS  # 1860s = 31 min
DEBATE_RESULT_TIMEOUT_SECONDS = DSH_DEBATE_EXECUTION_CEILING_SECONDS + MULTIMODE_RESULT_GRACE_SECONDS   # 5100s = 85 min


def result_timeout_seconds_for(runtime_class: str) -> int:
    """The one function every v2 call site uses to pick a collector
    deadline from `payload.runtime_class` ("normal"|"long") -- never an
    inline literal at the call site, so the two named bounds above stay
    the single source of truth."""
    return LONG_RESULT_TIMEOUT_SECONDS if runtime_class == "long" else NORMAL_RESULT_TIMEOUT_SECONDS


def result_timeout_seconds_for_v3(mode: str, *, runtime_class: str = "normal") -> int:
    """P18-W5: the one function every v3 call site uses to pick a collector
    deadline. SINGLE delegates to the existing runtime-class rule
    (normal/long) so v3 SINGLE never regresses v2's bound; COUNCIL/DEBATE
    use their own mode-aware ceilings above. OBSERVATION patience only --
    it never cancels a still-running DSH task (a relay TIMEOUT settles the
    GitHub result as TIMEOUT without asserting the DSH task failed;
    post-timeout late settlement, relay/late_settlement.py, still applies)."""
    if mode == "council":
        return COUNCIL_RESULT_TIMEOUT_SECONDS
    if mode == "debate":
        return DEBATE_RESULT_TIMEOUT_SECONDS
    return result_timeout_seconds_for(runtime_class)

# P18-W4R4: typed terminal-LATE states. Reachable ONLY via settle_late(),
# and ONLY from a prior TIMEOUT -- never a substitute for or a variant of
# the ordinary COMPLETED/FAILED settle() path. See relay/late_settlement.py.
COMPLETED_LATE = "COMPLETED_LATE"
FAILED_LATE = "FAILED_LATE"
LATE_TERMINAL_STATES = {COMPLETED_LATE, FAILED_LATE}

# Matches renderTerminalResult()'s exact shape. DOTALL so `.*` in the
# result body can span newlines; the (?P=status) backreference requires
# the "Status:" line to literally repeat the status word from the title
# line, which is what the real renderer always does.
#
# P24.1R: the `\n` between `Result:` and the body is OPTIONAL (`\n?`),
# not mandatory. renderTerminalResult() (src/owner/telegram-owner-client.mjs)
# always emits the literal template `...Result:\n${body}${outcomeSuffix}` --
# when a task's own output and outcome suffix are both empty (a legitimate,
# common shape: e.g. a runtime/cancellation qualification task that produces
# no narrative output), that template renders a string that structurally
# ENDS in `Result:\n` with nothing after it. Telegram's own message transport
# strips trailing whitespace (including a lone trailing newline) from message
# text, so the message actually observed via get_history ends in `Result:`
# with NO trailing newline at all. The previous mandatory `\n` made that
# exact -- entirely legitimate, empty-payload -- shape permanently
# unparseable as a terminal candidate: an application-owned parsing gap, not
# a payload requirement. Identity (title/Task/Status) is fully present and
# unambiguous regardless of whether the Result body is empty, and the
# control-plane contract never requires model-authored Result content to
# establish terminal identity.
TERMINAL_RE = re.compile(
    r"^[✅❌⚪] DSH task (?P<status>completed|failed|cancelled)"
    r"\n\nTask: (?P<task_id>.+?)\nPM: .*?\nStatus: (?P=status)\n\nResult:\n?(?P<body>.*)$",
    re.DOTALL,
)


@dataclass
class TerminalCandidate:
    message_id: int
    status: str
    task_id: str


# DSH's OWN task-accepted ACK shape (renderOwnerAck()'s SUBMIT_TASK
# branch, `src/owner/telegram-owner-client.mjs`), v2 dispatch (which
# ALWAYS compiles --client-correlation, contract_v3.py Part D, so the
# Correlation line is always present for a v2-originated ACK):
#
#   ✅ DSH task accepted
#   Project: <project_id>
#   Task: <task_id>
#   PM: <pm_profile_id>
#   Correlation: <client_correlation_id>
#
# The literal `✅ DSH task accepted` marker is the ONE thing that
# distinguishes this from a TERMINAL message (`✅ DSH task completed` /
# `❌ DSH task failed` / `⚪ DSH task cancelled` -- different wording,
# never matched by ACK_ACCEPTED_MARKERS below) -- this is exactly what
# stops a stale, unrelated task's own terminal message (which also has a
# `Task: <id>` line) from ever being misparsed as an acceptance ACK.
# Charset bounds match contract_v3.py's own ID_CHARSET_RE / CORRELATION_ID_RE
# exactly (task_id: DSH's deterministicOwnerId() charset family;
# correlation: the same bounded charset the relay's own correlation_id
# already validates against, and what DSH's --client-correlation flag
# itself now enforces).
#
# P18-W5: DSH renders a DISTINCT first line for a COUNCIL/DEBATE acceptance
# ACK -- `✅ DSH council accepted` (renderOwnerAck()'s council branch,
# src/owner/telegram-owner-client.mjs @ 4c547db). Its `Task:`/`Correlation:`
# lines are the SAME shape as the SINGLE ACK's (P18-W5 multimode
# correlation rendering added them to the council branch: identityLine =
# `\n\nTask: <task_id>` + optional `\nCorrelation: <id>`), so recognizing
# the extra first-line marker is all that is needed -- the Task/Correlation
# regexes below are unchanged. Both markers are ACCEPTANCE-only wording;
# neither ever appears as a terminal message's own title line.
ACK_ACCEPTED_MARKER = "✅ DSH task accepted"          # v1/v2/v3 SINGLE
ACK_COUNCIL_ACCEPTED_MARKER = "✅ DSH council accepted"  # v3 COUNCIL / DEBATE
ACK_ACCEPTED_MARKERS = frozenset({ACK_ACCEPTED_MARKER, ACK_COUNCIL_ACCEPTED_MARKER})
ACK_TASK_ID_RE = re.compile(r"^Task:\s*([A-Za-z0-9][A-Za-z0-9._:-]{0,127})\s*$", re.MULTILINE)
ACK_CORRELATION_RE = re.compile(r"^Correlation:\s*([A-Za-z0-9_.-]{6,128})\s*$", re.MULTILINE)
# P18-W5 ACK cardinality hardening (owner review): loose per-line prefix
# matchers used ONLY to COUNT how many identity-bearing lines a single ACK
# message carries -- so a well-formed `Task:`/`Correlation:` line followed
# by a second (malformed-value) one still fails closed, not just the case
# where both values happen to be charset-valid. `parse_accepted_ack()`
# requires exactly one `Task:` line and at most one `Correlation:` line
# per message; it never deduplicates duplicate lines and never selects the
# first of several.
ACK_TASK_LINE_RE = re.compile(r"^Task:", re.MULTILINE)
ACK_CORRELATION_LINE_RE = re.compile(r"^Correlation:", re.MULTILINE)

# P18-W4 ACK-causal-correlation remediation (Part E/F): DSH's own ACK is
# synchronous and near-instant (SUBMIT_TASK acceptance never waits on a
# real backend call) -- this bounded window only ever matters when the
# synchronous send_message reply was non-authoritative (Part E's fast-path
# rejected it) and the relay must independently confirm the real ACK via
# get_history. Deliberately much shorter than NORMAL/LONG_RESULT_TIMEOUT_
# SECONDS above -- this is acceptance-latency patience, not execution
# patience -- and, critically, is safe to re-run in full on every restart
# (Part F): it is driven entirely by the already-durable `boundary_id` +
# `correlation_id`, no additional state needs to be persisted for it to be
# resumable.
ACK_RECOVERY_TIMEOUT_SECONDS = 60
ACK_RECOVERY_POLL_INTERVAL_SECONDS = 5


@dataclass
class AckCandidate:
    message_id: int
    task_id: str
    correlation_id: str


def parse_accepted_ack(text: str) -> Optional[dict]:
    """Structural parse of ONE message's text as a DSH accepted-ACK.

    P18-W4 FAST-PATH-UNIQUENESS remediation (PM review, strict-ACK-shape
    follow-up): the original version only checked `ACK_ACCEPTED_MARKER in
    text` -- a SUBSTRING check anywhere in the message. A terminal
    message's `Result:` body is free-form (it can echo arbitrary prior
    conversation/model output, per `renderTerminalResult()`), so a
    completed/failed/cancelled terminal whose Result body happened to
    quote the literal string "✅ DSH task accepted" (e.g. echoing a
    log line, a prior ACK, or adversarial-looking model output) could
    satisfy that substring check and be misparsed as an acceptance ACK,
    even though its own message TITLE line is `✅ DSH task completed`/
    `❌ DSH task failed`/`⚪ DSH task cancelled` -- never the accepted
    marker. Fixed: the marker must be the message's exact FIRST line, not
    merely present somewhere in the text -- structurally the only place
    `renderOwnerAck()`'s SUBMIT_TASK acceptance branch ever puts it, and
    structurally never where `renderTerminalResult()` puts its own,
    differently-worded title line. Returns None (never guesses) unless
    that first-line check passes AND a `Task:` line is present --
    `correlation_id` in the returned dict is None when no `Correlation:`
    line exists at all (an ordinary dispatch that never requested one),
    which the caller must treat as "not authoritative for a correlation-
    scoped lookup", never as a wildcard match.

    P18-W5: the accepted first line is now one of ACK_ACCEPTED_MARKERS --
    `✅ DSH task accepted` (SINGLE) OR `✅ DSH council accepted`
    (COUNCIL/DEBATE). Both are acceptance-only wording; a terminal
    message's own title line (`✅ DSH task completed`, `✅ DSH council
    completed`, `⚠️ Council completed with degraded participation`, ...)
    is never in this set, so a stale terminal still can't be misparsed as
    an ACK.

    P18-W5 ACK cardinality hardening (owner review): an authoritative ACK
    must NEVER silently pick one identity-bearing field when several are
    present in the SAME message. This function now requires EXACTLY one
    `Task:` line and AT MOST one `Correlation:` line:

      * 0 `Task:` lines                      -> None
      * >1 `Task:` lines (identical OR not)  -> None (no dedup, no "first")
      * exactly 1 `Task:` line, valid value  -> ok
      * exactly 1 `Task:` line, bad value    -> None
      * 0 `Correlation:` lines               -> correlation_id = None
                                               (ordinary non-correlated ACK)
      * exactly 1 `Correlation:` line, valid -> that value
      * exactly 1 `Correlation:` line, bad   -> None
      * >1 `Correlation:` lines (id. OR not) -> None (no dedup, no "first")

    This concerns malformed cardinality INSIDE one Telegram message only.
    It does NOT change history-level semantics: multiple SEPARATE ACK
    messages agreeing on one task_id still dedup through classify_ack(),
    and multiple messages sharing a correlation but disagreeing on task_id
    still fail closed AMBIGUOUS. v2/v3 correlated recovery still requires
    the exact expected `Correlation:` value at the call site."""
    first_line = text.split("\n", 1)[0]
    if first_line not in ACK_ACCEPTED_MARKERS:
        return None
    # Exactly one Task: line, and its value must be well-formed.
    if len(ACK_TASK_LINE_RE.findall(text)) != 1:
        return None
    task_matches = ACK_TASK_ID_RE.findall(text)
    if len(task_matches) != 1:
        return None
    # Zero or one Correlation: line. More than one -> ambiguous identity,
    # fail closed. Exactly one whose value is malformed -> also fail closed
    # (never fall back to "no correlation" for a message that clearly tried
    # to carry one).
    corr_line_count = len(ACK_CORRELATION_LINE_RE.findall(text))
    if corr_line_count > 1:
        return None
    corr_matches = ACK_CORRELATION_RE.findall(text)
    if corr_line_count == 1 and len(corr_matches) != 1:
        return None
    return {"task_id": task_matches[0], "correlation_id": corr_matches[0] if corr_matches else None}


def extract_task_id_from_ack(send_payload: dict, expected_correlation_id: str) -> Optional[str]:
    """FAST PATH ONLY (Part E) -- the synchronous send_message reply is
    NOT causally authoritative on its own (see this module's docstring
    addendum): telegram-mcp's real send_message() returns the first bot
    message newer than the sent one, with no reply-linkage/nonce
    validation, so this reply could belong to a completely different,
    concurrently-settling task. This function is therefore authoritative
    ONLY when the reply is BOTH a valid accepted-ACK shape AND carries the
    EXACT expected `Correlation:` value -- any other shape (wrong/missing
    correlation, a terminal message's own wording, no reply observed at
    all) returns None, and the caller MUST fall through to the bounded,
    restart-safe scan_ack_candidates_by_correlation() scan below rather
    than trust this alone."""
    if send_payload.get("status") != "ok":
        return None
    reply = send_payload.get("reply")
    if not isinstance(reply, str):
        return None
    parsed = parse_accepted_ack(reply)
    if not parsed or parsed["correlation_id"] != expected_correlation_id:
        return None
    return parsed["task_id"]


# P18-W4 ACK-causal-correlation remediation (Part E): the AUTHORITATIVE
# recovery path -- bot identity + post-dispatch boundary + valid ACK shape
# + EXACT expected correlation_id. Structurally identical shape to
# scan_terminal_candidates_by_task_id() below (same bot-identity/boundary
# discipline), scanning for the ACCEPTANCE shape instead of the TERMINAL
# shape. A stale/unrelated task's terminal message is never a candidate
# (parse_accepted_ack() rejects it on the marker check alone, PM review
# scenario 1); an unrelated task's OWN accepted ACK is never a candidate
# unless it happens to carry the exact SAME correlation_id, which would
# itself be a genuine relay-side correlation_id collision, not a false
# match (PM review scenario 2/3).
def scan_ack_candidates_by_correlation(history_payload: dict, boundary_id: int,
                                        correlation_id: str, bot_username: str) -> list[AckCandidate]:
    bot_from = f"@{bot_username}"
    out: list[AckCandidate] = []
    for m in history_payload.get("messages") or []:
        if m.get("from") != bot_from:
            continue
        if m.get("id", 0) <= boundary_id:
            continue
        text = m.get("text") or ""
        parsed = parse_accepted_ack(text)
        if not parsed or parsed["correlation_id"] != correlation_id:
            continue
        out.append(AckCandidate(message_id=m["id"], task_id=parsed["task_id"], correlation_id=parsed["correlation_id"]))
    return out


def classify_ack(candidates: list[AckCandidate]) -> tuple[Optional[str], bool, str]:
    """Returns (task_id_or_None, ambiguous, reason). Never guesses:
    zero candidates -> (None, False, "not found yet") -- caller keeps
    polling until its bounded window expires. All candidates agreeing on
    the SAME task_id (a literal duplicate delivery of the identical ACK)
    -> that one task_id, not ambiguous. Candidates disagreeing on task_id
    for the SAME correlation_id (PM review scenario 8 -- a genuine
    correlation_id collision) -> (None, True, ...), fails closed exactly
    like classify() does for terminal candidates."""
    if not candidates:
        return None, False, "no exact correlated ACK found yet"
    task_ids = {c.task_id for c in candidates}
    if len(task_ids) > 1:
        return None, True, f"{len(candidates)} ACKs share correlation_id {candidates[0].correlation_id!r} but disagree on task_id ({sorted(task_ids)}), refusing to guess"
    return candidates[0].task_id, False, f"{len(candidates)} exact correlated ACK match(es)"


def find_boundary_id(history_payload: dict) -> int:
    """Highest message id currently in history, BEFORE dispatch. Any
    message at or below this id is pre-existing and must never be
    considered a candidate for the task we are about to send -- this is
    what makes "old pre-dispatch matching history ignored" hold even if a
    prior, unrelated message happened to contain the same-shaped text."""
    messages = history_payload.get("messages") or []
    ids = [m.get("id", 0) for m in messages]
    return max(ids) if ids else 0


# ---------------------------------------------------------------------------
# P24.1R2: boundary-aware bounded history retrieval.
#
# The installed Telegram MCP `get_history` tool (telegram_mcp.server) has no
# offset/cursor parameter at all -- it is a single `client.get_messages(
# entity, limit=limit)` call, always returning the chat's CURRENT newest
# `limit` messages, never anything anchored to a message id. A single
# fixed-size poll (the pre-P24.1R2 `mcp_get_history(session, limit=20)`) can
# therefore permanently miss a genuine post-boundary terminal message once
# enough newer, unrelated chat traffic has pushed it out of that window --
# no amount of repeated polling at the SAME fixed size can ever recover it,
# because every poll re-asks the same question ("what are the newest 20
# messages right now?") and gets an answer that keeps moving further away
# from the message we need. This is a P18-W3-CANARY incident #67 finding:
# only 21 total chat messages separated two consecutive dispatch boundaries
# across a single 40-minute LONG collector window -- right at the old
# limit=20 ceiling.
#
# Fix: escalate the SAME tool's `limit` parameter through a small, fixed,
# bounded sequence of increasing sizes (HISTORY_ESCALATION_LIMITS), stopping
# as soon as one fetch's own window can be PROVEN to already cover the
# entire post-boundary interval (its oldest returned message id is <=
# boundary_id, or it returned fewer messages than requested -- the whole
# chat's history is exhausted, so there is nothing further back to miss
# either way). This is still, per poll, a small bounded number of single
# get_history calls (never more than len(HISTORY_ESCALATION_LIMITS)), never
# an unbounded walk of the whole chat, and never a true multi-page merge
# (the installed MCP tool has no cursor for this to build on) -- each
# escalation step re-fetches from the newest message, just with a larger
# limit. The escalation decision is made ENTIRELY from message ids (has the
# boundary been reached?), never from whether a terminal candidate was
# found -- a legitimate PENDING task (no terminal has been posted at all
# yet) must never trigger escalation just because nothing matched.
HISTORY_PAGE_SIZE = 20  # unchanged from pre-P24.1R2 -- the ordinary-path cost
HISTORY_ESCALATION_LIMITS = (HISTORY_PAGE_SIZE, 100, 500)
MAX_HISTORY_MESSAGES = HISTORY_ESCALATION_LIMITS[-1]


def history_window_covers_boundary(history_payload: dict, boundary_id: int, requested_limit: int) -> bool:
    """True when this ONE get_history response can be trusted to contain
    every message newer than boundary_id in this chat -- i.e. escalating to
    a larger `limit` could not possibly reveal anything new that matters.

    Two independent ways that can be true:
      * the response's OLDEST message id already reaches back to/through
        boundary_id (nothing between boundary_id and the oldest returned
        message could have been missed); or
      * fewer messages were returned than requested (`limit`) -- the whole
        chat's history is exhausted, so there is no older message at all,
        post-boundary or not.

    A malformed/error payload (no "messages" key, or an empty list because
    the underlying MCP call failed) is treated as "covered" -- retrying
    with a bigger `limit` cannot fix a connectivity/auth-level failure, and
    this preserves the pre-P24.1R2 failure semantics exactly (an error/
    empty payload already yielded zero candidates and PENDING, unchanged)."""
    messages = history_payload.get("messages") or []
    if len(messages) < requested_limit:
        return True
    oldest_id = min(m.get("id", 0) for m in messages)
    return oldest_id <= boundary_id


def scan_terminal_candidates(history_payload: dict, boundary_id: int,
                              correlation_marker: str, bot_username: str) -> list[TerminalCandidate]:
    bot_from = f"@{bot_username}"
    out: list[TerminalCandidate] = []
    for m in history_payload.get("messages") or []:
        if m.get("from") != bot_from:
            continue
        if m.get("id", 0) <= boundary_id:
            continue
        text = m.get("text") or ""
        if correlation_marker not in text:
            continue
        match = TERMINAL_RE.match(text)
        if not match:
            continue  # marker present but not terminal-shaped (e.g. it's the ACK) -- not a candidate
        out.append(TerminalCandidate(
            message_id=m["id"], status=match.group("status"), task_id=match.group("task_id"),
        ))
    return out


# P18-W4 Part E: the v2 terminal scan -- by bot identity + post-dispatch
# boundary + exact `Task: <task_id>` + valid terminal message shape.
# Deliberately does NOT require any correlation marker anywhere in the
# message body: a FAILED/CANCELLED terminal (whose body is always the
# generic error reason, never model output -- renderTerminalResult()'s own
# `body` selection) is scanned and matched identically to a COMPLETED one,
# closing exactly the gap issue #8 exposed. `classify()` below is REUSED
# unchanged -- it already reduces 0/1/many candidates to PENDING/
# COMPLETED/FAILED/AMBIGUOUS with no dependency on how the candidate list
# was produced.
def scan_terminal_candidates_by_task_id(history_payload: dict, boundary_id: int,
                                         task_id: str, bot_username: str) -> list[TerminalCandidate]:
    bot_from = f"@{bot_username}"
    out: list[TerminalCandidate] = []
    for m in history_payload.get("messages") or []:
        if m.get("from") != bot_from:
            continue
        if m.get("id", 0) <= boundary_id:
            continue
        text = m.get("text") or ""
        match = TERMINAL_RE.match(text)
        if not match:
            continue  # not a valid terminal message shape at all (e.g. it's an ACK) -- not a candidate
        if match.group("task_id") != task_id:
            continue  # a real terminal message, but for a DIFFERENT task -- ignored, never a false match
        out.append(TerminalCandidate(
            message_id=m["id"], status=match.group("status"), task_id=match.group("task_id"),
        ))
    return out


# ---------------------------------------------------------------------------
# P18-W5: COUNCIL / DEBATE terminal recognition.
#
# renderTerminalResult() (src/owner/telegram-owner-client.mjs @ 4c547db)
# emits a COUNCIL/DEBATE-specific shape ONLY for a `status === 'completed'`
# council/debate run. Its exact first lines are:
#
#   ✅ DSH council completed
#   ✅ DSH council/debate completed
#   ⚠️ Council completed with degraded participation
#   ⚠️ Council/Debate completed with degraded participation
#
# followed by a small, bounded HEADER block ending in a `Task: <task_id>`
# line (P18-W5 multimode correlation rendering's `taskIdSuffix`), then a
# blank line, then `Result:` (non-degraded) or `Failed:`/`Completed:`
# blocks (degraded). Example:
#
#   ✅ DSH council completed
#   Project: proj-a
#   Chair: chair-x
#   Council: p1, p2, p3
#   Rounds: 2
#   Task: task-council-01
#
#   Result:
#   <chair synthesis>
#
# A council/debate FAILED or CANCELLED run does NOT use this shape -- it
# falls through to the generic `<icon> DSH task <status>` shape already
# matched by TERMINAL_RE / scan_terminal_candidates_by_task_id() above
# (verified: tests/p18-w5-multimode-correlation-rendering.test.mjs cases
# 8 and 10). So v3 terminal collection is: the generic TERMINAL_RE scan
# (covers SINGLE completed/failed/cancelled AND council/debate failed/
# cancelled) UNION this council-completed structured scan.
#
# Structured parse (not a mega-regex): first line must be exactly one of
# the four markers; the `Task:` line is looked for ONLY inside the bounded
# HEADER (the lines from index 1 up to -- not including -- the first blank
# line), so a free-form `Result:` body that happens to contain the text
# `Task: <something>` can never contaminate identity. A valid council-
# completed shape MUST have a `Chair:` header line and exactly one header
# `Task:` line, else it is treated as non-terminal (fail closed -- never a
# guessed task_id). DSH always threads item.task_id through for council/
# debate (per the W5 test file's own note), so a real council-completed
# terminal always has exactly one such line.
# ---------------------------------------------------------------------------

COUNCIL_TERMINAL_MARKERS = frozenset({
    "✅ DSH council completed",
    "✅ DSH council/debate completed",
    "⚠️ Council completed with degraded participation",
    "⚠️ Council/Debate completed with degraded participation",
})
_COUNCIL_HEADER_TASK_RE = re.compile(r"^Task: ([A-Za-z0-9][A-Za-z0-9._:-]{0,127})$")
_COUNCIL_HEADER_CHAIR_RE = re.compile(r"^Chair: .+$")


def parse_council_terminal_message(text: str) -> Optional[dict]:
    """Structural parse of ONE message as a DSH COUNCIL/DEBATE *completed*
    terminal. Returns {"task_id": <str>, "terminal_status": "completed"} or
    None (never guesses). Degraded completion (`⚠️ Council ... degraded
    participation`) is still a real completion server-side
    (status === 'completed'), so it maps to terminal_status "completed"."""
    if (text or "").split("\n", 1)[0] not in COUNCIL_TERMINAL_MARKERS:
        return None
    lines = text.split("\n")
    # Bounded header = lines[1:] up to the first blank line.
    header: list[str] = []
    for ln in lines[1:]:
        if ln == "":
            break
        header.append(ln)
    if not any(_COUNCIL_HEADER_CHAIR_RE.match(h) for h in header):
        return None  # missing the structural Chair anchor -- not a real council terminal
    task_ids = [m.group(1) for m in (_COUNCIL_HEADER_TASK_RE.match(h) for h in header) if m]
    if len(task_ids) != 1:
        # 0 -> renderer had no durable task_id (structurally impossible for a
        # real council-completed terminal); >1 -> structurally impossible.
        # Either way: fail closed, do not settle from this message.
        return None
    return {"task_id": task_ids[0], "terminal_status": "completed"}


def scan_terminal_candidates_by_task_id_multimode(history_payload: dict, boundary_id: int,
                                                    task_id: str, bot_username: str) -> list[TerminalCandidate]:
    """P18-W5: the v3 terminal scan for ALL THREE modes. Union of:
      1. the generic scan_terminal_candidates_by_task_id() (SINGLE
         completed/failed/cancelled + council/debate failed/cancelled), and
      2. the COUNCIL/DEBATE *completed* structured parse above.
    Both branches bind ONLY by exact recovered `task_id` (never by
    correlation text -- correlation is ACK binding, task_id is terminal
    binding). `classify()` is reused unchanged: 0 -> PENDING, 1 ->
    COMPLETED/FAILED, >1 incompatible -> AMBIGUOUS (fail closed). An
    intermediate/ACK/liveness/AWAIT_OWNER message matches neither branch
    and can never settle the task."""
    out: list[TerminalCandidate] = list(
        scan_terminal_candidates_by_task_id(history_payload, boundary_id, task_id, bot_username)
    )
    bot_from = f"@{bot_username}"
    for m in history_payload.get("messages") or []:
        if m.get("from") != bot_from:
            continue
        if m.get("id", 0) <= boundary_id:
            continue
        parsed = parse_council_terminal_message(m.get("text") or "")
        if not parsed:
            continue
        if parsed["task_id"] != task_id:
            continue  # a real council terminal, but for a DIFFERENT task -- ignored
        out.append(TerminalCandidate(
            message_id=m["id"], status=parsed["terminal_status"], task_id=parsed["task_id"],
        ))
    return out


def classify(candidates: list[TerminalCandidate]) -> tuple[str, Optional[TerminalCandidate], str]:
    """Returns (result_state, candidate_or_None, reason)."""
    if not candidates:
        return PENDING, None, "no terminal candidate yet"
    if len(candidates) > 1:
        return AMBIGUOUS, None, f"{len(candidates)} plausible terminal candidates, refusing to guess"
    c = candidates[0]
    if not c.task_id or c.task_id.strip().lower() == "unknown":
        return AMBIGUOUS, None, f"terminal-shaped match has missing/malformed task_id ({c.task_id!r})"
    if c.status == "completed":
        return COMPLETED, c, "exact single completed terminal match"
    if c.status in ("failed", "cancelled"):
        return FAILED, c, f"exact single {c.status} terminal match"
    return AMBIGUOUS, None, f"unrecognized terminal status {c.status!r}"  # unreachable given TERMINAL_RE


class ResultStateStore:
    """Durable, restart-safe result-correlation state, keyed the same way
    as relay.state_machine.DispatchStateStore (repo+issue+correlation_id).
    A restart resumes polling against the SAME stored deadline/boundary
    rather than creating a fresh one -- polling time is bounded and
    deterministic across restarts, and a terminal settlement is written
    exactly once."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _key(self, repo: str, issue: int, correlation_id: str) -> str:
        return f"{repo}#{issue}#{correlation_id}"

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {}

    def _save(self, state: dict) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, repo: str, issue: int, correlation_id: str) -> Optional[dict]:
        return self._load().get(self._key(repo, issue, correlation_id))

    def start_or_resume(self, repo: str, issue: int, correlation_id: str,
                         boundary_id: int, timeout_seconds: int) -> dict:
        """Idempotent: if an entry already exists (any status, including a
        prior PENDING from a crashed run), returns it UNCHANGED rather than
        resetting boundary_id/deadline -- a restart must not extend the
        polling window or move the boundary."""
        with self._lock:
            state = self._load()
            key = self._key(repo, issue, correlation_id)
            existing = state.get(key)
            if existing is not None:
                return existing
            now = datetime.now(timezone.utc)
            entry = {
                "status": PENDING,
                "boundary_id": boundary_id,
                "deadline": (now + timedelta(seconds=timeout_seconds)).isoformat(),
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "task_id": None,
                "terminal_status": None,
                "terminal_message_id": None,
                "comment_posted": False,
            }
            state[key] = entry
            self._save(state)
            return entry

    def settle(self, repo: str, issue: int, correlation_id: str, status: str, *,
               task_id: Optional[str] = None, terminal_status: Optional[str] = None,
               terminal_message_id: Optional[int] = None, reason: str = "") -> dict:
        """Move PENDING -> a terminal state exactly once. If already
        terminal, returns the existing entry unchanged (idempotent) so a
        rerun never re-settles or re-derives a different outcome."""
        with self._lock:
            state = self._load()
            key = self._key(repo, issue, correlation_id)
            entry = state.get(key)
            if entry is None:
                raise ValueError(f"{key}: settle() called with no prior start_or_resume()")
            if entry["status"] in TERMINAL_STATES:
                return entry  # already settled -- idempotent no-op
            entry["status"] = status
            entry["task_id"] = task_id
            entry["terminal_status"] = terminal_status
            entry["terminal_message_id"] = terminal_message_id
            entry["reason"] = reason
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            state[key] = entry
            self._save(state)
            return entry

    def mark_comment_posted(self, repo: str, issue: int, correlation_id: str) -> None:
        with self._lock:
            state = self._load()
            key = self._key(repo, issue, correlation_id)
            entry = state.get(key)
            if entry is None:
                return
            entry["comment_posted"] = True
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            state[key] = entry
            self._save(state)

    # -----------------------------------------------------------------
    # P18-W4R4: post-timeout terminal settlement
    # -----------------------------------------------------------------

    def settle_late(self, repo: str, issue: int, correlation_id: str, late_status: str, *,
                     task_id: str, terminal_status: str, terminal_message_id: int,
                     reason: str = "") -> dict:
        """Move a settled TIMEOUT entry to a typed terminal-LATE state
        (COMPLETED_LATE / FAILED_LATE) exactly once, when the SAME
        correlated DSH task later reaches a real terminal state that the
        original bounded collector's process was no longer running to see.

        Idempotent: if the entry is already in a LATE state, returns it
        UNCHANGED -- a rerun never re-settles or overwrites an already
        -recorded task_id/terminal_status/terminal_message_id with a
        different later finding.

        Fail-closed: refuses any starting status other than TIMEOUT.
        COMPLETED/FAILED/AMBIGUOUS/PENDING are never touched by this
        method -- late settlement exists to correct exactly one gap (a
        collector process that exited at TIMEOUT while the task kept
        running), never to re-derive or override an outcome the ordinary
        settle() path already recorded.
        """
        if late_status not in LATE_TERMINAL_STATES:
            raise ValueError(f"settle_late(): late_status must be one of {LATE_TERMINAL_STATES}, got {late_status!r}")
        with self._lock:
            state = self._load()
            key = self._key(repo, issue, correlation_id)
            entry = state.get(key)
            if entry is None:
                raise ValueError(f"{key}: settle_late() called with no prior start_or_resume()/settle()")
            if entry["status"] in LATE_TERMINAL_STATES:
                return entry  # already late-settled -- idempotent no-op
            if entry["status"] != TIMEOUT:
                raise ValueError(
                    f"{key}: settle_late() requires prior status={TIMEOUT!r}, found "
                    f"{entry['status']!r} -- late settlement only ever corrects a TIMEOUT"
                )
            entry["status"] = late_status
            entry["task_id"] = task_id
            entry["terminal_status"] = terminal_status
            entry["terminal_message_id"] = terminal_message_id
            entry["reason"] = reason
            entry["late_settled_at"] = datetime.now(timezone.utc).isoformat()
            entry["late_comment_posted"] = False
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            state[key] = entry
            self._save(state)
            return entry

    def mark_late_comment_posted(self, repo: str, issue: int, correlation_id: str) -> None:
        """Independent of mark_comment_posted()/`comment_posted` -- the
        original TIMEOUT comment's posted-flag is never touched, so it
        stays exactly as historical evidence while this separate flag
        gates the one later settlement comment."""
        with self._lock:
            state = self._load()
            key = self._key(repo, issue, correlation_id)
            entry = state.get(key)
            if entry is None:
                return
            entry["late_comment_posted"] = True
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            state[key] = entry
            self._save(state)
