"""Session client helpers. Env-only session loading; no DB/log persistence."""

from __future__ import annotations

from typing import Any


class EnvSessionClient:
    """Minimal injected client wrapper. Real HTTP client optional dependency.

    Production wiring may substitute a maintained session library behind this
    protocol. This module never stores credentials and never logs them.
    """

    def __init__(self, *, session: str) -> None:
        if not session:
            raise ValueError("Twitter session is not configured")
        self._session = session

    def fetch_user_tweets(self, *, username: str, cursor: str | None, limit: int) -> dict[str, Any]:
        raise NotImplementedError(
            "Configure a maintained Twitter session client; no live call in CI"
        )


class FakeTwitterClient:
    def __init__(self, pages: list[dict[str, Any]] | None = None) -> None:
        self.pages = pages or []
        self.calls: list[dict[str, Any]] = []

    def fetch_user_tweets(self, *, username: str, cursor: str | None, limit: int) -> dict[str, Any]:
        self.calls.append({"username": username, "cursor": cursor, "limit": limit})
        if not self.pages:
            return {"tweets": [], "next_cursor": None}
        index = 0
        if cursor:
            for i, page in enumerate(self.pages):
                if page.get("cursor") == cursor:
                    index = i + 1
                    break
        if index >= len(self.pages):
            return {"tweets": [], "next_cursor": None}
        page = self.pages[index]
        return {"tweets": page.get("tweets", []), "next_cursor": page.get("next_cursor")}
