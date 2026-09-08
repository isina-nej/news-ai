"""Contextual Bandit for editorial style and timing decisions.

Selects strictly among pre-screened safe actions — NEVER bypasses credibility
gates, duplicate filters, or rate limits.
"""

from __future__ import annotations

import random
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.ops.models import FeatureFlag
from apps.ops.services.audience import AudienceLearningService
from apps.ranking.models import DecisionLog, to_score_decimal  # noqa: F401

SAFE_STYLE_ACTIONS = [
    {"headline_style": "factual_short", "tone": "neutral"},
    {"headline_style": "concise", "tone": "direct"},
    {"headline_style": "technical", "tone": "analytical"},
    {"headline_style": "bullet", "tone": "structured"},
]


def exploration_enabled() -> bool:
    try:
        row = FeatureFlag.objects.filter(key="ENABLE_EXPLORATION").first()
        if row is not None:
            return bool(row.enabled)
    except Exception:  # noqa: S110 — flag lookup fallback to settings
        pass
    flags = getattr(settings, "FEATURE_FLAGS", {})
    return bool(flags.get("ENABLE_EXPLORATION", True))


def get_exploration_rate() -> float:
    return float(getattr(settings, "EXPLORATION_RATE", 0.05) or 0.05)


class ContextualBanditService:
    @classmethod
    def select_action(
        cls,
        *,
        story,
        candidate_actions: list[dict[str, Any]] | None = None,
        epsilon: float | None = None,
    ) -> tuple[dict[str, Any], bool, float]:
        """Select action using epsilon-greedy over expected audience reward.

        Returns: (selected_action, is_exploration, exploration_probability)
        """
        actions = candidate_actions or SAFE_STYLE_ACTIONS
        if not actions:
            default = {"headline_style": "factual_short", "tone": "neutral"}
            return default, False, 0.0

        eps = get_exploration_rate() if epsilon is None else epsilon
        is_explore_active = exploration_enabled()

        topic_slug = story.primary_topic.slug if story.primary_topic else None

        # 1. Epsilon-greedy exploration
        if is_explore_active and random.random() < eps:  # noqa: S311
            chosen = random.choice(actions)  # noqa: S311
            return chosen, True, eps

        # 2. Exploitation: score each safe candidate with expected performance
        best_action = actions[0]
        best_expected = -1.0

        for act in actions:
            expected = AudienceLearningService.expected_channel_performance(
                topic_slug=topic_slug,
                headline_style=act.get("headline_style", "factual_short"),
                tone=act.get("tone", "neutral"),
            )
            # Composite expected score
            score = (
                0.40 * expected["expected_forward_percentile"]
                + 0.35 * expected["expected_view_percentile"]
                + 0.25 * expected["expected_reaction_percentile"]
            )
            if score > best_expected:
                best_expected = score
                best_action = act

        prob = (1.0 - eps) if is_explore_active else 1.0
        return best_action, False, prob

    @classmethod
    def record_feedback(
        cls,
        *,
        decision_log_id: int,
        reward: float,
    ) -> DecisionLog:
        """Update DecisionLog with observed reward and update audience preferences."""
        log = DecisionLog.objects.get(pk=decision_log_id)
        log.actual_reward = to_score_decimal(reward)
        log.reward_calculated_at = timezone.now()
        log.save(update_fields=["actual_reward", "reward_calculated_at"])

        # Feed back to AudienceLearningService
        detail = dict(log.action_detail or {})
        topic_slug = detail.get("topic_slug")
        if topic_slug:
            AudienceLearningService.update_preference(
                feature="topic",
                context={"topic": topic_slug},
                observed_performance=reward,
            )

        style = detail.get("style", {})
        if style:
            AudienceLearningService.update_preference(
                feature="style",
                context=style,
                observed_performance=reward,
            )

        return log
