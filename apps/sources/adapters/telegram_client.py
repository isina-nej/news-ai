"""Kurigram client lifecycle manager for the dedicated Telegram worker.

Design (matches the HTTPX Phase 2.1 lesson):
- One long-lived Kurigram ``Client`` per Telegram account inside a stable
  event loop owned by the dedicated ``telegram`` Celery queue
  (concurrency 1 per account).
- The client object is NEVER shared across incompatible event loops: the
  manager records the owning loop and rebuilds the client on mismatch.
- Explicit connect/start, health-check (``get_me``), disconnect/stop and
  reconnect paths. No interactive login ever happens inside workers:
  missing credentials fail fast with ``AuthenticationError``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from apps.core.redaction import sanitize_error_message
from apps.sources.adapters.base import AuthenticationError, NetworkError
from apps.sources.adapters.telegram_accounts import (
    TelegramAccountConfig,
    telegram_account_registry,
)

logger = logging.getLogger(__name__)


def _kurigram_client_class() -> Any:
    # Kurigram ships as the ``pyrogram`` top-level package (v2.2.25).
    from pyrogram import Client  # local import: rest of the system must not import Kurigram

    return Client


def _is_auth_failure(exc: BaseException) -> bool:
    try:
        from pyrogram import errors as pyro_errors

        return isinstance(exc, pyro_errors.Unauthorized)
    except Exception:
        name = type(exc).__name__
        return name in {
            "Unauthorized",
            "AuthKeyInvalid",
            "AuthKeyUnregistered",
            "AuthKeyPermEmpty",
            "SessionExpired",
            "SessionRevoked",
            "UserDeactivated",
            "UserDeactivatedBan",
        }


class TelegramClientManager:
    """Owns one Kurigram client per account_key inside a stable event loop."""

    def __init__(self, account_key: str = "default") -> None:
        self.account_key = account_key or "default"
        self._client: Any | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._started = False

    @property
    def config(self) -> TelegramAccountConfig:
        cfg = telegram_account_registry.get(self.account_key)
        cfg.validate()
        return cfg

    def _build_client(self, cfg: TelegramAccountConfig) -> Any:
        client_cls = _kurigram_client_class()
        return client_cls(
            name=f"newsai-tg-{cfg.account_key}",
            api_id=cfg.api_id,
            api_hash=cfg.api_hash,
            session_string=cfg.session_string,
            in_memory=True,
            no_updates=True,
        )

    async def _ensure_started_in_loop(self) -> Any:
        # Validate credentials BEFORE touching the vendor SDK so missing
        # env creds fail fast with AuthenticationError (no interactive login).
        self.config.validate()
        loop = asyncio.get_running_loop()
        if (
            self._client is not None
            and self._loop is not None
            and (self._loop is not loop or self._loop.is_closed())
        ):
            # Cross-loop reuse is forbidden: drop the stale client handle.
            self._client = None
            self._loop = None
            self._started = False
        if self._client is None:
            cfg = self.config  # raises AuthenticationError when incomplete
            self._client = self._build_client(cfg)
            self._loop = loop
            self._started = False
        if not self._started:
            assert self._client is not None
            try:
                await self._client.start()
            except Exception as exc:
                self._client = None
                self._loop = None
                self._started = False
                if _is_auth_failure(exc) or "API key is required" in str(exc):
                    raise AuthenticationError(
                        "Telegram session invalid/expired; re-issue TELEGRAM_SESSION_STRING."
                    ) from exc
                raise NetworkError(f"Telegram client failed to start: {exc}") from exc
            self._started = True
        assert self._client is not None
        return self._client

    async def run(self, coro_fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run ``coro_fn(client, *args, **kwargs)`` with a started client.

        FloodWait and auth errors propagate to the caller for mapping;
        unexpected errors are wrapped as NetworkError (message sanitized).
        """
        client = await self._ensure_started_in_loop()
        try:
            return await coro_fn(client, *args, **kwargs)
        except Exception as exc:
            if _is_auth_failure(exc):
                self._started = False
                raise AuthenticationError(
                    "Telegram session is invalid/expired/revoked; re-issue TELEGRAM_SESSION_STRING."
                ) from exc
            # FloodWait and friends pass through untouched (caller maps retry_after).
            name = type(exc).__name__
            if name in {"FloodWait", "FloodPremiumWait", "SlowmodeWait", "TakeoutInitDelay"}:
                raise
            raise NetworkError(f"Telegram request failed: {exc}") from exc

    async def health_check(self) -> dict[str, Any]:
        async def _ping(client: Any) -> Any:
            return await client.get_me()

        me = await self.run(_ping)
        return {
            "ok": True,
            "account_key": self.account_key,
            "user_id": getattr(me, "id", None),
            "username": getattr(me, "username", None),
        }

    async def disconnect(self) -> None:
        if self._client is not None and self._started:
            try:
                await self._client.stop()
            except Exception as exc:  # never fail shutdown on a stale socket
                logger.warning("telegram client stop failed: %s", sanitize_error_message(str(exc)))
        self._client = None
        self._loop = None
        self._started = False

    async def reconnect(self) -> None:
        await self.disconnect()


_default_managers: dict[str, TelegramClientManager] = {}
_managers_lock = threading.Lock()


def get_telegram_client_manager(account_key: str = "default") -> TelegramClientManager:
    key = (account_key or "default").strip() or "default"
    with _managers_lock:
        manager = _default_managers.get(key)
        if manager is None:
            manager = TelegramClientManager(account_key=key)
            _default_managers[key] = manager
        return manager
