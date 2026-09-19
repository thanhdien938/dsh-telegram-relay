"""
Qualification and security verification tests for DSH Telegram Relay.

Validates:
- Configured allowed GitHub users accepted.
- Unknown/unauthorized GitHub users rejected.
- Missing owner configuration fails closed.
- Wildcard ('*') owner configuration rejected.
- Configured Telegram target is used.
- Issue body cannot override Telegram target or inject unknown fields.
- Mode invariants (SINGLE, COUNCIL, DEBATE) compile accurately.
- workspace_output forwarding behavior and restrictions held.
- Issue identity payload mutation guard fails closed on post-acceptance edits.
- Duplicate dispatch remains idempotent and prevents duplicate sends.
"""
import os
import tempfile
import pytest
from pathlib import Path

from contract_v3 import (
    parse_and_validate,
    compile_telegram_command,
    ContractError,
    UNAUTHORIZED_AUTHOR,
    UNKNOWN_FIELD,
    INVALID_SCHEMA,
    WORKSPACE_OUTPUT_SINGLE_ONLY,
    WORKSPACE_OUTPUT_REQUIRES_GIT_COMMIT,
)
from issue_identity import IssueIdentityStore, NEW, RESUME, PAYLOAD_MUTATED
from state_machine import DispatchStateStore, TransitionDenied, RESERVED, COMPLETED, SENT
from dispatch_v3 import _get_allowed_authors, _get_pinned_bot


VALID_SINGLE_BODY = """\
schema: p18-dsh-dispatch/v3
mode: single
correlation_id: gen_test_single_001
project_id: demo-project
pm_profile_id: pm-lead
runtime_class: normal
durability: direct
git:
  commit: false
  push: false
  remote: null
review:
  requested: false
task: |
  Run generalization test.
"""

VALID_COUNCIL_BODY = """\
schema: p18-dsh-dispatch/v3
mode: council
correlation_id: gen_test_council_001
project_id: demo-project
pm_profile_id: pm-chair
participants:
  - pm-p1
  - pm-p2
durability: direct
git:
  commit: false
  push: false
  remote: null
review:
  requested: false
task: |
  Council test task.
"""

VALID_DEBATE_BODY = """\
schema: p18-dsh-dispatch/v3
mode: debate
correlation_id: gen_test_debate_001
project_id: demo-project
pm_profile_id: pm-chair
participants:
  - pm-p1
  - pm-p2
debate_rounds: 2
implementation_profile_id: pm-p1
durability: direct
git:
  commit: false
  push: false
  remote: null
review:
  requested: false
task: |
  Debate test task.
"""


def test_configured_allowed_github_user_accepted():
    allowed = {"alice", "bob"}
    parsed = parse_and_validate(
        "[DSH-TASK] test task",
        VALID_SINGLE_BODY,
        required_title_prefix=("[DSH-TASK]", "[P18-W3-CANARY]"),
        author="alice",
        expected_author=allowed,
    )
    assert parsed.correlation_id == "gen_test_single_001"
    assert parsed.project_id == "demo-project"


def test_unknown_github_user_rejected():
    allowed = {"alice", "bob"}
    with pytest.raises(ContractError) as excinfo:
        parse_and_validate(
            "[DSH-TASK] test task",
            VALID_SINGLE_BODY,
            required_title_prefix="[DSH-TASK]",
            author="mallory",
            expected_author=allowed,
        )
    assert excinfo.value.code == UNAUTHORIZED_AUTHOR


def test_missing_owner_config_fails_closed():
    # Empty set, empty string, or None must fail closed
    for empty_val in [set(), "", None, []]:
        with pytest.raises(ContractError) as excinfo:
            parse_and_validate(
                "[DSH-TASK] test task",
                VALID_SINGLE_BODY,
                required_title_prefix="[DSH-TASK]",
                author="alice",
                expected_author=empty_val,
            )
        assert excinfo.value.code == UNAUTHORIZED_AUTHOR


def test_wildcard_owner_config_rejected():
    for wildcard_val in ["*", {"*"}, ["*"], {"alice", "*"}]:
        with pytest.raises(ContractError) as excinfo:
            parse_and_validate(
                "[DSH-TASK] test task",
                VALID_SINGLE_BODY,
                required_title_prefix="[DSH-TASK]",
                author="alice",
                expected_author=wildcard_val,
            )
        assert excinfo.value.code == UNAUTHORIZED_AUTHOR


def test_get_allowed_authors_from_env(monkeypatch):
    # JSON array canonical format
    monkeypatch.setenv("DSH_RELAY_ALLOWED_GITHUB_USERS_JSON", '["alice", "bob"]')
    users = _get_allowed_authors()
    assert users == {"alice", "bob"}

    # Invalid JSON fails closed (returns empty set)
    monkeypatch.setenv("DSH_RELAY_ALLOWED_GITHUB_USERS_JSON", '["unclosed_json')
    assert _get_allowed_authors() == set()

    # Comma-separated fallback
    monkeypatch.delenv("DSH_RELAY_ALLOWED_GITHUB_USERS_JSON", raising=False)
    monkeypatch.setenv("DSH_RELAY_ALLOWED_GITHUB_USERS", "alice, bob, charlie ")
    users = _get_allowed_authors()
    assert users == {"alice", "bob", "charlie"}

    monkeypatch.setenv("DSH_RELAY_ALLOWED_GITHUB_USERS", "")
    assert _get_allowed_authors() == set()


def test_exact_author_matching_and_substring_rejection(monkeypatch):
    monkeypatch.setenv("DSH_RELAY_ALLOWED_GITHUB_USERS_JSON", '["alice", "bob"]')
    allowed = _get_allowed_authors()

    # Exact author matches
    p_alice = parse_and_validate("[DSH-TASK] test", VALID_SINGLE_BODY, required_title_prefix="[DSH-TASK]", author="alice", expected_author=allowed)
    assert p_alice.correlation_id == "gen_test_single_001"

    p_bob = parse_and_validate("[DSH-TASK] test", VALID_SINGLE_BODY, required_title_prefix="[DSH-TASK]", author="bob", expected_author=allowed)
    assert p_bob.correlation_id == "gen_test_single_001"

    # Substring authors MUST be rejected
    for sub in ["ali", "al", "alice2", "ice", "bo", "b", "bob2"]:
        with pytest.raises(ContractError) as excinfo:
            parse_and_validate("[DSH-TASK] test", VALID_SINGLE_BODY, required_title_prefix="[DSH-TASK]", author=sub, expected_author=allowed)
        assert excinfo.value.code == UNAUTHORIZED_AUTHOR


def test_deployment_template_workflow_static_contract():
    wf_path = Path(__file__).resolve().parent.parent / "templates" / "workflows" / "relay-v3.yml"
    assert wf_path.exists(), "Deployment workflow template must exist"
    content = wf_path.read_text(encoding="utf-8")

    # Prove substring match was removed and exact fromJson match is present
    assert "contains(vars.DSH_RELAY_ALLOWED_GITHUB_USERS," not in content
    assert "contains(fromJson(vars.DSH_RELAY_ALLOWED_GITHUB_USERS_JSON), github.event.issue.user.login)" in content
    assert "!contains(fromJson(vars.DSH_RELAY_ALLOWED_GITHUB_USERS_JSON), '*')" in content

    # Prove runner label is configurable
    assert "${{ vars.DSH_RELAY_RUNNER_LABEL || 'dsh-relay' }}" in content

    # Prove Windows baseline is explicit
    assert "windows" in content


def test_configured_telegram_target_used(monkeypatch):
    monkeypatch.setenv("DSH_RELAY_TELEGRAM_TARGET", "@MyPrivateDshBot")
    assert _get_pinned_bot() == "MyPrivateDshBot"

    monkeypatch.setenv("DSH_RELAY_TELEGRAM_TARGET", "DirectBotName")
    assert _get_pinned_bot() == "DirectBotName"


def test_issue_supplied_destination_override_rejected():
    tampered = VALID_SINGLE_BODY + "destination: '@malicious_bot'\n"
    with pytest.raises(ContractError) as excinfo:
        parse_and_validate(
            "[DSH-TASK] test",
            tampered,
            required_title_prefix="[DSH-TASK]",
            author="alice",
            expected_author={"alice"},
        )
    assert excinfo.value.code == UNKNOWN_FIELD


def test_single_behavior_compiled_accurately():
    parsed = parse_and_validate(
        "[DSH-TASK] test single",
        VALID_SINGLE_BODY,
        required_title_prefix="[DSH-TASK]",
        author="alice",
        expected_author={"alice"},
    )
    cmd = compile_telegram_command(parsed)
    assert cmd.startswith("@demo-project --pm pm-lead --durability direct ")
    assert "--client-correlation gen_test_single_001" in cmd
    assert "Run generalization test." in cmd


def test_council_behavior_compiled_accurately():
    parsed = parse_and_validate(
        "[DSH-TASK] test council",
        VALID_COUNCIL_BODY,
        required_title_prefix="[DSH-TASK]",
        author="alice",
        expected_author={"alice"},
    )
    cmd = compile_telegram_command(parsed)
    assert "@demo-project --pm pm-chair --debate pm-p1,pm-p2 --durability direct " in cmd
    assert "--client-correlation gen_test_council_001" in cmd


def test_debate_behavior_compiled_accurately():
    parsed = parse_and_validate(
        "[DSH-TASK] test debate",
        VALID_DEBATE_BODY,
        required_title_prefix="[DSH-TASK]",
        author="alice",
        expected_author={"alice"},
    )
    cmd = compile_telegram_command(parsed)
    assert "@demo-project --pm pm-chair --debate pm-p1,pm-p2 --debate-extend --debate-rounds 2 --implementation pm-p1" in cmd
    assert "--client-correlation gen_test_debate_001" in cmd


def test_workspace_output_forwarding_in_single():
    body_with_wo = VALID_SINGLE_BODY.replace(
        "commit: false\n  push: false",
        "commit: true\n  push: false",
    ) + "\nworkspace_output:\n  report_path: reports/summary.md\n  non_empty: true\n"

    parsed = parse_and_validate(
        "[DSH-TASK] test wo",
        body_with_wo,
        required_title_prefix="[DSH-TASK]",
        author="alice",
        expected_author={"alice"},
    )
    assert parsed.workspace_output_report_path == "reports/summary.md"
    assert parsed.workspace_output_non_empty is True

    cmd = compile_telegram_command(parsed)
    assert "--report-path reports/summary.md --report-non-empty true" in cmd


def test_workspace_output_requires_commit():
    body_wo_no_commit = VALID_SINGLE_BODY + "\nworkspace_output:\n  report_path: reports/summary.md\n"
    with pytest.raises(ContractError) as excinfo:
        parse_and_validate(
            "[DSH-TASK] test wo no commit",
            body_wo_no_commit,
            required_title_prefix="[DSH-TASK]",
            author="alice",
            expected_author={"alice"},
        )
    assert excinfo.value.code == WORKSPACE_OUTPUT_REQUIRES_GIT_COMMIT


def test_workspace_output_rejected_for_council():
    body_council_wo = VALID_COUNCIL_BODY + "\nworkspace_output:\n  report_path: reports/summary.md\n"
    with pytest.raises(ContractError) as excinfo:
        parse_and_validate(
            "[DSH-TASK] test council wo",
            body_council_wo,
            required_title_prefix="[DSH-TASK]",
            author="alice",
            expected_author={"alice"},
        )
    assert excinfo.value.code == WORKSPACE_OUTPUT_SINGLE_ONLY


def test_payload_mutation_guard_held(tmp_path):
    store = IssueIdentityStore(tmp_path / "issue_identity.json")
    repo = "example-owner/relay-control"
    issue_num = 42
    corr_id = "corr-fixed-001"

    digest1 = "sha256-first-content"
    outcome1, rec1 = store.register_or_verify(repo, issue_num, corr_id, digest1)
    assert outcome1 == NEW

    # Resuming identical payload is accepted
    outcome_resume, rec_resume = store.register_or_verify(repo, issue_num, corr_id, digest1)
    assert outcome_resume == RESUME

    # Modified payload is detected and rejected
    digest_modified = "sha256-attacker-tampered"
    outcome_mutated, rec_mutated = store.register_or_verify(repo, issue_num, corr_id, digest_modified)
    assert outcome_mutated == PAYLOAD_MUTATED


def test_duplicate_dispatch_protection(tmp_path):
    store = DispatchStateStore(tmp_path / "dispatch_state.json")
    repo = "example-owner/relay-control"
    issue_num = 42
    corr_id = "corr-fixed-002"

    # Initial reservation succeeds
    store.reserve(repo, issue_num, corr_id)
    assert store.get_status(repo, issue_num, corr_id) == RESERVED

    # Double reservation for same correlation fails
    with pytest.raises(TransitionDenied):
        store.reserve(repo, issue_num, corr_id)

    # Transition to SENT and COMPLETED
    store.mark_sent(repo, issue_num, corr_id, {"msg": 1})
    store.mark_completed(repo, issue_num, corr_id, {"result": "ok"})
    assert store.get_status(repo, issue_num, corr_id) == COMPLETED

    # Further attempt to re-reserve completed task is denied
    with pytest.raises(TransitionDenied):
        store.reserve(repo, issue_num, corr_id)
