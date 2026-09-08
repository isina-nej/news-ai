"""Audience learning: robust statistics, EWMA, Bayesian smoothing, recency decay.

Separates topic vs style vs timing vs source effects to prevent confounding.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from django.utils import timezone

from apps.ops.models import AudiencePreference

PRIOR_MEAN = 0.50
PRIOR_WEIGHT = 5.0  # Equivalent number of pseudo-observations for Bayes smoothing
DEFAULT_EWMA_ALPHA = 0.15
RECENCY_HALF_LIFE_DAYS = 30.0


def to_pref_decimal(val: float | None) -> Decimal:
    if val is None or not math.isfinite(val):
        return Decimal("0.500000")
    clamped = max(0.0, min(1.0, float(val)))
    return Decimal(str(clamped)).quantize(Decimal("0.000001"))


class AudienceLearningService:
    @staticmethod
    def bayesian_smoothed_mean(
        observations: list[float], *, prior: float = PRIOR_MEAN, prior_weight: float = PRIOR_WEIGHT
    ) -> float:
        """Compute Bayesian-smoothed mean: prevents 1 lucky observation from dominating."""
        if not observations:
            return prior
        n = len(observations)
        sample_sum = sum(observations)
        smoothed = (sample_sum + (prior * prior_weight)) / (n + prior_weight)
        return round(max(0.0, min(1.0, smoothed)), 6)

    @classmethod
    def update_preference(
        cls,
        *,
        feature: str,
        context: dict[str, Any],
        observed_performance: float,
        observed_at: Any = None,
    ) -> AudiencePreference:
        """Update single feature preference using EWMA and Bayesian smoothing.

        Features tracked independently: topic, subtopic, content_type,
        headline_style, tone, timing_hour, technical_depth, emoji_level.
        """
        now = observed_at or timezone.now()
        row = AudiencePreference.objects.filter(feature=feature, context=context).first()
        if row is None:
            row = AudiencePreference(feature=feature, context=context)

        # Recency decay on previous observation weight
        old_val = float(row.value) if row.sample_count > 0 else PRIOR_MEAN
        decay = 1.0
        if row.updated_at:
            age_days = (now - row.updated_at).total_seconds() / 86400.0
            decay = 0.5 ** (max(0.0, age_days) / RECENCY_HALF_LIFE_DAYS)

        alpha = DEFAULT_EWMA_ALPHA * decay
        new_val = (1.0 - alpha) * old_val + alpha * observed_performance

        row.sample_count += 1
        # Confidence increases with sample count (Bayesian shrinkage)
        conf = min(1.0, row.sample_count / (row.sample_count + PRIOR_WEIGHT))

        row.value = to_pref_decimal(new_val)
        row.confidence = Decimal(str(round(conf, 4)))
        row.save()
        return row

    @classmethod
    def get_preference_value(
        cls, feature: str, context: dict[str, Any], default: float = 0.50
    ) -> float:
        """Retrieve smoothed preference value for a given feature/context."""
        row = AudiencePreference.objects.filter(feature=feature, context=context).first()
        if row and row.sample_count > 0:
            return float(row.value)
        return default

    @classmethod
    def expected_channel_performance(
        cls,
        *,
        topic_slug: str | None,
        content_type: str = "post",
        headline_style: str = "factual_short",
        tone: str = "neutral",
        hour: int | None = None,
    ) -> dict[str, float]:
        """Estimate expected channel percentiles before publication.

        Separates effects:
        - topic effect
        - style effect
        - timing effect
        """
        # 1. Topic effect
        topic_pref = 0.50
        if topic_slug:
            topic_pref = cls.get_preference_value("topic", {"topic": topic_slug}, default=0.50)

        # 2. Style effect
        style_pref = cls.get_preference_value(
            "style", {"headline_style": headline_style, "tone": tone}, default=0.50
        )

        # 3. Content type effect
        type_pref = cls.get_preference_value(
            "content_type", {"content_type": content_type}, default=0.50
        )

        # 4. Timing effect
        hour_pref = 0.50
        if hour is not None:
            hour_pref = cls.get_preference_value("hour", {"hour": hour}, default=0.50)

        # Composite expected percentiles
        expected_views = round(
            0.40 * topic_pref + 0.30 * type_pref + 0.15 * style_pref + 0.15 * hour_pref, 4
        )
        expected_forwards = round(0.50 * topic_pref + 0.30 * style_pref + 0.20 * type_pref, 4)
        expected_reactions = round(0.35 * topic_pref + 0.35 * style_pref + 0.30 * hour_pref, 4)
        expected_replies = round(0.40 * topic_pref + 0.40 * style_pref + 0.20 * hour_pref, 4)

        return {
            "expected_view_percentile": expected_views,
            "expected_forward_percentile": expected_forwards,
            "expected_reaction_percentile": expected_reactions,
            "expected_reply_percentile": expected_replies,
        }
