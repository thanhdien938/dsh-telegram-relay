"""
P18-W1 pre-remediation: durable dispatch state machine.

Fixes P18-W0-STATE-001 -- the W0 guard's `status` field conflated
"confirmed delivered" and "confirmed not delivered" into one terminal
value, which forced a manual human-authored override note to retry a
pre-delivery failure. W1 automation cannot have a human in that loop, so
the guard must be able to tell these apart on its own and fail closed
only on genuine ambiguity.

States (exactly the five required):

  RESERVED          -- reservation made; outcome not yet known. Fresh
                        RESERVED entries block a concurrent/duplicate
                        attempt. A RESERVED entry older than
                        STALE_RESERVATION_SECONDS is treated as an
                        unrecoverable crash boundary and demoted to
                        FAILED_TERMINAL rather than left stuck or
                        silently retried.
  SENT              -- the outbound Telegram send is CONFIRMED to have
                        reached Telegram (send_message returned status
                        "ok" or "timeout" -- both only occur after
                        Telethon's own send call already returned
                        successfully). Never safe to resend from here.
  COMPLETED         -- SENT plus evidence (get_history + GitHub comment)
                        successfully recorded. Terminal, success.
  FAILED_RETRYABLE  -- CONFIRMED pre-delivery failure: the error is
                        recognized as coming from a stage that runs
                        strictly before Telethon's send call (missing
                        config, invalid/expired session, bot entity not
                        found). Safe to retry automatically.
  FAILED_TERMINAL   -- an error occurred but its position relative to the
                        actual Telegram send call cannot be confidently
                        classified (unrecognized exception shape, or a
                        stale/orphaned RESERVED left by a process that
                        died mid-flight). Never auto-retried; requires
                        explicit human review (matching W0's
                        AMBIGUOUS_SEND_STATE fail-closed rule).

Transition table:

  (none)            --reserve()-->            RESERVED
  RESERVED          --mark_sent()-->           SENT
  RESERVED          --mark_failed_retryable()--> FAILED_RETRYABLE
  RESERVED          --mark_failed_terminal()-->  FAILED_TERMINAL
  SENT              --mark_completed()-->      COMPLETED
  FAILED_RETRYABLE  --reserve()-->              RESERVED  (auto-retry, archived)
  FAILED_TERMINAL   --reserve()-->              DENY (requires manual override)
  COMPLETED / SENT  --reserve()-->              DENY (already delivered / in flight)
  RESERVED (fresh)  --reserve()-->              DENY (concurrent/replay-before-send)
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

RESERVED = "RESERVED"
SENT = "SENT"
COMPLETED = "COMPLETED"
FAILED_RETRYABLE = "FAILED_RETRYABLE"
FAILED_TERMINAL = "FAILED_TERMINAL"

TERMINAL_STATES = {COMPLETED, FAILED_TERMINAL}
STALE_RESERVATION_SECONDS = 120  # generous vs. the 45s send_message timeout

# Exception/error-text signatures that are ONLY ever raised by
# telegram-mcp's server.py strictly before Telethon's send_message call
# returns (see _check_config / _get_client / get_entity in server.py).
# Matching one of these is what lets a failure be classified
# FAILED_RETRYABLE instead of the conservative FAILED_TERMINAL.
_PRE_SEND_ERROR_SIGNATURES = [
    r"Missing TELEGRAM_API_ID or TELEGRAM_API_HASH",
    r"Session file not found",
    r"Telegram session expired",
    r"Cannot find any entity",
    r"No user has",  # Telethon "no user has \"X\" as username"
    r"Nobody is using this username",
]


class TransitionDenied(Exception):
    """Raised when a transition is refused; .code is a stable reason tag."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def classify_send_error(error_text: str) -> str:
    """Return FAILED_RETRYABLE if error_text matches a known pre-send-only
    signature, else FAILED_TERMINAL (ambiguous -- ability to have occurred
    during/after the actual Telethon send call cannot be ruled out)."""
    for pattern in _PRE_SEND_ERROR_SIGNATURES:
        if re.search(pattern, error_text or "", re.IGNORECASE):
            return FAILED_RETRYABLE
    return FAILED_TERMINAL


def classify_send_result(send_payload: dict) -> str:
    """Classify a telegram-mcp send_message JSON payload.

    status == "ok"      -> delivery confirmed (Telethon's send returned
                            AND a reply was observed)               => SENT
    status == "timeout"  -> delivery confirmed (Telethon's send
                            returned), no reply observed in window   => SENT
    status == "error"    -> classify via error text                 => FAILED_RETRYABLE
                                                                          or FAILED_TERMINAL
    anything else         -> unrecognized shape, fail closed         => FAILED_TERMINAL
    """
    status = send_payload.get("status")
    if status in ("ok", "timeout"):
        return SENT
    if status == "error":
        return classify_send_error(str(send_payload.get("error", "")))
    return FAILED_TERMINAL


@dataclass
class Entry:
    status: str
    updated_at: str
    attempts: list = field(default_factory=list)
    detail: Optional[dict] = None

    def to_json(self) -> dict:
        d = {"status": self.status, "updated_at": self.updated_at, "attempts": self.attempts}
        if self.detail is not None:
            d["detail"] = self.detail
        return d

    @staticmethod
    def from_json(d: dict) -> "Entry":
        return Entry(
            status=d["status"], updated_at=d["updated_at"],
            attempts=d.get("attempts", []), detail=d.get("detail"),
        )


class DispatchStateStore:
    """File-backed, process-lock-guarded state store for repo+issue+correlation_id keys."""

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
        tmp.replace(self.path)  # atomic on the same filesystem

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def get_status(self, repo: str, issue: int, correlation_id: str) -> Optional[str]:
        state = self._load()
        entry = state.get(self._key(repo, issue, correlation_id))
        return entry["status"] if entry else None

    def get_entry(self, repo: str, issue: int, correlation_id: str) -> Optional[dict]:
        """Full entry (status/updated_at/attempts/detail), or None. Lets a
        restarted caller recover a prior attempt's `detail` (e.g. a stored
        pre-dispatch boundary_id) instead of recomputing it -- recomputing
        would silently widen what counts as a valid result-collector
        search window on every restart."""
        state = self._load()
        return state.get(self._key(repo, issue, correlation_id))

    def reserve(self, repo: str, issue: int, correlation_id: str) -> None:
        """Attempt to move (none)|FAILED_RETRYABLE -> RESERVED.

        Raises TransitionDenied for every other case, fail-closed:
          - fresh RESERVED (concurrent/replay-before-send)
          - stale RESERVED (unrecoverable crash boundary -> demoted to
            FAILED_TERMINAL as a side effect, then denied)
          - SENT / COMPLETED (already delivered / in flight to completion)
          - FAILED_TERMINAL (requires explicit human override, not this API)
        """
        with self._lock:
            state = self._load()
            key = self._key(repo, issue, correlation_id)
            existing = state.get(key)

            if existing is None:
                state[key] = Entry(status=RESERVED, updated_at=self._now()).to_json()
                self._save(state)
                return

            entry = Entry.from_json(existing)

            if entry.status == RESERVED:
                reserved_at = datetime.fromisoformat(entry.updated_at)
                age = datetime.now(timezone.utc) - reserved_at
                if age > timedelta(seconds=STALE_RESERVATION_SECONDS):
                    entry.attempts.append({"status": entry.status, "updated_at": entry.updated_at,
                                            "note": "stale RESERVED at replay time -- demoted"})
                    entry.status = FAILED_TERMINAL
                    entry.updated_at = self._now()
                    entry.detail = {"reason": "STALE_RESERVATION_CRASH_BOUNDARY",
                                     "stale_after_seconds": STALE_RESERVATION_SECONDS}
                    state[key] = entry.to_json()
                    self._save(state)
                    raise TransitionDenied(
                        "STALE_RESERVATION_AMBIGUOUS",
                        f"{key}: RESERVED entry older than {STALE_RESERVATION_SECONDS}s with no "
                        "recorded outcome -- cannot rule out delivery, refusing auto-retry "
                        "(demoted to FAILED_TERMINAL, needs manual review)",
                    )
                raise TransitionDenied(
                    "REPLAY_BEFORE_SEND",
                    f"{key}: fresh RESERVED (age={age.total_seconds():.1f}s) -- a send is already "
                    "in flight for this exact key, refusing concurrent/duplicate send",
                )

            if entry.status in (SENT, COMPLETED):
                raise TransitionDenied(
                    "ALREADY_DELIVERED",
                    f"{key}: status={entry.status} -- Telegram delivery already confirmed, "
                    "refusing to send again",
                )

            if entry.status == FAILED_TERMINAL:
                raise TransitionDenied(
                    "TERMINAL_REQUIRES_MANUAL_OVERRIDE",
                    f"{key}: status=FAILED_TERMINAL -- ambiguous prior outcome, refusing "
                    "automatic retry; requires explicit human review",
                )

            if entry.status == FAILED_RETRYABLE:
                entry.attempts.append({"status": entry.status, "updated_at": entry.updated_at,
                                        "detail": entry.detail})
                entry.status = RESERVED
                entry.updated_at = self._now()
                entry.detail = None
                state[key] = entry.to_json()
                self._save(state)
                return

            raise TransitionDenied("UNKNOWN_STATE", f"{key}: unrecognized status={entry.status!r}")

    def mark_sent(self, repo: str, issue: int, correlation_id: str, detail: dict) -> None:
        self._transition(repo, issue, correlation_id, expect=RESERVED, to=SENT, detail=detail)

    def mark_completed(self, repo: str, issue: int, correlation_id: str, detail: dict) -> None:
        self._transition(repo, issue, correlation_id, expect=SENT, to=COMPLETED, detail=detail)

    def mark_failed_retryable(self, repo: str, issue: int, correlation_id: str, detail: dict) -> None:
        self._transition(repo, issue, correlation_id, expect=RESERVED, to=FAILED_RETRYABLE, detail=detail)

    def mark_failed_terminal(self, repo: str, issue: int, correlation_id: str, detail: dict) -> None:
        self._transition(repo, issue, correlation_id, expect=RESERVED, to=FAILED_TERMINAL, detail=detail)

    def _transition(self, repo: str, issue: int, correlation_id: str, *, expect: str, to: str, detail: dict) -> None:
        with self._lock:
            state = self._load()
            key = self._key(repo, issue, correlation_id)
            existing = state.get(key)
            if existing is None or existing["status"] != expect:
                got = existing["status"] if existing else None
                raise TransitionDenied(
                    "BAD_TRANSITION",
                    f"{key}: expected status={expect!r} to move to {to!r}, found {got!r}",
                )
            entry = Entry.from_json(existing)
            entry.status = to
            entry.updated_at = self._now()
            entry.detail = detail
            state[key] = entry.to_json()
            self._save(state)
