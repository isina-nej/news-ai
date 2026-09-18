"""Session client helpers. Env-only session loading; no DB/log persistence."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from apps.core.redaction import sanitize_error_message
from apps.sources.adapters.base import (
    AuthenticationError,
    NetworkError,
    PermanentSourceError,
    RateLimitError,
)

TWITTER_WEB_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"  # noqa: S105
)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def parse_session_credentials(session_str: str) -> dict[str, str]:
    """Parse session credentials from JSON or cookie header string."""
    raw = (session_str or "").strip()
    if not raw:
        return {}
    if raw.startswith("{") and raw.endswith("}"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if v is not None}
        except Exception:  # noqa: S110 — fallback to cookie parse
            pass
    cookies: dict[str, str] = {}
    for item in raw.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            cookies[k.strip()] = v.strip()
    if cookies:
        return cookies
    return {"auth_token": raw}


def normalize_graphql_tweet(
    result: dict[str, Any], default_author: str = ""
) -> dict[str, Any] | None:
    """Normalize a Twitter GraphQL tweet result into a standard tweet dictionary."""
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet") or {}
    if not isinstance(result, dict) or not result:
        return None

    rest_id = str(result.get("rest_id") or result.get("id") or "")
    if not rest_id:
        return None

    legacy = result.get("legacy") or {}
    core = result.get("core") or {}
    user_res = (core.get("user_results") or {}).get("result") or {}
    user_legacy = user_res.get("legacy") or user_res.get("core") or {}
    screen_name = str(user_legacy.get("screen_name") or default_author)

    text = str(legacy.get("full_text") or legacy.get("text") or result.get("text") or "")
    created_at_raw = legacy.get("created_at") or result.get("created_at")
    created_at_iso = None
    if created_at_raw:
        try:
            dt = datetime.strptime(str(created_at_raw), "%a %b %d %H:%M:%S %z %Y")
            created_at_iso = dt.astimezone(UTC).isoformat()
        except Exception:
            try:
                dt = datetime.fromisoformat(str(created_at_raw).replace("Z", "+00:00"))
                created_at_iso = dt.astimezone(UTC).isoformat()
            except Exception:  # noqa: S110 — keep raw string fallback
                created_at_iso = str(created_at_raw)

    views_data = result.get("views") or {}
    views_count = views_data.get("count") if isinstance(views_data, dict) else None

    metrics = {
        "like_count": legacy.get("favorite_count"),
        "retweet_count": legacy.get("retweet_count"),
        "reply_count": legacy.get("reply_count"),
        "bookmark_count": legacy.get("bookmark_count"),
        "impression_count": views_count,
    }

    retweeted_status = legacy.get("retweeted_status_result")
    is_retweet = bool(retweeted_status or legacy.get("retweeted"))
    retweeted_id = None
    if isinstance(retweeted_status, dict):
        retweeted_id = str((retweeted_status.get("result") or {}).get("rest_id") or "")

    quoted_status = result.get("quoted_status_result") or legacy.get("quoted_status_result")
    is_quote = bool(quoted_status or legacy.get("is_quote_status"))
    referenced_tweet = None
    if isinstance(quoted_status, dict):
        q_res = quoted_status.get("result") or {}
        q_legacy = q_res.get("legacy") or {}
        q_core = (q_res.get("core") or {}).get("user_results", {}).get("result", {}).get(
            "legacy"
        ) or {}
        referenced_tweet = {
            "id": str(q_res.get("rest_id") or ""),
            "author": {"username": str(q_core.get("screen_name") or "")},
            "text": str(q_legacy.get("full_text") or q_legacy.get("text") or ""),
        }

    media_items: list[dict[str, Any]] = []
    entities = legacy.get("extended_entities") or legacy.get("entities") or {}
    for m in entities.get("media", []) if isinstance(entities, dict) else []:
        if isinstance(m, dict):
            media_items.append(
                {
                    "type": m.get("type", "photo"),
                    "url": m.get("media_url_https") or m.get("url"),
                }
            )

    return {
        "id": rest_id,
        "text": text,
        "author": {"username": screen_name},
        "created_at": created_at_iso,
        "public_metrics": metrics,
        "is_retweet": is_retweet,
        "retweeted_id": retweeted_id,
        "is_quote": is_quote,
        "referenced_tweet": referenced_tweet,
        "media": media_items,
        "lang": legacy.get("lang") or "und",
    }


def extract_graphql_timeline(data: dict[str, Any], default_author: str = "") -> dict[str, Any]:
    """Extract tweets and bottom pagination cursor from a Twitter GraphQL response."""
    tweets: list[dict[str, Any]] = []
    next_cursor: str | None = None

    instructions: list[dict[str, Any]] = []
    user_res = (data.get("data") or {}).get("user", {}).get("result", {})
    timeline = user_res.get("timeline_v2", {}).get("timeline") or user_res.get("timeline", {})
    if isinstance(timeline, dict) and "instructions" in timeline:
        instructions = timeline["instructions"]
    elif "instructions" in data:
        instructions = data["instructions"]
    elif "data" in data and isinstance(data["data"], dict) and "instructions" in data["data"]:
        instructions = data["data"]["instructions"]

    for instr in instructions:
        if not isinstance(instr, dict):
            continue
        entries = instr.get("entries") or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("entryId") or "")
            content = entry.get("content") or {}

            cursor_type = str(content.get("cursorType") or "")
            if "bottom" in entry_id.lower() or cursor_type.lower() == "bottom":
                raw_val = str(content.get("value") or content.get("cursor") or "")
                next_cursor = raw_val or next_cursor
                continue

            item_content = content.get("itemContent") or {}
            tweet_results = item_content.get("tweet_results") or {}
            result = tweet_results.get("result") or {}
            if not result:
                if "legacy" in content:
                    result = content
                elif "tweet" in content:
                    result = content["tweet"]

            if isinstance(result, dict) and result:
                parsed_tweet = normalize_graphql_tweet(result, default_author=default_author)
                if parsed_tweet:
                    tweets.append(parsed_tweet)

    return {"tweets": tweets, "next_cursor": next_cursor}


def extract_syndication_timeline(
    html_or_json: str | dict[str, Any], default_author: str = ""
) -> dict[str, Any]:
    """Extract tweets from Twitter syndication profile HTML or parsed JSON."""
    if isinstance(html_or_json, str):
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            html_or_json,
            re.DOTALL,
        )
        if match:
            try:
                data = json.loads(match.group(1))
            except Exception:
                data = {}
        else:
            return {"tweets": [], "next_cursor": None}
    else:
        data = html_or_json

    entries = (data.get("props") or {}).get("pageProps", {}).get("timeline", {}).get("entries", [])
    tweets: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content") or entry.get("tweet") or entry
        tweet_id = str(content.get("id_str") or content.get("id") or "")
        if not tweet_id:
            continue
        text = str(content.get("text") or content.get("full_text") or "")
        user = content.get("user") or {}
        username = str(user.get("screen_name") or default_author)
        created_at = content.get("created_at")
        metrics = {
            "like_count": content.get("favorite_count"),
            "retweet_count": content.get("retweet_count"),
            "reply_count": content.get("reply_count"),
        }
        tweets.append(
            {
                "id": tweet_id,
                "text": text,
                "author": {"username": username},
                "created_at": created_at,
                "public_metrics": metrics,
                "is_retweet": bool(content.get("retweeted_status")),
                "lang": content.get("lang") or "und",
            }
        )
    return {"tweets": tweets, "next_cursor": None}


def parse_tweets_payload(payload: Any, default_author: str = "") -> dict[str, Any]:
    """Detect and parse various Twitter API response payloads into a standard dict."""
    if isinstance(payload, str):
        cleaned = payload.strip()
        if cleaned.startswith("{") or cleaned.startswith("["):
            try:
                payload = json.loads(cleaned)
            except Exception:
                return extract_syndication_timeline(cleaned, default_author)
        else:
            return extract_syndication_timeline(cleaned, default_author)

    if isinstance(payload, dict):
        if "tweets" in payload and isinstance(payload["tweets"], list):
            return {
                "tweets": payload["tweets"],
                "next_cursor": payload.get("next_cursor"),
            }
        if "data" in payload and isinstance(payload["data"], list):
            tweets = []
            for item in payload["data"]:
                if isinstance(item, dict):
                    t = dict(item)
                    if "author_id" in t and "author" not in t:
                        t["author"] = {"id": t["author_id"], "username": default_author}
                    tweets.append(t)
            next_cursor = (payload.get("meta") or {}).get("next_token")
            return {"tweets": tweets, "next_cursor": next_cursor}
        if "props" in payload:
            return extract_syndication_timeline(payload, default_author)
        return extract_graphql_timeline(payload, default_author)

    if isinstance(payload, list):
        return {"tweets": [t for t in payload if isinstance(t, dict)], "next_cursor": None}

    return {"tweets": [], "next_cursor": None}


class EnvSessionClient:
    """Production session client for Twitter/X.

    Communicates over HTTP using session credentials from TWITTER_SESSION.
    Never stores or logs credentials.
    """

    def __init__(
        self,
        *,
        session: str,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not session or not session.strip():
            raise ValueError("Twitter session is not configured")
        self._credentials = parse_session_credentials(session)
        self._auth_token = self._credentials.get("auth_token", "")
        self._ct0 = self._credentials.get("ct0", "")
        self._base_url = self._credentials.get("base_url", "https://x.com").rstrip("/")
        self._proxy = self._credentials.get("proxy")
        self._timeout = timeout
        self._transport = transport

    def fetch_user_tweets(
        self, *, username: str, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Fetch latest tweets for the specified username using session credentials."""
        clean_user = username.lstrip("@").strip()
        if not clean_user:
            raise ValueError("Twitter username is required")

        headers = {
            "User-Agent": self._credentials.get("user_agent", DEFAULT_USER_AGENT),
            "Authorization": self._credentials.get("bearer_token", TWITTER_WEB_BEARER),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
        }
        if self._ct0:
            headers["x-csrf-token"] = self._ct0

        cookies: dict[str, str] = {}
        if self._auth_token:
            cookies["auth_token"] = self._auth_token
        if self._ct0:
            cookies["ct0"] = self._ct0

        custom_endpoint = self._credentials.get("endpoint") or self._credentials.get("api_url")
        if custom_endpoint:
            url = custom_endpoint.format(username=clean_user, cursor=cursor or "", limit=limit)
            params: dict[str, str] = {}
            if "{username}" not in custom_endpoint:
                params["username"] = clean_user
            if cursor and "{cursor}" not in custom_endpoint:
                params["cursor"] = cursor
            if "{limit}" not in custom_endpoint:
                params["limit"] = str(limit)
        else:
            url = f"{self._base_url}/i/api/graphql/UserTweets"
            params = {
                "variables": json.dumps(
                    {
                        "screen_name": clean_user,
                        "count": limit,
                        "cursor": cursor,
                        "includePromotedContent": False,
                        "withQuickPromoteEligibilityTweetFields": True,
                        "withVoice": True,
                        "withV2Timeline": True,
                    }
                ),
                "features": json.dumps(
                    {
                        "rweb_lists_timeline_redesign_enabled": True,
                        "responsive_web_graphql_exclude_directive_enabled": True,
                        "verified_phone_label_enabled": False,
                        "creator_subscriptions_tweet_preview_api_enabled": True,
                        "responsive_web_graphql_timeline_navigation_enabled": True,
                        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
                        "tweetypie_unmention_optimization_enabled": True,
                        "responsive_web_edit_tweet_api_enabled": True,
                        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
                        "view_counts_everywhere_api_enabled": True,
                        "longform_notetweets_consumption_enabled": True,
                        "tweet_awards_web_tipping_enabled": False,
                        "freedom_of_speech_not_reach_fetch_enabled": True,
                        "standardized_nudges_misinfo": True,
                        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": (
                            True
                        ),
                        "longform_notetweets_rich_text_read_enabled": True,
                        "longform_notetweets_inline_media_enabled": True,
                        "responsive_web_enhance_cards_enabled": False,
                    }
                ),
            }

        client_kwargs: dict[str, Any] = {
            "timeout": self._timeout,
            "follow_redirects": True,
        }
        if self._proxy:
            client_kwargs["proxy"] = self._proxy
        if self._transport:
            client_kwargs["transport"] = self._transport

        try:
            with httpx.Client(**client_kwargs) as client:
                response = client.get(url, params=params, headers=headers, cookies=cookies)
        except httpx.TimeoutException as exc:
            raise NetworkError(f"Twitter request timed out: {exc}") from exc
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            raise NetworkError(f"Twitter network connectivity failure: {exc}") from exc
        except Exception as exc:
            raise NetworkError(sanitize_error_message(str(exc))) from exc

        if response.status_code in (401, 403):
            raise AuthenticationError(
                f"Twitter session unauthorized or expired (HTTP {response.status_code})"
            )
        if response.status_code == 404:
            raise PermanentSourceError(f"Twitter user @{clean_user} not found (HTTP 404)")
        if response.status_code == 429:
            reset_hdr = response.headers.get("x-rate-limit-reset")
            retry_after = None
            if reset_hdr:
                try:
                    retry_after = max(1, int(float(reset_hdr) - time.time()))
                except (ValueError, TypeError):
                    pass
            if retry_after is None:
                try:
                    retry_after = int(response.headers.get("retry-after", 3600))
                except (ValueError, TypeError):
                    retry_after = 3600
            raise RateLimitError(
                f"Twitter rate limit exceeded (HTTP 429, retry in {retry_after}s)",
                retry_after=retry_after,
            )
        if response.status_code >= 500:
            raise NetworkError(f"Twitter server error (HTTP {response.status_code})")

        try:
            payload = response.json()
        except Exception:
            payload = response.text

        return parse_tweets_payload(payload, default_author=clean_user)


class FakeTwitterClient:
    """In-memory stub client for testing without network requests."""

    def __init__(self, pages: list[dict[str, Any]] | None = None) -> None:
        self.pages = pages or []
        self.calls: list[dict[str, Any]] = []

    def fetch_user_tweets(
        self, *, username: str, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
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
