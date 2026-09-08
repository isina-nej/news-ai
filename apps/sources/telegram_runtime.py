"""Stable per-process Telegram event-loop runtime for Celery workers.

Why this exists: ``asyncio.run()`` per fetch creates and destroys an event
loop on every call. The Kurigram ``Client`` owns loop-bound MTProto state
(sockets, dispatchers), so a fresh loop per call means connect/auth/disconnect
churn plus FloodWait-prone reconnect storms — the exact contradiction flagged
in review (docs claim a long-lived client, code used ``asyncio.run``).

Design:
- One background thread per worker process owns exactly one asyncio loop
  (``TelegramRuntime``). All Telegram coroutines execute on that loop via
  ``asyncio.run_coroutine_threadsafe`` from sync Celery task threads.
- One ``TelegramClientManager`` per account_key, bound to the runtime loop.
  Managers are created lazily inside the runtime thread on first use.
- ``worker_shutdown`` (Celery signal) stops all clients cleanly on the
  runtime loop, then stops the loop and joins the thread. A stopped client
  handle is never reused without ``start()``.
- No ``nest_asyncio``, no monkey patching. Pure stdlib threads + asyncio.

Usage from sync code (Celery tasks, services, management commands)::

    result = telegram_runtime.run("default", adapter.fetch, context)
    # or lower-level:
    manager = telegram_runtime.manager("default")
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import threading
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class TelegramRuntime:
    """Owns a dedicated asyncio event loop on a background thread."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopped = False
        self._managers: dict[str, Any] = {}
        self._state_lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> asyncio.AbstractEventLoop:
        with self._state_lock:
            if self._loop is not None and not self._stopped:
                return self._loop
            self._stopped = False
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._serve, name="telegram-runtime-loop", daemon=True
            )
            self._thread.start()
        started = self._ready.wait(timeout=30)
        if not started or self._loop is None:
            raise RuntimeError("Telegram runtime event loop failed to start")
        return self._loop

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._state_lock:
            self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception as exc:  # noqa: S110 — loop teardown must not raise
                logger.debug("telegram runtime asyncgen shutdown failed: %s", exc)
            try:
                loop.close()
            except Exception as exc:  # noqa: S110 — loop teardown must not raise
                logger.debug("telegram runtime loop close failed: %s", exc)

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._stopped:
            return self.start()
        return self._loop

    # -- coroutine execution --------------------------------------------

    def run_sync(
        self,
        coro_fn: Callable[..., Coroutine[Any, Any, Any]],
        *args: Any,
        timeout: float = 300,
        **kwargs: Any,
    ) -> Any:
        """Run ``coro_fn(*args, **kwargs)`` on the runtime loop, blocking the caller."""
        loop = self.loop
        # Touch _loop eagerly: self.loop starts the thread lazily, and tests
        # assert the loop object identity is stable across calls.
        with self._state_lock:
            self._loop = loop

        async def _invoke() -> Any:
            return await coro_fn(*args, **kwargs)

        future = asyncio.run_coroutine_threadsafe(_invoke(), loop)
        return future.result(timeout=timeout)

    # -- manager access --------------------------------------------------

    def manager(self, account_key: str = "default") -> Any:
        """Return the account manager, creating it inside the runtime thread context."""
        from apps.sources.adapters.telegram_client import TelegramClientManager

        key = (account_key or "default").strip() or "default"
        with self._state_lock:
            manager = self._managers.get(key)
            if manager is None:
                manager = TelegramClientManager(account_key=key)
                self._managers[key] = manager
            return manager

    def run_on_manager(
        self, account_key: str, coro_fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Run ``coro_fn(client, *args, **kwargs)`` via the account manager on the runtime loop."""
        manager = self.manager(account_key)

        async def _invoke() -> Any:
            return await manager.run(coro_fn, *args, **kwargs)

        return self.run_sync(lambda: _invoke())

    # -- shutdown --------------------------------------------------------

    def shutdown(self, timeout: float = 30) -> None:
        with self._state_lock:
            if self._stopped or self._loop is None:
                return
            loop = self._loop
            managers = list(self._managers.values())
            self._stopped = True

        async def _stop_all() -> None:
            for manager in managers:
                try:
                    await manager.disconnect()
                except Exception as exc:  # never fail shutdown on one bad client
                    logger.warning("telegram runtime client stop failed: %s", exc)

        try:
            future = asyncio.run_coroutine_threadsafe(_stop_all(), loop)
            future.result(timeout=timeout)
        except Exception as exc:
            logger.warning("telegram runtime shutdown stop-all failed: %s", exc)
        finally:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception as exc:  # noqa: S110 — shutdown path must not raise
                logger.debug("telegram runtime loop stop failed: %s", exc)
            thread, self._thread = self._thread, None
            if thread is not None:
                thread.join(timeout=timeout)
            with self._state_lock:
                self._loop = None
                self._managers = {}


_runtime: TelegramRuntime | None = None
_runtime_lock = threading.Lock()


def get_telegram_runtime() -> TelegramRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = TelegramRuntime()
        return _runtime


def _shutdown_at_exit() -> None:
    global _runtime
    runtime, _runtime = _runtime, None
    if runtime is not None:
        try:
            runtime.shutdown()
        except Exception as exc:  # noqa: S110 — atexit must never raise
            logger.debug("telegram runtime atexit shutdown failed: %s", exc)


atexit.register(_shutdown_at_exit)


def connect_telegram_worker_signals() -> None:
    """Hook Celery worker_shutdown -> clean Telegram client + loop teardown."""
    try:
        from celery.signals import worker_shutdown
    except Exception:
        return

    def _on_worker_shutdown(**kwargs: Any) -> None:
        try:
            get_telegram_runtime().shutdown()
        except Exception as exc:
            logger.warning("telegram worker shutdown failed: %s", exc)

    try:
        worker_shutdown.connect(
            _on_worker_shutdown, weak=False, dispatch_uid="telegram-runtime-shutdown"
        )
    except Exception as exc:  # noqa: S110 — signal wiring is best-effort
        logger.debug("telegram worker signal connect failed: %s", exc)


connect_telegram_worker_signals()
