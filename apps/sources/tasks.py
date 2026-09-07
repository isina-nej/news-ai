"""Celery background tasks for source fetching with bounded exponential backoff and jitter."""

from __future__ import annotations

import random
from typing import Any

from celery import shared_task

from apps.sources.adapters.base import NetworkError, RateLimitError, TimeoutError
from apps.sources.services.fetcher import source_fetch_service


@shared_task(
    bind=True,
    max_retries=3,
    acks_late=True,
    autoretry_for=(),  # Retries handled explicitly to control backoff & jitter
)
def fetch_source_task(self, source_id: int, *, correlation_id: str | None = None) -> dict[str, Any]:
    """Thin Celery task delegating ingestion to SourceFetchService."""
    try:
        return source_fetch_service.fetch_source(source_id, correlation_id=correlation_id)
    except RateLimitError as exc:
        # Honor Retry-After if server provided one; otherwise exponential backoff
        countdown = exc.retry_after or int((2**self.request.retries) * 60 + random.uniform(5, 15))  # noqa: S311
        raise self.retry(exc=exc, countdown=countdown) from exc
    except (NetworkError, TimeoutError) as exc:
        # Transient connection/timeout error: exponential backoff + jitter
        countdown = int((2**self.request.retries) * 30 + random.uniform(2, 8))  # noqa: S311
        raise self.retry(exc=exc, countdown=countdown) from exc
