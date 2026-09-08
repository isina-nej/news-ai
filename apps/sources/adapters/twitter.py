"""Twitter/X session-based read-only ingestion adapter.

Conservative production posture:
- Read-only via an injected session client (no posting, no bypass automation).
- Adapter holds no ORM, no credentials; session comes from env-injected client.
- Retweets are observational signals, never independent confirmations.
- Quote tweets ingest as independent SourceItems with referenced metadata.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, Protocol

from apps.core.redaction import sanitize_error_message
from apps.sources.adapters.base import (
    AuthenticationError,
    FetchContext,
    FetchedItem,
    FetchResult,
    NetworkError,
    RateLimitError,
    SourceAdapter,
)


class TwitterSessionClient(Protocol):
    def fetch_user_tweets(self, *, username: str, cursor: str | None, limit: int) -> dict[str, Any]:
        """Return {tweets: [...], next_cursor: str|None}."""
        ...


def _safe_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            text = str(value).replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _tweet_url(username: str, tweet_id: str) -> str:
    user = (username or "").lstrip("@").strip()
    if user and tweet_id:
        return f"https://x.com/{user}/status/{tweet_id}"
    return ""


def tweet_to_fetched_item(tweet: dict[str, Any], *, default_author: str = "") -> FetchedItem | None:
    """Map one raw tweet dict to FetchedItem. Unavailable metrics stay None."""
    tweet_id = str(tweet.get("id") or tweet.get("tweet_id") or "").strip()
    text = str(tweet.get("text") or tweet.get("full_text") or "").strip()
    if not tweet_id or not text:
        return None

    author_obj = tweet.get("author") or {}
    if isinstance(author_obj, dict):
        username = str(
            author_obj.get("username") or author_obj.get("screen_name") or default_author
        )
    else:
        username = str(author_obj or default_author)

    published_at = _parse_dt(tweet.get("published_at") or tweet.get("created_at"))
    metrics = tweet.get("metrics") or tweet.get("public_metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}

    referenced = tweet.get("referenced_tweet") or tweet.get("quoted_tweet")
    raw_payload: dict[str, Any] = {
        "tweet_id": tweet_id,
        "author": username,
        "conversation_id": tweet.get("conversation_id"),
        "in_reply_to": tweet.get("in_reply_to") or tweet.get("in_reply_to_status_id"),
        "is_retweet": bool(tweet.get("is_retweet") or tweet.get("retweeted")),
        "is_quote": bool(tweet.get("is_quote") or referenced),
        "retweeted_id": tweet.get("retweeted_id"),
        "media": tweet.get("media") or [],
    }
    if isinstance(referenced, dict):
        raw_payload["referenced_tweet"] = {
            "id": str(referenced.get("id") or ""),
            "author": str((referenced.get("author") or {}).get("username", ""))
            if isinstance(referenced.get("author"), dict)
            else str(referenced.get("author") or ""),
            "text": str(referenced.get("text") or "")[:2000],
        }

    # Retweets are observational signals, not independent confirmation.
    if raw_payload["is_retweet"]:
        raw_payload["independent_confirmation"] = False

    url = _tweet_url(username, tweet_id)
    media = tweet.get("media") or []
    media_dict = {"items": media} if isinstance(media, list) else {}

    return FetchedItem(
        url=url,
        title=text[:220],
        raw_text=text,
        external_id=tweet_id,
        canonical_url=url,
        published_at=published_at,
        source_updated_at=_parse_dt(tweet.get("edited_at")),
        author=username,
        language=str(tweet.get("lang") or tweet.get("language") or "und"),
        content_type="post",
        views=_safe_int(metrics.get("views") or metrics.get("impression_count")),
        forwards=_safe_int(metrics.get("reposts") or metrics.get("retweet_count")),
        shares=_safe_int(metrics.get("reposts") or metrics.get("retweet_count")),
        reactions=_safe_int(metrics.get("likes") or metrics.get("like_count")),
        replies=_safe_int(metrics.get("replies") or metrics.get("reply_count")),
        saves=_safe_int(metrics.get("bookmarks") or metrics.get("bookmark_count")),
        media=media_dict,
        raw_payload=raw_payload,
        source_metadata={"platform": "twitter_x", "username": username},
    )


class TwitterSourceAdapter(SourceAdapter):
    """Session-based X adapter. Client injected; credentials never touch adapter."""

    def __init__(self, *, client: TwitterSessionClient | None = None) -> None:
        self._client = client

    async def fetch(self, context: FetchContext) -> FetchResult:
        started = time.monotonic()
        if self._client is None:
            raise AuthenticationError("Twitter session client is not configured")
        username = str(context.configuration.get("username") or context.url or "").strip()
        if not username:
            raise AuthenticationError("Twitter username is not configured")
        cursor = context.configuration.get("cursor")
        limit = max(1, min(100, int(context.configuration.get("limit", 50) or 50)))
        try:
            page = self._client.fetch_user_tweets(username=username, cursor=cursor, limit=limit)
        except AuthenticationError:
            raise
        except RateLimitError:
            raise
        except Exception as exc:
            raise NetworkError(sanitize_error_message(str(exc))) from exc

        tweets = page.get("tweets", []) if isinstance(page, dict) else []
        items: list[FetchedItem] = []
        for tweet in tweets:
            if not isinstance(tweet, dict):
                continue
            mapped = tweet_to_fetched_item(tweet, default_author=username)
            if mapped is not None:
                items.append(mapped)

        duration_ms = int((time.monotonic() - started) * 1000)
        return FetchResult(
            items=items,
            duration_ms=duration_ms,
            raw_preview=f"twitter:{username}:{len(items)}",
        )
