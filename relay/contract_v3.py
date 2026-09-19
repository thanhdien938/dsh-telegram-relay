"""
P18-W3 production-shaped SINGLE-task dispatch contract: strict parser,
bounded canonical-ID syntax validation (project_id/pm_profile_id
membership is NOT checked here -- see P18-RELAY-PRODUCTION-GENERALIZATION
below and the module-level comment above ID_CHARSET_RE), and canonical
Telegram command compiler.

P18-RELAY-PRODUCTION-GENERALIZATION: this module previously also
enforced a hard-coded project_id/pm_profile_id membership allowlist
(ALLOWED_PROJECT_IDS / ALLOWED_PM_PROFILE_IDS), appropriate for the P18-W3
canary/bootstrap phase when exactly two identities were proven live. Now
that DSH is at production-testing stage with multiple canonical projects
and PM profiles, that hard allowlist became operationally restrictive and
has been removed; this relay validates project_id/pm_profile_id SHAPE
only (the bounded canonical-ID charset), and DSH's OwnerControlService is
the sole authority on whether a given project/profile actually exists and
is eligible to run. This does not grant the relay any new execution
authority: task content still flows only through the existing fixed
Telegram -> DSH control path, and every other security invariant below
(author allowlist, title prefix, strict YAML, schema/mode pin, bounded
correlation ID, bounded task size, hard-pinned Telegram destination, no
eval/shell interpolation, fixed entrypoint, issue-identity immutability,
double-send/result-correlation guards, git-intent/remote rules) is
unchanged.

Canonical syntax and exact flag semantics verified by reading (not
guessing) `src/owner/telegram-owner-client.mjs` (parseOwnerFlags,
applyLifecycleFlags, PROJECT_MENTION, routeTelegramUpdate) and
`src/owner/owner-task-controller.mjs` (normalizeDurability,
normalizeGitSyncRequest, normalizeReviewRequest) in the DSH repo at
authority e91873a0428149440817ff6e37799d1b1d983be7:

  @<project_id> [--pm <pm_profile_id>] [--durability direct|local|remote]
    [--commit] [--push] [--remote <name>] [--review] <task text>

  PROJECT_MENTION charset: ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$
  --remote charset (normalizeGitSyncRequest): ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$
  durability values: direct -> DIRECT, local -> DURABLE_LOCAL, remote -> DURABLE_REMOTE
    (omitted -> DIRECT for a SHORT task; W3 always emits it explicitly --
    see COMPILE NOTE below)
  --commit / --push / --review are bare boolean flags (no value token)
  --remote only has any effect when normalizeGitSyncRequest sees push OR
    commit true (applyLifecycleFlags never even constructs `payload.git`
    otherwise, so a bare --remote with neither is silently dropped by
    DSH). This relay is DELIBERATELY STRICTER than that DSH leniency:
    - push=true with commit=false is REJECTED here (INVALID_GIT_INTENT),
      never silently upgraded the way DSH's own applyLifecycleFlags()
      would (`if(flags.commit||flags.push){payload.git={commit:true,...`).
    - a `remote` value is REJECTED here (INVALID_REMOTE) unless push is
      also true, never silently dropped.
  DSH remains the sole final authority; every rule enforced in this
  module is an additive fail-closed layer in front of it, not a
  replacement for OwnerControlService's own validation.

COMPILE NOTE: durability is a REQUIRED field in this contract (unlike the
raw Telegram syntax, where omitting --durability defaults to DIRECT for a
SHORT task) and is always compiled explicitly as --durability <value>, so
the produced command is fully self-describing and never depends on a
default that could change on the DSH side later. commit/push/remote/
review are compiled only when actually requested, per the plan's own
"compile only the minimum flags actually requested" rule.

P18-W5 MULTIMODE (p18-dsh-dispatch/v3): a NEW, additive schema value that
extends the contract from SINGLE-only to SINGLE + COUNCIL + DEBATE.
`p18-dsh-dispatch/v1` and `p18-dsh-dispatch/v2` are UNTOUCHED -- their
meaning, field sets, digests, compiler output and result-matching are
byte-for-byte what they always were (v1 = legacy NORMAL SINGLE with a
model-visible correlation footer; v2 = SINGLE NORMAL/LONG with transport-
only --client-correlation and ACK/terminal task_id correlation). v3 adds:

  mode: single | council | debate

  * single  -- semantically identical to v2 SINGLE. Required:
               runtime_class (normal|long). Forbidden: participants,
               debate_rounds, implementation_profile_id.
  * council -- P7 COUNCIL. `pm_profile_id` is the chair. Required:
               participants (a non-empty YAML sequence of canonical
               profile ids). Forbidden: runtime_class, debate_rounds,
               implementation_profile_id. Compiles to native
               `--pm <chair> --debate <p1,p2,...>` and NOTHING else
               mode-specific (never --debate-extend / --debate-rounds /
               --implementation / --long).
  * debate  -- P19 Debate extension layered on COUNCIL. Required:
               participants + debate_rounds (1|2). Optional:
               implementation_profile_id (must be one of participants).
               Forbidden: runtime_class. Compiles to native
               `--pm <chair> --debate <p1,p2,...> --debate-extend
               --debate-rounds <1|2> [--implementation <id>]`.

Native grammar / participant semantics verified by reading (not guessing)
`parseOwnerFlags()` in src/owner/telegram-owner-client.mjs and
`normalizeCouncilSpec()` in src/pm/council/council-contracts.mjs of the
DSH repo at authority 4c547dbffd9a4154e98880ca9d8f6b435fcad0c8:

  - participants: non-empty; each a canonical profile id
    (^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ -- the SAME charset DSH's
    boundedProfileId() enforces); DUPLICATES rejected
    (DSH COUNCIL_DUPLICATE_PARTICIPANT); at most COUNCIL_MAX_PARTICIPANTS
    (4, DSH's own bound); the chair MAY also appear as a participant
    (DSH explicitly enforces NO chair/participant uniqueness constraint).
  - debate rounds: exactly 1 or 2 (DSH DEBATE_MIN_ROUNDS/MAX_ROUNDS).
    Unlike native DSH (where --debate-rounds is optional and defaults to
    2), this relay contract REQUIRES it explicitly for a debate payload,
    the same way it already requires --durability explicitly -- the
    produced command never depends on a DSH-side default. This is an
    additive fail-closed strictness, never a redefinition of native.
  - implementation participant: optional, explicit only; must already be
    one of THIS payload's participants (DSH
    COUNCIL_UNKNOWN_IMPLEMENTATION_PARTICIPANT). Never inferred, never
    auto-selected.

No LONG Council/Debate exists (native has none -- see task_rule.md); a
runtime_class field on a council/debate payload is rejected.

Correlation stays TRANSPORT-ONLY for every v3 mode: `correlation_id`
compiles to `--client-correlation <id>` and is NEVER injected into the
model-visible task prose (no v3 correlation footer -- same as v2).

Final compiled Telegram command length is bounded (MAX_COMPILED_COMMAND_
CHARS) so a long council/debate header plus task body can never silently
exceed Telegram's per-message limit; an oversized payload is rejected
explicitly (COMPILED_COMMAND_TOO_LARGE), never truncated.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Typed validation failures (stable string codes)
# ---------------------------------------------------------------------------

INVALID_SCHEMA = "INVALID_SCHEMA"
INVALID_MODE = "INVALID_MODE"
UNAUTHORIZED_AUTHOR = "UNAUTHORIZED_AUTHOR"
UNKNOWN_FIELD = "UNKNOWN_FIELD"
INVALID_PROJECT = "INVALID_PROJECT"
INVALID_PM_PROFILE = "INVALID_PM_PROFILE"
INVALID_DURABILITY = "INVALID_DURABILITY"
INVALID_GIT_INTENT = "INVALID_GIT_INTENT"
INVALID_REMOTE = "INVALID_REMOTE"
EMPTY_TASK = "EMPTY_TASK"
TASK_TOO_LARGE = "TASK_TOO_LARGE"
INVALID_CORRELATION_ID = "INVALID_CORRELATION_ID"
PAYLOAD_MUTATED = "PAYLOAD_MUTATED"
ALREADY_DELIVERED = "ALREADY_DELIVERED"
AMBIGUOUS_RESULT = "AMBIGUOUS_RESULT"
# P18-W4: v2-only -- an explicit runtime_class field present but not
# exactly "normal"/"long" (case-insensitive).
INVALID_RUNTIME_CLASS = "INVALID_RUNTIME_CLASS"
# P18-W5 multimode (v3-only) typed failures.
INVALID_PARTICIPANTS = "INVALID_PARTICIPANTS"
INVALID_DEBATE_ROUNDS = "INVALID_DEBATE_ROUNDS"
INVALID_IMPLEMENTATION_PROFILE = "INVALID_IMPLEMENTATION_PROFILE"
COMPILED_COMMAND_TOO_LARGE = "COMPILED_COMMAND_TOO_LARGE"
# P24.1G3b: v3 SINGLE-only typed `workspace_output` request (DSH G2/G4
# contract, forwarded to `--report-path`/`--report-non-empty`).
# INVALID_WORKSPACE_OUTPUT is this relay's own SHAPE-only failure (not a
# mapping / bad report_path / bad non_empty). WORKSPACE_OUTPUT_SINGLE_ONLY
# and WORKSPACE_OUTPUT_REQUIRES_GIT_COMMIT deliberately reuse DSH's OWN
# OwnerControlError code names (owner-task-controller.mjs) verbatim -- this
# relay fails closed on the same two structural conditions DSH would
# certainly reject anyway (defense-in-depth, never a redefinition of DSH's
# own policy), so an issue author sees the identical code either way.
INVALID_WORKSPACE_OUTPUT = "INVALID_WORKSPACE_OUTPUT"
WORKSPACE_OUTPUT_SINGLE_ONLY = "WORKSPACE_OUTPUT_SINGLE_ONLY"
WORKSPACE_OUTPUT_REQUIRES_GIT_COMMIT = "WORKSPACE_OUTPUT_REQUIRES_GIT_COMMIT"


class ContractError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Canonical-ID syntax validation (P18-RELAY-PRODUCTION-GENERALIZATION).
#
# Historically (P18-W3 canary/bootstrap) this module also enforced a hard
# membership allowlist here (ALLOWED_PROJECT_IDS / ALLOWED_PM_PROFILE_IDS,
# provenance: docs/phase17/P17_W1_ASYNC_RESULT_CORRELATION_REPORT.md and a
# live `/aliases` capture from P18-W0/W1) that only let through the exact
# two identities proven live at the time. That was an appropriate
# bootstrap safety gate for a single-canary phase, but at production-
# testing stage it became operationally restrictive: it blocks every
# legitimate canonical DSH project/PM profile other than the original two
# (e.g. `codex-sol-pm`), and every new one would require a
# relay code change just to keep this module's list in sync with DSH's
# own project/profile registry -- exactly the "another runtime mirror of
# DSH identities in the relay" this module's authority split forbids.
#
# The membership allowlist has been REMOVED. This relay now validates
# project_id/pm_profile_id SHAPE ONLY (the bounded canonical-ID charset
# below); it does not and must not know which project/profile IDs
# currently exist. DSH's OwnerControlService remains the sole authority
# on whether a syntactically valid project_id/pm_profile_id actually
# exists and is eligible to run -- a syntactically valid but nonexistent
# ID is expected to pass this relay and be rejected by DSH itself. This
# module never invents a relay-side mirror/registry of valid identities
# to compensate (no "UNKNOWN_PROFILE" list, no re-added membership set).
#
# ALLOWED_REMOTES is unrelated to this change (Git remote authority, not
# project/profile identity) and is intentionally retained unchanged.
# ---------------------------------------------------------------------------

ALLOWED_REMOTES = frozenset({"origin"})

# P18-W4 Part B: p18-dsh-dispatch/v1 keeps meaning EXACTLY what it always
# has -- NORMAL inline task text, correlation-marker-in-result-body
# correlation, no `runtime_class` field (an attempt to add one to a v1
# payload is UNKNOWN_FIELD, unchanged from before this wave). v2 is a
# NEW, additive schema value with one new required field
# (`runtime_class: normal|long`) and no model-visible correlation footer
# (see build_task_with_footer()/compile_telegram_command() below) --
# never a silent redefinition of what v1 itself accepts or means.
REQUIRED_SCHEMA_V1 = "p18-dsh-dispatch/v1"
REQUIRED_SCHEMA_V2 = "p18-dsh-dispatch/v2"
# P18-W5: additive third schema value -- SINGLE + COUNCIL + DEBATE. v1/v2
# keep their exact historical single-only meaning.
REQUIRED_SCHEMA_V3 = "p18-dsh-dispatch/v3"
SUPPORTED_SCHEMAS = frozenset({REQUIRED_SCHEMA_V1, REQUIRED_SCHEMA_V2, REQUIRED_SCHEMA_V3})
# v1/v2 are hard-pinned to this single value (unchanged). v3 carries its
# own `mode` vocabulary below.
REQUIRED_MODE = "single"

# P18-W5: v3 `mode` vocabulary. "single" is semantically identical to v2
# SINGLE; "council"/"debate" are the additive multimode values.
V3_MODE_SINGLE = "single"
V3_MODE_COUNCIL = "council"
V3_MODE_DEBATE = "debate"
V3_MODES = frozenset({V3_MODE_SINGLE, V3_MODE_COUNCIL, V3_MODE_DEBATE})

# P18-W5: mirrors DSH src/pm/council/council-contracts.mjs
# COUNCIL_MAX_PARTICIPANTS exactly -- a council can never legitimately need
# more distinct participants than there are product backends. Relay is
# SHAPE-authoritative only; DSH's OwnerControlService/normalizeCouncilSpec
# remains the authority on whether each id actually exists / is active.
COUNCIL_MAX_PARTICIPANTS = 4
# Mirrors DSH DEBATE_MIN_ROUNDS / DEBATE_MAX_ROUNDS.
DEBATE_ROUNDS_VALUES = frozenset({1, 2})

RUNTIME_CLASS_MAP = {"normal": "normal", "long": "long"}

ID_CHARSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")  # PROJECT_MENTION's charset, reused for pm_profile_id too
REMOTE_CHARSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")  # normalizeGitSyncRequest's charset
CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{6,128}$")

DURABILITY_MAP = {"direct": "direct", "local": "local", "remote": "remote"}

MAX_TASK_CHARS = 3000  # bounded well under Telegram's ~4096-char message limit, leaving room for flags + footer

# P18-W5: the FINAL compiled Telegram command (header flags + task body)
# must fit in a single Telegram message. DSH's own telegram-owner-client
# uses TELEGRAM_MESSAGE_CHAR_LIMIT=4096 with a TELEGRAM_MESSAGE_CHUNK_BUDGET
# =4000 reserve; a command that would be chunked/split cannot be parsed as
# one owner command (flags must all be at the front of ONE message), so the
# relay validates the whole compiled length against the SAME 4000 budget
# and rejects an oversized payload explicitly (COMPILED_COMMAND_TOO_LARGE)
# rather than letting DSH silently chunk it. Council/Debate headers are
# materially longer (up to 4 participant ids + chair + debate flags), which
# is exactly why a task-body-only bound (MAX_TASK_CHARS) is not sufficient
# on its own for v3.
MAX_COMPILED_COMMAND_CHARS = 4000

TOP_LEVEL_FIELDS_V1 = frozenset({
    "schema", "mode", "correlation_id", "project_id", "pm_profile_id",
    "durability", "git", "review", "task",
})
# P18-W4 Part B: v2 adds exactly one new REQUIRED top-level field.
TOP_LEVEL_FIELDS_V2 = TOP_LEVEL_FIELDS_V1 | {"runtime_class"}
# P18-W5: v3 shares the v1 common set; the mode-specific fields are added
# per-mode in parse_and_validate() (single -> +runtime_class;
# council -> +participants; debate -> +participants +debate_rounds
# +implementation_profile_id[optional]). Anything outside the resolved
# per-mode set fails closed as UNKNOWN_FIELD -- so e.g. `participants` on a
# single payload, or `runtime_class` on a council payload, is rejected.
TOP_LEVEL_FIELDS_V3_COMMON = TOP_LEVEL_FIELDS_V1
GIT_FIELDS = frozenset({"commit", "push", "remote"})
REVIEW_FIELDS = frozenset({"requested"})
# P24.1G3b: relay is SHAPE-authoritative only for workspace_output, exactly
# like every other v3 field above -- DSH's OwnerTaskController
# (normalizeWorkspaceOutputRequest, submit()) remains the sole authority on
# path containment / `.git` denial / symlink safety / artifact_v1
# eligibility; none of that is duplicated here. `required` is never an
# accepted input field -- it is DSH's own internal normalization only (see
# module docstring / owner-task-controller.mjs), so it is deliberately
# EXCLUDED from WORKSPACE_OUTPUT_FIELDS (present -> UNKNOWN_FIELD, never
# silently accepted-and-ignored).
WORKSPACE_OUTPUT_FIELDS = frozenset({"report_path", "non_empty"})
# Transport-tokenization safety only (NOT path/filesystem security): DSH's
# `--report-path` flag captures its value with a plain `\S+` token
# (telegram-owner-client.mjs's generic value-flag regex) -- a report_path
# containing whitespace would silently split into a truncated flag value
# plus stray trailing text fed back into the command parser, not a clean
# rejection. Rejecting whitespace here is the same class of guard as
# ID_CHARSET_RE/REMOTE_CHARSET_RE above: keeping the compiled command
# well-formed, never a second opinion on DSH's own path-safety policy.
WORKSPACE_OUTPUT_REPORT_PATH_RE = re.compile(r"^\S+$")


@dataclass
class ValidatedPayload:
    correlation_id: str
    project_id: str
    pm_profile_id: str
    durability: str            # "direct" | "local" | "remote"
    git_commit: bool
    git_push: bool
    git_remote: Optional[str]
    review_requested: bool
    task: str
    # P18-W4 Part B: "v1" | "v2" -- which schema this payload was parsed
    # under. P18-W5 adds "v3". Drives build_task_with_footer()/
    # compile_telegram_command()'s per-schema behavior and payload_digest()'s
    # schema-specific field inclusion below. Never itself part of the digest
    # (it is derived from the already-digested `schema` field's value, not
    # independent data).
    schema_version: str = "v1"
    # P18-W4 Part A/B: "normal" | "long" -- always "normal" for a v1
    # payload (v1 has no such field and continues to mean NORMAL inline
    # behavior, unchanged). Required and explicit for v2 and for v3 SINGLE.
    # Always "normal" for v3 council/debate (no LONG Council/Debate exists).
    runtime_class: str = "normal"
    # P18-W5 v3-only. "single" for every v1/v2 payload (and a v3 single
    # payload); "council"/"debate" only ever for a v3 multimode payload.
    mode: str = "single"
    # P18-W5 v3 council/debate only -- the ordered, deduplicated,
    # shape-validated participant profile id list. Empty tuple for SINGLE.
    # ORDER is semantically meaningful (it is the exact order compiled into
    # `--debate p1,p2,...` and the order DSH executes participant stages
    # in), so it is serialized in order into the digest -- reordering
    # participants changes the digest and therefore fails the issue
    # mutation guard.
    participants: tuple = ()
    # P18-W5 v3 debate only -- exactly 1 or 2. None for single/council.
    debate_rounds: Optional[int] = None
    # P18-W5 v3 debate only, optional -- an explicit implementation
    # participant (must be one of `participants`). None when not supplied
    # (never inferred, never auto-selected).
    implementation_profile_id: Optional[str] = None
    # P24.1G3b v3 SINGLE only, optional -- the typed, owner-authored
    # `workspace_output` request (DSH G2/G4 contract), forwarded verbatim
    # (never derived from `task`). `workspace_output_report_path` is None
    # whenever the field was omitted entirely -- the ONE flag this class
    # uses everywhere else (`implementation_profile_id`, `git_remote`, ...)
    # to distinguish "not requested" from "requested with this value".
    # `workspace_output_non_empty` is None when omitted OR when
    # `workspace_output` itself is absent; it is never defaulted here --
    # DSH owns that default (see module docstring / G2's
    # normalizeWorkspaceOutputRequest()).
    workspace_output_report_path: Optional[str] = None
    workspace_output_non_empty: Optional[bool] = None

    def payload_digest(self) -> str:
        """Stable digest over every validated field -- changing ANY of
        them (including correlation_id) after first acceptance changes
        this digest, which is exactly what powers the PAYLOAD_MUTATED /
        correlation-id-changed immutability guard.

        P18-W4 Part B: `runtime_class` is included in the digest ONLY for
        a v2 payload -- a v1 payload's digest is BYTE-FOR-BYTE UNCHANGED
        from before this wave (v1 has no such field; including a constant
        "normal" for it would still be a silent behavior change to every
        historically-accepted v1 issue's digest, which is exactly what
        Part B's "do not silently mutate v1" instruction forbids)."""
        fields = {
            "correlation_id": self.correlation_id, "project_id": self.project_id,
            "pm_profile_id": self.pm_profile_id, "durability": self.durability,
            "git_commit": self.git_commit, "git_push": self.git_push, "git_remote": self.git_remote,
            "review_requested": self.review_requested, "task": self.task,
        }
        if self.schema_version == "v2":
            fields["runtime_class"] = self.runtime_class
        # P18-W5: a v3 payload's digest additionally serializes every
        # semantically meaningful multimode field -- `mode`, and per mode:
        # single -> runtime_class; council/debate -> participants (IN
        # ORDER); debate -> debate_rounds + implementation_profile_id. A v1
        # or v2 payload's digest is BYTE-FOR-BYTE UNCHANGED (this whole
        # block is skipped for them), preserving historical compatibility.
        # Changing the chair is already covered (pm_profile_id, above).
        elif self.schema_version == "v3":
            fields["mode"] = self.mode
            if self.mode == V3_MODE_SINGLE:
                fields["runtime_class"] = self.runtime_class
            else:
                fields["participants"] = list(self.participants)
            if self.mode == V3_MODE_DEBATE:
                fields["debate_rounds"] = self.debate_rounds
                fields["implementation_profile_id"] = self.implementation_profile_id
            # P24.1G3b: only ever included when workspace_output was
            # actually present in the issue -- a v3 payload that never
            # mentions it keeps a BYTE-FOR-BYTE identical digest to before
            # this field existed (the two keys are simply absent, not
            # present-with-null), so no historically-accepted v3 issue's
            # identity is ever silently mutated by this addition. When
            # present, report_path and non_empty each independently
            # participate -- report_path A vs B, or non_empty true vs
            # false vs omitted (None), all produce different digests.
            if self.workspace_output_report_path is not None:
                fields["workspace_output_report_path"] = self.workspace_output_report_path
                fields["workspace_output_non_empty"] = self.workspace_output_non_empty
        canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Strict YAML parsing -- duplicate keys are a hard error (PyYAML's default
# safe_load silently keeps only the LAST of a duplicate key, which is
# exactly the "duplicate fields -> REJECTED" hole this closes). safe_load
# already refuses arbitrary Python object construction (no code
# execution); this only adds duplicate-key detection on top of it.
# ---------------------------------------------------------------------------

class _StrictSafeLoader(yaml.SafeLoader):
    pass


def _no_duplicate_keys_constructor(loader: yaml.SafeLoader, node):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            raise ContractError(INVALID_SCHEMA, f"duplicate field: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys_constructor,
)


def _strict_yaml_load(body: str) -> dict:
    try:
        data = yaml.load(body, Loader=_StrictSafeLoader)
    except ContractError:
        raise
    except yaml.YAMLError as e:
        raise ContractError(INVALID_SCHEMA, f"malformed YAML body: {e}") from e
    if not isinstance(data, dict):
        raise ContractError(INVALID_SCHEMA, "body must be a YAML mapping")
    return data


def _require_bool(value, field_name: str, code: str) -> bool:
    # Explicit isinstance check -- YAML's safe_load already distinguishes
    # a real `true`/`false` scalar (-> Python bool) from a quoted "true"
    # (-> Python str), but we check it explicitly anyway so a `1`/`0`
    # (-> Python int, YAML 1.1 also treats those as bool-ish in some
    # loaders) can never silently pass either. `isinstance(x, bool)` is
    # checked before any int check since bool is an int subclass in
    # Python.
    if not isinstance(value, bool):
        raise ContractError(code, f"{field_name} must be a real boolean (true/false), got {value!r}")
    return value


# ---------------------------------------------------------------------------
# Top-level validation
# ---------------------------------------------------------------------------

def parse_and_validate(title: str, body: str, *, required_title_prefix: str | tuple[str, ...],
                        author: str, expected_author: str | set[str] | list[str] | tuple[str, ...] | None) -> ValidatedPayload:
    if not expected_author:
        raise ContractError(UNAUTHORIZED_AUTHOR, "No authorized GitHub users configured")

    if isinstance(expected_author, str):
        allowed = {expected_author.strip()}
    elif isinstance(expected_author, (set, list, tuple)):
        allowed = {str(a).strip() for a in expected_author if str(a).strip()}
    else:
        raise ContractError(UNAUTHORIZED_AUTHOR, "Invalid expected_author configuration")

    if not allowed or "*" in allowed:
        raise ContractError(UNAUTHORIZED_AUTHOR, "Invalid or wildcard expected_author configuration")

    if not author or author not in allowed:
        raise ContractError(UNAUTHORIZED_AUTHOR, f"author={author!r}")

    if isinstance(required_title_prefix, (str, tuple)):
        if not (title or "").startswith(required_title_prefix):
            raise ContractError(INVALID_SCHEMA, f"title prefix mismatch: {title!r}")
    elif isinstance(required_title_prefix, (list, set)):
        if not any((title or "").startswith(p) for p in required_title_prefix):
            raise ContractError(INVALID_SCHEMA, f"title prefix mismatch: {title!r}")
    else:
        raise ContractError(INVALID_SCHEMA, f"invalid required_title_prefix: {required_title_prefix!r}")

    data = _strict_yaml_load(body or "")

    # P18-W4 Part B: schema determines which top-level field set (and
    # required-ness of `runtime_class`) applies -- resolved BEFORE the
    # unknown/missing-field checks below, so a v1 issue that adds
    # `runtime_class` still fails UNKNOWN_FIELD (v1's meaning is
    # untouched) and a v2 issue that omits it still fails with a clear
    # missing-required-field error, never silently defaulting either way.
    schema = data.get("schema")
    # v3_mode is only meaningful for schema_version == "v3"; kept None
    # otherwise so the v1/v2 flow below is byte-for-byte unchanged.
    v3_mode = None
    required_fields = None
    if schema == REQUIRED_SCHEMA_V1:
        schema_version = "v1"
        top_level_fields = TOP_LEVEL_FIELDS_V1
    elif schema == REQUIRED_SCHEMA_V2:
        schema_version = "v2"
        top_level_fields = TOP_LEVEL_FIELDS_V2
    elif schema == REQUIRED_SCHEMA_V3:
        # P18-W5: for v3 the applicable field set depends on `mode`, so
        # `mode` is resolved and validated HERE, before the unknown/missing
        # checks -- a v3 single payload that adds `participants`, or a v3
        # council payload that adds `runtime_class`/`debate_rounds`, still
        # fails UNKNOWN_FIELD (mode-specific fields fail closed); a v3
        # council payload that omits `participants`, or a v3 debate payload
        # that omits `debate_rounds`, still fails with a clear missing-
        # required-field error.
        schema_version = "v3"
        v3_mode = data.get("mode")
        if v3_mode not in V3_MODES:
            raise ContractError(INVALID_MODE, f"mode={v3_mode!r} (v3 supports {sorted(V3_MODES)})")
        if v3_mode == V3_MODE_SINGLE:
            required_fields = TOP_LEVEL_FIELDS_V3_COMMON | {"runtime_class"}
            optional_fields = frozenset({"workspace_output"})
        elif v3_mode == V3_MODE_COUNCIL:
            required_fields = TOP_LEVEL_FIELDS_V3_COMMON | {"participants"}
            # P24.1G3b: `workspace_output` is accepted SYNTACTICALLY here
            # too (not left to fall through to generic UNKNOWN_FIELD) so
            # the mode guard below can fail closed with the specific,
            # DSH-matching WORKSPACE_OUTPUT_SINGLE_ONLY code instead of a
            # generic one -- it is still always rejected for council.
            optional_fields = frozenset({"workspace_output"})
        else:  # V3_MODE_DEBATE
            required_fields = TOP_LEVEL_FIELDS_V3_COMMON | {"participants", "debate_rounds"}
            optional_fields = frozenset({"implementation_profile_id", "workspace_output"})
        top_level_fields = required_fields | optional_fields
    else:
        raise ContractError(INVALID_SCHEMA, f"schema={schema!r} (must be one of {sorted(SUPPORTED_SCHEMAS)})")

    unknown = set(data) - top_level_fields
    if unknown:
        raise ContractError(UNKNOWN_FIELD, f"unknown top-level field(s): {sorted(unknown)}")
    # For v3, only the mode-specific REQUIRED set must be present (an
    # optional field like `implementation_profile_id` may be absent); v1/v2
    # keep their exact all-fields-required check.
    _required_present_set = required_fields if schema_version == "v3" else top_level_fields
    missing = _required_present_set - set(data)
    if missing:
        raise ContractError(INVALID_SCHEMA, f"missing required field(s): {sorted(missing)}")

    # v1/v2 hard-pin mode to 'single'. v3's `mode` was already validated
    # against V3_MODES above.
    if schema_version != "v3" and data.get("mode") != REQUIRED_MODE:
        raise ContractError(INVALID_MODE, f"mode={data.get('mode')!r} (only 'single' is supported for v1/v2)")

    correlation_id = data.get("correlation_id")
    if not isinstance(correlation_id, str) or not CORRELATION_ID_RE.match(correlation_id):
        raise ContractError(INVALID_CORRELATION_ID, f"correlation_id={correlation_id!r}")

    # Shape-only validation. DSH's OwnerControlService is the sole
    # authority on whether this project_id/pm_profile_id actually exists
    # and is eligible to run -- see the module-level comment above. A
    # syntactically valid ID is allowed through here even if no such
    # project/profile currently exists; only the charset is enforced.
    project_id = data.get("project_id")
    if not isinstance(project_id, str) or not ID_CHARSET_RE.match(project_id):
        raise ContractError(INVALID_PROJECT, f"project_id={project_id!r} (bad charset)")

    pm_profile_id = data.get("pm_profile_id")
    if not isinstance(pm_profile_id, str) or not ID_CHARSET_RE.match(pm_profile_id):
        raise ContractError(INVALID_PM_PROFILE, f"pm_profile_id={pm_profile_id!r} (bad charset)")

    durability_raw = data.get("durability")
    if not isinstance(durability_raw, str) or durability_raw.lower() not in DURABILITY_MAP:
        raise ContractError(INVALID_DURABILITY, f"durability={durability_raw!r} (must be direct|local|remote)")
    durability = DURABILITY_MAP[durability_raw.lower()]

    git_raw = data.get("git")
    if not isinstance(git_raw, dict):
        raise ContractError(INVALID_GIT_INTENT, "git must be a mapping with commit/push/remote")
    git_unknown = set(git_raw) - GIT_FIELDS
    if git_unknown:
        raise ContractError(UNKNOWN_FIELD, f"unknown git field(s): {sorted(git_unknown)}")
    if set(git_raw) != GIT_FIELDS:
        raise ContractError(INVALID_GIT_INTENT, f"git must specify exactly {sorted(GIT_FIELDS)}")
    git_commit = _require_bool(git_raw["commit"], "git.commit", INVALID_GIT_INTENT)
    git_push = _require_bool(git_raw["push"], "git.push", INVALID_GIT_INTENT)
    git_remote_raw = git_raw["remote"]
    if git_remote_raw is not None and not isinstance(git_remote_raw, str):
        raise ContractError(INVALID_REMOTE, f"git.remote must be a string or null, got {git_remote_raw!r}")

    # This relay's OWN stricter rules (see module docstring) -- fail
    # closed rather than reuse DSH's lenient auto-upgrade/silent-drop.
    if git_push and not git_commit:
        raise ContractError(INVALID_GIT_INTENT, "push=true requires commit=true")
    if git_remote_raw is not None:
        if not git_push:
            raise ContractError(INVALID_REMOTE, "remote is illegal unless push=true")
        if not REMOTE_CHARSET_RE.match(git_remote_raw):
            raise ContractError(INVALID_REMOTE, f"remote={git_remote_raw!r} (bad charset)")
        if git_remote_raw not in ALLOWED_REMOTES:
            raise ContractError(INVALID_REMOTE, f"remote={git_remote_raw!r} not in allowlist {sorted(ALLOWED_REMOTES)}")
    git_remote = git_remote_raw

    review_raw = data.get("review")
    if not isinstance(review_raw, dict):
        raise ContractError(INVALID_SCHEMA, "review must be a mapping with requested")
    review_unknown = set(review_raw) - REVIEW_FIELDS
    if review_unknown:
        raise ContractError(UNKNOWN_FIELD, f"unknown review field(s): {sorted(review_unknown)}")
    if set(review_raw) != REVIEW_FIELDS:
        raise ContractError(INVALID_SCHEMA, f"review must specify exactly {sorted(REVIEW_FIELDS)}")
    review_requested = _require_bool(review_raw["requested"], "review.requested", INVALID_SCHEMA)

    task_raw = data.get("task")
    if not isinstance(task_raw, str):
        raise ContractError(EMPTY_TASK, f"task must be a string, got {type(task_raw).__name__}")
    if not task_raw.strip():
        raise ContractError(EMPTY_TASK, "task is empty or whitespace-only")
    if len(task_raw) > MAX_TASK_CHARS:
        raise ContractError(TASK_TOO_LARGE, f"task is {len(task_raw)} chars, max {MAX_TASK_CHARS}")

    # P18-W4 Part A/B: v2-only, required. `runtime_class: long` is what
    # ultimately compiles `--long` onto the canonical Telegram command
    # (compile_telegram_command() below) -- DSH's OwnerTaskController
    # remains the sole authority on what LONG actually means/costs; this
    # relay only ever forwards the owner's explicit request, never infers
    # it from task text/length (never touched here).
    # runtime_class is a REQUIRED, explicit field for v2 and for v3 SINGLE.
    # It is FORBIDDEN for v3 council/debate (rejected earlier as
    # UNKNOWN_FIELD -- there is no LONG Council/Debate) and is always
    # "normal" for them and for v1.
    if schema_version == "v2" or (schema_version == "v3" and v3_mode == V3_MODE_SINGLE):
        runtime_class_raw = data.get("runtime_class")
        if not isinstance(runtime_class_raw, str) or runtime_class_raw.lower() not in RUNTIME_CLASS_MAP:
            raise ContractError(INVALID_RUNTIME_CLASS, f"runtime_class={runtime_class_raw!r} (must be normal|long)")
        runtime_class = RUNTIME_CLASS_MAP[runtime_class_raw.lower()]
    else:
        runtime_class = "normal"

    # ------------------------------------------------------------------
    # P18-W5: v3 mode-specific fields. Relay is SHAPE-authoritative only
    # (charset/count/uniqueness/membership) -- DSH's OwnerControlService /
    # normalizeCouncilSpec is the authority on whether each id actually
    # exists and is an active, eligible profile. No task-body
    # interpolation happens anywhere here.
    # ------------------------------------------------------------------
    mode = v3_mode if schema_version == "v3" else V3_MODE_SINGLE
    participants: tuple = ()
    debate_rounds: Optional[int] = None
    implementation_profile_id: Optional[str] = None

    if schema_version == "v3" and mode in (V3_MODE_COUNCIL, V3_MODE_DEBATE):
        participants_raw = data.get("participants")
        if not isinstance(participants_raw, list) or isinstance(participants_raw, bool):
            raise ContractError(INVALID_PARTICIPANTS, "participants must be a YAML sequence")
        if len(participants_raw) == 0:
            raise ContractError(INVALID_PARTICIPANTS, "participants must not be empty")
        if len(participants_raw) > COUNCIL_MAX_PARTICIPANTS:
            raise ContractError(INVALID_PARTICIPANTS,
                                 f"{len(participants_raw)} participants, max {COUNCIL_MAX_PARTICIPANTS}")
        seen: set = set()
        norm_participants: list = []
        for entry in participants_raw:
            if not isinstance(entry, str) or not ID_CHARSET_RE.match(entry):
                raise ContractError(INVALID_PARTICIPANTS, f"participant {entry!r} (bad charset)")
            if entry in seen:
                # Mirrors DSH normalizeCouncilSpec()'s
                # COUNCIL_DUPLICATE_PARTICIPANT.
                raise ContractError(INVALID_PARTICIPANTS, f"duplicate participant: {entry!r}")
            seen.add(entry)
            norm_participants.append(entry)
        # The chair (pm_profile_id) MAY also appear as a participant -- DSH
        # explicitly enforces no chair/participant uniqueness constraint,
        # so this relay does not either.
        participants = tuple(norm_participants)

    if schema_version == "v3" and mode == V3_MODE_DEBATE:
        debate_rounds_raw = data.get("debate_rounds")
        # A real YAML integer only -- reject bool (YAML `true`/`false` are
        # bool, not int) and strings like "1".
        if isinstance(debate_rounds_raw, bool) or not isinstance(debate_rounds_raw, int) \
                or debate_rounds_raw not in DEBATE_ROUNDS_VALUES:
            raise ContractError(INVALID_DEBATE_ROUNDS,
                                 f"debate_rounds={debate_rounds_raw!r} (must be 1 or 2)")
        debate_rounds = debate_rounds_raw

        if "implementation_profile_id" in data:
            impl_raw = data.get("implementation_profile_id")
            if not isinstance(impl_raw, str) or not ID_CHARSET_RE.match(impl_raw):
                raise ContractError(INVALID_IMPLEMENTATION_PROFILE,
                                     f"implementation_profile_id={impl_raw!r} (bad charset)")
            if impl_raw not in participants:
                # Mirrors DSH COUNCIL_UNKNOWN_IMPLEMENTATION_PARTICIPANT --
                # it must already be one of THIS payload's participants;
                # never inferred, never auto-selected.
                raise ContractError(INVALID_IMPLEMENTATION_PROFILE,
                                     f"implementation_profile_id={impl_raw!r} is not one of participants "
                                     f"{list(participants)}")
            implementation_profile_id = impl_raw

    # ------------------------------------------------------------------
    # P24.1G3b: v3-only typed `workspace_output` request (DSH G2/G4
    # contract: payload.workspace_output = {report_path, non_empty?}).
    # SHAPE-only validation -- see WORKSPACE_OUTPUT_FIELDS' own comment for
    # why `required` is never an accepted input. Absent entirely -> both
    # fields stay None and every downstream code path (digest, compiler)
    # is a byte-for-byte no-op, exactly like every other optional v3 field.
    # ------------------------------------------------------------------
    workspace_output_report_path: Optional[str] = None
    workspace_output_non_empty: Optional[bool] = None
    if schema_version == "v3" and "workspace_output" in data:
        wo_raw = data.get("workspace_output")
        if not isinstance(wo_raw, dict):
            raise ContractError(INVALID_WORKSPACE_OUTPUT, f"workspace_output must be a mapping, got {type(wo_raw).__name__}")
        wo_unknown = set(wo_raw) - WORKSPACE_OUTPUT_FIELDS
        if wo_unknown:
            raise ContractError(UNKNOWN_FIELD, f"unknown workspace_output field(s): {sorted(wo_unknown)}")
        if "report_path" not in wo_raw:
            raise ContractError(INVALID_WORKSPACE_OUTPUT, "workspace_output.report_path is required")
        report_path_raw = wo_raw["report_path"]
        if not isinstance(report_path_raw, str) or not report_path_raw.strip():
            raise ContractError(INVALID_WORKSPACE_OUTPUT,
                                 f"workspace_output.report_path must be a non-empty string, got {report_path_raw!r}")
        if not WORKSPACE_OUTPUT_REPORT_PATH_RE.match(report_path_raw):
            raise ContractError(INVALID_WORKSPACE_OUTPUT,
                                 f"workspace_output.report_path must not contain whitespace, got {report_path_raw!r}")
        # Forwarded byte-for-byte -- never trimmed/normalized (matches DSH
        # G2's normalizeWorkspaceOutputRequest(), which stores raw.report_path
        # verbatim; the `.strip()` above is a shape CHECK only).
        workspace_output_report_path = report_path_raw
        if "non_empty" in wo_raw:
            workspace_output_non_empty = _require_bool(
                wo_raw["non_empty"], "workspace_output.non_empty", INVALID_WORKSPACE_OUTPUT)

        # Defense-in-depth mode/git guards (task §6): DSH's own
        # OwnerTaskController.submit() already refuses these two exact
        # conditions (WORKSPACE_OUTPUT_SINGLE_ONLY /
        # WORKSPACE_OUTPUT_REQUIRES_GIT_COMMIT) -- failing fast here avoids
        # ever dispatching a request DSH is certain to reject, without
        # duplicating any of DSH's deeper path-safety/artifact_v1 policy.
        if mode != V3_MODE_SINGLE:
            raise ContractError(WORKSPACE_OUTPUT_SINGLE_ONLY,
                                 f"workspace_output is only supported for mode=single, got mode={mode!r}")
        if not git_commit:
            raise ContractError(WORKSPACE_OUTPUT_REQUIRES_GIT_COMMIT,
                                 "workspace_output requires git.commit=true")

    payload = ValidatedPayload(
        correlation_id=correlation_id, project_id=project_id, pm_profile_id=pm_profile_id,
        durability=durability, git_commit=git_commit, git_push=git_push, git_remote=git_remote,
        review_requested=review_requested, task=task_raw,
        schema_version=schema_version, runtime_class=runtime_class,
        mode=mode, participants=participants, debate_rounds=debate_rounds,
        implementation_profile_id=implementation_profile_id,
        workspace_output_report_path=workspace_output_report_path,
        workspace_output_non_empty=workspace_output_non_empty,
    )

    # P18-W5: final compiled-command length bound. Only enforced for v3
    # (v1/v2 headers are short and their existing MAX_TASK_CHARS bound
    # already keeps the whole message comfortably under Telegram's limit;
    # applying a new gate to them could only ever be a behavior change).
    # For v3 the council/debate header can be long, so the ACTUAL final
    # message length is validated, not just the task body.
    if schema_version == "v3":
        enforce_compiled_command_bound(payload)

    return payload


def enforce_compiled_command_bound(payload: ValidatedPayload) -> None:
    """P18-W5: fail closed (COMPILED_COMMAND_TOO_LARGE) if the FULL compiled
    Telegram command (header flags + verbatim task body) would exceed
    MAX_COMPILED_COMMAND_CHARS. Never truncates task text or participant
    ids. This is a real invariant on the produced message, independent of
    (and stricter, in aggregate, than) the per-field MAX_TASK_CHARS bound:
    with today's field bounds a v3 council header is short enough that
    MAX_TASK_CHARS alone keeps the message in range, but this guard means a
    future widening of MAX_TASK_CHARS or the id charset can never silently
    produce an un-sendable / DSH-would-chunk command."""
    compiled = compile_telegram_command(payload)
    if len(compiled) > MAX_COMPILED_COMMAND_CHARS:
        raise ContractError(
            COMPILED_COMMAND_TOO_LARGE,
            f"compiled Telegram command is {len(compiled)} chars, max {MAX_COMPILED_COMMAND_CHARS} "
            "(task text and participant ids are never silently truncated)",
        )


# ---------------------------------------------------------------------------
# Correlation footer -- relay-owned, appended AFTER the literal owner task.
# The owner's task text is never altered; nothing in `task` can change
# what marker actually gets required for correlation, because the footer
# is built here from the ALREADY-VALIDATED correlation_id, never from any
# substring of `task` itself.
#
# P18-W4 Part C: v1 ONLY. For v2, the whole point of task_id-based terminal
# correlation (relay/result_collector.py's scan_terminal_candidates_by_
# task_id()) is that the model no longer needs to carry ANY relay
# transport metadata in its answer -- correlation_id is GitHub/relay
# identity, never part of the PM objective. This function is never called
# for a v2 payload; see compile_telegram_command() below.
# ---------------------------------------------------------------------------

def build_task_with_footer(payload: ValidatedPayload) -> str:
    footer = (
        "\n\n[P18 relay metadata]\n"
        "Include this exact correlation marker in your final result:\n"
        f"{payload.correlation_id}"
    )
    return payload.task + footer


# ---------------------------------------------------------------------------
# Canonical Telegram command compiler
# ---------------------------------------------------------------------------

def compile_telegram_command(payload: ValidatedPayload) -> str:
    # P18-W5: v3 has its own mode-aware compiler. v1/v2 fall through to the
    # exact pre-W5 logic below, byte-for-byte unchanged.
    if payload.schema_version == "v3":
        return _compile_v3_command(payload)
    parts = [f"@{payload.project_id}", "--pm", payload.pm_profile_id,
              "--durability", payload.durability]
    # P18-W4 Part A: only ever compiled for v2 (`runtime_class` is always
    # "normal" for v1 -- v1 has no such field, see ValidatedPayload's own
    # docstring). "normal" compiles no flag at all, matching the pre-W4
    # canonical Telegram syntax exactly.
    if payload.runtime_class == "long":
        parts.append("--long")
    # P18-W4 ACK-causal-correlation remediation (Part D): v2 ALWAYS
    # compiles --client-correlation using the value ALREADY validated as
    # `correlation_id` above -- never a second, user-supplied v2 YAML
    # field (there is no separate "client_correlation" input; the owner's
    # GitHub-issue correlation_id itself becomes DSH's transport
    # correlation fact). This is what gives v2's ACK recovery
    # (result_collector.py's parse_accepted_ack()/
    # scan_ack_candidates_by_correlation()) an authoritative anchor,
    # replacing the retired "some bot message newer than X" assumption.
    # v1 never compiles this flag at all.
    if payload.schema_version == "v2":
        parts.extend(["--client-correlation", payload.correlation_id])
    if payload.git_commit:
        parts.append("--commit")
    if payload.git_push:
        parts.append("--push")
    if payload.git_remote:
        parts.extend(["--remote", payload.git_remote])
    if payload.review_requested:
        parts.append("--review")
    header = " ".join(parts)
    # P18-W4 Part C: v2 sends the owner's literal task text verbatim -- no
    # relay-owned correlation footer at all. The model sees only the
    # user's actual task text; terminal correlation is by DSH's own
    # task_id (Part D/E), never by a marker the model must remember to
    # echo back.
    task_text = payload.task if payload.schema_version == "v2" else build_task_with_footer(payload)
    return f"{header} {task_text}"


# ---------------------------------------------------------------------------
# P18-W5: v3 multimode Telegram command compiler.
#
# Emits ONLY native DSH grammar (verified against parseOwnerFlags() in
# src/owner/telegram-owner-client.mjs @ 4c547db, and against the P24.1G4
# `--report-path`/`--report-non-empty` bridge @ 0b2cc08):
#
#   single  : @<proj> --pm <pm> --durability <d> [--long]
#             --client-correlation <corr> [git] [--report-path <p>
#             [--report-non-empty true|false]] [--review] <task>
#   council : @<proj> --pm <chair> --debate <p1,p2,...> --durability <d>
#             --client-correlation <corr> [git/review] <task>
#   debate  : @<proj> --pm <chair> --debate <p1,p2,...> --debate-extend
#             --debate-rounds <1|2> [--implementation <id>]
#             --durability <d> --client-correlation <corr> [git/review] <task>
#
# Never emits --debate-extend / --debate-rounds / --implementation for a
# council payload, and never emits --long for council/debate. `--report-path`/
# `--report-non-empty` are SINGLE-only in practice (parse_and_validate()
# already refuses `workspace_output` for council/debate, WORKSPACE_OUTPUT_
# SINGLE_ONLY, before a payload with one could ever reach this function --
# see `workspace_output_report_path`'s own field comment), so this compiler
# never needs its own mode check for them. The task body is passed VERBATIM
# -- no correlation footer for any v3 mode (correlation is transport-only,
# carried entirely by --client-correlation). Participant ids are joined
# deterministically with a plain "," and are already charset-validated (no
# shell metacharacters, no whitespace) by parse_and_validate(); report_path
# is similarly already whitespace-rejected by parse_and_validate() (P24.1G3b,
# WORKSPACE_OUTPUT_REPORT_PATH_RE) before it can ever reach here.
# ---------------------------------------------------------------------------

def _compile_v3_command(payload: ValidatedPayload) -> str:
    parts = [f"@{payload.project_id}", "--pm", payload.pm_profile_id]

    if payload.mode in (V3_MODE_COUNCIL, V3_MODE_DEBATE):
        parts.extend(["--debate", ",".join(payload.participants)])
    if payload.mode == V3_MODE_DEBATE:
        parts.extend(["--debate-extend", "--debate-rounds", str(payload.debate_rounds)])
        if payload.implementation_profile_id:
            parts.extend(["--implementation", payload.implementation_profile_id])

    parts.extend(["--durability", payload.durability])

    # --long is SINGLE-only and only for runtime_class=long. council/debate
    # never reach here with runtime_class != "normal".
    if payload.mode == V3_MODE_SINGLE and payload.runtime_class == "long":
        parts.append("--long")

    # Every v3 mode ALWAYS compiles transport-only --client-correlation
    # from the already-validated correlation_id -- this is the sole
    # correlation channel (no footer, no model-visible marker).
    parts.extend(["--client-correlation", payload.correlation_id])

    if payload.git_commit:
        parts.append("--commit")
    if payload.git_push:
        parts.append("--push")
    if payload.git_remote:
        parts.extend(["--remote", payload.git_remote])
    # P24.1G3b: a no-op (both fields None) for every payload that never set
    # `workspace_output` -- byte-for-byte unchanged compiled command for
    # every pre-existing issue shape (SINGLE/COUNCIL/DEBATE alike).
    if payload.workspace_output_report_path:
        parts.extend(["--report-path", payload.workspace_output_report_path])
        if payload.workspace_output_non_empty is not None:
            parts.extend(["--report-non-empty", "true" if payload.workspace_output_non_empty else "false"])
    if payload.review_requested:
        parts.append("--review")

    header = " ".join(parts)
    return f"{header} {payload.task}"
