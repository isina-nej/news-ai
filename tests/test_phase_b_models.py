"""Phase B model tests: StoryObservationState, StoryMomentumSnapshot, StoryIntelligenceSnapshot,
MediaAsset, PublicationSelectionRun, ScoreRecord score separation, DecisionLog and Publication.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.ai.models import MaterialUpdateDecision, StoryIntelligenceSnapshot
from apps.core.choices import LifecycleState, Platform, TrendState
from apps.news.models import MediaAsset, MediaValidationStatus, SourceItem
from apps.publishing.models import Publication
from apps.ranking.models import DecisionLog, DecisionType, PublicationSelectionRun, ScoreRecord
from apps.sources.models import Source
from apps.stories.models import Story, StoryMomentumSnapshot, StoryObservationState

pytestmark = pytest.mark.django_db


def _create_source():
    return Source.objects.create(name="Source 1", platform=Platform.TELEGRAM, identifier="@src1")


def _create_story():
    return Story.objects.create(canonical_title="Test Story")


def _create_item(source, story=None):
    return SourceItem.objects.create(
        source=source,
        title="Test Item",
        story=story,
        external_id="ext-1",
    )


def test_story_observation_state():
    story = _create_story()
    obs = StoryObservationState.objects.create(
        story=story,
        lifecycle_state=LifecycleState.WATCHING,
        trend_state=TrendState.EARLY_SIGNAL,
        observation_interval_seconds=180,
        latest_momentum=Decimal("0.7500"),
        latest_confidence=Decimal("0.8500"),
    )
    assert obs.story == story
    assert obs.lifecycle_state == LifecycleState.WATCHING
    assert obs.trend_state == TrendState.EARLY_SIGNAL
    assert obs.active is True
    assert obs.observation_interval_seconds == 180

    # OneToOne constraint
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            StoryObservationState.objects.create(story=story)


def test_story_momentum_snapshot_and_idempotency():
    story = _create_story()
    snap1 = StoryMomentumSnapshot.objects.create(
        story=story,
        lifecycle_state=LifecycleState.RISING,
        trend_state=TrendState.RISING,
        view_velocity=120.5,
        acceleration=15.2,
        momentum_score=Decimal("0.8200"),
        confidence=Decimal("0.9000"),
        idempotency_hash="hash-1",
    )
    assert snap1.view_velocity == 120.5
    assert snap1.acceleration == 15.2
    assert snap1.momentum_score == Decimal("0.8200")

    # Duplicate idempotency_hash for same story must fail
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            StoryMomentumSnapshot.objects.create(
                story=story,
                lifecycle_state=LifecycleState.RISING,
                trend_state=TrendState.RISING,
                idempotency_hash="hash-1",
            )


def test_story_intelligence_snapshot():
    story = _create_story()
    snap = StoryIntelligenceSnapshot.objects.create(
        story=story,
        evidence_hash="ev-hash-123",
        canonical_event="Government announces new tech policy",
        what_happened="Policy enacted today",
        confirmed_facts=["Policy enacted", "Covers AI"],
        importance=Decimal("0.8500"),
        credibility=Decimal("0.9000"),
    )
    assert snap.story == story
    assert len(snap.confirmed_facts) == 2
    assert snap.importance == Decimal("0.8500")


def test_media_asset_and_aspect_ratio():
    source = _create_source()
    item = _create_item(source)
    asset = MediaAsset.objects.create(
        source_item=item,
        original_url="https://example.com/image.jpg",
        width=1200,
        height=675,
        mime_type="image/jpeg",
        validation_status=MediaValidationStatus.VALID,
    )
    assert asset.aspect_ratio == pytest.approx(1.7778, rel=1e-3)
    assert asset.validation_status == MediaValidationStatus.VALID


def test_publication_selection_run():
    run = PublicationSelectionRun.objects.create(
        algorithm_version="ranking-v2",
        policy_version="editorial-v1",
        candidates=[{"story_id": 1, "score": 0.85, "selected": True}],
        selected_count=1,
    )
    assert run.selected_count == 1
    assert len(run.candidates) == 1


def test_score_record_newsworthiness_and_priority():
    story = _create_story()
    record = ScoreRecord.objects.create(
        story=story,
        algorithm_version="ranking-v2",
        news_value=Decimal("0.8000"),
        audience_fit=Decimal("0.7000"),
        momentum=Decimal("0.8500"),
        final_score=Decimal("0.7800"),
        newsworthiness_score=Decimal("0.8200"),
        publish_priority_score=Decimal("0.7800"),
        breakdown={"news_value": {"a": 0.8}, "audience_fit": {"b": 0.7}, "momentum": {"c": 0.85}},
    )
    assert record.newsworthiness_score == Decimal("0.8200")
    assert record.publish_priority_score == Decimal("0.7800")
    assert record.final_score == Decimal("0.7800")


def test_decision_log_extensions():
    story = _create_story()
    log = DecisionLog.objects.create(
        story=story,
        algorithm_version="editorial-v1",
        decision_type=DecisionType.WHAT,
        selected_action="publish_now",
        policy_version="policy-v2",
        decision_batch_id="batch-999",
        reward_breakdown={"views": 0.25, "forwards": 0.50},
        predicted_reward=Decimal("0.8500"),
    )
    assert log.decision_batch_id == "batch-999"
    assert log.policy_version == "policy-v2"
    assert log.reward_breakdown["forwards"] == 0.50


def test_material_update_decision_info_diff():
    story = _create_story()
    source = _create_source()
    item = _create_item(source, story)
    decision = MaterialUpdateDecision.objects.create(
        story=story,
        source_item=item,
        label="MATERIAL_UPDATE",
        confidence=Decimal("0.9000"),
        information_unit_diff={
            "new_facts": ["Casualty count rose to 15"],
            "changed_facts": [],
            "repeated_facts": ["Fire occurred at industrial park"],
        },
    )
    assert "Casualty count rose to 15" in decision.information_unit_diff["new_facts"]


def test_publication_extensions():
    story = _create_story()
    source = _create_source()
    item = _create_item(source, story)
    asset = MediaAsset.objects.create(
        source_item=item,
        original_url="https://example.com/photo.png",
        width=800,
        height=600,
    )
    parent = Publication.objects.create(
        story=story,
        channel="@channel",
        idempotency_key="parent-key-1",
        headline="Initial Breaking News",
        content="First report.",
    )
    follow_up = Publication.objects.create(
        story=story,
        channel="@channel",
        publication_version=2,
        update_type="material_update",
        idempotency_key="followup-key-2",
        headline="Update: News Expands",
        content="Further details emerged.",
        selected_media=asset,
        telegram_chat_id="-100123456",
        telegram_message_id="42",
        parent_publication=parent,
        update_sequence=1,
        fingerprint_version="fp-v2",
    )
    assert follow_up.selected_media == asset
    assert follow_up.parent_publication == parent
    assert follow_up.update_sequence == 1
    assert follow_up.fingerprint_version == "fp-v2"
