"""Celery tasks for the Phase 4 intelligence pipeline (idempotent, bounded retry)."""

from __future__ import annotations

import random
from typing import Any

from celery import shared_task


@shared_task(
    bind=True,
    max_retries=3,
    acks_late=True,
    queue="intelligence",
    autoretry_for=(),
)
def prepare_clustering_metadata_task(self, source_item_id: int) -> dict[str, Any]:
    """Compute MinHash sketch + language/metadata. Pure CPU, no network."""
    from django.conf import settings

    from apps.news.models import SourceItem
    from apps.stories.models import ItemMinHash
    from apps.stories.services.language import detect_language
    from apps.stories.services.lexical import digest_to_list, minhash_for_text
    from apps.stories.services.representation import (
        build_clustering_text,
        clustering_input_hash,
    )

    try:
        item = SourceItem.objects.get(pk=source_item_id)
    except SourceItem.DoesNotExist:
        return {"status": "skipped", "reason": "item_not_found"}
    text = build_clustering_text(item.title, item.normalized_text)
    detected = detect_language(text)
    if (item.language or "und") == "und" and detected.get("reliable"):
        item.language = detected["language"]
        item.save(update_fields=["language", "updated_at"])
    num_perm = int(getattr(settings, "MINHASH_NUM_PERM", 128))
    scheme = str(getattr(settings, "MINHASH_SCHEME", "affine32"))
    try:
        mh = minhash_for_text(text, num_perm=num_perm, scheme=scheme)
        ItemMinHash.objects.update_or_create(
            source_item=item,
            defaults={
                "num_perm": num_perm,
                "scheme": scheme,
                "digest": digest_to_list(mh),
            },
        )
    except Exception as exc:
        return {"status": "failed", "error": str(exc)[:200]}
    return {
        "status": "ready",
        "item_id": item.pk,
        "input_hash": clustering_input_hash(item.title, item.normalized_text),
        "language": item.language,
    }


@shared_task(
    bind=True,
    max_retries=2,
    acks_late=True,
    queue="intelligence",
    autoretry_for=(),
)
def embed_item_task(self, source_item_id: int) -> dict[str, Any]:
    """Compute embedding + upsert Qdrant point. Eventual-consistent, never blocks ingest."""
    from apps.news.models import SourceItem
    from apps.stories.models import ItemEmbedding
    from apps.stories.services import vector_store
    from apps.stories.services.embeddings import embedding_identity, get_embedding_provider
    from apps.stories.services.representation import build_clustering_text, clustering_input_hash

    try:
        item = SourceItem.objects.get(pk=source_item_id)
    except SourceItem.DoesNotExist:
        return {"status": "skipped", "reason": "item_not_found"}
    ident = embedding_identity()
    input_hash = clustering_input_hash(item.title, item.normalized_text)
    existing = ItemEmbedding.objects.filter(source_item=item).first()
    if (
        existing is not None
        and existing.status == "indexed"
        and existing.content_hash == input_hash
        and existing.model == ident["model"]
        and existing.model_version == ident["model_version"]
    ):
        return {"status": "skipped", "reason": "already_indexed"}
    text = build_clustering_text(item.title, item.normalized_text)
    try:
        vector = get_embedding_provider().embed([text])[0]
    except Exception as exc:
        ItemEmbedding.objects.update_or_create(
            source_item=item,
            defaults={
                "content_hash": input_hash,
                "provider": str(ident["provider"]),
                "model": str(ident["model"]),
                "model_version": str(ident["model_version"]),
                "dimension": int(ident["dimension"]),
                "status": "failed",
                "point_id": vector_store.point_id_for_item(item.pk),
                "error": str(exc)[:500],
            },
        )
        countdown = int((2**self.request.retries) * 30 + random.uniform(2, 8))  # noqa: S311
        raise self.retry(exc=exc, countdown=countdown) from exc
    ItemEmbedding.objects.update_or_create(
        source_item=item,
        defaults={
            "content_hash": input_hash,
            "provider": str(ident["provider"]),
            "model": str(ident["model"]),
            "model_version": str(ident["model_version"]),
            "dimension": len(vector),
            "status": "pending",
            "point_id": vector_store.point_id_for_item(item.pk),
            "error": "",
        },
    )
    try:
        vector_store.ensure_collection(
            len(vector),
            provider=str(ident["provider"]),
            model=str(ident["model"]),
            model_version=str(ident["model_version"]),
        )
        vector_store.upsert_point(
            point_id=vector_store.point_id_for_item(item.pk),
            vector=vector,
            payload={"source_item_id": item.pk},
            provider=str(ident["provider"]),
            model=str(ident["model"]),
            model_version=str(ident["model_version"]),
        )
        ItemEmbedding.objects.filter(source_item=item).update(status="indexed")
    except ValueError:
        ItemEmbedding.objects.filter(source_item=item).update(status="failed")
        raise
    except Exception as exc:
        ItemEmbedding.objects.filter(source_item=item).update(status="failed")
        countdown = int((2**self.request.retries) * 30 + random.uniform(2, 8))  # noqa: S311
        raise self.retry(exc=exc, countdown=countdown) from exc
    return {"status": "indexed", "item_id": item.pk, "dimension": len(vector)}


@shared_task(
    bind=True,
    max_retries=2,
    acks_late=True,
    queue="intelligence",
    autoretry_for=(),
)
def cluster_source_item_task(
    self, source_item_id: int, *, correlation_id: str | None = None
) -> dict[str, Any]:
    """Assign one SourceItem to a Story (or create one). Idempotent."""
    from apps.stories.services.clustering import story_clustering_service

    try:
        return story_clustering_service.cluster_item(
            source_item_id, correlation_id=correlation_id or ""
        )
    except Exception as exc:
        countdown = int((2**self.request.retries) * 20 + random.uniform(1, 5))  # noqa: S311
        raise self.retry(exc=exc, countdown=countdown) from exc


@shared_task(
    bind=True,
    max_retries=2,
    acks_late=True,
    queue="celery",
    autoretry_for=(),
)
def recompute_story_momentum_task(
    self, story_id: int, *, has_material_update: bool = False
) -> dict[str, Any]:
    """Thin Celery task to recompute story momentum and evaluate adaptive transitions."""
    from apps.stories.services.coordinator import StoryReanalysisCoordinator

    try:
        return StoryReanalysisCoordinator.process_story(
            story_id=story_id, has_material_update=has_material_update
        )
    except Exception as exc:
        countdown = int((2**self.request.retries) * 15 + random.uniform(1, 5))  # noqa: S311
        raise self.retry(exc=exc, countdown=countdown) from exc


@shared_task(queue="celery")
def observe_due_stories_task(*, limit: int = 50) -> dict[str, Any]:
    """Periodic dispatcher: scan active stories due for observation and dispatch recompute."""
    from django.utils import timezone

    from apps.stories.models import StoryObservationState

    now = timezone.now()
    due_states = list(
        StoryObservationState.objects.filter(active=True, next_observation_at__lte=now)
        .order_by("next_observation_at")
        .values_list("story_id", flat=True)[:limit]
    )
    for story_id in due_states:
        recompute_story_momentum_task.delay(story_id=story_id)

    return {"dispatched_count": len(due_states)}


@shared_task(
    bind=True,
    max_retries=2,
    acks_late=True,
    queue="celery",
    autoretry_for=(),
)
def immediate_rescore_task(self, story_id: int, *, is_breaking: bool = False) -> dict[str, Any]:
    """Fast-path ranking: immediate evaluation on breaking news or acceleration spike."""
    from apps.ranking.services.selection import SelectionService
    from apps.stories.models import Story

    try:
        story = Story.objects.get(pk=story_id)
        eval_res = SelectionService.evaluate_story(story)
        if eval_res.get("action") == "publish":
            from apps.publishing.services import _auto_publish_enabled, publish_story

            if _auto_publish_enabled() or is_breaking:
                publish_story(story.pk, force=is_breaking)
        return eval_res
    except Exception as exc:
        countdown = int((2**self.request.retries) * 15 + random.uniform(1, 5))  # noqa: S311
        raise self.retry(exc=exc, countdown=countdown) from exc
