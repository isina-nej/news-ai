"""Story selection for publishing with anti-repeat, topic saturation, and credibility gates."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone
from rapidfuzz import fuzz

from apps.ai.models import MaterialUpdateDecision
from apps.publishing.models import Publication, PublicationStatus
from apps.ranking.models import DecisionLog, DecisionType
from apps.ranking.services.scoring import (
    RANKING_ALGORITHM_VERSION,
    StoryScoringService,
    to_score_decimal,
)
from apps.stories.models import Story, StoryStatus

SELECTION_THRESHOLD = 0.55
SATURATION_WINDOW_HOURS = 4
REPETITION_WINDOW_HOURS = 12


class SelectionService:
    @classmethod
    def evaluate_story(cls, story: Story) -> dict[str, Any]:
        """Evaluate whether a story should be published, skipped, or held."""
        scored = StoryScoringService.score_story(story)
        final_score = scored["final_score"]
        gate_passed = scored["gate_passed"]
        gate_reason = scored["gate_reason"]

        now = timezone.now()

        # 1. Gate Check
        if not gate_passed:
            cls._log_decision(
                story=story,
                action="hold",
                predicted_reward=final_score,
                scored=scored,
                detail={"reason": f"credibility_gate:{gate_reason}"},
            )
            return {
                "story_id": story.pk,
                "action": "hold",
                "reason": f"credibility_gate:{gate_reason}",
                "adjusted_score": 0.0,
                "scored": scored,
            }

        # 2. Anti-Repeat Policy
        existing_pubs = Publication.objects.filter(
            story=story,
            status__in=[
                PublicationStatus.PUBLISHED,
                PublicationStatus.PUBLISHING,
                PublicationStatus.SCHEDULED,
            ],
        )
        is_already_published = existing_pubs.exists()
        material_update = None
        if is_already_published:
            latest_update = (
                MaterialUpdateDecision.objects.filter(story=story).order_by("-created_at").first()
            )
            if latest_update is None:
                cls._log_decision(
                    story=story,
                    action="skip",
                    predicted_reward=final_score,
                    scored=scored,
                    detail={"reason": "already_published_no_update"},
                )
                return {
                    "story_id": story.pk,
                    "action": "skip",
                    "reason": "already_published_no_update",
                    "adjusted_score": 0.0,
                    "scored": scored,
                }

            if latest_update.label in ("NO_NEW_INFORMATION", "MINOR_UPDATE"):
                cls._log_decision(
                    story=story,
                    action="skip",
                    predicted_reward=final_score,
                    scored=scored,
                    detail={"reason": f"already_published_{latest_update.label.lower()}"},
                )
                return {
                    "story_id": story.pk,
                    "action": "skip",
                    "reason": f"already_published_{latest_update.label.lower()}",
                    "adjusted_score": 0.0,
                    "scored": scored,
                }
            material_update = latest_update.label

        # 3. Topic Saturation Penalty
        saturation_penalty = 0.0
        if story.primary_topic:
            recent_same_topic_count = (
                Publication.objects.filter(
                    story__primary_topic=story.primary_topic,
                    status__in=[PublicationStatus.PUBLISHED, PublicationStatus.SCHEDULED],
                    created_at__gte=now - timedelta(hours=SATURATION_WINDOW_HOURS),
                )
                .exclude(story=story)
                .count()
            )
            if recent_same_topic_count >= 2:
                saturation_penalty = min(0.30, 0.10 * (recent_same_topic_count - 1))

        # 4. Repetition Penalty (lexical similarity to recent publications)
        repetition_penalty = 0.0
        recent_pubs = (
            Publication.objects.filter(
                status__in=[PublicationStatus.PUBLISHED, PublicationStatus.SCHEDULED],
                created_at__gte=now - timedelta(hours=REPETITION_WINDOW_HOURS),
            )
            .exclude(story=story)
            .select_related("story")[:30]
        )
        for pub in recent_pubs:
            other_title = (pub.headline or getattr(pub.story, "canonical_title", "") or "").strip()
            if not other_title:
                continue
            sim = fuzz.token_sort_ratio(story.canonical_title, other_title) / 100.0
            if sim >= 0.85:
                repetition_penalty = 0.25
                break

        # Boost for corrections
        boost = 0.10 if material_update == "CORRECTION" else 0.0

        adjusted = max(0.0, min(1.0, final_score + boost - saturation_penalty - repetition_penalty))

        action = "publish" if adjusted >= SELECTION_THRESHOLD else "skip"
        reason = "score_above_threshold" if action == "publish" else "score_below_threshold"
        if material_update:
            reason = f"material_update:{material_update.lower()}"

        penalties = {
            "saturation_penalty": round(saturation_penalty, 4),
            "repetition_penalty": round(repetition_penalty, 4),
            "boost": boost,
        }

        topic_slug = story.primary_topic.slug if story.primary_topic else None
        cls._log_decision(
            story=story,
            action=action,
            predicted_reward=adjusted,
            scored=scored,
            detail={
                "reason": reason,
                "penalties": penalties,
                "material_update": material_update,
                "topic_slug": topic_slug,
            },
        )

        return {
            "story_id": story.pk,
            "action": action,
            "reason": reason,
            "adjusted_score": round(adjusted, 4),
            "penalties": penalties,
            "scored": scored,
        }

    @classmethod
    def select_publishable_stories(
        cls, *, limit: int = 10, lookback_hours: int = 48
    ) -> list[dict[str, Any]]:
        """Find and rank publishable stories from recent active stories."""
        cutoff = timezone.now() - timedelta(hours=lookback_hours)
        candidates = list(
            Story.objects.filter(
                status__in=[StoryStatus.ACTIVE, StoryStatus.EMERGING],
                latest_source_update_at__gte=cutoff,
            ).order_by("-latest_source_update_at")[:50]
        )

        evaluated = []
        for story in candidates:
            res = cls.evaluate_story(story)
            if res["action"] == "publish":
                evaluated.append(res)

        evaluated.sort(key=lambda r: r["adjusted_score"], reverse=True)
        return evaluated[:limit]

    @classmethod
    def _log_decision(
        cls,
        *,
        story: Story,
        action: str,
        predicted_reward: float,
        scored: dict[str, Any],
        detail: dict[str, Any],
    ) -> DecisionLog:
        return DecisionLog.objects.create(
            story=story,
            algorithm_version=RANKING_ALGORITHM_VERSION,
            decision_type=DecisionType.WHAT,
            feature_snapshot={
                "freshness": scored["freshness"],
                "gate_passed": scored["gate_passed"],
                "gate_reason": scored["gate_reason"],
            },
            score_breakdown=scored["breakdown"],
            predicted_reward=to_score_decimal(predicted_reward),
            selected_action=action,
            action_detail=detail,
            is_exploration=False,
        )
