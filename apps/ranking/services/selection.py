"""Story selection for publishing with anti-repeat, topic saturation, and credibility gates."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from apps.core.choices import EditorialAction
from apps.ranking.services.editorial import EditorialPolicyEngine
from apps.stories.models import Story, StoryStatus


class SelectionService:
    """Unified facade for story editorial evaluation and selection."""

    @classmethod
    def evaluate_story(cls, story: Story, *, now: Any | None = None) -> dict[str, Any]:
        """Evaluate whether a story should be published, skipped, or held."""
        current_time = now or timezone.now()
        res = EditorialPolicyEngine.evaluate_story(story, now=current_time)

        # Backward compatibility mapping for callers expecting legacy action strings
        action_map = {
            EditorialAction.PUBLISH_NOW: "publish",
            EditorialAction.UPDATE_EXISTING_STORY: "publish",
            EditorialAction.SCHEDULE: "publish",
            EditorialAction.WATCH: "hold",
            EditorialAction.SKIP: "skip",
            EditorialAction.REJECT: "hold",
        }
        legacy_action = action_map.get(res["action"], "skip")

        return {
            "story_id": story.pk,
            "action": legacy_action,
            "editorial_action": res["action"],
            "reason": res["reason"],
            "adjusted_score": res["publish_priority_score"],
            "newsworthiness_score": res["newsworthiness_score"],
            "publish_priority_score": res["publish_priority_score"],
            "recommended_wait_minutes": res["recommended_wait_minutes"],
            "penalties": res["evaluated"]["penalties"],
            "scored": {
                "score_record_id": res["score_record_id"],
                "final_score": res["evaluated"]["raw_priority_score"],
                "freshness": res["evaluated"]["freshness_score"],
                "gate_passed": res["gate_passed"],
                "gate_reason": res["evaluated"]["gate_reason"],
                "breakdown": res["evaluated"]["breakdown"],
            },
        }

    @classmethod
    def select_publishable_stories(
        cls, *, limit: int = 10, lookback_hours: int = 48, now: Any | None = None
    ) -> list[dict[str, Any]]:
        """Find and rank publishable stories from recent active stories."""
        current_time = now or timezone.now()
        cutoff = current_time - timedelta(hours=lookback_hours)
        candidates = list(
            Story.objects.filter(
                status__in=[StoryStatus.ACTIVE, StoryStatus.EMERGING],
                latest_source_update_at__gte=cutoff,
            ).order_by("-latest_source_update_at")[:50]
        )

        batch_selected = EditorialPolicyEngine.select_batch(
            candidates, limit=limit, now=current_time
        )
        evaluated = []
        for item in batch_selected:
            evaluated.append(
                {
                    "story_id": item["story_id"],
                    "action": "publish",
                    "editorial_action": item["action"],
                    "reason": item["reason"],
                    "adjusted_score": item["publish_priority_score"],
                    "publish_priority_score": item["publish_priority_score"],
                    "newsworthiness_score": item["newsworthiness_score"],
                }
            )

        return evaluated
