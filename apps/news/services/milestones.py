"""Idempotent milestone snapshot writer: crash/retry-safe by construction.

Invariant: if the milestone row already exists (worker crashed after insert
but before ``advance_tracking``), the milestone counts as DONE and tracking
advances instead of wedging on the same bucket forever. Only unexpected DB
errors propagate so they stay observable in monitoring.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from django.db import IntegrityError, transaction

from apps.news.models import EngagementSnapshot, SourceItem

logger = logging.getLogger(__name__)


def record_milestone_snapshot(
    source_item: SourceItem,
    *,
    target_age_seconds: int | None,
    now: datetime,
    views: int | None = None,
    forwards: int | None = None,
    reactions: int | None = None,
    replies: int | None = None,
    raw_metrics: dict[str, Any] | None = None,
) -> tuple[EngagementSnapshot | None, bool]:
    """Create the milestone snapshot, or reuse the existing one on retry.

    Returns ``(snapshot, created)`` where ``created=False`` means the row
    already existed (e.g. crash between insert and state advance) and the
    caller must still advance tracking.
    """
    if target_age_seconds is None:
        snapshot = EngagementSnapshot.objects.create(
            source_item=source_item,
            captured_at=now,
            views=views,
            forwards=forwards,
            shares=None,
            reactions=reactions,
            replies=replies,
            saves=None,
            raw_metrics=raw_metrics or {},
            capture_reason="milestone",
        )
        return snapshot, True
    try:
        with transaction.atomic():
            snapshot = EngagementSnapshot.objects.create(
                source_item=source_item,
                captured_at=now,
                target_age_seconds=target_age_seconds,
                views=views,
                forwards=forwards,
                shares=None,
                reactions=reactions,
                replies=replies,
                saves=None,
                raw_metrics=raw_metrics or {},
                capture_reason="milestone",
            )
            return snapshot, True
    except IntegrityError:
        # Retry after crash: milestone already recorded -> treat as done.
        existing = EngagementSnapshot.objects.filter(
            source_item=source_item, target_age_seconds=target_age_seconds
        ).first()
        if existing is None:  # pragma: no cover - defensive; constraint says it exists
            logger.warning(
                "milestone snapshot conflict without existing row: item=%s target=%s",
                source_item.pk,
                target_age_seconds,
            )
            raise
        return existing, False
