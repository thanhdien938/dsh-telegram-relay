"""
P18-W3 immutable issue identity tests.

Run: .venv\\Scripts\\python.exe relay\\test_issue_identity.py
"""
import os
import sys
import tempfile
from pathlib import Path

from issue_identity import IssueIdentityStore, NEW, RESUME, PAYLOAD_MUTATED, CORRELATION_CHANGED

REPO = "example-owner/relay-control"
ISSUE = 3

failures = []


def check(name, condition):
    if condition:
        print(f"PASS: {name}")
    else:
        print(f"FAIL: {name}")
        failures.append(name)


def fresh_store() -> IssueIdentityStore:
    fd, name = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    tmp = Path(name)
    tmp.unlink()
    return IssueIdentityStore(tmp)


def test_first_acceptance_is_new():
    store = fresh_store()
    outcome, record = store.register_or_verify(REPO, ISSUE, "CORR_A", "digest1")
    check("first-acceptance: NEW", outcome == NEW)
    check("first-acceptance: record stores correlation/digest",
          record.correlation_id == "CORR_A" and record.payload_digest == "digest1")


def test_same_issue_same_payload_resumes():
    store = fresh_store()
    store.register_or_verify(REPO, ISSUE, "CORR_A", "digest1")
    outcome, record = store.register_or_verify(REPO, ISSUE, "CORR_A", "digest1")
    check("same-payload: RESUME (idempotent)", outcome == RESUME)


def test_same_correlation_different_digest_is_payload_mutated():
    store = fresh_store()
    store.register_or_verify(REPO, ISSUE, "CORR_A", "digest1")
    # simulates an owner editing the task text but leaving correlation_id alone
    outcome, record = store.register_or_verify(REPO, ISSUE, "CORR_A", "digest2-EDITED")
    check("payload-mutated: detected", outcome == PAYLOAD_MUTATED)
    check("payload-mutated: original digest preserved, not overwritten", record.payload_digest == "digest1")


def test_different_correlation_id_is_correlation_changed():
    store = fresh_store()
    store.register_or_verify(REPO, ISSUE, "CORR_A", "digest1")
    outcome, record = store.register_or_verify(REPO, ISSUE, "CORR_B_DIFFERENT", "digest1")
    check("correlation-changed: detected", outcome == CORRELATION_CHANGED)
    check("correlation-changed: original correlation_id preserved", record.correlation_id == "CORR_A")


def test_mutation_never_silently_overwrites_stored_identity():
    store = fresh_store()
    store.register_or_verify(REPO, ISSUE, "CORR_A", "digest1")
    store.register_or_verify(REPO, ISSUE, "CORR_A", "digest2-EDITED")  # PAYLOAD_MUTATED, ignored
    store.register_or_verify(REPO, ISSUE, "CORR_C_ANOTHER", "digest3")  # CORRELATION_CHANGED, ignored
    final = store.get(REPO, ISSUE)
    check("no-silent-overwrite: correlation_id still the FIRST one ever accepted",
          final.correlation_id == "CORR_A")
    check("no-silent-overwrite: digest still the FIRST one ever accepted",
          final.payload_digest == "digest1")


def test_different_issues_are_independent():
    store = fresh_store()
    store.register_or_verify(REPO, 10, "CORR_10", "digestA")
    store.register_or_verify(REPO, 11, "CORR_11", "digestB")
    check("different-issues-independent: issue 10 unaffected by issue 11",
          store.get(REPO, 10).correlation_id == "CORR_10")
    check("different-issues-independent: issue 11 unaffected by issue 10",
          store.get(REPO, 11).correlation_id == "CORR_11")


def main():
    test_first_acceptance_is_new()
    test_same_issue_same_payload_resumes()
    test_same_correlation_different_digest_is_payload_mutated()
    test_different_correlation_id_is_correlation_changed()
    test_mutation_never_silently_overwrites_stored_identity()
    test_different_issues_are_independent()

    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)} failing checks)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS (all P18-W3 issue-identity checks passed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
