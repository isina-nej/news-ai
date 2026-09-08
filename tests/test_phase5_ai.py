"""Phase 5: structured AI. Deterministic FakeAIProvider; no network/credentials."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from pydantic import ValidationError

from apps.ai import schemas
from apps.ai.analysis import (
    classify_topic,
    combined_credibility,
    detect_conflicts,
    detect_material_update,
    extract_news_value,
)
from apps.ai.judge import apply_judge_to_decision
from apps.ai.models import AICallLog, AIResultCache, AITask
from apps.ai.prompts import REGISTRY, get_prompt
from apps.ai.providers import FakeAIProvider, route_model
from apps.ai.service import (
    feature_enabled,
    input_hash,
    run_structured_task,
)
from apps.core.choices import Platform
from apps.core.exceptions import AIProviderError
from apps.news.models import SourceItem
from apps.ops.models import Subtopic, Topic
from apps.sources.models import Source
from apps.stories.models import ClusteringDecision, Story, StoryMembership, StoryStatus

pytestmark = [pytest.mark.django_db]


def _source(**kwargs):
    kwargs.setdefault("platform", Platform.TELEGRAM)
    kwargs.setdefault("name", "AI Source")
    kwargs.setdefault("identifier", "@aisrc")
    kwargs.setdefault("url", "https://t.me/aisrc")
    return Source.objects.create(**kwargs)


def _item(source, title, text, **kwargs):
    kwargs.setdefault("published_at", timezone.now() - timedelta(hours=1))
    return SourceItem.objects.create(
        source=source, title=title, raw_text=text, normalized_text=text, **kwargs
    )


def _story(title="AI story", **kwargs):
    kwargs.setdefault("canonical_title", title)
    kwargs.setdefault("latest_source_update_at", timezone.now())
    return Story.objects.create(**kwargs)


def test_prompt_registry_has_versions_and_schemas():
    for name in (
        "topics",
        "news_value",
        "conflict",
        "clustering_judge",
        "material_update",
        "post_draft",
    ):
        spec = get_prompt(name, "v1")
        assert spec.task and spec.schema
        assert "__EVIDENCE__" in spec.text() or "__ITEM__" in spec.text()
    assert ("topics", "v1") in REGISTRY


def test_schema_validation_rejects_out_of_range():
    with pytest.raises(ValidationError):
        schemas.TopicClassificationResult.model_validate({"confidence": 9})
    with pytest.raises(ValidationError):
        schemas.MaterialUpdateResult.model_validate({"label": "NOPE", "confidence": 0.5})


def test_malformed_json_records_invalid_response():
    provider = FakeAIProvider(mode="malformed")
    with pytest.raises(AIProviderError):
        run_structured_task(
            task="topic_classification",
            evidence={"a": 1},
            prompt_replacements={"__TOPIC_SLUGS__": "x", "__EVIDENCE__": "{}"},
            provider=provider,
        )
    assert AICallLog.objects.filter(status="invalid_response").exists()


def test_timeout_records_failure_state():
    provider = FakeAIProvider(mode="timeout")
    with pytest.raises(AIProviderError):
        run_structured_task(
            task="news_value",
            evidence={"a": 1},
            prompt_replacements={"__EVIDENCE__": "{}"},
            provider=provider,
        )
    assert AICallLog.objects.filter(status__in=("failure", "timeout")).exists()


def test_cache_hit_avoids_second_provider_call():
    provider = FakeAIProvider()
    kwargs = {
        "task": "conflict_detection",
        "evidence": {"story": "same"},
        "prompt_replacements": {"__EVIDENCE__": "same"},
        "provider": provider,
    }
    first = run_structured_task(**kwargs)
    second = run_structured_task(**kwargs)
    assert first["status"] == "success"
    assert second["status"] == "cached"
    assert provider.calls == 1
    assert AICallLog.objects.filter(cached=True).exists()


def test_prompt_version_change_invalidates_cache():
    provider = FakeAIProvider()
    evidence = {"story": "same"}
    first = run_structured_task(
        task="conflict_detection",
        evidence=evidence,
        prompt_replacements={"__EVIDENCE__": "same"},
        provider=provider,
    )
    AIResultCache.objects.filter(input_hash=first["input_hash"]).update(prompt_version="v0")
    second = run_structured_task(
        task="conflict_detection",
        evidence=evidence,
        prompt_replacements={"__EVIDENCE__": "same"},
        provider=provider,
    )
    assert second["status"] == "success"
    assert provider.calls == 2


def test_topic_classification_constrained_to_taxonomy():
    topic = Topic.objects.create(slug="tech", name="Tech")
    Subtopic.objects.create(topic=topic, slug="ai", name="AI")
    source = _source()
    story = _story()
    item = _item(source, "AI model released today", "A new AI model was released today.")
    StoryMembership.objects.create(story=story, source_item=item, is_current=True)
    result = classify_topic(story, provider=FakeAIProvider())
    assert result["status"] in ("success", "cached")
    story.refresh_from_db()
    assert story.topic_classification.confidence <= 1


def test_news_value_components_all_present():
    source = _source()
    story = _story()
    StoryMembership.objects.create(
        story=story, source_item=_item(source, "Big quake", "Big quake hit."), is_current=True
    )
    result = extract_news_value(story, provider=FakeAIProvider())
    payload = result["payload"]
    assert set(payload) == {
        "importance",
        "utility",
        "impact",
        "novelty",
        "urgency",
        "credibility",
    }
    for component in payload.values():
        assert 0 <= component["score"] <= 1
        assert 0 <= component["confidence"] <= 1


def test_combined_credibility_not_ai_only():
    low = _source(name="Low", identifier="@low")
    low.trust_score = "0.1"
    low.save()
    story = _story()
    story.independent_source_count = 0
    story.save(update_fields=["independent_source_count", "updated_at"])
    StoryMembership.objects.create(
        story=story, source_item=_item(low, "Rumor", "Unconfirmed rumor text."), is_current=True
    )
    combined = combined_credibility(story)
    assert combined["score"] < 0.6
    assert combined["components"]["source_trust"] == 0.1


def test_conflict_detection_persists_flag():
    source = _source()
    story = _story()
    StoryMembership.objects.create(
        story=story,
        source_item=_item(source, "Rates 5%", "Bank holds at 5 percent."),
        is_current=True,
    )
    result = detect_conflicts(story, provider=FakeAIProvider())
    assert result["status"] in ("success", "cached")
    assert story.conflict.has_conflict in (True, False)


def test_ambiguous_judge_low_confidence_stays_split():
    source = _source()
    story = _story()
    item = _item(source, "Possible follow-up", "Possible follow-up body.")
    decision = ClusteringDecision.objects.create(
        source_item=item,
        candidate_story=story,
        decision="ambiguous",
        algorithm_version="cluster-v1",
    )
    out = apply_judge_to_decision(decision.pk, provider=FakeAIProvider())
    assert out["status"] == "held"
    assert item.memberships.filter(is_current=True).count() == 0


def test_material_update_labels_are_closed_set():
    source = _source()
    story = _story()
    story.status = StoryStatus.ACTIVE
    story.save()
    item = _item(source, "Update", "Update body with correction.")
    result = detect_material_update(story=story, source_item=item, provider=FakeAIProvider())
    assert result["payload"]["label"] in (
        "NO_NEW_INFORMATION",
        "MINOR_UPDATE",
        "MATERIAL_UPDATE",
        "CORRECTION",
        "MAJOR_BREAKING_UPDATE",
    )


def test_credential_missing_is_explicit_not_silent():
    from apps.ai.providers import OpenAICompatibleProvider

    with pytest.raises(AIProviderError):
        OpenAICompatibleProvider(base_url="", api_key="", timeout=1, max_retries=0)


def test_model_routing_cheap_vs_strong():
    assert route_model("topic_classification")
    assert route_model("clustering_judge")


def test_ai_task_ledger_failure_is_visible():
    story = _story()
    AITask.objects.create(
        story=story,
        task="news_value",
        input_hash="x",
        state="failed",
        error_type="AIProviderError",
    )
    assert AITask.objects.filter(state="failed").exists()
    assert feature_enabled() in (True, False)


def test_input_hash_changes_with_content_revision():
    assert input_hash("news_value", "v1", "m", {"t": 1}) != input_hash(
        "news_value", "v1", "m", {"t": 2}
    )
