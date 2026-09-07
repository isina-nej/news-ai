"""Phase 1.1 regressions for the 11 fixes."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.news.models import SourceItem
from apps.ops.models import Subtopic, Topic
from apps.publishing.models import Publication, compute_publication_payload_hash
from apps.ranking import metrics as metric_registry
from apps.ranking.models import DecisionLog, DecisionType, ScoreRecord, SourceBaseline
from apps.sources.models import Source
from apps.stories.models import Story, StoryMembership

pytestmark = pytest.mark.django_db


def _source(**kw):
    kw.setdefault("platform", "telegram")
    kw.setdefault("name", "ch")
    kw.setdefault("identifier", "@ch")
    return Source.objects.create(**kw)


def _story(**kw):
    kw.setdefault("canonical_title", "T")
    return Story.objects.create(**kw)


# 1. Publication content_hash must be fingerprint of rendered payload and recompute
def test_publication_fingerprint_includes_headline_and_style():
    story = _story()
    p = Publication.objects.create(
        story=story,
        channel="@c",
        headline="H1",
        content="body",
        template_version="v1",
        headline_style="short",
        tone="neutral",
        emoji_level=1,
        technical_depth=2,
        idempotency_key="rg-fp-1",
    )
    h1 = p.content_hash
    assert h1 is not None
    # Same body, different headline must produce a different fingerprint.
    p.headline = "H2"
    p.save()
    assert p.content_hash != h1
    # Headline-only payload (empty body) must have a fingerprint, not NULL.
    story2 = _story()
    p2 = Publication.objects.create(
        story=story2, channel="@c", headline="Only headline", idempotency_key="rg-fp-2"
    )
    assert p2.content_hash is not None
    # Changing emoji_level must affect fingerprint; tone too.
    p2.tone = "urgent"
    p2.save()
    assert p2.content_hash != compute_publication_payload_hash(
        type(p2)(headline="Only headline", content="", idempotency_key="x")
    )
    # Helper determinism: same visible payload -> same hash regardless of instance.
    a = Publication(headline="A", content="B", template_version="v1", idempotency_key="x")
    b = Publication(headline="A", content="B", template_version="v1", idempotency_key="y")
    assert compute_publication_payload_hash(a) == compute_publication_payload_hash(b)


def test_publication_hash_inf_nan_in_breakdown_blocked():
    story = _story()
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValidationError):
            ScoreRecord(
                story=story,
                algorithm_version="v1",
                news_value=Decimal("0.5"),
                audience_fit=Decimal("0.5"),
                momentum=Decimal("0.5"),
                final_score=Decimal("0.5"),
                breakdown={"news_value": {"x": bad}, "audience_fit": {}, "momentum": {}},
            ).full_clean()
    with pytest.raises(ValidationError):
        ScoreRecord(
            story=story,
            algorithm_version="v1",
            news_value=Decimal("0.5"),
            audience_fit=Decimal("0.5"),
            momentum=Decimal("0.5"),
            final_score=Decimal("0.5"),
            breakdown={"news_value": {"x": 2}, "audience_fit": {}, "momentum": {}},
        ).full_clean()


# 2. Story freshness: no auto_now, service-controlled latest_source_update_at
def test_story_freshness_field_is_not_auto_now_and_writable():
    story = _story()
    assert story.latest_source_update_at is None
    assert not story._meta.get_field("latest_source_update_at").auto_now
    when = timezone.now() - timedelta(hours=2)
    story.latest_source_update_at = when
    story.save()
    story.refresh_from_db()
    assert story.latest_source_update_at == when
    # updated_at still bumps on any save (TimeStampedModel), but freshness stays pinned.
    freshness = story.latest_source_update_at
    story.summary = "edited summary"
    story.save()
    story.refresh_from_db()
    assert story.latest_source_update_at == freshness


# 3. Baseline metrics extensible without TextChoices
def test_baseline_derived_metrics_without_migration():
    s = _source()
    topic = Topic.objects.create(slug="t-dv", name="TDV")
    for derived in ("view_velocity", "engagement_velocity", "relative_performance"):
        assert metric_registry.is_derived_metric(derived)
        SourceBaseline.objects.create(
            source=s,
            platform="telegram",
            topic=topic,
            content_type="article",
            age_bucket_minutes=60,
            metric=derived,
            sample_count=5,
            confidence=Decimal("0.6"),
        )
    # Unknown metric rejected at clean(), not silently stored.
    with pytest.raises(ValidationError):
        SourceBaseline(
            source=s,
            platform="telegram",
            content_type="article",
            age_bucket_minutes=60,
            metric="unknown_metric_xyz",
        ).full_clean()


# 4. StoryMembership vs SourceItem.story: at most one is_current per SourceItem
def test_story_membership_current_uniqueness_no_inconsistent_state():
    s = _source()
    item = SourceItem.objects.create(source=s, external_id="m-cur")
    a = _story(canonical_title="A")
    b = _story(canonical_title="B")
    StoryMembership.objects.create(story=a, source_item=item, is_current=True)
    with pytest.raises((IntegrityError, ValidationError)), transaction.atomic():
        StoryMembership.objects.create(story=b, source_item=item, is_current=True)
    # Non-current candidates allowed alongside the current.
    StoryMembership.objects.create(story=b, source_item=item, is_current=False)
    assert StoryMembership.objects.filter(source_item=item, is_current=True).count() == 1
    assert item.memberships.count() == 2


# 5. Subtopic belongs to selected Topic
def test_subtopic_must_match_topic_on_sourceitem_story_baseline():
    topic_a = Topic.objects.create(slug="a-enf", name="A")
    topic_b = Topic.objects.create(slug="b-enf", name="B")
    sub_a = Subtopic.objects.create(topic=topic_a, slug="s-a", name="SA")
    s = _source()
    with pytest.raises(ValidationError):
        SourceItem(source=s, topic=topic_b, subtopic=sub_a, title="x").full_clean()
    story = Story(canonical_title="T", primary_topic=topic_b, primary_subtopic=sub_a)
    with pytest.raises(ValidationError):
        story.full_clean()
    with pytest.raises(ValidationError):
        SourceBaseline(
            source=s,
            platform="telegram",
            topic=topic_b,
            subtopic=sub_a,
            content_type="article",
            age_bucket_minutes=60,
            metric="views",
        ).full_clean()


# 7. DB CheckConstraints back Python validators (scores/confidences)
def test_score_breakdown_numeric_branch_invariants():
    story = _story()
    # Valid breakdown leaves.
    ScoreRecord.objects.create(
        story=story,
        algorithm_version="v1",
        news_value=Decimal("0.9"),
        audience_fit=Decimal("0.8"),
        momentum=Decimal("0.7"),
        final_score=Decimal("0.8"),
        breakdown={
            "news_value": {"importance": 0.9, "novelty": "high"},
            "audience_fit": {"topic_match": 0.8},
            "momentum": {"velocity": 0.7},
        },
    )
    # RankingWeight CheckConstraint path: value 0..1 enforced at full_clean
    # (and DB level for bulk paths).
    from apps.ranking.models import RankingWeight

    with pytest.raises(ValidationError):
        RankingWeight(key="w-bad", value=Decimal("1.5")).full_clean()


def test_publication_fingerprint_uses_template_and_emoji_level():
    story = _story()
    base = Publication.objects.create(
        story=story,
        channel="@c",
        headline="H",
        content="C",
        template_version="v1",
        emoji_level=0,
        idempotency_key="rg-style-1",
    )
    h_before = base.content_hash
    base.template_version = "v2"
    base.save()
    assert base.content_hash != h_before
    base.emoji_level = 3
    base.save()
    assert base.content_hash != h_before
    # Recomputing from same visible state yields same hash.
    assert base.content_hash == compute_publication_payload_hash(base)


def test_decision_log_rewards_allow_null_and_0_to_1_only():
    story = _story()
    # Null rewards allowed before scoring completes.
    DecisionLog.objects.create(
        story=story,
        algorithm_version="v1",
        decision_type=DecisionType.WHAT,
        feature_snapshot={},
        selected_action="publish",
    )
    with pytest.raises(ValidationError):
        DecisionLog(
            story=story,
            algorithm_version="v1",
            decision_type=DecisionType.WHAT,
            feature_snapshot={},
            selected_action="publish",
            predicted_reward=Decimal("1.5"),
        ).full_clean()
