"""PublishPriorityService: separates publishing decision from intrinsic newsworthiness."""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

from django.utils import timezone
from rapidfuzz import fuzz

from apps.core.choices import TrendState
from apps.publishing.models import Publication, PublicationStatus
from apps.ranking.services.newsworthiness import NewsworthinessService
from apps.ranking.services.novelty import ChannelNoveltyService, ChannelNoveltyType
from apps.ranking.services.scoring import CredibilityGate, StoryScoringService, compute_freshness
from apps.stories.models import Story
from apps.stories.services.momentum import StoryMomentumService

PRIORITY_ALGORITHM_VERSION = "priority-v1"
SATURATION_WINDOW_HOURS = 4
REPETITION_WINDOW_HOURS = 12


class PublishPriorityService:
    """Calculates actionable publishing priority by combining intrinsic value, dynamics and penalties."""

    @classmethod
    def evaluate_priority(
        cls,
        story: Story,
        *,
        now: Any | None = None,
    ) -> dict[str, Any]:
        """Compute full priority score, gate status, component breakdowns, and penalties."""
        current_time = now or timezone.now()

        # 1. Intrinsic Newsworthiness
        news_val, news_breakdown = NewsworthinessService.evaluate(story)

        # 2. Audience Fit
        aud_fit, aud_breakdown = StoryScoringService.compute_audience_fit(story)

        # 3. Real-time Momentum and Acceleration
        momentum_metrics = StoryMomentumService.compute_metrics(story, now=current_time)
        mom_score = float(momentum_metrics.normalized_momentum)
        acc_raw = float(momentum_metrics.acceleration or 0.0)
        norm_acc = max(0.0, min(1.0, 0.5 + (acc_raw * 0.5)))

        # 4. Freshness
        freshness = compute_freshness(story)

        # 5. Independent Confirmation (saturating curve)
        indep_count = max(0, story.independent_source_count)
        indep_confirm = round(1.0 - math.exp(-0.7 * indep_count), 4)

        # 6. Channel Novelty
        novelty_type, novelty_detail = ChannelNoveltyService.evaluate_novelty(story)
        novelty_score = (
            1.0
            if novelty_type == ChannelNoveltyType.NEW_STORY
            else (
                0.90
                if novelty_type
                in (ChannelNoveltyType.MATERIAL_UPDATE, ChannelNoveltyType.CORRECTION)
                else 0.10
            )
        )

        # Credibility Gate check
        cred_val = news_breakdown.get("credibility", 0.50)
        gate_passed, gate_reason = CredibilityGate.evaluate(story, cred_val)

        # Raw positive priority combination
        raw_priority = (
            0.35 * news_val
            + 0.20 * mom_score
            + 0.10 * norm_acc
            + 0.15 * aud_fit
            + 0.05 * freshness
            + 0.10 * indep_confirm
            + 0.05 * novelty_score
        )

        # Breaking News Override: high urgency or breaking trend mitigates saturation/cooldown
        is_breaking = (
            news_breakdown.get("urgency", 0.5) >= 0.85
            or getattr(getattr(story, "observation_state", None), "trend_state", "")
            == TrendState.BREAKING
        )

        # 7. Penalties
        # 7a. Topic Saturation
        saturation_penalty = 0.0
        if story.primary_topic and not is_breaking:
            recent_same_topic = (
                Publication.objects.filter(
                    story__primary_topic=story.primary_topic,
                    status__in=[PublicationStatus.PUBLISHED, PublicationStatus.SCHEDULED],
                    created_at__gte=current_time - timedelta(hours=SATURATION_WINDOW_HOURS),
                )
                .exclude(story=story)
                .count()
            )
            if recent_same_topic >= 2:
                saturation_penalty = min(0.30, 0.10 * (recent_same_topic - 1))

        # 7b. Recent Lexical Repetition
        repetition_penalty = 0.0
        recent_pubs = (
            Publication.objects.filter(
                status__in=[PublicationStatus.PUBLISHED, PublicationStatus.SCHEDULED],
                created_at__gte=current_time - timedelta(hours=REPETITION_WINDOW_HOURS),
            )
            .exclude(story=story)
            .select_related("story")[:30]
        )

        for pub in recent_pubs:
            other_title = (pub.headline or getattr(pub.story, "canonical_title", "") or "").strip()
            if other_title and fuzz.token_sort_ratio(story.canonical_title, other_title) >= 85:
                repetition_penalty = 0.25
                break

        # 7c. Credibility Risk Penalty
        credibility_risk_penalty = 0.0
        if cred_val < 0.40:
            credibility_risk_penalty = round(0.40 - cred_val, 4)

        # 7d. Boost for corrections or major breaking
        boost = 0.10 if novelty_type == ChannelNoveltyType.CORRECTION else 0.0

        final_priority = max(
            0.0,
            min(
                1.0,
                raw_priority
                + boost
                - saturation_penalty
                - repetition_penalty
                - credibility_risk_penalty,
            ),
        )

        # Schema-compliant breakdown for ScoreRecord: required groups news_value, audience_fit, momentum
        mom_breakdown = {
            "relative_performance": round(mom_score, 4),
            "engagement_velocity": round(norm_acc, 4),
            "multi_source_spread": indep_confirm,
            "cross_source_consistency": 0.85,
            "freshness": freshness,
        }
        score_breakdown = {
            "news_value": {
                k: round(float(v), 4)
                for k, v in news_breakdown.items()
                if isinstance(v, (int, float)) and 0 <= v <= 1
            },
            "audience_fit": {
                k: round(float(v), 4)
                for k, v in aud_breakdown.items()
                if isinstance(v, (int, float)) and 0 <= v <= 1
            },
            "momentum": mom_breakdown,
        }

        penalties = {
            "saturation_penalty": round(saturation_penalty, 4),
            "repetition_penalty": round(repetition_penalty, 4),
            "credibility_risk_penalty": round(credibility_risk_penalty, 4),
            "boost": boost,
        }

        return {
            "story_id": story.pk,
            "newsworthiness_score": round(news_val, 4),
            "audience_fit_score": round(aud_fit, 4),
            "momentum_score": round(mom_score, 4),
            "acceleration": round(norm_acc, 4),
            "freshness_score": freshness,
            "raw_priority_score": round(raw_priority, 4),
            "publish_priority_score": round(final_priority, 4),
            "gate_passed": gate_passed,
            "gate_reason": gate_reason,
            "novelty_type": novelty_type,
            "novelty_detail": novelty_detail,
            "is_breaking": is_breaking,
            "breakdown": score_breakdown,
            "penalties": penalties,
            "algorithm_version": PRIORITY_ALGORITHM_VERSION,
        }
