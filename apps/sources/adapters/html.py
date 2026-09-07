"""Static HTML website adapter using httpx and trafilatura article extraction."""

from __future__ import annotations

from datetime import UTC, datetime

import trafilatura
from bs4 import BeautifulSoup

from apps.sources.adapters.base import (
    ExtractionError,
    FetchContext,
    FetchedItem,
    FetchResult,
    SourceAdapter,
)
from apps.sources.adapters.http_client import SafeHttpClient, default_http_client


def _parse_iso_date(date_str: str | None) -> datetime | None:
    """Parse date string into UTC datetime, or return None."""
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        # Try basic YYYY-MM-DD
        try:
            dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
            return dt.replace(tzinfo=UTC)
        except Exception:
            return None


def _extract_meta_datetime(soup: BeautifulSoup) -> datetime | None:
    """Extract explicit ISO datetime from meta tags if present."""
    for attr, val in (
        ("property", "article:published_time"),
        ("name", "date"),
        ("name", "pubdate"),
        ("name", "publishdate"),
        ("property", "og:published_time"),
    ):
        tag = soup.find("meta", attrs={attr: val})
        if tag and tag.get("content"):
            parsed = _parse_iso_date(str(tag["content"]).strip())
            if parsed:
                return parsed
    time_tag = soup.find("time")
    if time_tag and time_tag.get("datetime"):
        parsed = _parse_iso_date(str(time_tag["datetime"]).strip())
        if parsed:
            return parsed
    return None


def _fallback_extract(html_text: str, fallback_url: str) -> tuple[str, str, str]:
    """Lightweight fallback using BeautifulSoup when trafilatura extracts nothing.

    Returns: (title, main_text, canonical_url)
    """
    soup = BeautifulSoup(html_text, "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    # Look for canonical link tag
    canonical = ""
    can_tag = soup.find("link", rel=lambda val: val and "canonical" in val.lower())
    if can_tag and can_tag.get("href"):
        canonical = str(can_tag["href"]).strip()

    # Extract text from main semantic containers
    container = (
        soup.find("article")
        or soup.find("main")
        or soup.find("div", class_=lambda c: c and "content" in c)
    )
    if container:
        paragraphs = [p.get_text().strip() for p in container.find_all("p") if p.get_text().strip()]
        text = "\n\n".join(paragraphs)
    else:
        paragraphs = [p.get_text().strip() for p in soup.find_all("p") if p.get_text().strip()]
        text = "\n\n".join(paragraphs)

    return (title, text, canonical)


class HTMLSourceAdapter(SourceAdapter):
    """Adapter for static web pages and blog posts."""

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

        html_text = body.decode("utf-8", errors="replace")

        # 1. Extract metadata via trafilatura
        meta = trafilatura.extract_metadata(body, default_url=context.url)

        # 2. Extract article main text via trafilatura
        main_text = trafilatura.extract(
            body,
            url=context.url,
            output_format="txt",
            include_comments=False,
            include_tables=True,
            no_fallback=False,
        )

        title = meta.title if meta and meta.title else ""
        author = meta.author if meta and meta.author else ""
        canonical_url = meta.url if meta and meta.url else ""
        language = meta.language if meta and meta.language else "und"
        soup = BeautifulSoup(html_text, "html.parser")
        meta_dt = _extract_meta_datetime(soup)
        published_at = meta_dt or (_parse_iso_date(meta.date) if meta else None)

        # 3. Fallback extraction if trafilatura extracted insufficient text
        if not main_text or len(main_text.strip()) < 50:
            fb_title, fb_text, fb_canonical = _fallback_extract(html_text, context.url)
            if not title:
                title = fb_title
            if not canonical_url:
                canonical_url = fb_canonical
            if fb_text:
                main_text = fb_text

        # If page had neither title nor text, raise ExtractionError
        if not title and not (main_text and main_text.strip()):
            raise ExtractionError(f"Failed to extract readable article from {context.url}")

        item = FetchedItem(
            url=context.url,
            external_id=canonical_url or context.url,
            canonical_url=canonical_url or context.url,
            title=title or context.url,
            raw_text=main_text or "",
            published_at=published_at,
            author=author,
            language=language or "und",
            raw_payload={
                "title": title,
                "author": author,
                "canonical_url": canonical_url,
                "sitename": meta.sitename if meta else "",
                "description": meta.description if meta else "",
                "categories": meta.categories if meta else [],
                "tags": meta.tags if meta else [],
            },
            source_metadata={
                "sitename": meta.sitename if meta else "",
            },
        )

        return FetchResult(
            items=[item],
            status_code=status,
            etag=headers.get("etag") or context.etag,
            last_modified=headers.get("last-modified") or context.last_modified,
            not_modified=False,
            duration_ms=duration_ms,
            raw_preview=html_text[:500],
        )
