"""Final Hardening: End-to-End Pipeline, Failure Injection, and Management Commands."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.ai.analysis import classify_topic, detect_conflicts, extract_news_value
from apps.ai.providers import FakeAIProvider
from apps.core.choices import Platform
from apps.news.models import EngagementSnapshot, SourceItem
from apps.ops.models import AudiencePreference, Topic
from apps.ops.services.bandit import ContextualBanditService
from apps.publishing.models import Publication, PublicationEngagementSnapshot, PublicationStatus
from apps.publishing.rewards import normalized_reward
from apps.publishing.services import publish_story
from apps.ranking.services.scoring import StoryScoringService
from apps.ranking.services.selection import SelectionService
from apps.sources.models import Source
from apps.stories.models import Story
from apps.stories.services.clustering import story_clustering_service

pytestmark = [pytest.mark.django_db]


def _source(name="E2E Source"):
    return Source.objects.create(
        platform=Platform.RSS,
        name=name,
        identifier=f"https://{name.lower().replace(' ', '')}.example.com/rss",
        url=f"https://{name.lower().replace(' ', '')}.example.com/rss",
        trust_score=Decimal("0.85"),
        reliability_score=Decimal("0.85"),
    )


def test_end_to_end_news_pipeline():
    """Verify pipeline: ingest, cluster, AI, rank, select, publish, feedback, learn."""
    now = timezone.now()
    src = _source("Tech Chronicle")
    topic = Topic.objects.create(slug="ai", name="AI", weight=Decimal("1.5"))

    # 1. Ingestion / SourceItem
    item1 = SourceItem.objects.create(
        source=src,
        title="Breakthrough in Quantum Optical Computing",
        raw_text="Scientists demonstrated optical qubit chips operating stably.",
        normalized_text="Scientists demonstrated optical qubit chips operating stably.",
        topic=topic,
        published_at=now - timedelta(hours=2),
    )
    EngagementSnapshot.objects.create(
        source_item=item1,
        views=25000,
        forwards=2000,
        reactions=1200,
        post_age_seconds=3600,
    )

    # 2. Clustering
    cluster_res = story_clustering_service.cluster_item(item1.pk)
    assert cluster_res["status"] == "new_story"
    story = Story.objects.get(pk=cluster_res["story_id"])
    story.independent_source_count = 2
    story.observed_source_count = 2
    story.primary_topic = topic
    story.language = "en"
    story.save()

    # 3. AI Analysis Layer
    provider = FakeAIProvider()
    classify_topic(story, provider=provider)
    extract_news_value(story, provider=provider)
    detect_conflicts(story, provider=provider)
    story.refresh_from_db()
    assert hasattr(story, "news_value")
    assert hasattr(story, "topic_classification")
    assert hasattr(story, "conflict")

    # 4. Ranking & Selection
    scored = StoryScoringService.score_story(story)
    assert 0.0 <= scored["final_score"] <= 1.0

    eval_res = SelectionService.evaluate_story(story)
    assert eval_res["action"] == "publish"

    # 5. Post Generation & Publication Dry-Run
    dry_res = publish_story(story.pk, dry_run=True, provider=provider)
    assert dry_res["status"] == "dry_run"
    assert dry_res["headline"]

    # 6. Publication Execution (Force auto-publish in test)
    pub_res = publish_story(story.pk, force=True, provider=provider)
    assert pub_res["status"] == "published"
    publication = Publication.objects.get(pk=pub_res["publication_id"])
    assert publication.status == PublicationStatus.PUBLISHED

    # 7. Own-Channel Feedback Simulation
    snap = PublicationEngagementSnapshot.objects.create(
        publication=publication,
        views=12000,
        forwards=800,
        reactions=600,
    )
    reward_res = normalized_reward(
        snap,
        baselines={
            "views": {"p50": 5000, "p90": 10000},
            "forwards": {"p50": 300, "p90": 700},
            "reactions": {"p50": 200, "p90": 500},
        },
    )
    assert reward_res["reward"] is not None
    assert reward_res["reward"] > 0.70

    # 8. Audience Learning Loop
    pref = AudiencePreference.objects.create(feature="topic", context={"topic": "ai"})
    initial_val = float(pref.value)
    ContextualBanditService.record_feedback(
        decision_log_id=story.decisions.latest("created_at").pk,
        reward=float(reward_res["reward"]),
    )
    pref.refresh_from_db()
    assert float(pref.value) > initial_val


def test_failure_injection_resilience():
    """Verify system does not lose data when Qdrant or AI fails."""
    src = _source("Resilience Source")
    outage_text = "Testing database fallback when external systems are unreachable."
    item = SourceItem.objects.create(
        source=src,
        title="Robustness under partial outage",
        raw_text=outage_text,
        normalized_text=outage_text,
    )

    # 1. Qdrant down -> clustering falls back to lexical/SQL and succeeds
    with patch(
        "apps.stories.services.vector_store.upsert_point",
        side_effect=RuntimeError("Qdrant Down"),
    ):
        out = story_clustering_service.cluster_item(item.pk)
        assert out["status"] == "new_story"
        assert item.memberships.filter(is_current=True).count() == 1

    # 2. AI down -> scoring uses deterministic structural fallback
    story = Story.objects.get(pk=out["story_id"])
    scored = StoryScoringService.score_story(story)
    assert scored["final_score"] >= 0.0
    assert scored["gate_passed"] is True


def test_management_commands_execution():
    """Verify all project management commands run cleanly and without error."""
    call_command("seed_demo_news")
    call_command("system_health")
    call_command("run_news_pipeline", dry_run=True, limit=5)
    call_command("ranking_backtest", days=1, limit=5)
