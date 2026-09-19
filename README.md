# DSH Telegram Relay

[![CI](https://github.com/thanhdien938/dsh-telegram-relay/actions/workflows/ci.yml/badge.svg)](https://github.com/thanhdien938/dsh-telegram-relay/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)

Official companion repository for **[DSH (Distributed Software House)](https://github.com/thanhdien938/dsh-cross-model)**.

The **DSH Telegram Relay** bridges structured GitHub Issues to a private Telegram-controlled DSH runtime, enabling authorized owners to trigger autonomous single-agent tasks, council decisions, and multi-model debates remotely from GitHub with durable dispatch idempotency, causal ACK recovery, and terminal result correlation.

---

## Architecture Overview

```text
PUBLIC COMPANION:
dsh-cross-model + dsh-telegram-relay
        ↓ (Source code, workflow templates, documentation)
USER PRIVATE DEPLOYMENT:
Your own PRIVATE relay control repo
        ↓ (Typed GitHub Issues opened by authorized owners)
Self-Hosted Runner (Official Baseline: Windows)
        ↓ (Invokes relay/dispatch_v3.py entrypoint)
Telegram MCP Client (Local stdio JSON-RPC)
        ↓ (Encrypted MTProto transport)
Your DSH Telegram Bot / Runtime (OwnerControlService)
        ↓ (Executes task across multi-model council/debate)
Async Result Collector (Polling Telegram MCP get_history)
        ↓ (Correlates terminal result by task_id and marker)
Terminal Result Comment posted back to GitHub Issue
```

---

## Security Invariants & Trust Model

> [!IMPORTANT]
> **SECURITY REQUIREMENT: DEPLOYMENT REPOSITORY MUST BE PRIVATE**
> While the relay source code is public, **the GitHub repository where you deploy the issue-triggered relay workflow MUST be private**.
> If you deploy an issue-triggered workflow on a public repository, anyone could open an issue and trigger execution on your self-hosted runner.

Key security layers enforced by the relay:

1. **Defense-in-Depth Author Authorization**:
   - Both the GitHub Actions workflow `if:` condition AND the authoritative Python contract gate (`contract_v3.py`) enforce the configured allowlist (`DSH_RELAY_ALLOWED_GITHUB_USERS_JSON`).
   - Uses exact JSON array matching (`contains(fromJson(...), github.event.issue.user.login)`) rather than substring matching to eliminate authorization bypass vulnerabilities (e.g. username `alice` cannot be triggered by `malice`). Usernames are matched exactly and are case-sensitive to match GitHub's login format.
   - Fails closed if no authorized users are configured, if the JSON array is malformed, or if wildcards (`*`) are provided.
2. **Untrusted Data Boundary**:
   - Issue text is **never** passed as shell arguments or interpolated into PowerShell/Bash commands.
   - Only numeric issue numbers and repository identifiers are passed via environment variables.
   - The issue body is fetched directly inside Python via `gh issue view` and strictly parsed using YAML with duplicate-key rejection.
3. **Immutable Issue Identity & Mutation Guard**:
   - The relay triggers only on `issues: [opened]`, never on `edited`.
   - On re-runs, the issue identity store validates the payload digest. Any edit after opening causes the run to fail closed (`PAYLOAD_MUTATED`) to prevent issues from becoming hidden command channels.
4. **Durable Send Idempotency**:
   - State machine (`state_machine.py`) tracks states: `RESERVED`, `SENT`, `COMPLETED`, `FAILED_RETRYABLE`, `FAILED_TERMINAL`.
   - Prevents duplicate dispatches if workflows re-run or restart.
5. **Destination Pinning & Fail-Closed Behavior**:
   - Telegram destination target is hard-pinned via trusted configuration (`DSH_RELAY_TELEGRAM_TARGET`).
   - Missing, empty, or whitespace-only target configuration fails closed before any Telegram transport call or dispatch reservation occurs. Zero implicit default fallback.
   - Issue bodies cannot specify or override the destination bot, session, or chat ID.
6. **Causal ACK & Result Correlation**:
   - Recovers authoritative task IDs from post-boundary bot ACKs matching the dispatch correlation ID.
   - Avoids race conditions with concurrently running tasks in the same Telegram chat.

---

## Prerequisites & Platform Support

> [!NOTE]
> **OFFICIAL DEPLOYMENT BASELINE: Windows Self-Hosted Runner**
> The relay Python code and unit test suite are cross-platform and validated on both Windows and Linux in CI. However, the provided production deployment workflow template (`templates/workflows/relay-v3.yml`) is specifically authored for a **Windows self-hosted runner** (utilizing PowerShell shell execution). A Linux deployment template may be added in a later release.

1. **DSH Core Runtime**: Installed and operational with Telegram owner transport enabled (see [dsh-cross-model](https://github.com/thanhdien938/dsh-cross-model)).
2. **Python 3.10+**: Installed on your runner machine.
3. **Telegram MCP (`telegram-mcp`)**: An MCP server providing Telegram MTProto client tools (`send_message`, `get_history`).
4. **GitHub CLI (`gh`)**: Installed and authenticated on the runner machine (`gh auth login`).
5. **Self-Hosted GitHub Actions Runner**: Windows runner machine configured with labels `self-hosted`, `windows`, and `dsh-relay` (or a custom runner label configured via `vars.DSH_RELAY_RUNNER_LABEL`).

---

## Setup Guide

### Step 1: Create Your Private Relay Control Repository

1. On GitHub, click **Use this template** on this repository (or clone and push to a new **Private** repository).
2. Ensure the new repository visibility is set to **Private**.

### Step 2: Set Up Python and Telegram MCP on Runner

On your self-hosted runner machine:

```powershell
# 1. Create a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install relay dependencies
pip install -r requirements.txt

# 3. Install telegram-mcp
pip install telegram-mcp
```

Log in to your Telegram user account via telegram-mcp:
```powershell
telegram-mcp-auth
# Complete the phone number and SMS/2FA prompt
# Verify that session files (*.session) are saved in a secure directory
```

### Step 3: Register a Self-Hosted Runner

1. In your private relay control repository, go to **Settings** -> **Actions** -> **Runners** -> **New self-hosted runner**.
2. Select **Windows** as the runner OS. Download and configure the runner on your machine.
3. By default, the workflow template requests labels `self-hosted`, `windows`, and `dsh-relay`. If you wish to use a custom label, assign it to the runner and configure `DSH_RELAY_RUNNER_LABEL` in repository variables (Step 4).
4. Start the runner service.

### Step 4: Configure Repository Variables

In your private control repository, go to **Settings** -> **Secrets and variables** -> **Actions** -> **Variables**:

| Variable Name | Description | Example |
|---|---|---|
| `DSH_RELAY_ALLOWED_GITHUB_USERS_JSON` | **Required.** JSON array of authorized GitHub usernames (exact match, case-sensitive) | `["your-github-handle"]` |
| `DSH_RELAY_TELEGRAM_TARGET` | **Required.** Pinned Telegram bot username for your DSH bot (fails closed if missing, empty, or whitespace-only) | `your_dsh_bot` |
| `DSH_RELAY_RUNNER_LABEL` | *(Optional)* Custom runner label matching `runs-on: [self-hosted, windows, <label>]`. Default: `dsh-relay` | `dsh-relay` |
| `DSH_RELAY_ALLOWED_GITHUB_USERS` | *(Legacy fallback)* Comma-separated list of authorized usernames | `your-github-handle` |
| `DSH_RELAY_TELEGRAM_MCP_EXE` | *(Optional)* Path to `telegram-mcp.exe` | `C:\path\to\.venv\Scripts\telegram-mcp.exe` |
| `DSH_RELAY_PYTHON_EXE` | *(Optional)* Path to Python executable | `C:\path\to\.venv\Scripts\python.exe` |
| `DSH_RELAY_STATE_DIR` | *(Optional)* Directory for durable state files | `state` |

*(Optional Secret)*: If your `telegram-mcp` session uses an external `.env` file, store its path in repository Secret `TELEGRAM_ENV_PATH`.

### Step 5: Enable the Relay Workflow

Copy the deployment template into active workflows:

```powershell
New-Item -ItemType Directory -Path .github/workflows -Force
Copy-Item templates/workflows/relay-v3.yml .github/workflows/relay-v3.yml
git add .github/workflows/relay-v3.yml
git commit -m "Enable private DSH relay workflow"
git push origin main
```

---

## Issue Contract & Examples

All relay issues must start with the title prefix `[DSH-TASK]` and contain strict structured YAML.

### Example 1: SINGLE Mode (Single-Agent Task)

```yaml
schema: p18-dsh-dispatch/v3
mode: single
correlation_id: my_task_20260919_01
project_id: dsh-cross-model
pm_profile_id: codex-pm
runtime_class: normal
durability: direct
git:
  commit: false
  push: false
  remote: null
review:
  requested: false
task: |
  Inspect failing unit test in tests/test_parser.py and provide fix recommendations.
```

### Example 2: COUNCIL Mode (Multi-Agent Consensus)

In `council` mode, `pm_profile_id` acts as the chair, and `participants` lists profile IDs for the council:

```yaml
schema: p18-dsh-dispatch/v3
mode: council
correlation_id: my_council_20260919_02
project_id: dsh-cross-model
pm_profile_id: claude-chair
participants:
  - claude-chair
  - codex-pm
  - gemini-pm
durability: direct
git:
  commit: false
  push: false
  remote: null
review:
  requested: false
task: |
  Evaluate architectural options for database schema migration to PostgreSQL 17.
```

### Example 3: DEBATE Mode (Multi-Round Debate + Implementation)

In `debate` mode, multiple rounds of debate occur before a selected implementation profile executes the result:

```yaml
schema: p18-dsh-dispatch/v3
mode: debate
correlation_id: my_debate_20260919_03
project_id: dsh-cross-model
pm_profile_id: claude-chair
participants:
  - claude-chair
  - codex-pm
debate_rounds: 2
implementation_profile_id: codex-pm
durability: direct
git:
  commit: true
  push: true
  remote: origin
review:
  requested: true
task: |
  Refactor API error handling middleware to follow RFC 7807 Problem Details.
```

### Example 4: Typed `workspace_output` (Automated Qualification Report)

For `single` mode tasks with `git.commit: true`, you can request typed report file validation:

```yaml
schema: p18-dsh-dispatch/v3
mode: single
correlation_id: my_report_task_04
project_id: dsh-cross-model
pm_profile_id: codex-pm
runtime_class: normal
durability: direct
git:
  commit: true
  push: false
  remote: null
review:
  requested: false
workspace_output:
  report_path: reports/audit_summary.md
  non_empty: true
task: |
  Run dependency vulnerability scan and write results to reports/audit_summary.md.
```

---

## Recovery & Late Settlement

- **ACK Recovery**: If Telegram experiences latency during dispatch, `dispatch_v3.py` polls `get_history` across a bounded window (default: 60s) to authoritatively correlate the bot's ACK with the dispatch correlation ID.
- **Late Settlement**: If a long task exceeds the standard collection deadline, it transitions to `TIMEOUT`. When the task completes later, run `relay/recover_late_v3.py` to scan history, confirm the terminal state, and post the final resolution comment.
- **Duplicate Send Protection**: Any attempt to re-dispatch an already-delivered or active task is intercepted and rejected without sending duplicate commands to Telegram.

---

## Troubleshooting

1. **`UNAUTHORIZED_AUTHOR`**: The issue author does not match `DSH_RELAY_ALLOWED_GITHUB_USERS_JSON`. Verify that your exact GitHub username is listed in the JSON array (case-sensitive, e.g. `["MyUser"]` does not match `myuser`).
2. **`INVALID_SCHEMA`**: The issue title does not start with `[DSH-TASK]`, or the body YAML is missing required fields.
3. **`PAYLOAD_MUTATED`**: An accepted issue was edited. Issue bodies cannot be modified after initial dispatch. Open a new issue with a new `correlation_id`.
4. **`telegram-mcp` connection errors**: Ensure the Telegram session is authenticated (`telegram-mcp-auth`) and `DSH_RELAY_TELEGRAM_MCP_EXE` points to the valid executable.

---

## License

This project is licensed under the [MIT License](LICENSE).
