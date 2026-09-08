"""Persist validated AI outputs. Combinatorial credibility stays deterministic."""

from __future__ import annotations

import json

from apps.ai.models import (
    AI_VERSION,
    MaterialUpdateDecision,
    StoryConflict,
    StoryNewsValue,
    TopicClassification,
)
from apps.ai.service import run_structured_task, to_decimal


def _evidence_json(evidence: dict) -> str:
    return json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str)[:12000]


def classify_topic(story, *, provider=None) -> dict:
    from apps.ai.evidence import story_evidence

    evidence = story_evidence(story)
    result = run_structured_task(
        task="topic_classification",
        evidence=evidence,
        prompt_replacements={
            "__TOPIC_SLUGS__": _allowed_topic_slugs(),
            "__EVIDENCE__": _evidence_json(evidence),
        },
        provider=provider,
    )
    payload = result["payload"]
    topic = _resolve_topic(payload.get("topic_slug"))
    subtopic = _resolve_subtopic(topic, payload.get("subtopic_slug"))
    TopicClassification.objects.update_or_create(
        story=story,
        defaults={
            "topic": topic,
            "subtopic": subtopic,
            "candidate_topic": (payload.get("candidate_topic") or "")[:128],
            "confidence": to_decimal(payload.get("confidence", 0)),
            "reason_codes": payload.get("reason_codes", []),
            "provider": result.get("provider", provider.name if provider else "fake"),
            "model": result.get("model", ""),
            "prompt_version": result.get("prompt_version", "v1"),
            "algorithm_version": AI_VERSION,
        },
    )
    return result


def extract_news_value(story, *, provider=None) -> dict:
    from apps.ai.evidence import story_evidence

    evidence = story_evidence(story)
    result = run_structured_task(
        task="news_value",
        evidence=evidence,
        prompt_replacements={"__EVIDENCE__": _evidence_json(evidence)},
        provider=provider,
    )
    payload = result["payload"]
    StoryNewsValue.objects.update_or_create(
        story=story,
        defaults={
            **{
                key: to_decimal(payload[key]["score"])
                for key in (
                    "importance",
                    "utility",
                    "impact",
                    "novelty",
                    "urgency",
                    "credibility",
                )
            },
            "component_detail": payload,
            "provider": result.get("provider", provider.name if provider else "fake"),
            "model": result.get("model", ""),
            "prompt_version": result.get("prompt_version", "v1"),
            "algorithm_version": AI_VERSION,
        },
    )
    return result


def detect_conflicts(story, *, provider=None) -> dict:
    from apps.ai.evidence import story_evidence

    evidence = story_evidence(story)
    result = run_structured_task(
        task="conflict_detection",
        evidence=evidence,
        prompt_replacements={"__EVIDENCE__": _evidence_json(evidence)},
        provider=provider,
    )
    payload = result["payload"]
    StoryConflict.objects.update_or_create(
        story=story,
        defaults={
            "has_conflict": bool(payload.get("has_conflict")),
            "summary": (payload.get("summary") or "")[:1000],
            "confidence": to_decimal(payload.get("confidence", 0)),
            "reason_codes": payload.get("reason_codes", []),
            "provider": result.get("provider", provider.name if provider else "fake"),
            "model": result.get("model", ""),
            "prompt_version": result.get("prompt_version", "v1"),
            "algorithm_version": AI_VERSION,
        },
    )
    return result


def combined_credibility(story) -> dict:
    """Deterministic combination. AI is one signal, never the source of truth."""
    source_trust = 0.5
    members = list(story.memberships.filter(is_current=True).select_related("source_item__source"))
    if members:
        trusts = []
        for membership in members:
            try:
                trusts.append(float(membership.source_item.source.trust_score or 0.5))
            except Exception:
                trusts.append(0.5)
        source_trust = sum(trusts) / len(trusts)
    spread = min(1.0, story.independent_source_count / 3)
    ai_value = None
    try:
        ai_value = float(story.news_value.credibility)
    except Exception:
        ai_value = None
    conflict_obj = getattr(story, "conflict", None)
    conflict_penalty = 0.25 if getattr(conflict_obj, "has_conflict", False) else 0.0
    ai_component = ai_value if ai_value is not None else 0.5
    score = max(
        0.0,
        min(1.0, 0.45 * source_trust + 0.25 * spread + 0.30 * ai_component - conflict_penalty),
    )
    return {
        "score": round(score, 4),
        "components": {
            "source_trust": round(source_trust, 4),
            "independent_spread": round(spread, 4),
            "ai_credibility": round(ai_component, 4),
            "conflict_penalty": conflict_penalty,
        },
        "algorithm_version": AI_VERSION,
    }


def judge_ambiguous_candidate(*, source_item, candidate_story, decision, provider=None) -> dict:  # noqa: ARG001
    from apps.ai.evidence import story_evidence

    story_evidence_json = _evidence_json(story_evidence(candidate_story))
    item_json = _evidence_json(
        {
            "title": source_item.title,
            "text": (source_item.normalized_text or source_item.raw_text)[:2000],
            "published_at": (
                source_item.published_at.isoformat() if source_item.published_at else None
            ),
        }
    )
    return run_structured_task(
        task="clustering_judge",
        evidence={"item": item_json, "story": story_evidence_json},
        prompt_replacements={"__ITEM__": item_json, "__STORY__": story_evidence_json},
        provider=provider,
    )


def detect_material_update(*, story, source_item, provider=None) -> dict:
    from apps.ai.evidence import story_evidence

    story_json = _evidence_json(story_evidence(story))
    item_json = _evidence_json(
        {
            "title": source_item.title,
            "text": (source_item.normalized_text or source_item.raw_text)[:2000],
            "published_at": (
                source_item.published_at.isoformat() if source_item.published_at else None
            ),
            "source_updated_at": (
                source_item.source_updated_at.isoformat() if source_item.source_updated_at else None
            ),
        }
    )
    result = run_structured_task(
        task="material_update_detection",
        evidence={"story": story_json, "item": item_json},
        prompt_replacements={"__STORY__": story_json, "__ITEM__": item_json},
        provider=provider,
    )
    payload = result["payload"]
    MaterialUpdateDecision.objects.create(
        story=story,
        source_item=source_item,
        label=payload["label"],
        confidence=to_decimal(payload.get("confidence", 0)),
        provider=result.get("provider", provider.name if provider else "fake"),
        model=result.get("model", ""),
        prompt_version=result.get("prompt_version", "v1"),
        algorithm_version=AI_VERSION,
        reason_codes=payload.get("reason_codes", []),
    )
    return result


def _allowed_topic_slugs() -> str:
    from apps.ops.models import Topic

    slugs = list(Topic.objects.filter(enabled=True).values_list("slug", flat=True)[:100])
    return ", ".join(slugs) if slugs else "(none configured)"


def _resolve_topic(slug: str | None):
    if not slug:
        return None
    from apps.ops.models import Topic

    return Topic.objects.filter(slug=slug, enabled=True).first()


def _resolve_subtopic(topic, slug: str | None):
    if not topic or not slug:
        return None
    from apps.ops.models import Subtopic

    return Subtopic.objects.filter(topic=topic, slug=slug, enabled=True).first()
