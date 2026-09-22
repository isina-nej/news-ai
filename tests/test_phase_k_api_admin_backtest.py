"""Tests for Phase K: API endpoints, Admin, Observability, and Backtest CLI."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from apps.core.choices import LifecycleState, Platform, TrendState
from apps.news.models import SourceItem
from apps.ops.services.observability import metrics
from apps.ranking.models import PublicationSelectionRun
from apps.sources.models import Source
from apps.stories.models import Story, StoryMembership, StoryObservationState

pytestmark = pytest.mark.django_db


def _create_story_for_api():
    now = timezone.now()
    source = Source.objects.create(
        name="API Source", platform=Platform.TELEGRAM, identifier="@api_src"
    )
    story = Story.objects.create(
        canonical_title="Autonomous Drone Logistics",
        latest_source_update_at=now,
    )
    item = SourceItem.objects.create(
        source=source,
        story=story,
        title="Drone Logistics",
        external_id="ext-dr-1",
        published_at=now,
    )
    StoryMembership.objects.create(story=story, source_item=item, is_current=True, is_primary=True)
    story.primary_item = item
    story.save()

    StoryObservationState.objects.create(
        story=story,
        lifecycle_state=LifecycleState.RISING,
        trend_state=TrendState.SURGING,
        latest_momentum=Decimal("0.8500"),
        latest_confidence=Decimal("0.9000"),
    )
    return story


def test_story_detail_api_includes_momentum_and_intelligence():
    story = _create_story_for_api()
    client = Client()

    response = client.get(f"/api/v1/stories/{story.pk}")
    assert response.status_code == 200
    data = response.json()

    assert data["id"] == story.pk
    assert data["canonical_title"] == story.canonical_title
    assert data["observation"] is not None
    assert data["observation"]["lifecycle_state"] == "rising"
    assert data["observation"]["trend_state"] == "surging"
    assert data["observation"]["latest_momentum"] == 0.85


def test_selection_runs_api_endpoint():
    PublicationSelectionRun.objects.create(
        algorithm_version="ranking-v2",
        policy_version="editorial-v1",
        candidates=[{"story_id": 1, "score": 0.8, "selected": True}],
        selected_count=1,
    )

    client = Client()
    response = client.get("/api/v1/selection-runs")
    assert response.status_code == 200
    data = response.json()

    assert "items" in data
    assert len(data["items"]) >= 1
    run_entry = data["items"][0]
    assert run_entry["algorithm_version"] == "ranking-v2"
    assert run_entry["selected_count"] == 1


def test_observability_snapshot_includes_momentum_counters():
    _create_story_for_api()
    snap = metrics.snapshot()

    assert "stories_watched" in snap
    assert "stories_rising" in snap
    assert "stories_breaking" in snap
    assert snap["stories_rising"] >= 1


def test_ranking_backtest_cli_with_extended_options(capsys):
    story = _create_story_for_api()
    call_command(
        "ranking_backtest",
        days=3,
        limit=5,
        ranking_version="ranking-v2",
        momentum_version="momentum-v2",
        json=True,
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert "total_stories" in report
    assert report["total_stories"] >= 1
    assert "records" in report
    rec = report["records"][0]
    assert rec["story_id"] == story.pk
    assert rec["ranking_version"] == "ranking-v2"
    assert rec["momentum_version"] == "momentum-v2"
    assert "newsworthiness_score" in rec
    assert "publish_priority_score" in rec
