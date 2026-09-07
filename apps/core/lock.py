"""Distributed lock with ownership token verification and atomic release.

Prevents a worker whose lock expired from inadvertently releasing a lock
subsequently acquired by another worker.
"""

from __future__ import annotations

import logging
import uuid
from types import TracebackType
from typing import Any

from django.core.cache import cache

logger = logging.getLogger(__name__)

# Lua script to release lock atomically only if token matches
LUA_RELEASE_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# Lua script to extend lock TTL atomically only if token matches
LUA_EXTEND_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], tonumber(ARGV[2]))
else
    return 0
end
"""


class DistributedLock:
    """Atomic token-checked distributed lock backed by Django cache / Redis."""

    def __init__(self, key: str, *, token: str | None = None, ttl_seconds: int = 180) -> None:
        self.key = key
        self.token = token or uuid.uuid4().hex
        self.ttl_seconds = ttl_seconds
        self.acquired = False

    def acquire(self) -> bool:
        """Attempt to acquire lock with unique token and TTL."""
        self.acquired = bool(cache.add(self.key, self.token, timeout=self.ttl_seconds))
        return self.acquired

    def release(self) -> bool:
        """Atomically release lock only if the stored token matches our token."""
        if not self.token:
            return False

        # Attempt raw Redis Lua script execution if using redis backend
        raw_client = self._get_raw_redis_client()
        if raw_client is not None:
            try:
                # Key must be prefixed with cache key prefix if configured
                versioned_key = cache.make_key(self.key)
                res = raw_client.eval(LUA_RELEASE_LOCK, 1, versioned_key, self.token)
                self.acquired = False
                return bool(res)
            except Exception as err:
                logger.warning("Redis Lua release failed, falling back to cache compare: %s", err)

        # Fallback for LocMemCache / non-Redis backends in unit tests
        current = cache.get(self.key)
        if current == self.token:
            cache.delete(self.key)
            self.acquired = False
            return True

        self.acquired = False
        return False

    def extend(self, additional_seconds: int) -> bool:
        """Extend lock TTL if token still matches (heartbeat)."""
        raw_client = self._get_raw_redis_client()
        if raw_client is not None:
            try:
                versioned_key = cache.make_key(self.key)
                res = raw_client.eval(LUA_EXTEND_LOCK, 1, versioned_key, additional_seconds)
                return bool(res)
            except Exception as err:
                logger.warning("Redis Lua extend failed: %s", err)

        current = cache.get(self.key)
        if current == self.token:
            cache.set(self.key, self.token, timeout=additional_seconds)
            return True
        return False

    @staticmethod
    def _get_raw_redis_client() -> Any | None:
        """Extract raw redis client from django cache backend if available."""
        try:
            # django-redis style
            if hasattr(cache, "client") and hasattr(cache.client, "get_client"):
                return cache.client.get_client()
            # redis-py directly wrapped
            if hasattr(cache, "_cache") and hasattr(cache._cache, "eval"):
                return cache._cache
        except (AttributeError, KeyError):
            return None
        return None

    def __enter__(self) -> DistributedLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.release()
