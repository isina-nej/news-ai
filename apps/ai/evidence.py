"""Truncated deterministic evidence builder. Keeps AI calls cheap and safe."""

from __future__ import annotations

TITLE_CHARS = 220
BODY_CHARS_PER_ITEM = 900
MAX_ITEMS = 6
MAX_URLS = 12


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def story_evidence(story, *, max_items: int = MAX_ITEMS) -> dict:
    memberships = list(
        story.memberships.filter(is_current=True)
        .select_related("source_item", "source_item__source")
        .order_by("-is_primary", "added_at")[:max_items]
    )
    items: list[dict] = []
    urls: list[str] = []
    for membership in memberships:
        item = membership.source_item
        if item.canonical_url and item.canonical_url not in urls and len(urls) < MAX_URLS:
            urls.append(item.canonical_url)
        items.append(
            {
                "source": getattr(item.source, "name", ""),
                "title": _clip(item.title, TITLE_CHARS),
                "text": _clip(item.normalized_text or item.raw_text, BODY_CHARS_PER_ITEM),
                "published_at": item.published_at.isoformat() if item.published_at else None,
            }
        )
    return {
        "canonical_title": _clip(story.canonical_title, TITLE_CHARS),
        "language": story.language,
        "observed_sources": story.observed_source_count,
        "independent_sources": story.independent_source_count,
        "evidence_urls": urls,
        "items": items,
    }
