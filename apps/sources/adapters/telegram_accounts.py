"""Telegram account registry: map non-secret account_key to env-only credentials."""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from apps.sources.adapters.base import AuthenticationError


@dataclass(frozen=True)
class TelegramAccountConfig:
    """Runtime credentials for one Telegram user account. Never persisted."""

    account_key: str
    api_id: int
    api_hash: str
    session_string: str

    def validate(self) -> None:
        if not self.api_id or not self.api_hash or not self.session_string:
            raise AuthenticationError(
                "Telegram account credentials are incomplete "
                "(TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION_STRING)."
            )


class TelegramAccountRegistry:
    """Resolve account_key from Source.configuration to env-backed credentials."""

    ENV_ACCOUNT_KEY = "TELEGRAM_ACCOUNT_KEY"

    def get(self, account_key: str = "") -> TelegramAccountConfig:
        key = (account_key or "").strip() or getattr(settings, self.ENV_ACCOUNT_KEY, "default")
        default_key = getattr(settings, self.ENV_ACCOUNT_KEY, "default") or "default"
        if key != default_key:
            raise AuthenticationError(
                f"Unknown Telegram account_key '{key}'. "
                f"Only account_key '{default_key}' is configured in this deployment."
            )
        return TelegramAccountConfig(
            account_key=key,
            api_id=int(getattr(settings, "TELEGRAM_API_ID", 0) or 0),
            api_hash=str(getattr(settings, "TELEGRAM_API_HASH", "") or ""),
            session_string=str(getattr(settings, "TELEGRAM_SESSION_STRING", "") or ""),
        )


telegram_account_registry = TelegramAccountRegistry()
