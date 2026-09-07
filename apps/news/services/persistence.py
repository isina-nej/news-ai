"""Persistence service for ingested news items with exact deduplication and payload hygiene."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.choices import ContentType
from apps.news.models import SourceItem, SourceItemStatus
from apps.news.services.canonical_url import canonical_url_service
from apps.news.services.normalization import normalization_service
from apps.sources.adapters.base import FetchedItem
from apps.sources.models import Source

MAX_PAYLOAD_BYTES = 64 * 1024  # 64 KB limit for raw_payload in DB


def _sanitize_payload(
    payload: dict[str, Any], max_bytes: int = MAX_PAYLOAD_BYTES
) -> dict[str, Any]:
    """Ensure raw_payload JSON representation does not exceed safe size in MySQL."""
    try:
        serialized = json.dumps(payload, default=str)
        if len(serialized.encode("utf-8")) <= max_bytes:
            return payload
    except (TypeError, ValueError, OverflowError):
        pass

    # Prune payload to essential keys
    pruned: dict[str, Any] = {
        "_truncated": True,
        "title": payload.get("title", ""),
        "link": payload.get("link", ""),
        "id": payload.get("id"),
        "author": payload.get("author", ""),
    }
    return pruned


@dataclass
class IngestionResult:
    created_count: int = 0
    duplicate_count: int = 0
    rejected_count: int = 0
    created_items: list[SourceItem] = field(default_factory=list)


class IngestionPersistenceService:
    """Service to persist, normalize, and exact-dedupe fetched items for a source."""

    def __init__(self) -> None:
        self.url_service = canonical_url_service
        self.norm_service = normalization_service

    def persist_items(
        self,
        source: Source,
        items: list[FetchedItem],
        *,
        batch_time: datetime | None = None,
    ) -> IngestionResult:
        """Persist a list of FetchedItems for a source.

        Deduplication rules for the SAME source:
        1. Exact external_id match -> DUPLICATE
        2. Exact canonical URL hash match -> DUPLICATE
        3. Exact normalized content hash match -> DUPLICATE

        Cross-source items with matching URL or content are NEVER discarded;
        they are saved as distinct SourceItems for independent multi-source confirmation.
        """
        now = batch_time or timezone.now()
        result = IngestionResult()

        for item in items:
            raw_url = item.url or ""
            raw_text = item.raw_text or ""
            title = item.title or ""

            # Skip items with no title and no text and no url
            if not title and not raw_text and not raw_url:
                result.rejected_count += 1
                continue

            # 1. Canonicalize URL and compute hash
            canonical_url, url_hash = self.url_service.canonicalize_and_hash(raw_url)

            # 2. Normalize text and compute hashes
            normalized_text, raw_hash, norm_hash = self.norm_service.normalize_and_hash(raw_text)

            external_id = item.external_id or None

            # 3. Exact dedupe checks against existing items from THIS source
            is_dup = False

            if (
                external_id
                and SourceItem.objects.filter(source=source, external_id=external_id).exists()
            ):
                is_dup = True
            elif url_hash and SourceItem.objects.filter(source=source, url_hash=url_hash).exists():
                is_dup = True
            elif (
                norm_hash
                and SourceItem.objects.filter(source=source, content_hash=norm_hash).exists()
            ):
                is_dup = True

            if is_dup:
                result.duplicate_count += 1
                continue

            # 4. Clean raw payload to prevent MySQL column bloat
            safe_payload = _sanitize_payload(item.raw_payload)

            # 5. Insert atomically with IntegrityError guard for concurrent workers
            try:
                with transaction.atomic():
                    source_item = SourceItem.objects.create(
                        source=source,
                        external_id=external_id,
                        canonical_url=canonical_url,
                        url_hash=url_hash,
                        title=title[:1024],
                        raw_text=raw_text,
                        normalized_text=normalized_text,
                        raw_content_hash=raw_hash,
                        content_hash=norm_hash,
                        media=item.media or {},
                        language=item.language or "und",
                        content_type=ContentType.ARTICLE,
                        published_at=item.published_at,
                        collected_at=now,
                        raw_payload=safe_payload,
                        status=SourceItemStatus.COLLECTED,
                    )
                    result.created_count += 1
                    result.created_items.append(source_item)
            except IntegrityError:
                # Concurrent worker inserted same source + external_id simultaneously
                result.duplicate_count += 1

        return result


ingestion_persistence_service = IngestionPersistenceService()
