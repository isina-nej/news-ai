"""Tests for Phase F: AI Story Intelligence, Information Units, and Editorial Decision Schemas."""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.ai import schemas
from apps.ai.analysis import (
    analyze_story_intelligence,
    detect_material_update,
    evaluate_editorial_decision,
)
from apps.ai.facts import FactDiffService
from apps.ai.models import MaterialUpdateDecision, StoryIntelligenceSnapshot, StoryNewsValue
from apps.core.choices import Platform
from apps.news.models import SourceItem
from apps.sources.models import Source
from apps.stories.models import Story, StoryMembership

pytestmark = pytest.mark.django_db


def _create_story_fixture():
    source = Source.objects.create(
        name="AI News", platform=Platform.TELEGRAM, identifier="@ai_news"
    )
    story = Story.objects.create(canonical_title="Autonomous System Released")
    item = SourceItem.objects.create(
        source=source,
        story=story,
        title="Autonomous System Released",
        normalized_text="Engineers released an autonomous coordination system today.",
        external_id="ext-ai-1",
    )
    StoryMembership.objects.create(
        story=story, source_item=item, is_current=True, independence="independent"
    )
    return story, item, source


def test_ai_schemas_validation():
    # 1. StoryIntelligenceResult
    intel = schemas.StoryIntelligenceResult(
        canonical_event="Event",
        what_happened="Details",
        confirmed_facts=["Fact 1", "Fact 2"],
        importance=0.85,
        credibility=0.90,
    )
    assert len(intel.confirmed_facts) == 2
    assert intel.importance == 0.85

    # 2. EditorialDecisionResult
    dec = schemas.EditorialDecisionResult(
        recommended_action="PUBLISH_NOW",
        confidence=0.88,
        primary_reason="Clear signal",
    )
    assert dec.recommended_action == "PUBLISH_NOW"

    # 3. StructuredDraftResult
    draft = schemas.StructuredDraftResult(
        headline_direct="تیتر مستقیم",
        headline_breaking="تیتر فوری",
        headline_contextual="تیتر تحلیلی",
        lead="لید خبر",
        body_points=["نکته اول"],
    )
    assert draft.headline_direct == "تیتر مستقیم"

    # 4. DraftCriticResult
    critic = schemas.DraftCriticResult(
        is_approved=True,
        severity="pass",
        persian_quality_score=0.98,
    )
    assert critic.is_approved is True


def test_fact_diff_service():
    prior = [
        "President announced trade agreement with partner countries",
        "Tariffs will be lowered by 10 percent",
    ]
    curr = [
        "President announced trade agreement with partner countries",  # Repeated
        "Tariffs will be lowered by 15 percent",  # Changed
        "New customs office will be established at the border",  # New
    ]

    diff = FactDiffService.diff_facts(prior_facts=prior, current_facts=curr)
    assert len(diff["repeated_facts"]) == 1
    assert len(diff["new_facts"]) == 1
    assert len(diff["changed_facts"]) == 1
    assert "New customs office" in diff["new_facts"][0]


def test_analyze_story_intelligence_execution():
    story, item, source = _create_story_fixture()
    res = analyze_story_intelligence(story)

    assert res["status"] == "success"
    assert res["snapshot_id"] is not None

    snap = StoryIntelligenceSnapshot.objects.get(pk=res["snapshot_id"])
    assert snap.story == story
    assert len(snap.confirmed_facts) > 0
    assert snap.importance > Decimal("0.0000")

    # StoryNewsValue synchronized
    nv = StoryNewsValue.objects.get(story=story)
    assert nv.importance > Decimal("0.0000")
    assert nv.credibility > Decimal("0.0000")


def test_evaluate_editorial_decision_execution():
    story, item, source = _create_story_fixture()
    res = evaluate_editorial_decision(story)

    assert res["status"] == "success"
    payload = res["payload"]
    assert "recommended_action" in payload
    assert payload["recommended_action"] == "PUBLISH_NOW"


def test_detect_material_update_with_diff():
    story, item1, source = _create_story_fixture()
    analyze_story_intelligence(story)

    # Ingest updated item
    item2 = SourceItem.objects.create(
        source=source,
        story=story,
        title="Autonomous System Released with security patch",
        normalized_text="Critical security patch was applied to autonomous system.",
        external_id="ext-ai-2",
    )

    res = detect_material_update(story=story, source_item=item2)
    assert res["status"] == "success"

    decision = MaterialUpdateDecision.objects.filter(story=story, source_item=item2).first()
    assert decision is not None
    assert isinstance(decision.information_unit_diff, dict)
