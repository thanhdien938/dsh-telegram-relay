"""
P18-W3 immutable issue identity ledger.

Keyed by repo+issue_number ALONE (unlike DispatchStateStore /
ResultStateStore, which are keyed by repo+issue+correlation_id) so that
an edited correlation_id cannot simply open a fresh key and redispatch
under a new identity. The first accepted (repo, issue_number) pairing
permanently owns that issue's correlation_id and payload digest:

  - no record yet                          -> NEW (first acceptance; record it)
  - same correlation_id, same digest       -> RESUME (idempotent replay/rerun)
  - same correlation_id, different digest  -> PAYLOAD_MUTATED (title/task/etc edited)
  - different correlation_id               -> CORRELATION_CHANGED (also fail closed)

Both non-RESUME outcomes must fail closed: never redispatch, never accept
the new content as if it were the original.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

NEW = "NEW"
RESUME = "RESUME"
PAYLOAD_MUTATED = "PAYLOAD_MUTATED"
CORRELATION_CHANGED = "CORRELATION_CHANGED"


@dataclass
class IdentityRecord:
    correlation_id: str
    payload_digest: str
    first_accepted_at: str


class IssueIdentityStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _key(self, repo: str, issue: int) -> str:
        return f"{repo}#{issue}"

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {}

    def _save(self, state: dict) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, repo: str, issue: int) -> Optional[IdentityRecord]:
        entry = self._load().get(self._key(repo, issue))
        return IdentityRecord(**entry) if entry else None

    def register_or_verify(self, repo: str, issue: int, correlation_id: str,
                            payload_digest: str) -> tuple[str, IdentityRecord]:
        with self._lock:
            state = self._load()
            key = self._key(repo, issue)
            existing = state.get(key)
            if existing is None:
                record = IdentityRecord(correlation_id=correlation_id, payload_digest=payload_digest,
                                         first_accepted_at=datetime.now(timezone.utc).isoformat())
                state[key] = record.__dict__
                self._save(state)
                return NEW, record
            record = IdentityRecord(**existing)
            if record.correlation_id != correlation_id:
                return CORRELATION_CHANGED, record
            if record.payload_digest != payload_digest:
                return PAYLOAD_MUTATED, record
            return RESUME, record
