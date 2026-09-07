"""Phase 1: constraints, idempotency, nullability, transitions, score validation."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.news.models import EngagementSnapshot, SourceItem
from apps.ops.models import AudiencePreference, Subtopic, Topic
from apps.publishing.models import (
    Publication,
    PublicationEngagementSnapshot,
    PublicationStatus,
    UpdateType,
)
from apps.ranking.models import DecisionLog, DecisionType, RankingWeight, ScoreRecord
from apps.sources.models import Source
from apps.stories.models import Story, StoryMembership

pytestmark = pytest.mark.django_db

BREAKDOWN = {"news_value": {"a": 1}, "audience_fit": {"b": 1}, "momentum": {"c": 1}}


def _source(**kw):
    kw.setdefault("platform", "telegram")
    kw.setdefault("name", "ch")
    kw.setdefault("identifier", "@ch")
    return Source.objects.create(**kw)


def _story(**kw):
    kw.setdefault("canonical_title", "T")
    return Story.objects.create(**kw)


def test_duplicate_same_source_external_id_blocked():
    s = _source()
    SourceItem.objects.create(source=s, external_id="m1", title="a")
    with pytest.raises(IntegrityError), transaction.atomic():
        SourceItem.objects.create(source=s, external_id="m1", title="b")


def test_null_external_id_not_unique_blocked():
    s = _source()
    SourceItem.objects.create(source=s, title="a")
    SourceItem.objects.create(source=s, title="b")
    assert SourceItem.objects.filter(source=s).count() == 2


def test_cross_source_same_content_allowed():
    s1 = _source(identifier="@a")
    s2 = _source(identifier="@b")
    SourceItem.objects.create(source=s1, external_id="m1", title="same", normalized_text="x")
    SourceItem.objects.create(source=s2, external_id="m1", title="same", normalized_text="x")
    assert SourceItem.objects.count() == 2


def test_story_membership_and_primary_cache():
    s = _source()
    item = SourceItem.objects.create(source=s, external_id="m1")
    story = _story()
    StoryMembership.objects.create(story=story, source_item=item, is_primary=True)
    item.story = story
    item.save()
    assert story.memberships.count() == 1
    assert story.items.count() == 1


def test_material_updates_allowed_same_story_channel():
    story = _story()
    p1 = Publication.objects.create(
        story=story,
        channel="@c",
        headline="h",
        content="first version text",
        idempotency_key="k-1",
    )
    p2 = Publication.objects.create(
        story=story,
        channel="@c",
        headline="h2",
        content="second distinct version text",
        publication_version=2,
        update_type=UpdateType.MATERIAL_UPDATE,
        idempotency_key="k-2",
    )
    assert (p1.publication_version, p2.publication_version) == (1, 2)
    assert p1.content_hash != p2.content_hash


def test_accidental_duplicate_publication_blocked():
    story = _story()
    Publication.objects.create(
        story=story, channel="@c", content="same text", idempotency_key="k-1"
    )
    # Model-level full_clean raises ValidationError; DB race raises IntegrityError.
    # Both mean "blocked". Production callers must reuse the row on retry.
    with pytest.raises((IntegrityError, ValidationError)), transaction.atomic():
        Publication.objects.create(
            story=story, channel="@c", content="same text", idempotency_key="k-2"
        )
    with pytest.raises((IntegrityError, ValidationError)), transaction.atomic():
        Publication.objects.create(
            story=story, channel="@c", content="other text", idempotency_key="k-1"
        )


def test_publication_initial_version_rules():
    story = _story()
    with pytest.raises(ValidationError):
        Publication(
            story=story,
            channel="@c",
            content="x",
            publication_version=2,
            update_type=UpdateType.INITIAL,
            idempotency_key="k-x",
        ).full_clean()
    with pytest.raises(ValidationError):
        Publication(story=story, channel="@c", content="x", idempotency_key="").full_clean()


def test_state_transitions():
    story = _story()
    p = Publication.objects.create(story=story, channel="@c", content="t", idempotency_key="k-t")
    assert p.status == PublicationStatus.DRAFT
    p.transition(PublicationStatus.READY)
    with pytest.raises(ValidationError):
        p.transition(PublicationStatus.PUBLISHED)
    p.transition(PublicationStatus.APPROVED)
    p.transition(PublicationStatus.SCHEDULED)
    p.transition(PublicationStatus.PUBLISHING)
    p.transition(PublicationStatus.PUBLISHED)
    with pytest.raises(ValidationError):
        p.transition(PublicationStatus.SCHEDULED)


def test_failed_retry_reuses_row_not_new_insert():
    story = _story()
    p = Publication.objects.create(story=story, channel="@c", content="t", idempotency_key="k-r")
    p.transition(PublicationStatus.READY)
    p.transition(PublicationStatus.APPROVED)
    p.transition(PublicationStatus.PUBLISHING)
    p.transition(PublicationStatus.FAILED)
    before = Publication.objects.count()
    p.transition(PublicationStatus.PUBLISHING)
    assert Publication.objects.count() == before


def test_nullable_engagement_metrics_unknown_vs_zero():
    s = _source()
    item = SourceItem.objects.create(source=s, external_id="m1")
    snap = EngagementSnapshot.objects.create(source_item=item, views=0)
    assert snap.views == 0
    assert snap.forwards is None
    assert snap.replies is None
    snap.refresh_from_db()
    assert snap.views == 0 and snap.forwards is None


def test_snapshot_post_age_auto_derived():
    s = _source()
    now = timezone.now()
    item = SourceItem.objects.create(
        source=s, external_id="m1", published_at=now - timedelta(hours=1)
    )
    snap = EngagementSnapshot.objects.create(source_item=item, captured_at=now, views=5)
    assert 3590 <= snap.post_age_seconds <= 3610


def test_score_validation_and_algorithm_version_required():
    story = _story()
    with pytest.raises(ValidationError):
        ScoreRecord(
            story=story,
            algorithm_version="",
            news_value=Decimal("0.5"),
            audience_fit=Decimal("0.5"),
            momentum=Decimal("0.5"),
            final_score=Decimal("0.5"),
            breakdown=dict(BREAKDOWN),
        ).full_clean()
    with pytest.raises(ValidationError):
        ScoreRecord(
            story=story,
            algorithm_version="v1",
            news_value=Decimal("1.5"),
            audience_fit=Decimal("0.5"),
            momentum=Decimal("0.5"),
            final_score=Decimal("0.5"),
            breakdown=dict(BREAKDOWN),
        ).full_clean()
    with pytest.raises(ValidationError):
        ScoreRecord(
            story=story,
            algorithm_version="v1",
            news_value=Decimal("0.5"),
            audience_fit=Decimal("0.5"),
            momentum=Decimal("0.5"),
            final_score=Decimal("0.5"),
            breakdown={"news_value": {}},
        ).full_clean()
    rec = ScoreRecord.objects.create(
        story=story,
        algorithm_version="v1",
        news_value=Decimal("0.5"),
        audience_fit=Decimal("0.5"),
        momentum=Decimal("0.5"),
        final_score=Decimal("0.5"),
        breakdown=dict(BREAKDOWN),
    )
    assert rec.pk is not None


def test_ranking_weight_range_enforced():
    with pytest.raises(ValidationError):
        RankingWeight(key="w", value=Decimal("2")).full_clean()


def test_decision_log_persistence_for_replay():
    story = _story()
    d = DecisionLog.objects.create(
        story=story,
        algorithm_version="v1",
        decision_type=DecisionType.WHAT,
        feature_snapshot={"topic_effect": 0.7, "style_effect": 0.2},
        score_breakdown=dict(BREAKDOWN),
        predicted_reward=Decimal("0.6"),
        selected_action="publish",
        is_exploration=True,
        exploration_probability=Decimal("0.05"),
    )
    d.actual_reward = Decimal("0.8")
    d.reward_calculated_at = timezone.now()
    d.save()
    d.refresh_from_db()
    assert d.actual_reward == Decimal("0.8")
    assert d.feature_snapshot["style_effect"] == 0.2


def test_audience_preference_no_migration_per_feature():
    Topic.objects.create(slug="tech", name="Tech")
    AudiencePreference.objects.create(
        feature="topic", context={"topic": "tech"}, value=Decimal("0.7")
    )
    AudiencePreference.objects.create(
        feature="headline_style", context={"style": "short"}, value=Decimal("0.4")
    )
    assert AudiencePreference.objects.count() == 2


def test_subtopic_unique_per_topic():
    t = Topic.objects.create(slug="tech", name="Tech")
    Subtopic.objects.create(topic=t, slug="ai", name="AI")
    with pytest.raises(IntegrityError), transaction.atomic():
        Subtopic.objects.create(topic=t, slug="ai", name="AI dup")


def test_publication_engagement_snapshot_separate_table():
    story = _story()
    p = Publication.objects.create(story=story, channel="@c", content="t", idempotency_key="k-s")
    snap = PublicationEngagementSnapshot.objects.create(publication=p, views=100)
    assert snap.views == 100
    assert snap.forwards is None
