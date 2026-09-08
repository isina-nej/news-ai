"""Persistence service for ingested news items with exact deduplication and payload hygiene."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlparse

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


def resolve_effective_url(fetched_url: str, doc_canonical: str | None) -> str:
    """Determine effective canonical URL following document preference with domain guard.

    Policy:
    1. If document provides a canonical URL:
       - Relative URLs are resolved against fetched_url.
       - Scheme must be http/https.
       - Host must share the same base domain as fetched_url (rejects foreign domain hijacking).
    2. Fallback to fetched_url.
    """
    if not doc_canonical or not doc_canonical.strip():
        return fetched_url

    doc_canonical = doc_canonical.strip()
    # Resolve relative URL against fetched URL
    resolved = urljoin(fetched_url, doc_canonical)

    try:
        parsed_fetched = urlparse(fetched_url)
        parsed_resolved = urlparse(resolved)
    except Exception:
        return fetched_url

    if parsed_resolved.scheme.lower() not in ("http", "https"):
        return fetched_url

    fetched_host = (parsed_fetched.hostname or "").lower()
    resolved_host = (parsed_resolved.hostname or "").lower()

    if not fetched_host or not resolved_host:
        return fetched_url

    # Check if hosts match or share base domain
    if (
        resolved_host == fetched_host
        or resolved_host.endswith("." + fetched_host)
        or fetched_host.endswith("." + resolved_host)
    ):
        return resolved

    # Foreign domain mismatch -> fallback to fetched URL
    return fetched_url


@dataclass
class IngestionResult:
    created_count: int = 0
    duplicate_count: int = 0
    updated_count: int = 0
    rejected_count: int = 0
    created_items: list[SourceItem] = field(default_factory=list)
    updated_items: list[SourceItem] = field(default_factory=list)


def _material_change(old: SourceItem, *, title: str, raw_text: str, normalized_text: str) -> bool:
    return (
        (old.title or "") != (title or "")
        or (old.raw_text or "") != (raw_text or "")
        or (old.normalized_text or "") != (normalized_text or "")
    )


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

        Same-source identity (in order):
        1. Exact external_id match:
           - identical material state -> UNCHANGED duplicate
           - newer source_updated_at or changed material content -> UPDATE
             (current row refreshed + SourceItemRevision appended)
        2. Exact canonical URL hash match -> DUPLICATE
        3. Exact normalized content hash match -> DUPLICATE

        Cross-source items with matching URL or content are NEVER discarded;
        they are saved as distinct SourceItems for independent multi-source confirmation.
        """
        from apps.news.models import SourceItemRevision  # local import: avoid cycle

        now = batch_time or timezone.now()
        result = IngestionResult()

        for item in items:
            raw_url = item.url or ""
            raw_text = item.raw_text or ""
            title = item.title or ""

            # Skip items with no title, no text, and no URL
            if not title and not raw_text and not raw_url:
                result.rejected_count += 1
                continue

            # 1. Resolve document-provided canonical URL with safety checks
            target_url = resolve_effective_url(raw_url, item.canonical_url)

            # 2. Canonicalize URL and compute hash
            canonical_url = ""
            url_hash: str | None = None
            if target_url:
                canonical_url, computed_url_hash = self.url_service.canonicalize_and_hash(
                    target_url
                )
                url_hash = computed_url_hash if canonical_url else None

            # 3. Normalize text and compute hashes
            normalized_text = ""
            raw_hash: str | None = None
            norm_hash: str | None = None
            if raw_text:
                normalized_text, r_hash, n_hash = self.norm_service.normalize_and_hash(raw_text)
                raw_hash = r_hash if raw_text.strip() else None
                norm_hash = n_hash if (normalized_text or raw_text.strip()) else None

            external_id = item.external_id or None

            # 4a. Same external_id exists -> duplicate or platform edit update.
            if external_id:
                existing = SourceItem.objects.filter(source=source, external_id=external_id).first()
                if existing is not None:
                    incoming_updated_at = item.source_updated_at
                    changed = _material_change(
                        existing,
                        title=title[:1024],
                        raw_text=raw_text,
                        normalized_text=normalized_text,
                    )
                    is_newer_edit = incoming_updated_at is not None and (
                        existing.source_updated_at is None
                        or incoming_updated_at > existing.source_updated_at
                    )
                    if changed and (is_newer_edit or incoming_updated_at is None and changed):
                        # Material edit: snapshot previous state, then refresh live row.
                        with transaction.atomic():
                            locked = SourceItem.objects.select_for_update().get(pk=existing.pk)
                            if _material_change(
                                locked,
                                title=title[:1024],
                                raw_text=raw_text,
                                normalized_text=normalized_text,
                            ):
                                last_rev = (
                                    SourceItemRevision.objects.filter(source_item=locked)
                                    .order_by("-revision_number")
                                    .first()
                                )
                                next_number = (last_rev.revision_number + 1) if last_rev else 1
                                SourceItemRevision.objects.create(
                                    source_item=locked,
                                    revision_number=next_number,
                                    source_updated_at=locked.source_updated_at,
                                    observed_at=now,
                                    title=locked.title,
                                    raw_text=locked.raw_text,
                                    normalized_text=locked.normalized_text,
                                    content_hash=locked.content_hash,
                                    change_metadata={
                                        "reason": "platform_edit",
                                        "incoming_source_updated_at": (
                                            incoming_updated_at.isoformat()
                                            if incoming_updated_at
                                            else None
                                        ),
                                    },
                                )
                                locked.title = title[:1024]
                                locked.raw_text = raw_text
                                locked.normalized_text = normalized_text
                                if incoming_updated_at is not None:
                                    locked.source_updated_at = incoming_updated_at
                                locked.save()
                                result.updated_count += 1
                                result.updated_items.append(locked)
                            else:
                                result.duplicate_count += 1
                    else:
                        result.duplicate_count += 1
                    self._record_snapshot(existing, item, now=now)
                    continue

            # 4b. URL / content exact dedupe (unchanged semantics).
            is_dup = False
            if url_hash and SourceItem.objects.filter(source=source, url_hash=url_hash).exists():
                is_dup = True
            elif (
                norm_hash
                and SourceItem.objects.filter(source=source, content_hash=norm_hash).exists()
            ):
                is_dup = True

            if is_dup:
                result.duplicate_count += 1
                continue

            # 5. Clean raw payload to prevent MySQL column bloat
            safe_payload = _sanitize_payload(item.raw_payload)

            # 6. Resolve content type from DTO
            raw_ct = str(item.content_type or "").lower()
            valid_cts = {c.value for c in ContentType}
            content_type = raw_ct if raw_ct in valid_cts else ContentType.ARTICLE

            # 7. Insert atomically with DB UniqueConstraint guard for concurrent workers
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
                        content_type=content_type,
                        published_at=item.published_at,
                        source_updated_at=item.source_updated_at,
                        collected_at=now,
                        raw_payload=safe_payload,
                        status=SourceItemStatus.COLLECTED,
                    )
                    result.created_count += 1
                    result.created_items.append(source_item)
                    self._record_snapshot(source_item, item, now=now)
            except IntegrityError:
                # Concurrent worker inserted same source + external_id/url/content simultaneously
                result.duplicate_count += 1

        return result

    def _record_snapshot(
        self, source_item: SourceItem, item: FetchedItem, *, now: datetime
    ) -> None:
        """Write the initial/immediate engagement snapshot when metrics are present."""
        from apps.news.models import EngagementSnapshot  # local import: avoid cycle

        has_metrics = any(
            v is not None for v in (item.views, item.forwards, item.reactions, item.replies)
        )
        if not has_metrics and not item.reaction_breakdown:
            return
        raw_metrics: dict[str, Any] = {}
        if item.reaction_breakdown:
            raw_metrics["reaction_breakdown"] = dict(item.reaction_breakdown)
        try:
            EngagementSnapshot.objects.create(
                source_item=source_item,
                captured_at=now,
                views=item.views,
                forwards=item.forwards,
                shares=None,
                reactions=item.reactions,
                replies=item.replies,
                saves=None,
                raw_metrics=raw_metrics,
            )
        except IntegrityError:
            pass


ingestion_persistence_service = IngestionPersistenceService()
