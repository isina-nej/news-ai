"""Publishing queue tasks. Idempotent; never duplicate on retry."""

from __future__ import annotations

import random
from typing import Any

from celery import shared_task


@shared_task(
    bind=True,
    max_retries=3,
    acks_late=True,
    queue="celery",
    autoretry_for=(),
)
def publish_story_task(self, story_id: int, *, dry_run: bool = False) -> dict[str, Any]:
    from apps.publishing.services import publish_story

    try:
        return publish_story(story_id, dry_run=dry_run)
    except Exception as exc:
        countdown = int((2**self.request.retries) * 30 + random.uniform(2, 8))  # noqa: S311
        raise self.retry(exc=exc, countdown=countdown) from exc


@shared_task(
    bind=True,
    max_retries=5,
    acks_late=True,
    queue="celery",
    autoretry_for=(),
)
def collect_publication_snapshot_task(self, publication_id: int) -> dict[str, Any]:
    from apps.publishing.models import Publication, PublicationEngagementSnapshot

    try:
        publication = Publication.objects.get(pk=publication_id)
    except Publication.DoesNotExist:
        return {"status": "skipped", "reason": "publication_not_found"}

    # Bot API exposes message views/forwards where available; unavailable stays NULL.
    PublicationEngagementSnapshot.objects.create(publication=publication)
    return {"status": "scheduled", "publication_id": publication.pk}
