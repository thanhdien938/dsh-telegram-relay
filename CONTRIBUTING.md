# Contributing to DSH Telegram Relay

Thank you for your interest in contributing to the DSH Telegram Relay!

## Development Guidelines

1. **Defense-in-Depth**:
   - The relay must always fail closed.
   - Any new contract field must have strict syntax validation, bounded character sets, and explicit rejection of unknown/duplicate keys.
   - Never allow GitHub Issue text to dictate execution commands, shell arguments, or Telegram destinations.

2. **Testing**:
   - Every change must include comprehensive unit and integration tests.
   - Run the test suite before submitting:
     ```powershell
     $env:PYTHONPATH = "relay"
     pytest -v relay
     ```

3. **No Private Secrets or Machine Paths**:
   - Never commit operational credentials, personal emails, internal IPs, Telegram session files, or host-specific absolute paths.
   - Test fixtures must always use generic identifiers (`alice`, `bob`, `example-org/relay-repo`, `demo-project`).

4. **Pull Requests**:
   - Open a pull request against `main`.
   - Ensure GitHub Actions CI passes across all supported Python versions and OS platforms.
