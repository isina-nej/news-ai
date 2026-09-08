"""Operational metrics registry and structured telemetry."""

from __future__ import annotations

import logging
from typing import Any

from django.utils import timezone

from apps.core.redaction import sanitize_error_message
from apps.news.models import SourceItem
from apps.publishing.models import Publication, PublicationStatus
from apps.sources.models import FetchRun, FetchRunStatus
from apps.stories.models import ClusteringDecision

logger = logging.getLogger("newsai.metrics")


class MetricsRegistry:
    """In-memory and DB-backed operational metrics collector."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}

    def inc(self, name: str, amount: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + amount

    def set_gauge(self, name: str, value: float) -> None:
        self._gauges[name] = float(value)

    def snapshot(self) -> dict[str, Any]:
        """Compute live operational health metrics from DB."""
        now = timezone.now()
        recent_runs = FetchRun.objects.filter(started_at__gte=now - timezone.timedelta(hours=24))
        total_runs = recent_runs.count()
        success_runs = recent_runs.filter(status=FetchRunStatus.SUCCESS).count()
        fetch_success_rate = (success_runs / total_runs) if total_runs else 1.0

        recent_items = SourceItem.objects.filter(
            collected_at__gte=now - timezone.timedelta(hours=24)
        )
        items_created = recent_items.count()

        recent_decisions = ClusteringDecision.objects.filter(
            created_at__gte=now - timezone.timedelta(hours=24)
        )
        total_decisions = recent_decisions.count()
        ambiguous_count = recent_decisions.filter(decision="ambiguous").count()
        ambiguous_rate = (ambiguous_count / total_decisions) if total_decisions else 0.0

        recent_pubs = Publication.objects.filter(created_at__gte=now - timezone.timedelta(hours=24))
        pub_success = recent_pubs.filter(status=PublicationStatus.PUBLISHED).count()
        pub_failure = recent_pubs.filter(status=PublicationStatus.FAILED).count()

        return {
            "fetch_success_rate": round(fetch_success_rate, 4),
            "items_created_24h": items_created,
            "clustering_decisions_24h": total_decisions,
            "ambiguous_rate_24h": round(ambiguous_rate, 4),
            "publications_published_24h": pub_success,
            "publications_failed_24h": pub_failure,
            "in_memory_counters": dict(self._counters),
            "in_memory_gauges": dict(self._gauges),
        }


metrics = MetricsRegistry()


def log_event(
    event: str,
    *,
    level: str = "info",
    correlation_id: str | None = None,
    job_id: str | None = None,
    fetch_run_id: int | None = None,
    story_id: int | None = None,
    publication_id: int | None = None,
    **kwargs: Any,
) -> None:
    """Emit structured log with correlation context and secret redaction."""
    safe_data = {k: sanitize_error_message(str(v)) for k, v in kwargs.items()}
    extra = {
        "correlation_id": correlation_id,
        "job_id": job_id,
        "fetch_run_id": fetch_run_id,
        "story_id": story_id,
        "publication_id": publication_id,
        "event": event,
        **safe_data,
    }
    log_fn = getattr(logger, level.lower(), logger.info)
    log_fn(event, extra=extra)
