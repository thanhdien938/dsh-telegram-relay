# Security Policy

## Reporting Security Vulnerabilities

Please **do not** open public GitHub issues or discussions for suspected security vulnerabilities.

If GitHub Private Vulnerability Reporting is enabled on this repository, please report vulnerabilities directly through the **Security** tab -> **Report a vulnerability**.

Alternatively, send security disclosures to the maintainers privately via GitHub Advisory Submission.

---

## Core Security Architecture & Trust Model

The DSH Telegram Relay bridges GitHub Issues to a private DSH Telegram bot using a self-hosted GitHub Actions runner.

```text
GitHub Issues (Untrusted Input)
      ↓
Issue Author & Title Gate (Workflow + Python Contract)
      ↓
Self-Hosted Runner (Private Environment)
      ↓
DSH Telegram Relay (Fail-closed Parser + State Store)
      ↓
Telegram MCP Client (Local Process)
      ↓
Telegram MTProto Transport
      ↓
DSH Runtime (Authoritative OwnerControlService)
```

### 1. The Deployment Control Repository Must Be PRIVATE
While this source repository is public, **any operational repository running the relay workflow MUST be private**.
- If a repository with an issue-triggered self-hosted runner is public, external actors could attempt to trigger your self-hosted runner.
- The workflow templates in `templates/workflows/` are designed specifically for private control repositories owned solely by authorized users.

### 2. GitHub Issue Input Is Untrusted Data
All content originating from GitHub Issues (issue title, issue body, metadata) is treated strictly as **untrusted data**:
- Issue bodies are **never** passed directly to shell command lines or interpolated into scripts.
- Only the numeric issue number and repository name are passed via environment variables.
- The Python relay layer fetches issue details directly using the GitHub API (`gh issue view`), parses structured YAML with a fail-closed schema validator, and strictly rejects unknown keys, duplicate keys, or out-of-spec characters.
- Issue text cannot choose the Telegram target, bot username, session, or execution environment.

### 3. Credential and Session Hygiene
- **Never publish Telegram session files**: Telegram session files (such as `*.session`) contain MTProto authorization tokens that grant full account access. They must never be checked into Git.
- **Never commit bot tokens or API credentials**: Store all tokens, session paths, and API keys exclusively in GitHub Actions Secrets or local `.env` files protected by `.gitignore`.
- `.gitignore` in this project excludes `.session`, `.env`, `state/`, and virtual environments by default.

### 4. Self-Hosted Runner Trust Boundary
The self-hosted runner executes within your private network. Maintain defense-in-depth:
- Run the self-hosted runner under a dedicated, least-privileged operating system account.
- Pin runner labels (`dsh-relay`) so other workflows cannot accidentally schedule jobs on it.
- Keep the relay code gate (`DSH_RELAY_ALLOWED_GITHUB_USERS`) active in Python as a secondary barrier even if workflow-level filters are modified.
