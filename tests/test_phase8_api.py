"""Phase 8: API endpoints, pagination, audit logs, and manual operational actions."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client

from apps.core.choices import Platform
from apps.news.models import SourceItem
from apps.ops.models import AuditLog, FeatureFlag
from apps.publishing.models import Publication, PublicationStatus
from apps.ranking.models import ScoreRecord
from apps.sources.models import Source
from apps.stories.models import Story, StoryMembership

pytestmark = [pytest.mark.django_db]


def _source(name="API Source"):
    return Source.objects.create(
        platform=Platform.TELEGRAM,
        name=name,
        identifier=f"@{name.lower().replace(' ', '_')}",
        url="https://t.me/apisrc",
    )


def _story(title="API Story"):
    return Story.objects.create(
        canonical_title=title,
        status="active",
        language="en",
        observed_source_count=1,
        independent_source_count=1,
    )


def _item(source, story, title="Item 1"):
    item = SourceItem.objects.create(
        source=source,
        title=title,
        raw_text=f"Body of {title}",
        normalized_text=f"Body of {title}",
    )
    StoryMembership.objects.create(story=story, source_item=item, is_current=True)
    return item


def test_api_health_endpoint():
    client = Client()
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": "1"}


def test_api_sources_and_pagination():
    client = Client()
    for i in range(5):
        _source(f"Source {i}")
    resp = client.get("/api/v1/sources")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert len(data["items"]) >= 5


def test_api_stories_and_detail():
    client = Client()
    source = _source("Story Source")
    story = _story("Detail Story")
    item = _item(source, story, "Detail Item")
    ScoreRecord.objects.create(
        story=story,
        algorithm_version="ranking-v1",
        news_value=Decimal("0.70"),
        audience_fit=Decimal("0.60"),
        momentum=Decimal("0.50"),
        final_score=Decimal("0.63"),
        breakdown={"news_value": {}, "audience_fit": {}, "momentum": {}},
    )
    Publication.objects.create(
        story=story,
        channel="@channel",
        status=PublicationStatus.DRAFT,
        idempotency_key="detail-pub-1",
    )

    resp_list = client.get("/api/v1/stories")
    assert resp_list.status_code == 200
    assert len(resp_list.json()["items"]) >= 1

    resp_detail = client.get(f"/api/v1/stories/{story.pk}")
    assert resp_detail.status_code == 200
    detail = resp_detail.json()
    assert detail["canonical_title"] == "Detail Story"
    assert len(detail["members"]) == 1
    assert detail["members"][0]["source_item_id"] == item.pk
    assert len(detail["scores"]) == 1
    assert len(detail["publications"]) == 1


def test_api_jobs_status_and_flags():
    client = Client()
    FeatureFlag.objects.create(key="ENABLE_AI", enabled=True)
    resp_flags = client.get("/api/v1/feature-flags")
    assert resp_flags.status_code == 200
    assert any(f["key"] == "ENABLE_AI" for f in resp_flags.json()["items"])

    resp_jobs = client.get("/api/v1/jobs/status")
    assert resp_jobs.status_code == 200
    data = resp_jobs.json()
    assert "ai_pending" in data
    assert "embeddings_pending" in data


def test_manual_action_rescore_with_audit():
    client = Client()
    source = _source("Rescore Source")
    story = _story("Rescore Story")
    _item(source, story)

    resp = client.post(f"/api/v1/stories/{story.pk}/re-score")
    assert resp.status_code == 200
    data = resp.json()
    assert "final_score" in data

    log = AuditLog.objects.filter(entity_type="story", entity_id=str(story.pk)).first()
    assert log is not None
    assert log.action == "story.rescore"


def test_manual_action_merge_with_audit():
    client = Client()
    source = _source("Merge Source")
    s1 = _story("Merge A")
    s2 = _story("Merge B")
    _item(source, s1, "Text A")
    _item(source, s2, "Text B")

    resp = client.post(f"/api/v1/stories/merge?source_story_id={s2.pk}&target_story_id={s1.pk}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "merged"

    log = AuditLog.objects.filter(action="story.merge").first()
    assert log is not None


def test_manual_action_approve_and_reject_publication():
    client = Client()
    story = _story("Pub Transition Story")
    pub = Publication.objects.create(
        story=story,
        channel="@c",
        headline="h",
        content="c",
        status=PublicationStatus.READY,
        idempotency_key="approve-key",
    )

    # Approve
    resp = client.post(f"/api/v1/publications/{pub.pk}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert AuditLog.objects.filter(action="publication.approve").exists()

    # Reject / Cancel
    resp_rej = client.post(f"/api/v1/publications/{pub.pk}/reject")
    assert resp_rej.status_code == 200
    assert resp_rej.json()["status"] == "cancelled"
    assert AuditLog.objects.filter(action="publication.reject").exists()


def test_manual_action_dry_run_publish():
    client = Client()
    source = _source("DryRun Source")
    story = _story("DryRun Story")
    _item(source, story)

    resp = client.post(f"/api/v1/stories/{story.pk}/dry-run-publish")
    assert resp.status_code == 200
    assert resp.json()["status"] == "dry_run"
    assert AuditLog.objects.filter(action="publication.dry_run").exists()


def test_manual_action_trigger_fetch():
    client = Client()
    source = _source("Fetch Source")
    with patch("apps.sources.tasks.fetch_source_task.delay") as mock_delay:
        resp = client.post(f"/api/v1/sources/{source.pk}/trigger-fetch")
        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"
        mock_delay.assert_called_once_with(source.pk)
    assert AuditLog.objects.filter(action="source.trigger_fetch").exists()
