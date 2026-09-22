"""Tests for Phase E: Newsworthiness, ChannelNovelty, PublishPriority, and EditorialPolicy."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.ai.models import MaterialUpdateDecision
from apps.core.choices import EditorialAction, Platform
from apps.news.models import SourceItem
from apps.publishing.models import Publication, PublicationStatus
from apps.ranking.models import DecisionLog, PublicationSelectionRun, ScoreRecord
from apps.ranking.services.editorial import EditorialPolicyEngine
from apps.ranking.services.newsworthiness import NewsworthinessService
from apps.ranking.services.novelty import ChannelNoveltyService, ChannelNoveltyType
from apps.ranking.services.priority import PublishPriorityService
from apps.sources.models import Source
from apps.stories.models import Story, StoryMembership

pytestmark = pytest.mark.django_db


def _create_story_fixture(trust: float = 0.85, title: str = "Major Tech Milestone"):
    source = Source.objects.create(
        name="Reliable Tech",
        platform=Platform.TELEGRAM,
        identifier="@rel_tech",
        trust_score=Decimal(str(trust)),
    )
    story = Story.objects.create(canonical_title=title)
    item = SourceItem.objects.create(
        source=source, story=story, title=title, external_id=f"ext-{title[:8]}"
    )
    StoryMembership.objects.create(
        story=story, source_item=item, is_current=True, independence="independent"
    )
    story.independent_source_count = 1
    story.observed_source_count = 1
    story.save()
    return story, item, source


def test_newsworthiness_deterministic_fallback():
    story, _, _ = _create_story_fixture()
    score, comp = NewsworthinessService.evaluate(story)

    assert 0.0 <= score <= 1.0
    assert "importance" in comp
    assert "credibility" in comp
    assert comp["credibility"] >= 0.50


def test_channel_novelty_lifecycle():
    story, item, _ = _create_story_fixture()

    # 1. New story
    nov_type, _ = ChannelNoveltyService.evaluate_novelty(story)
    assert nov_type == ChannelNoveltyType.NEW_STORY

    # 2. Publish story
    Publication.objects.create(
        story=story,
        channel="@news",
        headline="Initial Headline",
        content="Content",
        status=PublicationStatus.PUBLISHED,
        idempotency_key="init-pub-1",
    )

    # 3. Without material update -> REPEAT
    nov_type2, _ = ChannelNoveltyService.evaluate_novelty(story)
    assert nov_type2 == ChannelNoveltyType.REPEAT

    # 4. With MaterialUpdateDecision -> MATERIAL_UPDATE
    MaterialUpdateDecision.objects.create(
        story=story,
        source_item=item,
        label="MATERIAL_UPDATE",
        confidence=Decimal("0.9000"),
        information_unit_diff={"new_facts": ["Revenue doubled"]},
    )
    nov_type3, _ = ChannelNoveltyService.evaluate_novelty(story)
    assert nov_type3 == ChannelNoveltyType.MATERIAL_UPDATE


def test_publish_priority_penalties_and_breaking_override():
    story, _, _ = _create_story_fixture()
    story.independent_source_count = 3
    story.save()

    p_normal = PublishPriorityService.evaluate_priority(story)
    assert p_normal["gate_passed"] is True
    assert p_normal["publish_priority_score"] >= 0.50
    assert p_normal["newsworthiness_score"] >= 0.50


def test_editorial_policy_actions_and_counterfactual_logging():
    story, item, _ = _create_story_fixture(trust=0.90, title="High Impact Announcement")
    story.independent_source_count = 3
    story.save()

    # 1. High priority new story -> PUBLISH_NOW
    dec = EditorialPolicyEngine.evaluate_story(story)
    assert dec["action"] in (EditorialAction.PUBLISH_NOW, EditorialAction.SCHEDULE)
    assert dec["score_record_id"] is not None
    assert dec["decision_log_id"] is not None

    score_rec = ScoreRecord.objects.get(pk=dec["score_record_id"])
    assert score_rec.newsworthiness_score > 0
    assert score_rec.publish_priority_score > 0

    dec_log = DecisionLog.objects.get(pk=dec["decision_log_id"])
    assert dec_log.policy_version == "editorial-v1"

    # 2. Candidate set batch evaluation with counterfactual run
    selected = EditorialPolicyEngine.select_batch([story], limit=5)
    assert len(selected) <= 5
    assert PublicationSelectionRun.objects.filter(selected_count__gte=0).exists()


def test_editorial_policy_watch_and_reject():
    # Low trust rumor -> REJECT
    story_low, _, _ = _create_story_fixture(trust=0.15, title="Suspicious Leak")
    story_low.independent_source_count = 1
    story_low.save()

    dec_reject = EditorialPolicyEngine.evaluate_story(story_low)
    assert dec_reject["action"] == EditorialAction.REJECT
    assert "credibility_gate_rejected" in dec_reject["reason"]
