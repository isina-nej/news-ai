"""Tests for Phase D: TrendDetector, LifecycleStateMachine, and coordinator."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.core.choices import LifecycleState, Platform, TrendState
from apps.news.models import EngagementSnapshot, SourceItem
from apps.sources.models import Source
from apps.stories.models import Story, StoryMembership, StoryObservationState
from apps.stories.services.coordinator import StoryReanalysisCoordinator
from apps.stories.services.lifecycle import LifecycleStateMachine
from apps.stories.services.momentum_metrics import MomentumMetrics
from apps.stories.services.trend import TrendDetector
from apps.stories.tasks import observe_due_stories_task

pytestmark = pytest.mark.django_db


def test_trend_detector_early_signal():
    metrics = MomentumMetrics(
        normalized_momentum=0.55,
        acceleration=0.25,
        independent_sources_count=2,
        observed_sources_count=2,
        momentum_confidence=0.50,
    )
    trend, reason = TrendDetector.detect_state(metrics)
    assert trend == TrendState.EARLY_SIGNAL
    assert "early_signal" in reason


def test_trend_detector_breaking_and_hysteresis():
    # Normal to Breaking requires 0.85+ and samples >= 2
    metrics_high = MomentumMetrics(
        normalized_momentum=0.88,
        acceleration=0.30,
        momentum_confidence=0.80,
        independent_sources_count=3,
        observed_sources_count=3,
    )
    trend, _ = TrendDetector.detect_state(
        metrics_high, current_state=TrendState.NORMAL, sample_count=3
    )
    assert trend == TrendState.BREAKING

    # Exit hysteresis: 0.80 keeps BREAKING because exit threshold is 0.75
    metrics_drop = MomentumMetrics(
        normalized_momentum=0.80,
        acceleration=-0.05,
        momentum_confidence=0.75,
    )
    trend2, reason2 = TrendDetector.detect_state(metrics_drop, current_state=TrendState.BREAKING)
    assert trend2 == TrendState.BREAKING
    assert reason2 == "breaking_maintained"

    # Drop below 0.75 exits breaking
    metrics_cooled = MomentumMetrics(
        normalized_momentum=0.70,
        acceleration=-0.15,
        momentum_confidence=0.60,
    )
    trend3, _ = TrendDetector.detect_state(metrics_cooled, current_state=TrendState.BREAKING)
    assert trend3 != TrendState.BREAKING


def test_lifecycle_state_machine_transitions():
    metrics = MomentumMetrics(
        normalized_momentum=0.86,
        acceleration=0.20,
    )
    # Moving from WATCHING to BREAKING
    state, reason = LifecycleStateMachine.evaluate_lifecycle(
        current_lifecycle=LifecycleState.WATCHING,
        trend_state=TrendState.BREAKING,
        metrics=metrics,
    )
    assert state == LifecycleState.BREAKING
    assert reason == "breaking_trend_transition"

    # Reactivation from COOLING via material update
    state2, reason2 = LifecycleStateMachine.evaluate_lifecycle(
        current_lifecycle=LifecycleState.COOLING,
        trend_state=TrendState.NORMAL,
        metrics=MomentumMetrics(normalized_momentum=0.40),
        has_material_update=True,
    )
    assert state2 == LifecycleState.RISING
    assert reason2 == "material_update_reactivation"


def test_adaptive_observation_intervals():
    int_breaking = LifecycleStateMachine.get_interval_seconds(
        LifecycleState.BREAKING, TrendState.BREAKING
    )
    assert int_breaking == 60

    int_rising = LifecycleStateMachine.get_interval_seconds(
        LifecycleState.RISING, TrendState.RISING
    )
    assert int_rising == 180

    int_early = LifecycleStateMachine.get_interval_seconds(
        LifecycleState.WATCHING, TrendState.EARLY_SIGNAL
    )
    assert int_early == 300

    int_cooling = LifecycleStateMachine.get_interval_seconds(
        LifecycleState.COOLING, TrendState.COOLING
    )
    assert int_cooling == 1800


def test_story_coordinator_and_fast_path():
    source = Source.objects.create(name="Ch", platform=Platform.TELEGRAM, identifier="@ch")
    story = Story.objects.create(canonical_title="Breaking Tech Story")
    item = SourceItem.objects.create(source=source, story=story, title="Post 1", external_id="p-1")
    StoryMembership.objects.create(
        story=story, source_item=item, is_current=True, independence="independent"
    )

    # Add 2 snapshots with rapid growth
    now = timezone.now()
    EngagementSnapshot.objects.create(
        source_item=item, captured_at=now - timedelta(minutes=15), views=100
    )
    EngagementSnapshot.objects.create(source_item=item, captured_at=now, views=5000, forwards=300)

    with patch("apps.stories.tasks.immediate_rescore_task.delay") as mock_fast_path:
        result = StoryReanalysisCoordinator.process_story(story.pk)
        assert result["status"] == "processed"
        assert result["story_id"] == story.pk
        assert result["next_observation_at"] is not None

        obs = StoryObservationState.objects.get(story=story)
        assert obs.sample_count >= 1
        assert obs.next_observation_at is not None
        assert mock_fast_path.called or result["fast_path_triggered"] is not None


def test_observe_due_stories_task_dispatcher():
    story = Story.objects.create(canonical_title="Due Story")
    now = timezone.now()
    StoryObservationState.objects.create(
        story=story,
        active=True,
        next_observation_at=now - timedelta(minutes=5),  # Due
    )

    with patch("apps.stories.tasks.recompute_story_momentum_task.delay") as mock_delay:
        res = observe_due_stories_task(limit=10)
        assert res["dispatched_count"] >= 1
        mock_delay.assert_called_with(story_id=story.pk)
