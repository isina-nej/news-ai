"""Phase 6: Ranking, source-relative baselines, credibility gates, selection, backtesting."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.ai.models import MaterialUpdateDecision
from apps.core.choices import ContentType, Platform
from apps.news.models import EngagementSnapshot, SourceItem
from apps.ops.models import Topic
from apps.publishing.models import Publication, PublicationStatus
from apps.ranking.models import DecisionLog, ScoreRecord, SourceBaseline
from apps.ranking.services.baselines import (
    SourceBaselineService,
    calculate_percentiles,
)
from apps.ranking.services.metrics import compute_item_relative_metrics
from apps.ranking.services.scoring import (
    CredibilityGate,
    StoryScoringService,
    compute_freshness,
)
from apps.ranking.services.selection import SelectionService
from apps.sources.models import Source
from apps.stories.models import Story, StoryMembership, StoryStatus

pytestmark = [pytest.mark.django_db]


def _source(name="Rank Source", **kwargs):
    kwargs.setdefault("platform", Platform.TELEGRAM)
    kwargs.setdefault("identifier", f"@{name.lower().replace(' ', '_')}")
    kwargs.setdefault("url", "https://t.me/test")
    return Source.objects.create(name=name, **kwargs)


def _item(source, title="Item title", text="Item body", **kwargs):
    kwargs.setdefault("published_at", timezone.now() - timedelta(hours=2))
    return SourceItem.objects.create(
        source=source, title=title, raw_text=text, normalized_text=text, **kwargs
    )


def _story(title="Story title", **kwargs):
    kwargs.setdefault("canonical_title", title)
    kwargs.setdefault("latest_source_update_at", timezone.now() - timedelta(hours=1))
    return Story.objects.create(**kwargs)


def test_calculate_percentiles():
    obs = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    pcts = calculate_percentiles(obs)
    assert pcts["p25"] < pcts["p50"] < pcts["p75"] < pcts["p99"]
    assert pcts["p50"] == 55.0


def test_percentile_estimation_stepwise():
    baseline = SourceBaseline(
        p25=100.0,
        p50=500.0,
        p75=1000.0,
        p90=2000.0,
        p95=5000.0,
        p99=10000.0,
    )
    # Below p25
    assert 0.0 <= SourceBaselineService.estimate_percentile(50, baseline) <= 0.25
    # Exact median p50
    assert SourceBaselineService.estimate_percentile(500, baseline) == 0.50
    # Between p75 and p90
    assert 0.75 <= SourceBaselineService.estimate_percentile(1500, baseline) <= 0.90
    # Above p99
    assert SourceBaselineService.estimate_percentile(20000, baseline) == 0.9999
    # NULL value remains NULL
    assert SourceBaselineService.estimate_percentile(None, baseline) is None


def test_baseline_fallback_hierarchy():
    topic = Topic.objects.create(slug="finance", name="Finance")
    src = _source("Fallback Source")

    # Seed platform global
    SourceBaselineService.update_baseline_from_observations(
        source=None,
        platform=src.platform,
        metric="views",
        observations=[1000.0] * 10,
    )
    b, level, conf = SourceBaselineService.find_baseline(
        source=src,
        platform=src.platform,
        topic_id=topic.pk,
        subtopic_id=None,
        content_type=ContentType.POST,
        age_bucket_minutes=60,
        metric="views",
    )
    assert b is not None and level == "platform" and conf == 0.35

    # Seed source level
    SourceBaselineService.update_baseline_from_observations(
        source=src,
        platform=src.platform,
        metric="views",
        observations=[500.0] * 10,
    )
    b2, level2, conf2 = SourceBaselineService.find_baseline(
        source=src,
        platform=src.platform,
        topic_id=topic.pk,
        subtopic_id=None,
        content_type=ContentType.POST,
        age_bucket_minutes=60,
        metric="views",
    )
    assert b2.source_id == src.pk and level2 == "source" and conf2 > conf


def test_source_size_bias_elimination():
    """Small source exceeding baseline beats large source underperforming baseline."""
    small_src = _source("Small Blog")
    large_src = _source("Mega News")

    # Small blog baseline: median is 100 views
    SourceBaselineService.update_baseline_from_observations(
        source=small_src,
        platform=small_src.platform,
        metric="views",
        observations=[50.0, 100.0, 150.0, 200.0, 300.0, 500.0] * 5,
    )

    # Mega news baseline: median is 1,000,000 views
    SourceBaselineService.update_baseline_from_observations(
        source=large_src,
        platform=large_src.platform,
        metric="views",
        observations=[500000.0, 800000.0, 1000000.0, 1200000.0, 2000000.0] * 5,
    )

    # Item A: small source gets 5,000 views (50x its median! Exceptional!)
    item_small = _item(small_src, "Small viral post")
    EngagementSnapshot.objects.create(
        source_item=item_small,
        captured_at=timezone.now(),
        post_age_seconds=3600,
        views=5000,
    )
    m_small = compute_item_relative_metrics(item_small, target_age_minutes=60)

    # Item B: large source gets 700,000 views (below its median! Mediocre!)
    item_large = _item(large_src, "Mega ordinary post")
    EngagementSnapshot.objects.create(
        source_item=item_large,
        captured_at=timezone.now(),
        post_age_seconds=3600,
        views=700000,
    )
    m_large = compute_item_relative_metrics(item_large, target_age_minutes=60)

    # Small source's relative performance must beat large source
    assert m_small["views_percentile"] > m_large["views_percentile"]
    assert m_small["views_percentile"] >= 0.95
    assert m_large["views_percentile"] < 0.50


def test_credibility_gate_blocks_low_trust_rumor():
    low_src = _source("Rumor Mill", trust_score=Decimal("0.15"))
    story = _story("Unconfirmed rumor")
    story.independent_source_count = 1
    story.save()
    item = _item(low_src, "Rumor item")
    StoryMembership.objects.create(story=story, source_item=item, is_current=True)

    passed, reason = CredibilityGate.evaluate(story, credibility_score=0.20)
    assert not passed
    assert reason in ("low_credibility", "single_low_trust_rumor")

    evaluated = SelectionService.evaluate_story(story)
    assert evaluated["action"] == "hold"
    assert "credibility_gate" in evaluated["reason"]


def test_multi_source_saturation_curve():
    """100 sources do not scale linearly over 2 sources."""
    story1 = _story("One source")
    story1.independent_source_count = 1
    _, comp1 = StoryScoringService.compute_momentum(story1)

    story3 = _story("Three sources")
    story3.independent_source_count = 3
    _, comp3 = StoryScoringService.compute_momentum(story3)

    story100 = _story("Hundred copies")
    story100.independent_source_count = 100
    _, comp100 = StoryScoringService.compute_momentum(story100)

    assert comp1["multi_source_spread"] < comp3["multi_source_spread"]
    assert comp3["multi_source_spread"] <= comp100["multi_source_spread"]
    # Bounded to at most 1.0
    assert comp100["multi_source_spread"] <= 1.0


def test_freshness_decay():
    fresh_story = _story("Fresh story", latest_source_update_at=timezone.now() - timedelta(hours=1))
    old_story = _story("Old story", latest_source_update_at=timezone.now() - timedelta(hours=48))

    fresh_decay = compute_freshness(fresh_story, half_life_hours=24.0)
    old_decay = compute_freshness(old_story, half_life_hours=24.0)

    assert fresh_decay > old_decay
    assert old_decay <= 0.30


def test_anti_repeat_policy_and_material_update_recovery():
    src = _source("Main Source", trust_score=Decimal("0.85"))
    story = _story("Big Event Announcement")
    story.status = StoryStatus.ACTIVE
    story.independent_source_count = 3
    story.observed_source_count = 3
    story.language = "en"
    story.save()
    item = _item(src, "Big Event Announcement")
    StoryMembership.objects.create(story=story, source_item=item, is_current=True)

    # 1. Unpublished story evaluates to publish
    res1 = SelectionService.evaluate_story(story)
    assert res1["action"] == "publish"

    # 2. Publish the story
    Publication.objects.create(
        story=story,
        channel="@mychannel",
        headline=story.canonical_title,
        content="Published content here",
        status=PublicationStatus.PUBLISHED,
        idempotency_key="key-1",
    )

    # 3. Next evaluation without update -> skipped (anti-repeat)
    res2 = SelectionService.evaluate_story(story)
    assert res2["action"] == "skip"
    assert "already_published" in res2["reason"]

    # 4. New minor update -> still skipped
    MaterialUpdateDecision.objects.create(
        story=story,
        source_item=item,
        label="MINOR_UPDATE",
        confidence=Decimal("0.90"),
    )
    res3 = SelectionService.evaluate_story(story)
    assert res3["action"] == "skip"

    # 5. Major breaking update / correction arrives -> eligible again!
    MaterialUpdateDecision.objects.create(
        story=story,
        source_item=item,
        label="CORRECTION",
        confidence=Decimal("0.95"),
    )
    res4 = SelectionService.evaluate_story(story)
    assert res4["action"] == "publish"
    assert "correction" in res4["reason"]


def test_topic_saturation_penalty():
    topic = Topic.objects.create(slug="sports", name="Sports", weight=Decimal("1.0"))
    src = _source("Sports Source", trust_score=Decimal("0.80"))

    # Publish 3 stories on this topic in the last hour
    for i in range(3):
        st = _story(f"Sports Story {i}", primary_topic=topic)
        Publication.objects.create(
            story=st,
            channel="@mychannel",
            headline=st.canonical_title,
            content="Content",
            status=PublicationStatus.PUBLISHED,
            idempotency_key=f"sports-{i}",
        )

    # New candidate on same topic
    candidate = _story("New Sports Story", primary_topic=topic)
    candidate.independent_source_count = 2
    candidate.save()
    item = _item(src, "New Sports Story")
    StoryMembership.objects.create(story=candidate, source_item=item, is_current=True)

    evaluated = SelectionService.evaluate_story(candidate)
    assert evaluated["penalties"]["saturation_penalty"] > 0.0
    assert evaluated["adjusted_score"] < evaluated["scored"]["final_score"]


def test_score_record_persistence_and_breakdown_schema():
    src = _source("Tech News", trust_score=Decimal("0.85"))
    story = _story("Novel Quantum Computer Trial")
    story.independent_source_count = 2
    story.save()
    item = _item(src, "Novel Quantum Computer Trial")
    StoryMembership.objects.create(story=story, source_item=item, is_current=True)

    scored = StoryScoringService.score_story(story)
    record = ScoreRecord.objects.get(pk=scored["score_record_id"])

    assert record.algorithm_version == "ranking-v1"
    assert 0.0 <= float(record.final_score) <= 1.0
    assert 0.0 <= float(record.news_value) <= 1.0
    assert 0.0 <= float(record.audience_fit) <= 1.0
    assert 0.0 <= float(record.momentum) <= 1.0
    assert set(record.breakdown) == {"news_value", "audience_fit", "momentum"}


def test_decision_log_persists_what_choice():
    src = _source("General Source", trust_score=Decimal("0.75"))
    story = _story("Clean Energy Milestone")
    item = _item(src, "Clean Energy Milestone")
    StoryMembership.objects.create(story=story, source_item=item, is_current=True)

    SelectionService.evaluate_story(story)
    log = DecisionLog.objects.filter(story=story).latest("created_at")
    assert log.decision_type == "what"
    assert log.selected_action in ("publish", "skip", "hold")
    assert 0.0 <= float(log.predicted_reward) <= 1.0


def test_ranking_backtest_command_runs_safely():
    src = _source("Backtest Source")
    for i in range(3):
        st = _story(f"Backtest Story {i}")
        it = _item(src, f"Backtest Story {i}", f"Unique body text {i}")
        StoryMembership.objects.create(story=st, source_item=it, is_current=True)

    # Command runs with no errors and does not send any live publications
    call_command("ranking_backtest", days=3, limit=10)
    assert Publication.objects.filter(idempotency_key__contains="backtest").count() == 0
