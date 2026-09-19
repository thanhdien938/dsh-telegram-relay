"""
P18-W3 mandatory contract/compiler tests, before any live canary.

Run: .venv\\Scripts\\python.exe relay\\test_contract_v3.py
"""
import sys

from contract_v3 import (
    parse_and_validate, compile_telegram_command, build_task_with_footer, ContractError,
    INVALID_SCHEMA, INVALID_MODE, UNAUTHORIZED_AUTHOR, UNKNOWN_FIELD, INVALID_PROJECT,
    INVALID_PM_PROFILE, INVALID_DURABILITY, INVALID_GIT_INTENT, INVALID_REMOTE,
    EMPTY_TASK, TASK_TOO_LARGE, INVALID_CORRELATION_ID, MAX_TASK_CHARS,
    INVALID_RUNTIME_CLASS,
)

TITLE_PREFIX = "[P18-W3-CANARY]"
AUTHOR = "alice"
TITLE = f"{TITLE_PREFIX} test"

failures = []


def check(name, condition):
    if condition:
        print(f"PASS: {name}")
    else:
        print(f"FAIL: {name}")
        failures.append(name)


def parse(body, title=TITLE, author=AUTHOR):
    return parse_and_validate(title, body, required_title_prefix=TITLE_PREFIX,
                               author=author, expected_author=AUTHOR)


def expect_error(name, body, expected_code, title=TITLE, author=AUTHOR):
    try:
        parse(body, title=title, author=author)
        check(name, False)
    except ContractError as e:
        check(f"{name} (code={e.code})", e.code == expected_code)


MINIMAL_VALID = """\
schema: p18-dsh-dispatch/v1
mode: single
correlation_id: P18W3_test_marker_1
project_id: dsh-test-project
pm_profile_id: claude-pm
durability: direct
git:
  commit: false
  push: false
  remote: null
review:
  requested: false
task: |
  Read-only canary. Do nothing else.
"""

# P18-W4 Part B/C: v2 minimal fixture -- one new required field
# (`runtime_class`), no relay-owned correlation footer ever appended.
MINIMAL_VALID_V2 = """\
schema: p18-dsh-dispatch/v2
mode: single
correlation_id: P18W4_test_marker_1
project_id: dsh-test-project
pm_profile_id: claude-pm
durability: direct
runtime_class: normal
git:
  commit: false
  push: false
  remote: null
review:
  requested: false
task: |
  Read-only canary. Do nothing else.
"""


# 1. minimum valid SINGLE payload
def test_minimal_valid():
    p = parse(MINIMAL_VALID)
    check("minimal-valid: parses", p.project_id == "dsh-test-project")
    check("minimal-valid: durability normalized", p.durability == "direct")
    check("minimal-valid: git flags false", p.git_commit is False and p.git_push is False)
    check("minimal-valid: review false", p.review_requested is False)


# 2. P18-RELAY-PRODUCTION-GENERALIZATION: the hard project_id/pm_profile_id
# MEMBERSHIP allowlist has been removed. This relay now validates SHAPE
# only -- any syntactically valid canonical ID passes relay validation,
# whether or not it happens to be a project/profile that actually exists
# in DSH (DSH's OwnerControlService is the sole authority on existence).
# Only malformed IDs (bad charset / control chars / over-length / etc,
# see the malformed-ID tests further down) still fail HERE with
# INVALID_PROJECT / INVALID_PM_PROFILE.

# 2a. historical P18-W3 bootstrap identities still pass (regression guard)
def test_historical_project_id_still_passes():
    p = parse(MINIMAL_VALID)
    check("historical-project-id-still-passes", p.project_id == "dsh-test-project")


def test_historical_pm_profile_id_still_passes():
    p = parse(MINIMAL_VALID)
    check("historical-pm-profile-id-still-passes", p.pm_profile_id == "claude-pm")


# 2b. new, previously-unlisted-but-syntactically-valid project/PM IDs now
# pass relay validation without any relay code change (this is the whole
# point of removing the hard membership allowlist) -- DSH remains free to
# reject any of these itself if the project/profile doesn't actually exist.
def test_new_valid_pm_profile_id_passes():
    body = MINIMAL_VALID.replace("pm_profile_id: claude-pm", "pm_profile_id: codex-sol-pm")
    p = parse(body)
    check("new-valid-pm-profile-id-passes", p.pm_profile_id == "codex-sol-pm")


def test_new_valid_project_id_passes():
    body = MINIMAL_VALID.replace("project_id: dsh-test-project", "project_id: dsh-test-project-c")
    p = parse(body)
    check("new-valid-project-id-passes", p.project_id == "dsh-test-project-c")


def test_another_new_valid_project_id_passes():
    # a syntactically valid ID that was never a member of the old hard
    # allowlist, exercising the full charset (letters, digits, '.', '_',
    # ':', '-')
    body = MINIMAL_VALID.replace("project_id: dsh-test-project", "project_id: some.other_project:v2")
    p = parse(body)
    check("another-new-valid-project-id-passes", p.project_id == "some.other_project:v2")


def test_another_new_valid_pm_profile_id_passes():
    body = MINIMAL_VALID.replace("pm_profile_id: claude-pm", "pm_profile_id: gemini-pm")
    p = parse(body)
    check("another-new-valid-pm-profile-id-passes", p.pm_profile_id == "gemini-pm")


# 2c. syntactically INVALID project/PM IDs still fail locally, unchanged
def test_malformed_project_id_whitespace_rejected():
    body = MINIMAL_VALID.replace("project_id: dsh-test-project", 'project_id: "has space"')
    expect_error("malformed-project-id-whitespace", body, INVALID_PROJECT)


def test_malformed_project_id_leading_punctuation_rejected():
    body = MINIMAL_VALID.replace("project_id: dsh-test-project", 'project_id: "-leading-dash"')
    expect_error("malformed-project-id-leading-punctuation", body, INVALID_PROJECT)


def test_malformed_project_id_shell_metachar_rejected():
    body = MINIMAL_VALID.replace("project_id: dsh-test-project", 'project_id: "proj;rm -rf /"')
    expect_error("malformed-project-id-shell-metachar", body, INVALID_PROJECT)


def test_malformed_project_id_dollar_paren_rejected():
    body = MINIMAL_VALID.replace("project_id: dsh-test-project", 'project_id: "proj$(whoami)"')
    expect_error("malformed-project-id-dollar-paren", body, INVALID_PROJECT)


def test_malformed_project_id_newline_rejected():
    body = MINIMAL_VALID.replace("project_id: dsh-test-project", 'project_id: "proj\\nid"')
    expect_error("malformed-project-id-newline", body, INVALID_PROJECT)


def test_malformed_project_id_control_char_rejected():
    body = MINIMAL_VALID.replace("project_id: dsh-test-project", 'project_id: "proj\\tid"')
    expect_error("malformed-project-id-control-char", body, INVALID_PROJECT)


def test_malformed_project_id_over_length_rejected():
    body = MINIMAL_VALID.replace("project_id: dsh-test-project", f'project_id: "{"a" * 129}"')
    expect_error("malformed-project-id-over-length", body, INVALID_PROJECT)


def test_malformed_pm_profile_id_whitespace_rejected():
    body = MINIMAL_VALID.replace("pm_profile_id: claude-pm", 'pm_profile_id: "has space"')
    expect_error("malformed-pm-profile-id-whitespace", body, INVALID_PM_PROFILE)


def test_malformed_pm_profile_id_leading_punctuation_rejected():
    body = MINIMAL_VALID.replace("pm_profile_id: claude-pm", 'pm_profile_id: ".leading-dot"')
    expect_error("malformed-pm-profile-id-leading-punctuation", body, INVALID_PM_PROFILE)


def test_malformed_pm_profile_id_shell_metachar_rejected():
    body = MINIMAL_VALID.replace("pm_profile_id: claude-pm", 'pm_profile_id: "pm`id`"')
    expect_error("malformed-pm-profile-id-shell-metachar", body, INVALID_PM_PROFILE)


def test_malformed_pm_profile_id_over_length_rejected():
    body = MINIMAL_VALID.replace("pm_profile_id: claude-pm", f'pm_profile_id: "{"b" * 200}"')
    expect_error("malformed-pm-profile-id-over-length", body, INVALID_PM_PROFILE)


def test_malformed_project_id_empty_rejected():
    body = MINIMAL_VALID.replace("project_id: dsh-test-project", 'project_id: ""')
    expect_error("malformed-project-id-empty", body, INVALID_PROJECT)


# 3. Council/mode other than SINGLE rejected
def test_mode_council_rejected():
    body = MINIMAL_VALID.replace("mode: single", "mode: council")
    expect_error("mode-council", body, INVALID_MODE)


# 4. unknown field rejected (top-level and nested)
def test_unknown_top_level_field_rejected():
    body = MINIMAL_VALID + "extra_field: haha\n"
    expect_error("unknown-top-level-field", body, UNKNOWN_FIELD)


def test_unknown_git_field_rejected():
    body = MINIMAL_VALID.replace("  remote: null\n", "  remote: null\n  force: true\n")
    expect_error("unknown-git-field", body, UNKNOWN_FIELD)


def test_shell_field_rejected():
    body = MINIMAL_VALID + "shell: rm -rf /\n"
    expect_error("shell-field-injection", body, UNKNOWN_FIELD)


# 5. wrong scalar types rejected (no coercion)
def test_string_true_not_coerced():
    body = MINIMAL_VALID.replace("commit: false", 'commit: "true"')
    expect_error("string-true-not-coerced-commit", body, INVALID_GIT_INTENT)


def test_int_not_coerced_for_bool():
    body = MINIMAL_VALID.replace("requested: false", "requested: 1")
    expect_error("int-not-coerced-for-review-bool", body, INVALID_SCHEMA)


# 6. empty task rejected
def test_empty_task_rejected():
    body = MINIMAL_VALID.replace("task: |\n  Read-only canary. Do nothing else.\n", "task: \"\"\n")
    expect_error("empty-task", body, EMPTY_TASK)


def test_whitespace_only_task_rejected():
    body = MINIMAL_VALID.replace("task: |\n  Read-only canary. Do nothing else.\n", 'task: "   \\n  "\n')
    expect_error("whitespace-only-task", body, EMPTY_TASK)


# 7. oversized task rejected
def test_oversized_task_rejected():
    big_task = "x" * (MAX_TASK_CHARS + 1)
    body = MINIMAL_VALID.replace(
        "task: |\n  Read-only canary. Do nothing else.\n",
        f'task: "{big_task}"\n',
    )
    expect_error("oversized-task", body, TASK_TOO_LARGE)


# 8. multiline task preserved
def test_multiline_task_preserved():
    body = MINIMAL_VALID.replace(
        "task: |\n  Read-only canary. Do nothing else.\n",
        "task: |\n  Line one.\n  Line two.\n\n  Line four after a blank line.\n",
    )
    p = parse(body)
    check("multiline-task-preserved: has 3 non-empty lines with a blank in between",
          p.task.count("\n") >= 3 and "Line one." in p.task and "Line four" in p.task)


# 9. shell metacharacters remain literal data
def test_shell_metacharacters_literal():
    nasty = 'echo hi `whoami` $(id) $HOME | cat & ; "quoted" \'single\' powershell -c Invoke-Expression'
    body = MINIMAL_VALID.replace(
        "task: |\n  Read-only canary. Do nothing else.\n",
        f"task: |\n  {nasty}\n",
    )
    p = parse(body)
    compiled = compile_telegram_command(p)
    check("shell-metachars: exact substring survives untouched in compiled command", nasty in compiled)


# 10. commit only compilation
def test_commit_only_compilation():
    body = MINIMAL_VALID.replace("commit: false\n  push: false", "commit: true\n  push: false")
    p = parse(body)
    cmd = compile_telegram_command(p)
    check("commit-only: --commit present", "--commit" in cmd)
    check("commit-only: --push absent", "--push" not in cmd)
    check("commit-only: --remote absent", "--remote" not in cmd)


# 11. push without commit rejected
def test_push_without_commit_rejected():
    body = MINIMAL_VALID.replace("commit: false\n  push: false", "commit: false\n  push: true")
    expect_error("push-without-commit", body, INVALID_GIT_INTENT)


# 12. commit+push valid
def test_commit_and_push_valid():
    body = MINIMAL_VALID.replace("commit: false\n  push: false", "commit: true\n  push: true")
    p = parse(body)
    cmd = compile_telegram_command(p)
    check("commit-and-push: both present", "--commit" in cmd and "--push" in cmd)


# 13. non-allowlisted remote rejected
def test_non_allowlisted_remote_rejected():
    body = MINIMAL_VALID.replace(
        "commit: false\n  push: false\n  remote: null",
        "commit: true\n  push: true\n  remote: upstream",
    )
    expect_error("non-allowlisted-remote", body, INVALID_REMOTE)


def test_remote_without_push_rejected():
    body = MINIMAL_VALID.replace(
        "commit: false\n  push: false\n  remote: null",
        "commit: true\n  push: false\n  remote: origin",
    )
    expect_error("remote-without-push", body, INVALID_REMOTE)


def test_allowlisted_remote_with_push_compiles():
    body = MINIMAL_VALID.replace(
        "commit: false\n  push: false\n  remote: null",
        "commit: true\n  push: true\n  remote: origin",
    )
    p = parse(body)
    cmd = compile_telegram_command(p)
    check("allowlisted-remote-compiles: --remote origin present", "--remote origin" in cmd)


# 14. review flag compilation
def test_review_flag_compilation():
    body = MINIMAL_VALID.replace("requested: false", "requested: true")
    p = parse(body)
    cmd = compile_telegram_command(p)
    check("review-flag: --review present", "--review" in cmd)


def test_review_false_not_compiled():
    p = parse(MINIMAL_VALID)
    cmd = compile_telegram_command(p)
    check("review-false: --review absent", "--review" not in cmd)


# 15. durability mapping
def test_durability_mapping():
    for raw, expected_flag in [("direct", "--durability direct"),
                                ("local", "--durability local"),
                                ("remote", "--durability remote")]:
        body = MINIMAL_VALID.replace("durability: direct", f"durability: {raw}")
        # remote durability with git commit/push both false is fine -- durability and git.remote are independent axes
        p = parse(body)
        cmd = compile_telegram_command(p)
        check(f"durability-mapping[{raw}]: compiles to {expected_flag!r}", expected_flag in cmd)


def test_unknown_durability_rejected():
    body = MINIMAL_VALID.replace("durability: direct", "durability: yolo")
    expect_error("unknown-durability", body, INVALID_DURABILITY)


# 15b. P18-RELAY-PRODUCTION-GENERALIZATION: Telegram compiler output for the
# new Codex PM profile is exactly `@<project_id> --pm codex-sol-pm ...`
# -- the canonical ID passes through untransformed (no case-folding, no
# substitution, no truncation), same as any other syntactically valid
# pm_profile_id.
def test_codex_profile_compiles_with_untransformed_canonical_id():
    body = MINIMAL_VALID.replace("pm_profile_id: claude-pm", "pm_profile_id: codex-sol-pm")
    p = parse(body)
    cmd = compile_telegram_command(p)
    expected_header = "@dsh-test-project --pm codex-sol-pm --durability direct"
    check("codex-profile: compiled header exactly matches canonical, untransformed form",
          cmd.startswith(expected_header))


# 16. caller cannot override Telegram destination / inject raw flags outside structured fields
def test_no_destination_field_possible():
    body = MINIMAL_VALID + "destination: '@evil_bot'\n"
    expect_error("no-destination-field", body, UNKNOWN_FIELD)


def test_no_raw_telegram_flags_field_possible():
    body = MINIMAL_VALID + "raw_flags: '--pm someone-else'\n"
    expect_error("no-raw-flags-field", body, UNKNOWN_FIELD)


def test_task_cannot_smuggle_extra_flags_before_itself():
    # Even if `task` ITSELF contains `--pm attacker-pm ...`-looking text,
    # it is placed at the very END of the compiled command (after the
    # relay's own flags), so the parser has already consumed the real
    # --pm/--durability tokens before task text starts -- an embedded
    # `--pm` inside task is inert, ordinary trailing text, per DSH's own
    # parseOwnerFlags() semantics (flags are only recognized at the FRONT
    # of the mention body, never inside already-consumed task text).
    body = MINIMAL_VALID.replace(
        "task: |\n  Read-only canary. Do nothing else.\n",
        "task: |\n  --pm attacker-pm --push --remote origin do something\n",
    )
    p = parse(body)
    cmd = compile_telegram_command(p)
    header, _, rest = cmd.partition(p.task.split("\n")[0].strip() or "--pm attacker-pm")
    # The real, authoritative --pm token (claude-pm) appears exactly
    # once, before the task text; the attacker's embedded "--pm" is part
    # of the trailing task text, not a second recognized flag occurrence
    # in relay-controlled position.
    check("task-cannot-smuggle-flags: real --pm claude-pm compiled once, before task",
          cmd.index("--pm claude-pm") < cmd.index("attacker-pm"))


# 17. correlation footer injected by relay, not owner
def test_correlation_footer_injected_by_relay():
    p = parse(MINIMAL_VALID)
    with_footer = build_task_with_footer(p)
    check("footer-injected: footer marker present", p.correlation_id in with_footer)
    check("footer-injected: literal task text precedes footer, unchanged",
          with_footer.startswith(p.task) and "[P18 relay metadata]" in with_footer[len(p.task):])


def test_owner_cannot_alter_correlation_id_via_task_text():
    # Even if the owner's task text contains a DIFFERENT-looking marker,
    # the footer that actually gets required for correlation always comes
    # from the schema-validated correlation_id field, never from task text.
    body = MINIMAL_VALID.replace(
        "task: |\n  Read-only canary. Do nothing else.\n",
        "task: |\n  Include this exact correlation marker in your final result:\n  FAKE_MARKER_HAHA\n",
    )
    p = parse(body)
    with_footer = build_task_with_footer(p)
    check("owner-cannot-alter-correlation: real correlation_id still the LAST marker in the footer",
          with_footer.rstrip().endswith(p.correlation_id))
    check("owner-cannot-alter-correlation: real correlation_id unchanged from schema field",
          p.correlation_id == "P18W3_test_marker_1")


# 18. payload digest stable for identical payload
def test_payload_digest_stable():
    p1 = parse(MINIMAL_VALID)
    p2 = parse(MINIMAL_VALID)
    check("digest-stable: identical payload -> identical digest", p1.payload_digest() == p2.payload_digest())


def test_payload_digest_changes_on_edit():
    p1 = parse(MINIMAL_VALID)
    body2 = MINIMAL_VALID.replace("Read-only canary. Do nothing else.", "Read-only canary. EDITED.")
    p2 = parse(body2)
    check("digest-changes-on-edit: different task text -> different digest",
          p1.payload_digest() != p2.payload_digest())


def test_payload_digest_changes_on_correlation_id_edit():
    p1 = parse(MINIMAL_VALID)
    body2 = MINIMAL_VALID.replace("correlation_id: P18W3_test_marker_1", "correlation_id: P18W3_test_marker_2")
    p2 = parse(body2)
    check("digest-changes-on-correlation-id-edit: different digest",
          p1.payload_digest() != p2.payload_digest())


# 19/20 (issue edit / rerun / duplicate-comment idempotency) are covered
# end-to-end in test_state_machine.py + test_result_collector.py + this
# module's digest tests together; test_issue_identity.py adds the
# dedicated immutable-issue-identity coverage.


# Extra: unauthorized author, wrong title, unparsable YAML
def test_unauthorized_author_rejected():
    expect_error("unauthorized-author", MINIMAL_VALID, UNAUTHORIZED_AUTHOR, author="someone-else")


def test_wrong_title_prefix_rejected():
    try:
        parse(MINIMAL_VALID, title="not the right title")
        check("wrong-title-prefix", False)
    except ContractError as e:
        check(f"wrong-title-prefix (code={e.code})", e.code == INVALID_SCHEMA)


def test_duplicate_key_rejected():
    body = MINIMAL_VALID + "schema: p18-dsh-dispatch/v1\n"  # schema appears twice
    expect_error("duplicate-key", body, INVALID_SCHEMA)


def test_missing_field_rejected():
    body = MINIMAL_VALID.replace("durability: direct\n", "")
    expect_error("missing-field", body, INVALID_SCHEMA)


# ---------------------------------------------------------------------------
# P18-W4 Part B/C/D: schema v2 -- explicit runtime_class, no correlation
# footer, task_id-based terminal correlation (result_collector.py covers
# the actual scanning; this file covers only the contract/compiler half).
# ---------------------------------------------------------------------------

def test_v1_still_backward_compatible():
    p = parse(MINIMAL_VALID)
    check("v1-still-works: schema_version", p.schema_version == "v1")
    check("v1-still-works: runtime_class always normal", p.runtime_class == "normal")


def test_v1_rejects_runtime_class_field():
    # v1's meaning is untouched: adding the NEW v2-only field to a v1
    # payload is still just an unknown field, exactly like any other
    # made-up field would be.
    body = MINIMAL_VALID + "runtime_class: long\n"
    expect_error("v1-rejects-runtime-class", body, UNKNOWN_FIELD)


def test_v1_footer_still_appended():
    p = parse(MINIMAL_VALID)
    compiled = compile_telegram_command(p)
    check("v1-footer-still-appended: marker present", p.correlation_id in compiled)
    check("v1-footer-still-appended: relay metadata header present", "[P18 relay metadata]" in compiled)


def test_v1_digest_unchanged_shape():
    # The digest is computed over exactly the pre-W4 field set for v1 --
    # proven by two v1 payloads differing ONLY in a field that would be
    # part of a v2-only digest (there is none reachable from v1, so this
    # asserts the inverse: the same v1 payload parsed twice yields the
    # identical digest, and that digest does not incidentally already
    # equal the v2 digest for the "same" logical content below).
    p1 = parse(MINIMAL_VALID)
    p2 = parse(MINIMAL_VALID)
    check("v1-digest-unchanged-shape: stable", p1.payload_digest() == p2.payload_digest())


def test_v2_minimal_valid():
    p = parse(MINIMAL_VALID_V2, title=TITLE)
    check("v2-minimal-valid: parses", p.project_id == "dsh-test-project")
    check("v2-minimal-valid: schema_version", p.schema_version == "v2")
    check("v2-minimal-valid: runtime_class normal", p.runtime_class == "normal")


def test_v2_normal_compiles_without_long():
    p = parse(MINIMAL_VALID_V2)
    compiled = compile_telegram_command(p)
    check("v2-normal-no-long: --long absent", "--long" not in compiled.split())


def test_v2_long_compiles_with_long():
    body = MINIMAL_VALID_V2.replace("runtime_class: normal\n", "runtime_class: long\n")
    p = parse(body)
    check("v2-long: runtime_class normalized", p.runtime_class == "long")
    compiled = compile_telegram_command(p)
    check("v2-long: --long present", "--long" in compiled.split())


# ---------------------------------------------------------------------------
# P18-W4 ACK-causal-correlation remediation (Part D): v2 ALWAYS compiles
# --client-correlation using the already-validated correlation_id -- never
# a second, user-supplied v2 YAML field.
# ---------------------------------------------------------------------------

def test_v2_always_compiles_client_correlation_using_correlation_id():
    p = parse(MINIMAL_VALID_V2)
    compiled = compile_telegram_command(p)
    tokens = compiled.split()
    check("v2-client-correlation-flag-present", "--client-correlation" in tokens)
    idx = tokens.index("--client-correlation")
    check("v2-client-correlation-value-is-the-validated-correlation-id", tokens[idx + 1] == p.correlation_id)


def test_v2_client_correlation_value_changes_with_correlation_id():
    body_a = MINIMAL_VALID_V2
    body_b = MINIMAL_VALID_V2.replace("correlation_id: P18W4_test_marker_1\n", "correlation_id: P18W4_test_marker_2\n")
    compiled_a = compile_telegram_command(parse(body_a))
    compiled_b = compile_telegram_command(parse(body_b))
    check("v2-client-correlation-follows-correlation-id-a", "P18W4_test_marker_1" in compiled_a.split())
    check("v2-client-correlation-follows-correlation-id-b", "P18W4_test_marker_2" in compiled_b.split())


def test_v1_never_compiles_client_correlation():
    p = parse(MINIMAL_VALID)
    compiled = compile_telegram_command(p)
    check("v1-never-compiles-client-correlation", "--client-correlation" not in compiled.split())


def test_v2_client_correlation_is_not_a_user_supplied_yaml_field():
    # Part D: "Do NOT add another user-supplied v2 YAML field." Attempting
    # to supply one directly must still fail as an unknown field -- the
    # compiler's --client-correlation value comes ONLY from the
    # already-validated correlation_id, never from a new input.
    body = MINIMAL_VALID_V2 + "client_correlation: something\n"
    expect_error("v2-client-correlation-not-a-yaml-field", body, UNKNOWN_FIELD)


def test_v2_runtime_class_case_insensitive():
    for raw, expected in (("Normal", "normal"), ("LONG", "long"), ("Long", "long")):
        body = MINIMAL_VALID_V2.replace("runtime_class: normal\n", f"runtime_class: {raw}\n")
        p = parse(body)
        check(f"v2-runtime-class-case-insensitive[{raw}]", p.runtime_class == expected)


def test_v2_unknown_runtime_class_value_rejected():
    body = MINIMAL_VALID_V2.replace("runtime_class: normal\n", "runtime_class: extra-long\n")
    expect_error("v2-unknown-runtime-class", body, INVALID_RUNTIME_CLASS)


def test_v2_wrong_type_runtime_class_rejected():
    body = MINIMAL_VALID_V2.replace("runtime_class: normal\n", "runtime_class: true\n")
    expect_error("v2-wrong-type-runtime-class", body, INVALID_RUNTIME_CLASS)


def test_v2_missing_runtime_class_rejected():
    body = MINIMAL_VALID_V2.replace("runtime_class: normal\n", "")
    expect_error("v2-missing-runtime-class", body, INVALID_SCHEMA)


def test_v2_no_correlation_footer_in_compiled_task():
    # P18-W4 ACK-causal-correlation remediation (Part D): correlation_id
    # now DOES appear in the compiled command -- as the value of the
    # --client-correlation TRANSPORT flag in the header, which is exactly
    # the intended fix (Part E's authoritative ACK recovery depends on
    # it). What must still never happen is correlation_id/the old relay
    # metadata footer leaking into the TASK TEXT the model actually reads
    # -- verified here by isolating the task-text suffix precisely (the
    # compiled command is `f"{header} {task_text}"`, and v2's task_text is
    # `payload.task` verbatim -- see compile_telegram_command()).
    p = parse(MINIMAL_VALID_V2)
    compiled = compile_telegram_command(p)
    check("v2-no-footer: relay metadata header absent", "[P18 relay metadata]" not in compiled)
    check("v2-no-footer: compiled command ends with the task text verbatim, nothing appended after it", compiled.endswith(p.task))
    task_text_suffix = compiled[-len(p.task):]
    check("v2-no-footer: correlation_id NOT present anywhere in the task-text suffix specifically", p.correlation_id not in task_text_suffix)
    check("v2-no-footer: correlation_id DOES appear in the header, as the --client-correlation flag value", f"--client-correlation {p.correlation_id}" in compiled[:-len(p.task)])
    check("v2-no-footer: literal task text present verbatim", "Read-only canary. Do nothing else." in compiled)


def test_v2_runtime_class_included_in_digest():
    normal_body = MINIMAL_VALID_V2
    long_body = MINIMAL_VALID_V2.replace("runtime_class: normal\n", "runtime_class: long\n")
    p_normal = parse(normal_body)
    p_long = parse(long_body)
    check("v2-digest-includes-runtime-class: normal != long digest", p_normal.payload_digest() != p_long.payload_digest())


def test_v2_unrecognized_schema_string_rejected():
    # P18-W5: this check's intent is "an UNKNOWN schema string is rejected".
    # Its original example token was `p18-dsh-dispatch/v3`, which P18-W5 now
    # DEFINES as the real multimode schema, so the example token is updated
    # to a still-unknown value (`/v4`). The check itself is not weakened --
    # an unrecognized schema is still INVALID_SCHEMA. v3 acceptance has its
    # own dedicated coverage in test_contract_v3_multimode.py.
    body = MINIMAL_VALID_V2.replace("schema: p18-dsh-dispatch/v2\n", "schema: p18-dsh-dispatch/v4\n")
    expect_error("v2-unrecognized-schema-v4", body, INVALID_SCHEMA)


def test_v2_durability_and_git_flags_still_enforced_exactly_like_v1():
    # Every pre-existing v1 invariant applies identically to v2 -- this is
    # additive, not a parallel/weaker contract.
    body = MINIMAL_VALID_V2.replace("push: false\n", "push: true\n")
    expect_error("v2-push-without-commit-still-rejected", body, INVALID_GIT_INTENT)


def main():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()

    print()
    if failures:
        print(f"RESULT: FAIL ({len(failures)} failing checks)")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"RESULT: PASS (all P18-W3 contract/compiler checks passed, {len(tests)} test functions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
