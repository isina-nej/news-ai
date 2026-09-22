"""Tests for Phase C: ItemMetricSeriesService, StoryMomentumService, and MomentumMetrics."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.core.choices import LifecycleState, Platform, TrendState
from apps.news.models import EngagementSnapshot, SourceItem
from apps.ranking.models import SourceBaseline
from apps.sources.models import Source
from apps.stories.models import Story, StoryMembership, StoryObservationState
from apps.stories.services.momentum import StoryMomentumService
from apps.stories.services.series import ItemMetricSeriesService

pytestmark = pytest.mark.django_db


def _setup_story_with_snapshots():
    source = Source.objects.create(name="Channel A", platform=Platform.TELEGRAM, identifier="@ch_a")
    story = Story.objects.create(canonical_title="Breaking Tech News")
    now = timezone.now()

    # Seed baseline for views at 60m bucket
    SourceBaseline.objects.create(
        source=source,
        platform=Platform.TELEGRAM,
        age_bucket_minutes=60,
        metric="views",
        sample_count=50,
        p25=100.0,
        p50=500.0,
        p75=1500.0,
        p90=3000.0,
        p95=5000.0,
        p99=10000.0,
    )

    item1 = SourceItem.objects.create(
        source=source,
        title="Item 1",
        story=story,
        external_id="item-1",
        published_at=now - timedelta(hours=2),
        collected_at=now - timedelta(hours=2),
    )
    StoryMembership.objects.create(
        story=story,
        source_item=item1,
        is_current=True,
        independence="independent",
    )

    # 3 snapshots for item 1
    t1 = now - timedelta(minutes=70)
    t2 = now - timedelta(minutes=40)
    t3 = now - timedelta(minutes=10)

    EngagementSnapshot.objects.create(
        source_item=item1,
        captured_at=t1,
        post_age_seconds=3000,
        views=200,
        forwards=10,
    )
    EngagementSnapshot.objects.create(
        source_item=item1,
        captured_at=t2,
        post_age_seconds=4800,
        views=800,
        forwards=40,
    )
    EngagementSnapshot.objects.create(
        source_item=item1,
        captured_at=t3,
        post_age_seconds=6600,
        views=2500,
        forwards=150,
    )

    return story, item1, source


def test_item_metric_series_velocity_and_acceleration():
    story, item1, source = _setup_story_with_snapshots()
    series = ItemMetricSeriesService.compute_item_series_metrics(item1)

    assert series["sample_count"] == 3
    assert series["view_velocity"] is not None
    assert series["view_velocity"] > 0
    assert series["forward_velocity"] is not None
    assert series["forward_velocity"] > 0
    assert series["normalized_velocity"] is not None
    assert 0.0 <= series["normalized_velocity"] <= 1.0
    assert series["ewma_velocity"] is not None
    # Acceleration should be present for 3+ points
    assert series["acceleration"] is not None


def test_item_metric_series_counter_reset_anomaly():
    source = Source.objects.create(
        name="Src", platform=Platform.RSS, identifier="https://ex.com/feed"
    )
    item = SourceItem.objects.create(source=source, title="Reset test", external_id="rst-1")
    now = timezone.now()

    # Views drop from 1000 to 100 (counter reset or platform glitch)
    EngagementSnapshot.objects.create(
        source_item=item,
        captured_at=now - timedelta(minutes=20),
        views=1000,
    )
    EngagementSnapshot.objects.create(
        source_item=item,
        captured_at=now,
        views=100,
    )

    series = ItemMetricSeriesService.compute_item_series_metrics(item)
    assert series["sample_count"] == 2
    # Anomaly handled: delta not negative
    assert series["view_velocity"] == 0.0


def test_story_momentum_service_computation():
    story, item1, source = _setup_story_with_snapshots()
    # Add a second source
    source2 = Source.objects.create(
        name="Channel B", platform=Platform.TELEGRAM, identifier="@ch_b"
    )
    item2 = SourceItem.objects.create(
        source=source2,
        title="Item 2",
        story=story,
        external_id="item-2",
        published_at=timezone.now() - timedelta(minutes=10),
    )
    StoryMembership.objects.create(
        story=story,
        source_item=item2,
        is_current=True,
        independence="independent",
    )
    story.observed_source_count = 2
    story.independent_source_count = 2
    story.save()

    metrics = StoryMomentumService.compute_metrics(story)

    assert metrics.observed_sources_count == 2
    assert metrics.independent_sources_count == 2
    assert metrics.source_arrival_15m >= 1
    assert metrics.independent_arrival_15m >= 1
    assert 0.0 <= metrics.normalized_momentum <= 1.0
    assert 0.0 <= metrics.momentum_confidence <= 1.0
    assert metrics.momentum_score_decimal >= Decimal("0.0000")
    assert "normalized_velocity" in metrics.component_breakdown
    assert "acceleration_component" in metrics.component_breakdown


def test_story_momentum_recompute_and_persist():
    story, item1, source = _setup_story_with_snapshots()
    snap = StoryMomentumService.recompute_and_persist(
        story.pk,
        lifecycle_state=LifecycleState.RISING,
        trend_state=TrendState.SURGING,
        transition_reason="velocity_surge",
    )

    assert snap is not None
    assert snap.story == story
    assert snap.lifecycle_state == LifecycleState.RISING
    assert snap.trend_state == TrendState.SURGING
    assert snap.transition_reason == "velocity_surge"
    assert snap.momentum_score >= Decimal("0.0000")

    # Observation state updated
    obs = StoryObservationState.objects.get(story=story)
    assert obs.lifecycle_state == LifecycleState.RISING
    assert obs.trend_state == TrendState.SURGING
    assert obs.sample_count >= 1
    assert obs.latest_momentum == snap.momentum_score
