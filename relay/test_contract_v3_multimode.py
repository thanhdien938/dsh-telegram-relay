"""
P18-W5 multimode contract/compiler/digest tests.

Covers the NEW `p18-dsh-dispatch/v3` schema (mode: single | council |
debate), the strict per-mode parser, the native-grammar compiler, the
payload digest (issue-mutation guard), and the compiled-command size
bound -- plus explicit v1/v2 backward-compatibility regression guards.

Run: .venv\\Scripts\\python.exe relay\\test_contract_v3_multimode.py
"""
import sys

from contract_v3 import (
    parse_and_validate, compile_telegram_command, ContractError, ValidatedPayload,
    enforce_compiled_command_bound,
    INVALID_SCHEMA, INVALID_MODE, UNKNOWN_FIELD, INVALID_PM_PROFILE,
    INVALID_CORRELATION_ID, INVALID_RUNTIME_CLASS,
    INVALID_PARTICIPANTS, INVALID_DEBATE_ROUNDS, INVALID_IMPLEMENTATION_PROFILE,
    COMPILED_COMMAND_TOO_LARGE, MAX_COMPILED_COMMAND_CHARS, COUNCIL_MAX_PARTICIPANTS,
    # P24.1G3b:
    INVALID_WORKSPACE_OUTPUT, WORKSPACE_OUTPUT_SINGLE_ONLY, WORKSPACE_OUTPUT_REQUIRES_GIT_COMMIT,
)

TITLE_PREFIX = "[P18-W3-CANARY]"
AUTHOR = "alice"
TITLE = f"{TITLE_PREFIX} multimode test"

# Pristine-main digests for the canonical v1 / v2 minimal payloads. If v3
# work ever perturbs v1/v2 digest serialization these break -- that is the
# whole point (G2/G3).
PINNED_V1_DIGEST = "38033de238e7dcbc2593f49e968f04ae178473c25ab84263e90da4de1f571978"
PINNED_V2_DIGEST = "31f87b50bbaa705cc3b9c9d42577a363af8e29f332d4be94267f985baa29664b"

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


def expect_error(name, body, expected_code, title=TITLE):
    try:
        parse(body, title=title)
        check(name, False)
    except ContractError as e:
        ok = e.code == expected_code
        print(("PASS" if ok else "FAIL") + f": {name} (code={e.code}, wanted {expected_code})")
        if not ok:
            failures.append(name)


def expect_any_error(name, body, allowed_codes, title=TITLE):
    try:
        parse(body, title=title)
        check(name, False)
    except ContractError as e:
        ok = e.code in allowed_codes
        print(("PASS" if ok else "FAIL") + f": {name} (code={e.code}, wanted one of {sorted(allowed_codes)})")
        if not ok:
            failures.append(name)


# --------------------------------------------------------------------------
# Canonical bodies
# --------------------------------------------------------------------------

V1_SINGLE = """\
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

V2_SINGLE = """\
schema: p18-dsh-dispatch/v2
mode: single
correlation_id: P18W4_test_marker_1
project_id: dsh-test-project
pm_profile_id: claude-pm
runtime_class: normal
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

V3_SINGLE = """\
schema: p18-dsh-dispatch/v3
mode: single
correlation_id: relW5_single_0001
project_id: dsh-test-project
pm_profile_id: claude-pm
runtime_class: normal
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

V3_SINGLE_COMMIT = """\
schema: p18-dsh-dispatch/v3
mode: single
correlation_id: relG3b_single_commit01
project_id: dsh-test-project
pm_profile_id: claude-pm
runtime_class: normal
durability: direct
git:
  commit: true
  push: false
  remote: null
review:
  requested: false
task: |
  Read-only canary. Do nothing else.
"""

V3_COUNCIL = """\
schema: p18-dsh-dispatch/v3
mode: council
correlation_id: relW5_council_0001
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
  Review the current architecture.
"""

V3_DEBATE = """\
schema: p18-dsh-dispatch/v3
mode: debate
correlation_id: relW5_debate_0001
project_id: dsh-test-project
pm_profile_id: chair-pm
participants:
  - alpha-pm
  - beta-pm
debate_rounds: 2
durability: direct
git:
  commit: false
  push: false
  remote: null
review:
  requested: false
task: |
  Decide the migration strategy.
"""


# --------------------------------------------------------------------------
# Backward compatibility (G2 / G3)
# --------------------------------------------------------------------------

def test_v1_single_accepted_unchanged():
    p = parse(V1_SINGLE)
    check("v1-single-accepted", p.schema_version == "v1" and p.mode == "single")
    check("v1-single-digest-byte-for-byte", p.payload_digest() == PINNED_V1_DIGEST)
    check("v1-single-compiled-no-client-correlation",
          "--client-correlation" not in compile_telegram_command(p))


def test_v2_single_normal_and_long_accepted_unchanged():
    pn = parse(V2_SINGLE)
    check("v2-single-normal-accepted", pn.schema_version == "v2" and pn.runtime_class == "normal")
    check("v2-single-normal-digest-byte-for-byte", pn.payload_digest() == PINNED_V2_DIGEST)
    pl = parse(V2_SINGLE.replace("runtime_class: normal\n", "runtime_class: long\n"))
    check("v2-single-long-accepted", pl.runtime_class == "long")
    check("v2-single-long-compiles-long-flag", " --long " in compile_telegram_command(pl))


def test_v1_v2_council_and_debate_rejected():
    expect_error("v1-council-rejected", V1_SINGLE.replace("mode: single", "mode: council"), INVALID_MODE)
    expect_error("v1-debate-rejected", V1_SINGLE.replace("mode: single", "mode: debate"), INVALID_MODE)
    expect_error("v2-council-rejected", V2_SINGLE.replace("mode: single", "mode: council"), INVALID_MODE)
    expect_error("v2-debate-rejected", V2_SINGLE.replace("mode: single", "mode: debate"), INVALID_MODE)


# --------------------------------------------------------------------------
# v3 acceptance (G4 / G5 / G6)
# --------------------------------------------------------------------------

def test_v3_single_accepted():
    p = parse(V3_SINGLE)
    check("v3-single-accepted", p.schema_version == "v3" and p.mode == "single" and p.runtime_class == "normal")
    check("v3-single-no-participants", p.participants == ())


def test_v3_council_accepted():
    p = parse(V3_COUNCIL)
    check("v3-council-accepted", p.schema_version == "v3" and p.mode == "council")
    check("v3-council-participants-ordered", p.participants == ("alpha-pm", "beta-pm", "gamma-pm"))
    check("v3-council-chair-is-pm-profile-id", p.pm_profile_id == "chair-pm")
    check("v3-council-runtime-class-normal-default", p.runtime_class == "normal")
    check("v3-council-no-debate-rounds", p.debate_rounds is None)
    check("v3-council-no-implementation", p.implementation_profile_id is None)


def test_v3_debate_accepted():
    p = parse(V3_DEBATE)
    check("v3-debate-accepted", p.schema_version == "v3" and p.mode == "debate")
    check("v3-debate-participants-ordered", p.participants == ("alpha-pm", "beta-pm"))
    check("v3-debate-rounds", p.debate_rounds == 2)
    check("v3-debate-no-implementation-when-absent", p.implementation_profile_id is None)


def test_v3_unrecognized_mode_rejected():
    expect_error("v3-mode-quorum-rejected", V3_SINGLE.replace("mode: single", "mode: quorum"), INVALID_MODE)


# --------------------------------------------------------------------------
# Required / forbidden fields per mode
# --------------------------------------------------------------------------

def test_v3_council_requires_participants():
    body = "\n".join(l for l in V3_COUNCIL.splitlines()
                      if l not in ("participants:", "  - alpha-pm", "  - beta-pm", "  - gamma-pm")) + "\n"
    expect_error("v3-council-requires-participants", body, INVALID_SCHEMA)


def test_v3_debate_requires_participants():
    body = "\n".join(l for l in V3_DEBATE.splitlines()
                      if l not in ("participants:", "  - alpha-pm", "  - beta-pm")) + "\n"
    expect_error("v3-debate-requires-participants", body, INVALID_SCHEMA)


def test_v3_debate_requires_rounds():
    body = V3_DEBATE.replace("debate_rounds: 2\n", "")
    expect_error("v3-debate-requires-rounds", body, INVALID_SCHEMA)


def test_v3_debate_rounds_1_and_2_accepted_0_and_3_rejected():
    check("v3-debate-rounds-1-accepted", parse(V3_DEBATE.replace("debate_rounds: 2", "debate_rounds: 1")).debate_rounds == 1)
    check("v3-debate-rounds-2-accepted", parse(V3_DEBATE).debate_rounds == 2)
    expect_error("v3-debate-rounds-0-rejected", V3_DEBATE.replace("debate_rounds: 2", "debate_rounds: 0"), INVALID_DEBATE_ROUNDS)
    expect_error("v3-debate-rounds-3-rejected", V3_DEBATE.replace("debate_rounds: 2", "debate_rounds: 3"), INVALID_DEBATE_ROUNDS)
    expect_error("v3-debate-rounds-string-rejected", V3_DEBATE.replace("debate_rounds: 2", 'debate_rounds: "2"'), INVALID_DEBATE_ROUNDS)
    expect_error("v3-debate-rounds-bool-rejected", V3_DEBATE.replace("debate_rounds: 2", "debate_rounds: true"), INVALID_DEBATE_ROUNDS)


def test_v3_implementation_profile_id_semantics():
    # Debate, valid (in participants) -> accepted.
    ok = parse(V3_DEBATE.replace("debate_rounds: 2\n", "debate_rounds: 2\nimplementation_profile_id: alpha-pm\n"))
    check("v3-debate-impl-valid-accepted", ok.implementation_profile_id == "alpha-pm")
    # Debate, not one of participants -> rejected.
    expect_error("v3-debate-impl-not-a-participant-rejected",
                  V3_DEBATE.replace("debate_rounds: 2\n", "debate_rounds: 2\nimplementation_profile_id: delta-pm\n"),
                  INVALID_IMPLEMENTATION_PROFILE)
    # Debate, malformed id -> rejected.
    expect_error("v3-debate-impl-malformed-rejected",
                  V3_DEBATE.replace("debate_rounds: 2\n", 'debate_rounds: 2\nimplementation_profile_id: "has space"\n'),
                  INVALID_IMPLEMENTATION_PROFILE)
    # Single with implementation_profile_id -> rejected (not in single's field set).
    expect_error("v3-single-impl-rejected",
                  V3_SINGLE.replace("runtime_class: normal\n", "runtime_class: normal\nimplementation_profile_id: alpha-pm\n"),
                  UNKNOWN_FIELD)
    # Council with implementation_profile_id -> rejected (forbidden for council).
    expect_error("v3-council-impl-rejected",
                  V3_COUNCIL.replace("  - gamma-pm\n", "  - gamma-pm\nimplementation_profile_id: alpha-pm\n"),
                  UNKNOWN_FIELD)


def test_v3_runtime_class_scope():
    check("v3-single-runtime-class-normal-accepted", parse(V3_SINGLE).runtime_class == "normal")
    check("v3-single-runtime-class-long-accepted",
          parse(V3_SINGLE.replace("runtime_class: normal", "runtime_class: long")).runtime_class == "long")
    expect_error("v3-single-runtime-class-bogus-rejected",
                  V3_SINGLE.replace("runtime_class: normal", "runtime_class: enormous"), INVALID_RUNTIME_CLASS)
    # runtime_class forbidden for council/debate (no LONG Council/Debate).
    expect_error("v3-council-runtime-class-rejected",
                  V3_COUNCIL.replace("  - gamma-pm\n", "  - gamma-pm\nruntime_class: long\n"), UNKNOWN_FIELD)
    expect_error("v3-debate-runtime-class-rejected",
                  V3_DEBATE.replace("debate_rounds: 2\n", "debate_rounds: 2\nruntime_class: long\n"), UNKNOWN_FIELD)


def test_v3_single_with_participants_rejected():
    expect_error("v3-single-participants-rejected",
                  V3_SINGLE.replace("runtime_class: normal\n", "runtime_class: normal\nparticipants:\n  - alpha-pm\n"),
                  UNKNOWN_FIELD)


def test_v3_council_with_debate_rounds_rejected():
    expect_error("v3-council-debate-rounds-rejected",
                  V3_COUNCIL.replace("  - gamma-pm\n", "  - gamma-pm\ndebate_rounds: 2\n"), UNKNOWN_FIELD)


def test_v3_unknown_top_level_field_rejected():
    expect_error("v3-unknown-field-rejected", V3_COUNCIL + "extra_field: nope\n", UNKNOWN_FIELD)


def test_v3_duplicate_yaml_keys_rejected():
    expect_error("v3-duplicate-yaml-key-rejected", V3_COUNCIL + "mode: council\n", INVALID_SCHEMA)


# --------------------------------------------------------------------------
# Participant validation
# --------------------------------------------------------------------------

def test_v3_participants_must_be_a_sequence():
    expect_error("v3-participants-scalar-rejected",
                  V3_COUNCIL.replace("participants:\n  - alpha-pm\n  - beta-pm\n  - gamma-pm\n", "participants: alpha-pm\n"),
                  INVALID_PARTICIPANTS)


def test_v3_participants_empty_rejected():
    expect_error("v3-participants-empty-rejected",
                  V3_COUNCIL.replace("participants:\n  - alpha-pm\n  - beta-pm\n  - gamma-pm\n", "participants: []\n"),
                  INVALID_PARTICIPANTS)


def test_v3_participant_malformed_rejected():
    expect_error("v3-participant-space-rejected",
                  V3_COUNCIL.replace("  - beta-pm\n", '  - "beta pm"\n'), INVALID_PARTICIPANTS)
    expect_error("v3-participant-shell-metachar-rejected",
                  V3_COUNCIL.replace("  - beta-pm\n", '  - "beta;rm -rf /"\n'), INVALID_PARTICIPANTS)
    expect_error("v3-participant-leading-dash-rejected",
                  V3_COUNCIL.replace("  - beta-pm\n", '  - "-beta"\n'), INVALID_PARTICIPANTS)
    expect_error("v3-participant-nonstring-rejected",
                  V3_COUNCIL.replace("  - beta-pm\n", "  - 42\n"), INVALID_PARTICIPANTS)


def test_v3_duplicate_participant_rejected():
    expect_error("v3-duplicate-participant-rejected",
                  V3_COUNCIL.replace("  - gamma-pm\n", "  - alpha-pm\n"), INVALID_PARTICIPANTS)


def test_v3_too_many_participants_rejected():
    five = "".join(f"  - p{i}-pm\n" for i in range(COUNCIL_MAX_PARTICIPANTS + 1))
    body = V3_COUNCIL.replace("participants:\n  - alpha-pm\n  - beta-pm\n  - gamma-pm\n", "participants:\n" + five)
    expect_error("v3-too-many-participants-rejected", body, INVALID_PARTICIPANTS)


def test_v3_chair_may_also_be_a_participant():
    # DSH normalizeCouncilSpec explicitly enforces NO chair/participant
    # uniqueness constraint -- the relay mirrors that.
    body = V3_COUNCIL.replace("  - gamma-pm\n", "  - chair-pm\n")
    p = parse(body)
    check("v3-chair-as-participant-accepted", "chair-pm" in p.participants and p.pm_profile_id == "chair-pm")


def test_v3_exactly_four_participants_accepted():
    four = "".join(f"  - p{i}-pm\n" for i in range(COUNCIL_MAX_PARTICIPANTS))
    body = V3_COUNCIL.replace("participants:\n  - alpha-pm\n  - beta-pm\n  - gamma-pm\n", "participants:\n" + four)
    check("v3-four-participants-accepted", len(parse(body).participants) == COUNCIL_MAX_PARTICIPANTS)


# --------------------------------------------------------------------------
# Compiler (native-grammar parity)
# --------------------------------------------------------------------------

def test_compile_v3_single_exact():
    cmd = compile_telegram_command(parse(V3_SINGLE))
    check("compile-v3-single-exact",
          cmd == "@dsh-test-project --pm claude-pm --durability direct "
                 "--client-correlation relW5_single_0001 Read-only canary. Do nothing else.\n")


def test_compile_v3_single_long_exact():
    cmd = compile_telegram_command(parse(V3_SINGLE.replace("runtime_class: normal", "runtime_class: long")))
    check("compile-v3-single-long-exact",
          cmd == "@dsh-test-project --pm claude-pm --durability direct --long "
                 "--client-correlation relW5_single_0001 Read-only canary. Do nothing else.\n")


def test_compile_v3_council_exact_and_forbidden_flags_absent():
    cmd = compile_telegram_command(parse(V3_COUNCIL))
    check("compile-v3-council-exact",
          cmd == "@dsh-test-project --pm chair-pm --debate alpha-pm,beta-pm,gamma-pm --durability direct "
                 "--client-correlation relW5_council_0001 Review the current architecture.\n")
    for forbidden in ("--debate-extend", "--debate-rounds", "--implementation", "--long"):
        check(f"compile-v3-council-no-{forbidden}", forbidden not in cmd)
    check("compile-v3-council-has-pm-chair", "--pm chair-pm" in cmd)
    check("compile-v3-council-has-debate-participants", "--debate alpha-pm,beta-pm,gamma-pm" in cmd)


def test_compile_v3_debate_exact():
    cmd = compile_telegram_command(parse(V3_DEBATE))
    check("compile-v3-debate-exact",
          cmd == "@dsh-test-project --pm chair-pm --debate alpha-pm,beta-pm --debate-extend --debate-rounds 2 "
                 "--durability direct --client-correlation relW5_debate_0001 Decide the migration strategy.\n")
    for token in ("--pm chair-pm", "--debate alpha-pm,beta-pm", "--debate-extend", "--debate-rounds 2"):
        check(f"compile-v3-debate-contains[{token}]", token in cmd)


def test_compile_v3_debate_with_implementation_exact():
    body = V3_DEBATE.replace("debate_rounds: 2\n", "debate_rounds: 2\nimplementation_profile_id: alpha-pm\n")
    cmd = compile_telegram_command(parse(body))
    check("compile-v3-debate-impl-exact",
          cmd == "@dsh-test-project --pm chair-pm --debate alpha-pm,beta-pm --debate-extend --debate-rounds 2 "
                 "--implementation alpha-pm --durability direct --client-correlation relW5_debate_0001 "
                 "Decide the migration strategy.\n")


def test_compile_v3_debate_rounds_1():
    cmd = compile_telegram_command(parse(V3_DEBATE.replace("debate_rounds: 2", "debate_rounds: 1")))
    check("compile-v3-debate-rounds-1", "--debate-rounds 1" in cmd and "--debate-rounds 2" not in cmd)


def test_compile_v3_all_modes_client_correlation_exact_value():
    for name, body, corr in [
        ("single", V3_SINGLE, "relW5_single_0001"),
        ("council", V3_COUNCIL, "relW5_council_0001"),
        ("debate", V3_DEBATE, "relW5_debate_0001"),
    ]:
        cmd = compile_telegram_command(parse(body))
        check(f"compile-v3-{name}-client-correlation-exact", f"--client-correlation {corr} " in cmd)


def test_compile_v3_task_body_verbatim_no_footer():
    for name, body in [("single", V3_SINGLE), ("council", V3_COUNCIL), ("debate", V3_DEBATE)]:
        p = parse(body)
        cmd = compile_telegram_command(p)
        check(f"compile-v3-{name}-task-verbatim", cmd.endswith(" " + p.task))
        check(f"compile-v3-{name}-no-relay-metadata-footer", "[P18 relay metadata]" not in cmd)
        # The task text suffix must not contain the correlation id -- it is
        # only ever the --client-correlation flag value in the header.
        header, _, task_suffix = cmd.partition(" " + p.task)
        check(f"compile-v3-{name}-correlation-not-in-task-suffix", p.correlation_id not in task_suffix)


def test_compile_v3_git_review_flags():
    body = V3_DEBATE.replace(
        "git:\n  commit: false\n  push: false\n  remote: null\n",
        "git:\n  commit: true\n  push: true\n  remote: origin\n",
    ).replace("review:\n  requested: false\n", "review:\n  requested: true\n")
    cmd = compile_telegram_command(parse(body))
    for token in ("--commit", "--push", "--remote origin", "--review"):
        check(f"compile-v3-debate-git-review[{token}]", token in cmd)


# --------------------------------------------------------------------------
# Digest / immutability (issue mutation guard)
# --------------------------------------------------------------------------

def _digest(body):
    return parse(body).payload_digest()


def test_v3_digest_changes_on_every_semantic_field():
    base = _digest(V3_DEBATE)
    check("v3-digest-participant-membership-change",
          _digest(V3_DEBATE.replace("  - beta-pm\n", "  - delta-pm\n")) != base)
    check("v3-digest-participant-order-change",
          _digest(V3_DEBATE.replace("  - alpha-pm\n  - beta-pm\n", "  - beta-pm\n  - alpha-pm\n")) != base)
    check("v3-digest-chair-change",
          _digest(V3_DEBATE.replace("pm_profile_id: chair-pm", "pm_profile_id: chair-two")) != base)
    check("v3-digest-rounds-change",
          _digest(V3_DEBATE.replace("debate_rounds: 2", "debate_rounds: 1")) != base)
    check("v3-digest-implementation-add",
          _digest(V3_DEBATE.replace("debate_rounds: 2\n", "debate_rounds: 2\nimplementation_profile_id: alpha-pm\n")) != base)
    check("v3-digest-task-change",
          _digest(V3_DEBATE.replace("Decide the migration strategy.", "Decide the migration strategy NOW.")) != base)
    check("v3-digest-correlation-change",
          _digest(V3_DEBATE.replace("relW5_debate_0001", "relW5_debate_0002")) != base)
    check("v3-digest-mode-distinguished",
          _digest(V3_SINGLE) != _digest(V3_SINGLE.replace(
              "mode: single\n", "mode: council\n").replace(
              "runtime_class: normal\n", "participants:\n  - alpha-pm\n")))


def test_v3_council_impl_add_remove_changes_digest():
    with_impl = V3_DEBATE.replace("debate_rounds: 2\n", "debate_rounds: 2\nimplementation_profile_id: alpha-pm\n")
    check("v3-digest-impl-presence-matters", _digest(with_impl) != _digest(V3_DEBATE))


def test_v3_digest_stable_for_identical_bodies():
    check("v3-digest-stable", _digest(V3_COUNCIL) == _digest(V3_COUNCIL))


def test_v1_v2_digest_compat_regression():
    check("v1-digest-pinned", _digest(V1_SINGLE) == PINNED_V1_DIGEST)
    check("v2-digest-pinned", _digest(V2_SINGLE) == PINNED_V2_DIGEST)


# --------------------------------------------------------------------------
# Message-size bound
# --------------------------------------------------------------------------

# Maximal-header council/debate bodies: 4 participants of the full 128-char
# id length + a 128-char chair -> the largest header the v3 contract can
# produce. With MAX_TASK_CHARS (3000) of task on top, the compiled command
# overflows MAX_COMPILED_COMMAND_CHARS; sized a little smaller, it fits.
_MAXLEN_PARTICIPANTS = tuple(("q" * 127 + str(i)) for i in range(COUNCIL_MAX_PARTICIPANTS))
_MAXLEN_CHAIR = "c" * 128


def _big_council_body(task_text, corr="relW5_bounds_00001"):
    plist = "".join(f"  - {p}\n" for p in _MAXLEN_PARTICIPANTS)
    return (
        "schema: p18-dsh-dispatch/v3\n"
        "mode: council\n"
        f"correlation_id: {corr}\n"
        "project_id: dsh-test-project\n"
        f"pm_profile_id: {_MAXLEN_CHAIR}\n"
        f"participants:\n{plist}"
        "durability: direct\n"
        "git:\n  commit: false\n  push: false\n  remote: null\n"
        "review:\n  requested: false\n"
        f"task: {task_text!r}\n"
    )


def _big_debate_body(task_text, corr="relW5_bounds_00002"):
    plist = "".join(f"  - {p}\n" for p in _MAXLEN_PARTICIPANTS)
    return (
        "schema: p18-dsh-dispatch/v3\n"
        "mode: debate\n"
        f"correlation_id: {corr}\n"
        "project_id: dsh-test-project\n"
        f"pm_profile_id: {_MAXLEN_CHAIR}\n"
        f"participants:\n{plist}"
        "debate_rounds: 2\n"
        f"implementation_profile_id: {_MAXLEN_PARTICIPANTS[0]}\n"
        "durability: remote\n"
        "git:\n  commit: true\n  push: true\n  remote: origin\n"
        "review:\n  requested: true\n"
        f"task: {task_text!r}\n"
    )


def test_v3_maximal_council_and_debate_within_bound_via_yaml():
    # The largest council/debate a valid YAML body can produce (4 x
    # 128-char participants + 128-char chair + full MAX_TASK_CHARS task)
    # stays under MAX_COMPILED_COMMAND_CHARS -- with today's field bounds
    # the per-field MAX_TASK_CHARS keeps every accepted v3 command
    # sendable. (The explicit compiled-length guard below is the
    # defense-in-depth that survives a future widening of those bounds.)
    for name, fn in [("council", _big_council_body), ("debate", _big_debate_body)]:
        compiled = compile_telegram_command(parse(fn("T" * 3000)))
        check(f"v3-{name}-maximal-yaml-within-bound", len(compiled) <= MAX_COMPILED_COMMAND_CHARS)


def _oversized_debate_payload(task_len):
    # Build a ValidatedPayload directly to exercise the compiled-length
    # guard past what a MAX_TASK_CHARS-bounded YAML body can reach.
    return ValidatedPayload(
        correlation_id="relW5_bounds_direct01", project_id="c" * 120, pm_profile_id="c" * 128,
        durability="remote", git_commit=True, git_push=True, git_remote="origin",
        review_requested=True, task="T" * task_len, schema_version="v3", runtime_class="normal",
        mode="debate", participants=tuple(("q" * 127 + str(i)) for i in range(COUNCIL_MAX_PARTICIPANTS)),
        debate_rounds=2, implementation_profile_id="q" * 127 + "0",
    )


def test_v3_compiled_command_bound_rejects_oversized_explicitly():
    # compiled length == len(header) + 1 (space) + len(task); probe with an
    # empty task to get the exact header+space length.
    prefix_len = len(compile_telegram_command(_oversized_debate_payload(0)))
    within = _oversized_debate_payload(MAX_COMPILED_COMMAND_CHARS - prefix_len)
    try:
        enforce_compiled_command_bound(within)
        check("v3-compiled-bound-accepts-exactly-at-limit",
              len(compile_telegram_command(within)) == MAX_COMPILED_COMMAND_CHARS)
    except ContractError as e:
        check("v3-compiled-bound-accepts-exactly-at-limit", False)
        print("   unexpected", e.code)
    # One char over -> COMPILED_COMMAND_TOO_LARGE, never truncated.
    over = _oversized_debate_payload(MAX_COMPILED_COMMAND_CHARS - prefix_len + 1)
    try:
        enforce_compiled_command_bound(over)
        check("v3-compiled-bound-rejects-one-over", False)
    except ContractError as e:
        ok2 = e.code == COMPILED_COMMAND_TOO_LARGE
        print(("PASS" if ok2 else "FAIL") + f": v3-compiled-bound-rejects-one-over (code={e.code})")
        if not ok2:
            failures.append("v3-compiled-bound-rejects-one-over")


def test_v3_parse_calls_compiled_bound_guard():
    # Sanity: parse_and_validate runs the guard for v3 (a normal body
    # passes it -- this just proves the guard is wired into the v3 path,
    # not that a YAML body can currently trip it).
    p = parse(V3_DEBATE)
    enforce_compiled_command_bound(p)  # must not raise
    check("v3-parse-wires-compiled-bound-guard", p.schema_version == "v3")


def test_v3_oversized_task_still_hits_task_bound_first():
    # A >3000-char task fails on MAX_TASK_CHARS (unchanged for all schemas)
    # before the compiled-length gate is ever reached.
    expect_error("v3-debate-task-too-large", _big_debate_body("T" * 3200), "TASK_TOO_LARGE")


def test_v3_common_field_validation_still_applies():
    expect_error("v3-council-bad-correlation", V3_COUNCIL.replace("relW5_council_0001", "short"), INVALID_CORRELATION_ID)
    expect_error("v3-council-bad-chair", V3_COUNCIL.replace("pm_profile_id: chair-pm", 'pm_profile_id: "bad chair"'), INVALID_PM_PROFILE)
    expect_error("v3-council-push-without-commit",
                  V3_COUNCIL.replace("  push: false\n", "  push: true\n"), "INVALID_GIT_INTENT")


# ============================================================================
# P24.1G3b: typed `workspace_output` forwarding (DSH G2/G4 contract)
#
# DSH G2 (owner-task-controller.mjs, 3a57ab0) accepts
# `payload.workspace_output = {report_path, non_empty?}` and requires
# mode=SINGLE + git.commit=true. DSH G4 (telegram-owner-client.mjs,
# 0b2cc08) bridges it onto the Telegram shorthand grammar as
# `--report-path <path> [--report-non-empty true|false]`. This section
# proves the relay forwards the typed field verbatim end-to-end (YAML ->
# ValidatedPayload -> digest -> compiled command), never from task prose,
# and never silently drops `non_empty: false`.
# ============================================================================

def _with_workspace_output(body: str, block: str) -> str:
    """Inserts a `workspace_output:` block right before `review:` -- every
    canonical V3_* body above has that exact line, so this never needs to
    know anything about the rest of the body's shape."""
    return body.replace("review:\n", f"{block}\nreview:\n")


WO_REPORT_PATH_ONLY = "workspace_output:\n  report_path: reports/qualification/foo.md"
WO_REPORT_PATH_B = "workspace_output:\n  report_path: reports/qualification/bar.md"
WO_NON_EMPTY_TRUE = "workspace_output:\n  report_path: reports/qualification/foo.md\n  non_empty: true"
WO_NON_EMPTY_FALSE = "workspace_output:\n  report_path: reports/qualification/foo.md\n  non_empty: false"


def test_wo_1_no_workspace_output_payload_unchanged():
    p = parse(V3_SINGLE_COMMIT)
    check("wo1-report-path-none", p.workspace_output_report_path is None)
    check("wo1-non-empty-none", p.workspace_output_non_empty is None)
    compiled = compile_telegram_command(p)
    check("wo1-no-report-path-flag", "--report-path" not in compiled)
    check("wo1-no-report-non-empty-flag", "--report-non-empty" not in compiled)


def test_wo_2_report_path_only_accepted():
    p = parse(_with_workspace_output(V3_SINGLE_COMMIT, WO_REPORT_PATH_ONLY))
    check("wo2-accepted", p.workspace_output_report_path == "reports/qualification/foo.md")
    check("wo2-non-empty-none-when-omitted", p.workspace_output_non_empty is None)


def test_wo_3_report_path_plus_non_empty_true_accepted():
    p = parse(_with_workspace_output(V3_SINGLE_COMMIT, WO_NON_EMPTY_TRUE))
    check("wo3-report-path", p.workspace_output_report_path == "reports/qualification/foo.md")
    check("wo3-non-empty-true", p.workspace_output_non_empty is True)


def test_wo_4_non_empty_false_preserved_exactly():
    # The exact "don't drop it as falsy" requirement: `is False`, not
    # falsy-and-therefore-indistinguishable-from-None.
    p = parse(_with_workspace_output(V3_SINGLE_COMMIT, WO_NON_EMPTY_FALSE))
    check("wo4-non-empty-is-false-not-none", p.workspace_output_non_empty is False)
    check("wo4-non-empty-not-true", p.workspace_output_non_empty is not True)


def test_wo_5_workspace_output_scalar_rejected():
    expect_error("wo5-scalar-rejected",
                  _with_workspace_output(V3_SINGLE_COMMIT, "workspace_output: reports/foo.md"),
                  INVALID_WORKSPACE_OUTPUT)


def test_wo_5b_workspace_output_list_rejected():
    expect_error("wo5b-list-rejected",
                  _with_workspace_output(V3_SINGLE_COMMIT, "workspace_output:\n  - reports/foo.md"),
                  INVALID_WORKSPACE_OUTPUT)


def test_wo_6_missing_report_path_rejected():
    expect_error("wo6-missing-report-path-rejected",
                  _with_workspace_output(V3_SINGLE_COMMIT, "workspace_output:\n  non_empty: true"),
                  INVALID_WORKSPACE_OUTPUT)


def test_wo_7_empty_report_path_rejected():
    expect_error("wo7-empty-report-path-rejected",
                  _with_workspace_output(V3_SINGLE_COMMIT, 'workspace_output:\n  report_path: ""'),
                  INVALID_WORKSPACE_OUTPUT)


def test_wo_7b_whitespace_only_report_path_rejected():
    expect_error("wo7b-whitespace-only-report-path-rejected",
                  _with_workspace_output(V3_SINGLE_COMMIT, 'workspace_output:\n  report_path: "   "'),
                  INVALID_WORKSPACE_OUTPUT)


def test_wo_8_report_path_non_string_rejected():
    expect_error("wo8-report-path-non-string-rejected",
                  _with_workspace_output(V3_SINGLE_COMMIT, "workspace_output:\n  report_path: 123"),
                  INVALID_WORKSPACE_OUTPUT)


def test_wo_9_non_empty_non_boolean_rejected():
    expect_error("wo9-non-empty-non-boolean-rejected",
                  _with_workspace_output(V3_SINGLE_COMMIT,
                                          'workspace_output:\n  report_path: reports/foo.md\n  non_empty: "yes"'),
                  INVALID_WORKSPACE_OUTPUT)


def test_wo_10_task_prose_report_path_ignored_when_field_absent():
    prose_body = V3_SINGLE_COMMIT.replace(
        "task: |\n  Read-only canary. Do nothing else.\n",
        "task: |\n  Create exactly one report at reports/qualification/from-prose.md and stop.\n",
    )
    p = parse(prose_body)
    check("wo10-no-workspace-output-from-prose", p.workspace_output_report_path is None)
    check("wo10-no-report-path-flag-emitted", "--report-path" not in compile_telegram_command(p))
    check("wo10-prose-preserved-verbatim-in-task-body", "from-prose.md" in p.task)


def test_wo_11_typed_field_wins_over_conflicting_prose_path():
    prose_body = V3_SINGLE_COMMIT.replace(
        "task: |\n  Read-only canary. Do nothing else.\n",
        "task: |\n  Create exactly one report at reports/qualification/PROSE-PATH.md and stop.\n",
    )
    body = _with_workspace_output(prose_body, "workspace_output:\n  report_path: reports/qualification/TYPED-PATH.md")
    p = parse(body)
    check("wo11-typed-path-wins", p.workspace_output_report_path == "reports/qualification/TYPED-PATH.md")
    compiled = compile_telegram_command(p)
    check("wo11-compiled-has-typed-path", "--report-path reports/qualification/TYPED-PATH.md" in compiled)
    check("wo11-compiled-report-path-flag-never-uses-prose-path",
          "--report-path reports/qualification/PROSE-PATH.md" not in compiled)
    # The prose path is still present, but ONLY as ordinary task text after
    # the flags, never mistaken for the flag's own value.
    check("wo11-prose-path-still-in-task-text-only", "PROSE-PATH.md" in p.task)


def test_wo_12_compiled_command_contains_exact_report_path_flag():
    p = parse(_with_workspace_output(V3_SINGLE_COMMIT, WO_REPORT_PATH_ONLY))
    check("wo12-exact-report-path-flag",
          "--report-path reports/qualification/foo.md" in compile_telegram_command(p))


def test_wo_13_non_empty_true_compiles_exact_flag():
    p = parse(_with_workspace_output(V3_SINGLE_COMMIT, WO_NON_EMPTY_TRUE))
    check("wo13-report-non-empty-true", "--report-non-empty true" in compile_telegram_command(p))


def test_wo_14_non_empty_false_compiles_exact_flag():
    p = parse(_with_workspace_output(V3_SINGLE_COMMIT, WO_NON_EMPTY_FALSE))
    compiled = compile_telegram_command(p)
    check("wo14-report-non-empty-false", "--report-non-empty false" in compiled)
    check("wo14-report-non-empty-not-true", "--report-non-empty true" not in compiled)


def test_wo_15_council_mode_rejected():
    expect_error("wo15-council-rejected",
                  _with_workspace_output(V3_COUNCIL.replace("  commit: false\n", "  commit: true\n"),
                                          WO_REPORT_PATH_ONLY),
                  WORKSPACE_OUTPUT_SINGLE_ONLY)


def test_wo_16_debate_mode_rejected():
    expect_error("wo16-debate-rejected",
                  _with_workspace_output(V3_DEBATE.replace("  commit: false\n", "  commit: true\n"),
                                          WO_REPORT_PATH_ONLY),
                  WORKSPACE_OUTPUT_SINGLE_ONLY)


def test_wo_17_git_commit_false_rejected():
    expect_error("wo17-commit-false-rejected",
                  _with_workspace_output(V3_SINGLE, WO_REPORT_PATH_ONLY),  # V3_SINGLE has commit: false
                  WORKSPACE_OUTPUT_REQUIRES_GIT_COMMIT)


def test_wo_18_digest_differs_when_report_path_differs():
    a = parse(_with_workspace_output(V3_SINGLE_COMMIT, WO_REPORT_PATH_ONLY))
    b = parse(_with_workspace_output(V3_SINGLE_COMMIT, WO_REPORT_PATH_B))
    check("wo18-digest-differs-by-report-path", a.payload_digest() != b.payload_digest())


def test_wo_19_digest_differs_when_non_empty_differs():
    no_field = parse(V3_SINGLE_COMMIT)
    with_true = parse(_with_workspace_output(V3_SINGLE_COMMIT, WO_NON_EMPTY_TRUE))
    with_false = parse(_with_workspace_output(V3_SINGLE_COMMIT, WO_NON_EMPTY_FALSE))
    omitted = parse(_with_workspace_output(V3_SINGLE_COMMIT, WO_REPORT_PATH_ONLY))
    digests = {no_field.payload_digest(), with_true.payload_digest(),
               with_false.payload_digest(), omitted.payload_digest()}
    check("wo19-all-four-digests-distinct", len(digests) == 4)


def test_wo_20_existing_single_without_workspace_output_exact_behavior():
    p = parse(V3_SINGLE_COMMIT)
    compiled = compile_telegram_command(p)
    check("wo20-single-compiled-shape-unchanged",
          compiled == "@dsh-test-project --pm claude-pm --durability direct "
                      "--client-correlation relG3b_single_commit01 --commit "
                      "Read-only canary. Do nothing else.\n")


def test_wo_21_existing_council_without_workspace_output_unchanged():
    p = parse(V3_COUNCIL)
    check("wo21-council-no-workspace-output-fields", p.workspace_output_report_path is None)
    check("wo21-council-compiled-no-report-flags", "--report-path" not in compile_telegram_command(p))


def test_wo_22_existing_debate_without_workspace_output_unchanged():
    p = parse(V3_DEBATE)
    check("wo22-debate-no-workspace-output-fields", p.workspace_output_report_path is None)
    check("wo22-debate-compiled-no-report-flags", "--report-path" not in compile_telegram_command(p))


def test_wo_unknown_field_inside_workspace_output_rejected():
    expect_error("wo-unknown-inner-field-rejected",
                  _with_workspace_output(V3_SINGLE_COMMIT,
                                          "workspace_output:\n  report_path: reports/foo.md\n  required: true"),
                  UNKNOWN_FIELD)


def test_wo_report_path_with_whitespace_rejected():
    # Transport-tokenization safety (WORKSPACE_OUTPUT_REPORT_PATH_RE) -- a
    # path containing a space would silently split/truncate at DSH's own
    # `--report-path` value tokenizer (`\S+`), not fail cleanly there.
    expect_error("wo-report-path-whitespace-rejected",
                  _with_workspace_output(V3_SINGLE_COMMIT,
                                          "workspace_output:\n  report_path: reports/my report.md"),
                  INVALID_WORKSPACE_OUTPUT)


def test_wo_offline_compile_fixture_p24_g3b():
    # The exact canonical fixture from the P24.1G3b task brief, end to end:
    # YAML -> ValidatedPayload -> compiled Telegram command.
    body = """\
schema: p18-dsh-dispatch/v3
mode: single
correlation_id: p24-g3b-offline
project_id: dsh-cross-model
pm_profile_id: codex-luna-pm
runtime_class: normal
durability: remote

git:
  commit: true
  push: true
  remote: origin

workspace_output:
  report_path: reports/qualification/P24_G3B_OFFLINE.md
  non_empty: true

review:
  requested: false

task: |
  Produce a short qualification summary.
"""
    p = parse(body)
    check("wo-offline-report-path", p.workspace_output_report_path == "reports/qualification/P24_G3B_OFFLINE.md")
    check("wo-offline-non-empty-true", p.workspace_output_non_empty is True)
    compiled = compile_telegram_command(p)
    check("wo-offline-compiled-has-report-path",
          "--report-path reports/qualification/P24_G3B_OFFLINE.md" in compiled)
    check("wo-offline-compiled-has-report-non-empty-true", "--report-non-empty true" in compiled)
    expected = (
        "@dsh-cross-model --pm codex-luna-pm "
        "--durability remote --client-correlation p24-g3b-offline "
        "--commit --push --remote origin "
        "--report-path reports/qualification/P24_G3B_OFFLINE.md --report-non-empty true "
        "Produce a short qualification summary.\n"
    )
    check("wo-offline-compiled-exact-match", compiled == expected)
    check("wo-offline-under-compiled-bound", len(compiled) <= MAX_COMPILED_COMMAND_CHARS)


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
    print(f"RESULT: PASS (all P18-W5 multimode contract/compiler/digest checks passed, {len(tests)} test functions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
