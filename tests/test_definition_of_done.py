"""Definition of Done Integration Test: Complete End-to-End Newsroom Intelligence Journey."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.core.choices import EditorialAction, LifecycleState, Platform, TrendState
from apps.news.models import EngagementSnapshot, MediaAsset, MediaValidationStatus, SourceItem
from apps.news.services.persistence import IngestionPersistenceService
from apps.ops.services.audience import AudienceLearningService
from apps.publishing.models import Publication, PublicationEngagementSnapshot
from apps.publishing.rewards import normalized_reward
from apps.publishing.services import generate_validated_draft, publish_story
from apps.publishing.telegram import FakePublisher
from apps.ranking.models import SourceBaseline
from apps.ranking.services.editorial import EditorialPolicyEngine
from apps.ranking.services.newsworthiness import NewsworthinessService
from apps.ranking.services.priority import PublishPriorityService
from apps.sources.adapters.base import FetchedItem
from apps.sources.models import Source
from apps.stories.models import Story
from apps.stories.services.clustering import StoryClusteringService
from apps.stories.services.lifecycle import LifecycleStateMachine
from apps.stories.services.momentum import StoryMomentumService
from apps.stories.services.series import ItemMetricSeriesService
from apps.stories.services.trend import TrendDetector

pytestmark = pytest.mark.django_db


def test_full_newsroom_intelligence_definition_of_done():
    """Verify complete newsroom intelligence pipeline: ingest -> publish -> learn."""
    now = timezone.now()

    # 1. Source Setup & Baselines
    source1 = Source.objects.create(
        name="Global Tech",
        platform=Platform.TELEGRAM,
        identifier="@global_tech",
        trust_score=Decimal("0.90"),
        reliability_score=Decimal("0.90"),
    )
    source2 = Source.objects.create(
        name="Scientific Wire",
        platform=Platform.RSS,
        identifier="https://sciwire.org/feed",
        trust_score=Decimal("0.85"),
        reliability_score=Decimal("0.85"),
    )

    SourceBaseline.objects.create(
        source=source1,
        platform=Platform.TELEGRAM,
        age_bucket_minutes=60,
        metric="views",
        sample_count=100,
        p25=100.0,
        p50=500.0,
        p75=1500.0,
        p90=3000.0,
        p99=10000.0,
    )

    # 2. Ingest FetchedItem via IngestionPersistenceService
    fetched_dto = FetchedItem(
        url="https://t.me/global_tech/101",
        external_id="msg-101",
        title="Revolutionary Neural Architecture Unveiled",
        raw_text="Scientists announced a revolutionary neural computing architecture.",
        published_at=now - timedelta(hours=2),
        views=300,
        forwards=20,
    )
    persistence = IngestionPersistenceService()
    ingest_res = persistence.persist_items(
        source1, [fetched_dto], batch_time=now - timedelta(hours=2)
    )
    assert ingest_res.created_count == 1
    item1 = ingest_res.created_items[0]
    assert item1.pk is not None

    # 3. Cluster into Story via StoryClusteringService
    clustering_service = StoryClusteringService()
    cluster_res = clustering_service.cluster_item(item1.pk)
    assert cluster_res["status"] in ("new_story", "matched")
    story = Story.objects.get(pk=cluster_res["story_id"])
    assert story.canonical_title is not None

    # Ingest second independent source confirming the event
    item2 = SourceItem.objects.create(
        source=source2,
        title="Revolutionary Neural Architecture Unveiled in Scientific Reports",
        normalized_text="Independent validation confirms energy savings.",
        published_at=now - timedelta(hours=1),
        external_id="sci-202",
    )
    cluster_res2 = clustering_service.cluster_item(item2.pk)
    assert cluster_res2["status"] in ("new_story", "matched")

    # Ingest a verified media asset
    media = MediaAsset.objects.create(
        source_item=item1,
        original_url="https://example.com/neural.jpg",
        width=1280,
        height=720,
        validation_status=MediaValidationStatus.VALID,
    )

    # 4. Repeated Observations (Engagement Snapshots)
    EngagementSnapshot.objects.create(
        source_item=item1,
        captured_at=now - timedelta(minutes=40),
        post_age_seconds=4800,
        views=1200,
        forwards=110,
    )
    EngagementSnapshot.objects.create(
        source_item=item1,
        captured_at=now - timedelta(minutes=10),
        post_age_seconds=6600,
        views=4500,
        forwards=380,
    )

    # 5. Velocity and Acceleration Measured via ItemMetricSeriesService
    series_metrics = ItemMetricSeriesService.compute_item_series_metrics(item1)
    assert series_metrics["sample_count"] >= 2
    assert series_metrics["view_velocity"] is not None
    assert series_metrics["view_velocity"] > 0
    assert series_metrics["normalized_velocity"] is not None

    # 6. Story Momentum & Propagation Measured
    mom_metrics = StoryMomentumService.compute_metrics(story, now=now)
    assert mom_metrics.observed_sources_count >= 1
    assert mom_metrics.normalized_momentum > 0.0
    assert mom_metrics.momentum_confidence > 0.0

    # 7. Lifecycle & Trend Classification
    trend_state, _ = TrendDetector.detect_state(mom_metrics, sample_count=3)
    lifecycle_state, _ = LifecycleStateMachine.evaluate_lifecycle(
        current_lifecycle=LifecycleState.DISCOVERED,
        trend_state=trend_state,
        metrics=mom_metrics,
    )
    assert trend_state in TrendState.values
    assert lifecycle_state in LifecycleState.values

    # Persist momentum snapshot and observation state
    snap = StoryMomentumService.recompute_and_persist(
        story.pk,
        lifecycle_state=lifecycle_state,
        trend_state=trend_state,
        now=now,
    )
    assert snap is not None

    # 8. Newsworthiness Calculated
    news_val, news_comp = NewsworthinessService.evaluate(story)
    assert 0.0 <= news_val <= 1.0
    assert "credibility" in news_comp

    # 9. Publish Priority Calculated
    priority_res = PublishPriorityService.evaluate_priority(story, now=now)
    assert 0.0 <= priority_res["publish_priority_score"] <= 1.0
    assert priority_res["gate_passed"] is True

    # 10. Editorial Policy Decision
    editorial_res = EditorialPolicyEngine.evaluate_story(story, now=now)
    assert editorial_res["action"] in EditorialAction.values
    assert editorial_res["score_record_id"] is not None
    assert editorial_res["decision_log_id"] is not None

    # 11. Structured Draft Generated, Critic-Reviewed, and Rendered
    draft = generate_validated_draft(story, format_type="STANDARD")
    assert draft["headline"]
    assert draft["payload"]
    assert len(draft["payload"]) <= 4096

    # 12. Idempotent Telegram Publication
    publisher = FakePublisher()
    pub_res1 = publish_story(story.pk, force=True, publisher=publisher)
    assert pub_res1["status"] == "published"
    assert pub_res1["publication_id"] is not None

    # Verify publication row & media link
    pub = Publication.objects.get(pk=pub_res1["publication_id"])
    assert pub.status == "published"
    assert pub.telegram_message_id is not None
    assert pub.selected_media == media

    # Second send without force is skipped by editorial policy (already published)
    pub_res_skip = publish_story(story.pk, force=False, publisher=publisher)
    assert pub_res_skip["status"] == "skip"

    # Second send with force is caught by publication idempotency
    pub_res_dup = publish_story(story.pk, force=True, publisher=publisher)
    assert pub_res_dup["status"] == "duplicate"

    # 13. Post-Publication Measurement (Own-Channel Engagement)
    pub_snap = PublicationEngagementSnapshot.objects.create(
        publication=pub,
        post_age_seconds=3600,
        views=3500,
        forwards=180,
        reactions=75,
        replies=25,
    )
    assert pub_snap.pk is not None

    # 14. Normalized Reward Decomposed
    baselines = {
        "views": {"p25": 1000, "p50": 3000, "p75": 5000, "p90": 8000},
        "forwards": {"p25": 50, "p50": 150, "p75": 300, "p90": 500},
        "reactions": {"p25": 20, "p50": 60, "p75": 120, "p90": 250},
        "replies": {"p25": 5, "p50": 20, "p75": 50, "p90": 100},
    }
    reward_res = normalized_reward(
        pub_snap, baselines=baselines, credibility_score=news_comp["credibility"]
    )
    assert reward_res["reward"] is not None
    assert 0.0 <= reward_res["reward"] <= 1.0
    assert reward_res["forward_reward"] is not None

    # 15. Learning Loop Updated
    pref = AudienceLearningService.update_preference(
        feature="topic",
        context={"topic": "tech"},
        observed_performance=reward_res["reward"],
    )
    assert pref.sample_count >= 1
    assert pref.value is not None
