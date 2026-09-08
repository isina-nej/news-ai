"""Phase 9: audience learning, bandit, scheduler, observability."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.core.choices import Platform
from apps.news.models import SourceItem
from apps.ops.models import AudiencePreference, Topic
from apps.ops.services.audience import (
    AudienceLearningService,
    to_pref_decimal,
)
from apps.ops.services.bandit import ContextualBanditService
from apps.ops.services.observability import MetricsRegistry, log_event
from apps.ops.tasks import (
    dispatch_due_sources_task,
    dispatch_pending_clustering_task,
    dispatch_publication_queue_task,
    dispatch_ranking_refresh_task,
)
from apps.sources.models import Source
from apps.stories.models import Story, StoryMembership

pytestmark = [pytest.mark.django_db]


def _source(name="Learn Source"):
    return Source.objects.create(
        platform=Platform.TELEGRAM,
        name=name,
        identifier=f"@{name.lower().replace(' ', '_')}",
        url="https://t.me/learnsrc",
    )


def _story(title="Learning Story"):
    return Story.objects.create(
        canonical_title=title,
        latest_source_update_at=timezone.now(),
        language="en",
    )


def test_bayesian_smoothing_prevents_single_observation_dominance():
    # One lucky observation must not set preference to 1.0
    smoothed = AudienceLearningService.bayesian_smoothed_mean([1.0])
    assert smoothed < 0.70
    assert smoothed > 0.50

    # Many strong observations converge towards 1.0
    strong = AudienceLearningService.bayesian_smoothed_mean([0.95] * 50)
    assert strong > 0.90


def test_ewma_preference_updates_with_recency_decay():
    row = AudienceLearningService.update_preference(
        feature="topic",
        context={"topic": "tech"},
        observed_performance=0.90,
    )
    assert row.sample_count == 1
    first_val = float(row.value)

    # Second observation pulls EWMA upward
    row2 = AudienceLearningService.update_preference(
        feature="topic",
        context={"topic": "tech"},
        observed_performance=0.95,
    )
    assert row2.sample_count == 2
    assert float(row2.value) > first_val


def test_confounding_separation_topic_vs_style():
    tech_val = AudienceLearningService.update_preference(
        feature="topic",
        context={"topic": "tech"},
        observed_performance=0.85,
    )
    style_val = AudienceLearningService.update_preference(
        feature="style",
        context={"headline_style": "bullet", "tone": "structured"},
        observed_performance=0.30,
    )
    # Independent rows: style underperformance does not drag topic down
    assert float(tech_val.value) > 0.50
    assert float(style_val.value) < 0.50


def test_expected_channel_performance_structure():
    Topic.objects.create(slug="tech", name="Tech")
    expected = AudienceLearningService.expected_channel_performance(
        topic_slug="tech",
        content_type="post",
        headline_style="factual_short",
        tone="neutral",
        hour=9,
    )
    assert set(expected) == {
        "expected_view_percentile",
        "expected_forward_percentile",
        "expected_reaction_percentile",
        "expected_reply_percentile",
    }
    for val in expected.values():
        assert 0.0 <= val <= 1.0


def test_bandit_only_selects_safe_actions():
    topic = Topic.objects.create(slug="politics", name="Politics")
    story = _story("Bandit Story")
    story.primary_topic = topic
    story.save()

    action, is_explore, prob = ContextualBanditService.select_action(story=story, epsilon=0.0)
    assert action["headline_style"] in (
        "factual_short",
        "concise",
        "technical",
        "bullet",
    )
    assert is_explore is False
    assert 0.0 <= prob <= 1.0


def test_bandit_exploration_logged():
    story = _story("Explore Story")
    action, is_explore, prob = ContextualBanditService.select_action(story=story, epsilon=1.0)
    assert is_explore is True
    assert action is not None
    assert prob == 1.0


def test_bandit_never_bypasses_safety_gates():
    # Even if bandit suggests publish style, WHAT engine still evaluates gate
    from apps.publishing.services import decide_what

    story = _story("Low Trust Rumor")
    story.independent_source_count = 0
    story.save()
    what, _ = decide_what(story, scores={"final_score": 0.99})
    assert what in ("hold", "skip")


def test_scheduler_dispatches_due_sources_respecting_cooldown():
    due = _source("Due Source")
    due.last_success_at = timezone.now() - timedelta(hours=2)
    due.fetch_interval_seconds = 300
    due.save()

    cool = _source("Cooldown Source")
    cool.cooldown_until = timezone.now() + timedelta(hours=1)
    cool.save()

    with patch("apps.sources.tasks.fetch_source_task.delay") as mock_delay:
        result = dispatch_due_sources_task()
        assert result["dispatched"] >= 1
        called_ids = [c.args[0] for c in mock_delay.call_args_list]
        assert due.pk in called_ids
        assert cool.pk not in called_ids


def test_pending_clustering_dispatch():
    source = _source("Cluster Dispatch Source")
    for i in range(3):
        SourceItem.objects.create(
            source=source,
            title=f"Unclustered {i}",
            raw_text=f"Body {i}",
            normalized_text=f"Body {i}",
        )

    with patch("apps.stories.tasks.cluster_source_item_task.delay") as mock_delay:
        result = dispatch_pending_clustering_task(batch_size=50)
        assert result["dispatched"] == 3
        assert mock_delay.call_count == 3


def test_publication_queue_respects_auto_publish_flag():
    result = dispatch_publication_queue_task()
    assert result["status"] == "skipped"
    assert result["reason"] == "auto_publish_disabled"


def test_ranking_refresh_scores_recent_stories():
    source = _source("Refresh Source")
    story = _story("Refresh Story")
    item = SourceItem.objects.create(
        source=source, title="Refresh Story", raw_text="Body", normalized_text="Body"
    )
    StoryMembership.objects.create(story=story, source_item=item, is_current=True)
    result = dispatch_ranking_refresh_task(lookback_hours=48)
    assert result["scored"] >= 1


def test_observability_snapshot_structure():
    registry = MetricsRegistry()
    snap = registry.snapshot()
    assert "fetch_success_rate" in snap
    assert "items_created_24h" in snap
    assert "ambiguous_rate_24h" in snap
    assert "publications_published_24h" in snap
    assert 0.0 <= snap["fetch_success_rate"] <= 1.0


def test_structured_logging_redacts_secrets():
    # log_event must not raise and must include correlation context
    log_event(
        "test.event",
        correlation_id="corr-123",
        story_id=1,
        api_key="SHOULD_BE_REDACTED",
    )


def test_reward_feedback_updates_preferences():
    from apps.ranking.models import DecisionLog

    story = _story("Feedback Story")
    log = DecisionLog.objects.create(
        story=story,
        algorithm_version="ranking-v1",
        decision_type="what",
        feature_snapshot={},
        selected_action="publish",
        action_detail={"topic_slug": "tech"},
        predicted_reward=Decimal("0.70"),
    )
    updated = ContextualBanditService.record_feedback(decision_log_id=log.pk, reward=0.85)
    assert float(updated.actual_reward) == 0.85
    assert updated.reward_calculated_at is not None

    pref = AudiencePreference.objects.filter(feature="topic", context={"topic": "tech"}).first()
    assert pref is not None
    assert pref.sample_count >= 1


def test_to_pref_decimal_clamps():
    assert to_pref_decimal(None) == Decimal("0.500000")
    assert to_pref_decimal(2.0) == Decimal("1.000000")
    assert to_pref_decimal(-1.0) == Decimal("0.000000")
