"""RSS and Atom feed ingestion adapter using feedparser and SafeHttpClient."""

from __future__ import annotations

from calendar import timegm
from datetime import UTC, datetime
from typing import Any

import feedparser

from apps.sources.adapters.base import (
    FetchContext,
    FetchedItem,
    FetchResult,
    ParseError,
    SourceAdapter,
)
from apps.sources.adapters.http_client import SafeHttpClient, default_http_client


def _parse_feed_date(entry: dict[str, Any]) -> datetime | None:
    """Extract a reliable UTC datetime from feed entry, or return None."""
    # 1. Structured time tuples (feedparser provides *_parsed as 9-tuple)
    for date_key in ("published_parsed", "updated_parsed", "created_parsed"):
        time_tuple = entry.get(date_key)
        if time_tuple:
            try:
                # timegm interprets struct_time as UTC
                timestamp = timegm(time_tuple)
                return datetime.fromtimestamp(timestamp, tz=UTC)
            except (ValueError, OverflowError, TypeError):
                continue

    # 2. String fallbacks if struct_time wasn't present
    for str_key in ("published", "updated", "pubDate", "dc:date"):
        val = entry.get(str_key)
        if val and isinstance(val, str):
            try:
                # feedparser.datetimes or fromisoformat attempt
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt.astimezone(UTC)
            except (ValueError, TypeError, OverflowError):
                continue

    # If date was missing or untrustworthy, return None (never spoof collected_at)
    return None


def _extract_enclosures(entry: dict[str, Any]) -> dict[str, Any]:
    """Extract media attachments/enclosures from feed entry."""
    enclosures = []
    for enc in entry.get("enclosures", []):
        if isinstance(enc, dict) and enc.get("href"):
            enclosures.append(
                {
                    "url": enc.get("href"),
                    "type": enc.get("type", ""),
                    "length": enc.get("length", 0),
                }
            )
    # Check media:content if present
    media_content = entry.get("media_content", [])
    if isinstance(media_content, list):
        for m in media_content:
            if isinstance(m, dict) and m.get("url"):
                enclosures.append(
                    {
                        "url": m.get("url"),
                        "type": m.get("type", "image"),
                    }
                )
    return {"enclosures": enclosures} if enclosures else {}


class RSSSourceAdapter(SourceAdapter):
    """Adapter for RSS 2.0, Atom, and feed variants."""

    def __init__(self, http_client: SafeHttpClient | None = None) -> None:
        self.http_client = http_client or default_http_client

    async def fetch(self, context: FetchContext) -> FetchResult:
        status, headers, body, duration_ms = await self.http_client.fetch(
            context.url,
            etag=context.etag,
            last_modified=context.last_modified,
            timeout_seconds=context.timeout_seconds,
            max_bytes=context.max_bytes,
        )

        if status == 304:
            return FetchResult(
                items=[],
                status_code=304,
                etag=headers.get("etag") or context.etag,
                last_modified=headers.get("last-modified") or context.last_modified,
                not_modified=True,
                duration_ms=duration_ms,
            )

        # Parse feed with feedparser (passes bytes to respect XML encoding decl)
        parsed = feedparser.parse(body)

        # Bozo check: only fatal if no entries could be extracted
        if parsed.bozo and not parsed.entries:
            exc = getattr(parsed, "bozo_exception", None)
            raise ParseError(f"Malformed feed at {context.url}: {exc}")

        items: list[FetchedItem] = []
        for entry in parsed.entries:
            url = entry.get("link") or entry.get("id") or ""
            title = entry.get("title") or ""

            # Text content priority: content > summary_detail > summary
            raw_text = ""
            if "content" in entry and isinstance(entry["content"], list) and entry["content"]:
                raw_text = entry["content"][0].get("value", "")
            elif "summary" in entry:
                raw_text = entry.get("summary", "")
            elif "description" in entry:
                raw_text = entry.get("description", "")

            external_id = entry.get("id") or entry.get("guid") or url or None
            published_at = _parse_feed_date(entry)
            author = entry.get("author") or ""
            media = _extract_enclosures(entry)

            # Clean raw payload (omit massive binary blobs)
            raw_payload = {
                "title": title,
                "link": url,
                "id": external_id,
                "author": author,
                "tags": [
                    t.get("term")
                    for t in entry.get("tags", [])
                    if isinstance(t, dict) and t.get("term")
                ],
            }

            items.append(
                FetchedItem(
                    url=url,
                    external_id=str(external_id) if external_id else None,
                    title=title,
                    raw_text=raw_text,
                    published_at=published_at,
                    author=author,
                    media=media,
                    raw_payload=raw_payload,
                    source_metadata={
                        "feed_title": parsed.feed.get("title", ""),
                        "feed_link": parsed.feed.get("link", ""),
                    },
                )
            )

        new_etag = headers.get("etag") or parsed.get("etag") or context.etag
        new_last_modified = (
            headers.get("last-modified") or parsed.get("modified") or context.last_modified
        )

        return FetchResult(
            items=items,
            status_code=status,
            etag=new_etag,
            last_modified=new_last_modified,
            not_modified=False,
            duration_ms=duration_ms,
            raw_preview=body[:500].decode("utf-8", errors="replace"),
        )
