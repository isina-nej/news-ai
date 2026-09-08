"""Story scoring: NewsValue, AudienceFit, Momentum, Freshness, CredibilityGate.

All component and final scores are standardized to 0.0000..1.0000 (Decimal).
Saturating functions prevent copy networks from inflating momentum.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from django.utils import timezone

from apps.ranking.models import RankingWeight, ScoreRecord
from apps.ranking.services.metrics import compute_item_relative_metrics
from apps.stories.models import Story

RANKING_ALGORITHM_VERSION = "ranking-v1"

DEFAULT_WEIGHTS = {
    "weight_news_value": 0.45,
    "weight_audience_fit": 0.30,
    "weight_momentum": 0.20,
    "weight_freshness": 0.05,
}


def to_score_decimal(val: float | None) -> Decimal:
    if val is None or not math.isfinite(val):
        return Decimal("0.0000")
    clamped = max(0.0, min(1.0, float(val)))
    return Decimal(str(clamped)).quantize(Decimal("0.0001"))


def get_ranking_weights() -> dict[str, float]:
    """Retrieve ranking weights from database or defaults, normalized to sum to 1.0."""
    weights = dict(DEFAULT_WEIGHTS)
    for key in weights:
        row = RankingWeight.objects.filter(key=key, enabled=True).first()
        if row and row.value is not None:
            weights[key] = float(row.value)
    tot = sum(weights.values()) or 1.0
    return {k: round(v / tot, 4) for k, v in weights.items()}


def compute_freshness(story: Story, *, half_life_hours: float = 24.0) -> float:
    """Exponential half-life decay based on latest_source_update_at."""
    ts = story.latest_source_update_at or story.first_published_at
    if not ts:
        return 0.50
    now = timezone.now()
    age_hours = max(0.0, (now - ts).total_seconds() / 3600.0)
    decay = 0.5 ** (age_hours / max(1.0, half_life_hours))
    return round(max(0.0001, min(1.0, decay)), 4)


class CredibilityGate:
    """Publishing safety gate. Stops low-trust rumors and unresolved conflicts."""

    @staticmethod
    def evaluate(story: Story, credibility_score: float) -> tuple[bool, str]:
        """Returns (is_passed, reason_code).

        Failure reasons:
        - low_credibility: score below 0.25
        - unresolved_conflict: story has conflicting factual claims
        - single_low_trust_rumor: single source with trust < 0.30
        """
        if credibility_score < 0.25:
            return False, "low_credibility"

        conflict = getattr(story, "conflict", None)
        if conflict and conflict.has_conflict and float(conflict.confidence or 0.0) >= 0.60:
            return False, "unresolved_conflict"

        if story.independent_source_count <= 1:
            members = list(
                story.memberships.filter(is_current=True).select_related("source_item__source")
            )
            if members:
                first_src = members[0].source_item.source
                trust = float(first_src.trust_score or 0.5)
                if trust < 0.30:
                    return False, "single_low_trust_rumor"

        return True, "passed"


class StoryScoringService:
    @classmethod
    def compute_news_value(cls, story: Story) -> tuple[float, dict[str, float]]:
        """Compute NewsValue (0..1) and component breakdown."""
        nv_obj = getattr(story, "news_value", None)
        if nv_obj:
            comp = {
                "importance": float(nv_obj.importance),
                "utility": float(nv_obj.utility),
                "impact": float(nv_obj.impact),
                "novelty": float(nv_obj.novelty),
                "urgency": float(nv_obj.urgency),
                "credibility": float(nv_obj.credibility),
            }
            score = sum(comp.values()) / len(comp)
            return round(score, 4), comp

        # Deterministic fallback when AI is not yet run
        base_cred = min(1.0, 0.40 + 0.20 * min(3, story.independent_source_count))
        comp = {
            "importance": round(min(1.0, 0.40 + 0.15 * min(4, story.observed_source_count)), 4),
            "utility": 0.50,
            "impact": 0.50,
            "novelty": 0.70,
            "urgency": 0.50,
            "credibility": round(base_cred, 4),
        }
        score = sum(comp.values()) / len(comp)
        return round(score, 4), comp

    @classmethod
    def compute_audience_fit(cls, story: Story) -> tuple[float, dict[str, float]]:
        """Compute AudienceFit based on topic editorial weight and history."""
        # 1. Topic weight (default 1.0 -> 0.7)
        topic_weight = 0.50
        if story.primary_topic:
            w = float(story.primary_topic.weight or 1.0)
            topic_weight = round(min(1.0, w / 2.0), 4)

        # 2. Historical audience preference for topic (0..1)
        topic_pref = 0.50
        try:
            from apps.ops.models import AudiencePreference

            if story.primary_topic:
                row = AudiencePreference.objects.filter(feature="topic").first()
                if (
                    row
                    and isinstance(row.context, dict)
                    and row.context.get("topic") == story.primary_topic.slug
                ):
                    topic_pref = round(max(0.0, min(1.0, float(row.value))), 4)
        except Exception:
            topic_pref = 0.50

        # 3. Content type preference
        content_type_pref = 0.60

        # 4. Language fit
        lang = (story.language or "").lower()
        language_fit = 1.0 if lang in ("fa", "en") else 0.50

        comp = {
            "topic_weight": topic_weight,
            "historical_preference": topic_pref,
            "content_type_preference": content_type_pref,
            "language_fit": language_fit,
        }
        score = (
            0.40 * topic_weight + 0.30 * topic_pref + 0.15 * content_type_pref + 0.15 * language_fit
        )
        return round(score, 4), comp

    @classmethod
    def compute_momentum(cls, story: Story) -> tuple[float, dict[str, float]]:
        """Compute Momentum: relative performance + velocities + saturating multi-source spread."""
        memberships = list(
            story.memberships.filter(is_current=True).select_related(
                "source_item", "source_item__source"
            )
        )
        rel_perfs = []
        vel_perfs = []
        acc_perfs = []

        for m in memberships:
            m_metrics = compute_item_relative_metrics(m.source_item, target_age_minutes=60)
            if m_metrics.get("relative_performance") is not None:
                rel_perfs.append(m_metrics["relative_performance"])
            if m_metrics.get("engagement_velocity") is not None:
                # Scale velocity (e.g. 500/hr -> 1.0)
                v = max(0.0, min(1.0, float(m_metrics["engagement_velocity"]) / 500.0))
                vel_perfs.append(v)
            if m_metrics.get("engagement_acceleration") is not None:
                a = max(0.0, min(1.0, float(m_metrics["engagement_acceleration"]) / 100.0))
                acc_perfs.append(a)

        rel_score = sum(rel_perfs) / len(rel_perfs) if rel_perfs else 0.50
        vel_score = max(vel_perfs) if vel_perfs else 0.20
        acc_score = max(acc_perfs) if acc_perfs else 0.20

        # Multi-source confirmation: saturating function (1 - exp(-0.7 * count))
        # 1 source  -> 0.50
        # 2 sources -> 0.75
        # 3 sources -> 0.88
        # 100 copies -> 0.999 (does NOT give 100x confirmation!)
        indep_count = max(0, story.independent_source_count)
        multi_source_spread = round(1.0 - math.exp(-0.7 * indep_count), 4)

        # Cross-source consistency penalty
        conflict = getattr(story, "conflict", None)
        consistency = 0.20 if (conflict and conflict.has_conflict) else 0.85

        comp = {
            "relative_performance": round(rel_score, 4),
            "engagement_velocity": round(vel_score, 4),
            "engagement_acceleration": round(acc_score, 4),
            "multi_source_spread": multi_source_spread,
            "cross_source_consistency": consistency,
        }
        score = (
            0.40 * rel_score
            + 0.20 * vel_score
            + 0.10 * acc_score
            + 0.20 * multi_source_spread
            + 0.10 * consistency
        )
        return round(score, 4), comp

    @classmethod
    def score_story(cls, story: Story) -> dict[str, Any]:
        """Calculate complete score families, evaluate gate, and persist ScoreRecord."""
        news_val, news_breakdown = cls.compute_news_value(story)
        aud_fit, aud_breakdown = cls.compute_audience_fit(story)
        momentum, mom_breakdown = cls.compute_momentum(story)
        freshness = compute_freshness(story)

        weights = get_ranking_weights()
        raw_final = (
            weights["weight_news_value"] * news_val
            + weights["weight_audience_fit"] * aud_fit
            + weights["weight_momentum"] * momentum
            + weights["weight_freshness"] * freshness
        )
        final_score = round(max(0.0, min(1.0, raw_final)), 4)

        # Evaluate credibility gate
        cred_val = news_breakdown.get("credibility", 0.50)
        gate_passed, gate_reason = CredibilityGate.evaluate(story, cred_val)

        # Build schema-compliant breakdown (all numeric leaves must be finite 0..1)
        breakdown = {
            "news_value": news_breakdown,
            "audience_fit": aud_breakdown,
            "momentum": mom_breakdown,
        }

        # Persist ScoreRecord
        record = ScoreRecord.objects.create(
            story=story,
            algorithm_version=RANKING_ALGORITHM_VERSION,
            news_value=to_score_decimal(news_val),
            audience_fit=to_score_decimal(aud_fit),
            momentum=to_score_decimal(momentum),
            final_score=to_score_decimal(final_score),
            breakdown=breakdown,
        )

        return {
            "score_record_id": record.pk,
            "story_id": story.pk,
            "final_score": float(record.final_score),
            "news_value": float(record.news_value),
            "audience_fit": float(record.audience_fit),
            "momentum": float(record.momentum),
            "freshness": freshness,
            "gate_passed": gate_passed,
            "gate_reason": gate_reason,
            "breakdown": breakdown,
            "algorithm_version": RANKING_ALGORITHM_VERSION,
        }
