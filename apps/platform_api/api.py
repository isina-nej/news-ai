"""Versioned internal operations API. Paginated, filtered, ordering-allowlisted."""

from __future__ import annotations

from typing import Any

from django.shortcuts import get_object_or_404
from ninja import NinjaAPI
from ninja.pagination import PageNumberPagination, paginate

from apps.news.models import SourceItem
from apps.ops.models import AuditLog, DynamicSetting, FeatureFlag, Topic
from apps.platform_api.auth import InternalTokenAuth, auth_enabled
from apps.platform_api.schemas import (
    FetchRunOut,
    FlagOut,
    PublicationOut,
    SourceItemOut,
    SourceOut,
    StoryDetailOut,
    StoryOut,
    TopicOut,
)
from apps.publishing.models import Publication
from apps.ranking.models import ScoreRecord
from apps.sources.models import FetchRun, Source
from apps.stories.models import ClusteringDecision, Story, StoryMembership

api_auth = InternalTokenAuth() if auth_enabled() else None
api = NinjaAPI(version="1", urls_namespace="v1", auth=api_auth)

ORDER_ALLOWLIST = {
    "sources": {"id", "name", "-id", "-name", "priority", "-priority"},
    "items": {"id", "-id", "-collected_at", "collected_at", "-published_at", "published_at"},
    "stories": {
        "id",
        "-id",
        "-latest_source_update_at",
        "latest_source_update_at",
        "-first_published_at",
    },
    "publications": {"id", "-id", "-created_at", "created_at"},
}


def _ordering(requested: str | None, allowed: set[str], default: str) -> str:
    if requested in allowed:
        return requested
    return default


@api.get("/health")
def health(request):
    return {"status": "ok", "version": "1"}


@api.get("/sources", response=list[SourceOut])
@paginate(PageNumberPagination, page_size=50)
def list_sources(request, enabled: bool | None = None, ordering: str | None = None):
    qs = Source.objects.all().order_by(_ordering(ordering, ORDER_ALLOWLIST["sources"], "-id"))
    if enabled is not None:
        qs = qs.filter(enabled=enabled)
    return qs


@api.get("/source-items", response=list[SourceItemOut])
@paginate(PageNumberPagination, page_size=50)
def list_items(
    request,
    source_id: int | None = None,
    status: str | None = None,
    story_id: int | None = None,
    ordering: str | None = None,
):
    qs = SourceItem.objects.select_related("source").order_by(
        _ordering(ordering, ORDER_ALLOWLIST["items"], "-id")
    )
    if source_id:
        qs = qs.filter(source_id=source_id)
    if status:
        qs = qs.filter(status=status)
    if story_id:
        qs = qs.filter(story_id=story_id)
    return [
        SourceItemOut(
            id=item.pk,
            source_id=item.source_id,
            title=item.title[:200],
            language=item.language,
            status=item.status,
            published_at=item.published_at.isoformat() if item.published_at else None,
            collected_at=item.collected_at.isoformat() if item.collected_at else None,
            story_id=item.story_id,
        )
        for item in qs[:200]
    ]


@api.get("/stories", response=list[StoryOut])
@paginate(PageNumberPagination, page_size=50)
def list_stories(request, status: str | None = None, ordering: str | None = None):
    qs = Story.objects.all().order_by(
        _ordering(ordering, ORDER_ALLOWLIST["stories"], "-latest_source_update_at")
    )
    if status:
        qs = qs.filter(status=status)
    return qs


@api.post("/stories/merge")
def merge_stories_api(request, source_story_id: int, target_story_id: int):
    from apps.stories.services.merge import merge_stories

    result = merge_stories(
        source_story_id=source_story_id, target_story_id=target_story_id, reason="api"
    )
    _audit("story.merge", "story", str(source_story_id), result, request)
    return result


@api.get("/stories/{story_id}", response=StoryDetailOut)
def story_detail(request, story_id: int):
    story = get_object_or_404(Story, pk=story_id)
    members = [
        {
            "source_item_id": m.source_item_id,
            "similarity_score": float(m.similarity_score) if m.similarity_score else None,
            "match_method": m.match_method,
            "independence": m.independence,
            "is_primary": m.is_primary,
            "is_current": m.is_current,
        }
        for m in StoryMembership.objects.filter(story=story, is_current=True)[:50]
    ]
    scores = [
        {
            "algorithm_version": s.algorithm_version,
            "news_value": float(s.news_value),
            "audience_fit": float(s.audience_fit),
            "momentum": float(s.momentum),
            "final_score": float(s.final_score),
        }
        for s in ScoreRecord.objects.filter(story=story).order_by("-created_at")[:10]
    ]
    publications = [
        {"id": p.pk, "status": p.status, "channel": p.channel}
        for p in Publication.objects.filter(story=story)[:10]
    ]
    return {
        "id": story.pk,
        "canonical_title": story.canonical_title,
        "status": story.status,
        "language": story.language,
        "observed_source_count": story.observed_source_count,
        "independent_source_count": story.independent_source_count,
        "primary_item_id": story.primary_item_id,
        "members": members,
        "scores": scores,
        "publications": publications,
    }


@api.get("/story-members", response=list[dict])
@paginate(PageNumberPagination, page_size=50)
def list_members(request, story_id: int):
    return list(
        StoryMembership.objects.filter(story_id=story_id, is_current=True).values(
            "id", "source_item_id", "match_method", "independence", "is_primary"
        )[:200]
    )


@api.get("/clustering-decisions", response=list[dict])
@paginate(PageNumberPagination, page_size=50)
def list_decisions(request, story_id: int | None = None):
    qs = ClusteringDecision.objects.all().order_by("-created_at")
    if story_id:
        qs = qs.filter(candidate_story_id=story_id)
    return list(
        qs.values("id", "source_item_id", "candidate_story_id", "decision", "created_at")[:200]
    )


@api.get("/scores", response=list[dict])
@paginate(PageNumberPagination, page_size=50)
def list_scores(request, story_id: int):
    return list(
        ScoreRecord.objects.filter(story_id=story_id)
        .order_by("-created_at")
        .values("id", "algorithm_version", "final_score", "created_at")[:50]
    )


@api.get("/publications", response=list[PublicationOut])
@paginate(PageNumberPagination, page_size=50)
def list_publications(request, status: str | None = None, ordering: str | None = None):
    qs = Publication.objects.all().order_by(
        _ordering(ordering, ORDER_ALLOWLIST["publications"], "-id")
    )
    if status:
        qs = qs.filter(status=status)
    return qs


@api.get("/fetch-runs", response=list[FetchRunOut])
@paginate(PageNumberPagination, page_size=50)
def list_fetch_runs(request, source_id: int | None = None):
    qs = FetchRun.objects.all().order_by("-started_at")
    if source_id:
        qs = qs.filter(source_id=source_id)
    return qs[:200]


@api.get("/jobs/status")
def jobs_status(request) -> dict[str, Any]:
    from apps.ai.models import AITask
    from apps.stories.models import ItemEmbedding

    return {
        "ai_pending": AITask.objects.filter(state="pending").count(),
        "ai_failed": AITask.objects.filter(state="failed").count(),
        "embeddings_pending": ItemEmbedding.objects.filter(status="pending").count(),
        "embeddings_failed": ItemEmbedding.objects.filter(status="failed").count(),
    }


@api.get("/topics", response=list[TopicOut])
@paginate(PageNumberPagination, page_size=50)
def list_topics(request):
    return Topic.objects.all().order_by("slug")[:200]


@api.get("/settings", response=list[dict])
@paginate(PageNumberPagination, page_size=50)
def list_settings(request):
    return list(DynamicSetting.objects.all().values("key", "description")[:200])


@api.get("/feature-flags", response=list[FlagOut])
@paginate(PageNumberPagination, page_size=50)
def list_flags(request):
    return FeatureFlag.objects.all().order_by("key")[:100]


def _audit(action: str, entity_type: str, entity_id: str, detail: dict, request) -> None:
    AuditLog.objects.create(
        actor=str(getattr(request, "user", "api") or "api"),
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        detail=detail,
    )


@api.post("/stories/{story_id}/re-score")
def rescore_story(request, story_id: int):
    from apps.ranking.services.scoring import StoryScoringService

    story = get_object_or_404(Story, pk=story_id)
    result = StoryScoringService.score_story(story)
    _audit("story.rescore", "story", story_id, {"final_score": result["final_score"]}, request)
    return result


@api.post("/stories/{story_id}/re-cluster")
def recluster_story(request, story_id: int):
    from apps.stories.models import StoryMembership

    story = get_object_or_404(Story, pk=story_id)
    count = 0
    for membership in StoryMembership.objects.filter(story=story, is_current=True):
        from apps.stories.tasks import cluster_source_item_task

        cluster_source_item_task.delay(membership.source_item_id)
        count += 1
    _audit("story.recluster", "story", story_id, {"queued": count}, request)
    return {"status": "queued", "items": count}


@api.post("/items/{item_id}/reassign")
def reassign_item_api(request, item_id: int, target_story_id: int):
    from apps.stories.services.merge import reassign_item

    result = reassign_item(source_item_id=item_id, target_story_id=target_story_id, reason="api")
    _audit("item.reassign", "source_item", item_id, result, request)
    return result


@api.post("/publications/{publication_id}/approve")
def approve_publication(request, publication_id: int):
    publication = get_object_or_404(Publication, pk=publication_id)
    publication.transition("approved")
    _audit("publication.approve", "publication", publication_id, {}, request)
    return {"status": publication.status}


@api.post("/publications/{publication_id}/reject")
def reject_publication(request, publication_id: int):
    publication = get_object_or_404(Publication, pk=publication_id)
    publication.transition("cancelled")
    _audit("publication.reject", "publication", publication_id, {}, request)
    return {"status": publication.status}


@api.post("/stories/{story_id}/dry-run-publish")
def dry_run_publish(request, story_id: int):
    from apps.publishing.services import publish_story

    result = publish_story(story_id, dry_run=True)
    _audit("publication.dry_run", "story", story_id, {"status": result["status"]}, request)
    return result


@api.post("/sources/{source_id}/trigger-fetch")
def trigger_fetch(request, source_id: int):
    get_object_or_404(Source, pk=source_id)
    from apps.sources.tasks import fetch_source_task

    fetch_source_task.delay(source_id)
    _audit("source.trigger_fetch", "source", source_id, {}, request)
    return {"status": "queued", "source_id": source_id}
