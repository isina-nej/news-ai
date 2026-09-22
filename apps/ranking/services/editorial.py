"""EditorialPolicyEngine: issues audit-friendly editorial actions with counterfactual candidate logging."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.utils import timezone

from apps.core.choices import EditorialAction
from apps.ranking.models import DecisionLog, DecisionType, PublicationSelectionRun, ScoreRecord
from apps.ranking.services.novelty import ChannelNoveltyType
from apps.ranking.services.priority import PRIORITY_ALGORITHM_VERSION, PublishPriorityService
from apps.stories.models import Story

EDITORIAL_POLICY_VERSION = "editorial-v1"
PUBLISH_NOW_THRESHOLD = 0.68
BREAKING_PUBLISH_THRESHOLD = 0.58
SCHEDULE_THRESHOLD = 0.52


class EditorialPolicyEngine:
    """Deterministic policy engine: selects PUBLISH_NOW, WATCH, SCHEDULE, SKIP, UPDATE, or REJECT."""

    @classmethod
    def evaluate_story(
        cls,
        story: Story,
        *,
        batch_id: str = "",
        now: Any | None = None,
    ) -> dict[str, Any]:
        """Evaluate a single story and produce a binding editorial decision."""
        current_time = now or timezone.now()
        evaluated = PublishPriorityService.evaluate_priority(story, now=current_time)

        priority = evaluated["publish_priority_score"]
        newsworthiness = evaluated["newsworthiness_score"]
        gate_passed = evaluated["gate_passed"]
        gate_reason = evaluated["gate_reason"]
        novelty_type = evaluated["novelty_type"]
        is_breaking = evaluated["is_breaking"]
        indep_count = story.independent_source_count

        action: str
        reason: str
        wait_minutes: int | None = None

        # 1. Hard Gate: Credibility & Conflict Checks -> REJECT
        if not gate_passed:
            action = EditorialAction.REJECT
            reason = f"credibility_gate_rejected:{gate_reason}"

        # 2. Already published without new facts -> SKIP / REPEAT
        elif novelty_type == ChannelNoveltyType.REPEAT:
            action = EditorialAction.SKIP
            reason = "already_published_no_new_facts"

        elif novelty_type == ChannelNoveltyType.MINOR_UPDATE:
            action = EditorialAction.SKIP
            reason = "minor_update_below_publication_threshold"

        # 3. Material Update or Correction -> UPDATE_EXISTING_STORY
        elif novelty_type in (ChannelNoveltyType.MATERIAL_UPDATE, ChannelNoveltyType.CORRECTION):
            if priority >= SCHEDULE_THRESHOLD:
                action = (
                    EditorialAction.PUBLISH_NOW
                    if is_breaking
                    else EditorialAction.UPDATE_EXISTING_STORY
                )
                reason = f"material_update_approved:{novelty_type.lower()}"
            else:
                action = EditorialAction.SKIP
                reason = f"material_update_priority_too_low:{novelty_type.lower()}"

        # 4. WATCH: story is promising but confirmation or samples are premature
        elif (
            (indep_count < 2 and newsworthiness >= 0.70)
            or (evaluated.get("acceleration", 0.5) >= 0.70 and priority < PUBLISH_NOW_THRESHOLD)
            or (0.50 <= priority < PUBLISH_NOW_THRESHOLD and indep_count <= 1)
        ):
            action = EditorialAction.WATCH
            wait_minutes = 10 if indep_count < 2 else 5
            reason = (
                "awaiting_independent_confirmation"
                if indep_count < 2
                else "monitoring_acceleration_trend"
            )

        # 5. High Priority Breaking or Standard -> PUBLISH_NOW
        elif (
            is_breaking and priority >= BREAKING_PUBLISH_THRESHOLD
        ) or priority >= PUBLISH_NOW_THRESHOLD:
            action = EditorialAction.PUBLISH_NOW
            reason = "breaking_news_priority_cleared" if is_breaking else "priority_score_cleared"

        # 6. Moderate Priority -> SCHEDULE
        elif priority >= SCHEDULE_THRESHOLD:
            action = EditorialAction.SCHEDULE
            reason = "schedule_for_next_available_slot"

        # 7. Low Priority -> SKIP
        else:
            action = EditorialAction.SKIP
            reason = "score_below_selection_threshold"

        # Persist ScoreRecord with both explicit scores
        score_record = ScoreRecord.objects.create(
            story=story,
            algorithm_version=evaluated["algorithm_version"],
            news_value=Decimal(str(newsworthiness)).quantize(Decimal("0.0001")),
            audience_fit=Decimal(str(evaluated["audience_fit_score"])).quantize(Decimal("0.0001")),
            momentum=Decimal(str(evaluated["momentum_score"])).quantize(Decimal("0.0001")),
            final_score=Decimal(str(priority)).quantize(Decimal("0.0001")),
            newsworthiness_score=Decimal(str(newsworthiness)).quantize(Decimal("0.0001")),
            publish_priority_score=Decimal(str(priority)).quantize(Decimal("0.0001")),
            breakdown=evaluated["breakdown"],
        )

        # Log DecisionLog for audit & counterfactual replay
        decision_log = DecisionLog.objects.create(
            story=story,
            algorithm_version=evaluated["algorithm_version"],
            policy_version=EDITORIAL_POLICY_VERSION,
            decision_batch_id=batch_id,
            decision_type=DecisionType.WHAT,
            feature_snapshot={
                "freshness": evaluated["freshness_score"],
                "gate_passed": gate_passed,
                "gate_reason": gate_reason,
                "novelty_type": novelty_type,
                "is_breaking": is_breaking,
                "wait_minutes": wait_minutes,
            },
            score_breakdown=evaluated["breakdown"],
            predicted_reward=Decimal(str(priority)).quantize(Decimal("0.0001")),
            selected_action=action,
            action_detail={
                "reason": reason,
                "penalties": evaluated["penalties"],
                "novelty_detail": evaluated["novelty_detail"],
                "wait_minutes": wait_minutes,
                "topic_slug": story.primary_topic.slug if story.primary_topic else None,
            },
            is_exploration=False,
        )

        return {
            "story_id": story.pk,
            "action": action,
            "reason": reason,
            "newsworthiness_score": newsworthiness,
            "publish_priority_score": priority,
            "adjusted_score": priority,  # backward-compat alias
            "recommended_wait_minutes": wait_minutes,
            "gate_passed": gate_passed,
            "score_record_id": score_record.pk,
            "decision_log_id": decision_log.pk,
            "evaluated": evaluated,
        }

    @classmethod
    def select_batch(
        cls,
        stories: list[Story],
        *,
        limit: int = 10,
        now: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Evaluate a candidate set of stories, log the counterfactual run, and return winners."""
        current_time = now or timezone.now()
        import uuid

        batch_id = f"batch-{uuid.uuid4().hex[:12]}"
        candidates_out: list[dict[str, Any]] = []

        for story in stories:
            res = cls.evaluate_story(story, batch_id=batch_id, now=current_time)
            candidates_out.append(
                {
                    "story_id": story.pk,
                    "canonical_title": story.canonical_title[:80],
                    "action": res["action"],
                    "reason": res["reason"],
                    "newsworthiness_score": res["newsworthiness_score"],
                    "publish_priority_score": res["publish_priority_score"],
                    "selected": res["action"]
                    in (EditorialAction.PUBLISH_NOW, EditorialAction.UPDATE_EXISTING_STORY),
                }
            )

        # Sort by publish priority descending
        candidates_out.sort(key=lambda c: c["publish_priority_score"], reverse=True)
        selected = [c for c in candidates_out if c["selected"]][:limit]

        PublicationSelectionRun.objects.create(
            run_at=current_time,
            algorithm_version=PRIORITY_ALGORITHM_VERSION,
            policy_version=EDITORIAL_POLICY_VERSION,
            candidates=candidates_out,
            selected_count=len(selected),
            summary={"total_evaluated": len(stories), "batch_id": batch_id},
        )

        return selected
