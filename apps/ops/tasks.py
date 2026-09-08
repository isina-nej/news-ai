"""Periodic Celery Beat scheduled tasks for news-ai pipeline orchestration."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from celery import shared_task
from django.utils import timezone

from apps.news.models import SourceItem
from apps.ranking.services.selection import SelectionService
from apps.sources.models import Source
from apps.sources.tasks import fetch_source_task
from apps.stories.models import Story
from apps.stories.tasks import cluster_source_item_task


@shared_task(queue="celery")
def dispatch_due_sources_task() -> dict[str, Any]:
    """Scan enabled sources and dispatch fetch tasks if due and not in cooldown."""
    now = timezone.now()
    dispatched = 0
    sources = list(Source.objects.filter(enabled=True))
    for src in sources:
        # Cooldown check
        if src.cooldown_until and src.cooldown_until > now:
            continue
        # Interval check
        if src.last_success_at:
            due_at = src.last_success_at + timedelta(seconds=src.fetch_interval_seconds)
            if now < due_at:
                continue
        fetch_source_task.delay(src.pk)
        dispatched += 1
    return {"dispatched": dispatched}


@shared_task(queue="intelligence")
def dispatch_pending_clustering_task(*, batch_size: int = 50) -> dict[str, Any]:
    """Find unclustered items and dispatch clustering."""
    items = list(
        SourceItem.objects.filter(story__isnull=True).order_by("collected_at")[:batch_size]
    )
    for it in items:
        cluster_source_item_task.delay(it.pk)
    return {"dispatched": len(items)}


@shared_task(queue="celery")
def dispatch_ranking_refresh_task(*, lookback_hours: int = 48) -> dict[str, Any]:
    """Re-score recent active stories."""
    from apps.ranking.services.scoring import StoryScoringService

    cutoff = timezone.now() - timedelta(hours=lookback_hours)
    stories = list(
        Story.objects.filter(latest_source_update_at__gte=cutoff).exclude(status="merged")[:100]
    )
    scored_count = 0
    for st in stories:
        StoryScoringService.score_story(st)
        scored_count += 1
    return {"scored": scored_count}


@shared_task(queue="celery")
def dispatch_publication_queue_task() -> dict[str, Any]:
    """Select publishable stories and dispatch publication if auto-publish is active."""
    from apps.publishing.services import _auto_publish_enabled, publish_story

    if not _auto_publish_enabled():
        return {"status": "skipped", "reason": "auto_publish_disabled"}

    candidates = SelectionService.select_publishable_stories(limit=5)
    dispatched = 0
    for cand in candidates:
        if cand["action"] == "publish":
            publish_story(cand["story_id"], force=True)
            dispatched += 1
    return {"published": dispatched}
