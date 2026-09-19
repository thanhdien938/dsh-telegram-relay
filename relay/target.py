"""Trusted Telegram destination target configuration for DSH Relay."""
from __future__ import annotations

import os


class TelegramTargetError(ValueError):
    """Raised when DSH_RELAY_TELEGRAM_TARGET is missing, empty, or whitespace-only."""


def get_required_telegram_target(env: dict[str, str] | None = None) -> str:
    """Returns the trusted Telegram destination target bot username.

    Normalizes by stripping whitespace and any leading '@'.
    Fails closed by raising TelegramTargetError if DSH_RELAY_TELEGRAM_TARGET
    is unset, empty, whitespace-only, or contains only '@'.
    """
    source = env if env is not None else os.environ
    raw = source.get("DSH_RELAY_TELEGRAM_TARGET", "")
    target = raw.strip().lstrip("@").strip()
    if not target:
        raise TelegramTargetError(
            "DSH_RELAY_TELEGRAM_TARGET is required but unset, empty, or whitespace-only. "
            "Configure DSH_RELAY_TELEGRAM_TARGET with your pinned DSH bot username."
        )
    return target
