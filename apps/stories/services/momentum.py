"""Story-level momentum and propagation engine.

Calculates multi-source velocity, EWMA acceleration, independent propagation,
cross-source diversity, and audit-friendly composite momentum scores (0..1).
"""

from __future__ import annotations

import hashlib
import math
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.core.choices import LifecycleState, TrendState
from apps.ops.models import DynamicSetting
from apps.stories.models import Story, StoryMomentumSnapshot, StoryObservationState
from apps.stories.services.momentum_metrics import MomentumMetrics
from apps.stories.services.series import ItemMetricSeriesService

MOMENTUM_ALGORITHM_VERSION = "momentum-v1"

DEFAULT_MOMENTUM_WEIGHTS = {
    "normalized_velocity": 0.25,
    "acceleration": 0.20,
    "propagation_breadth": 0.20,
    "independent_arrival": 0.15,
    "growth_persistence": 0.10,
    "freshness": 0.10,
}


def get_momentum_weights() -> dict[str, float]:
    """Load configurable momentum component weights from DynamicSetting or defaults."""
    weights = dict(DEFAULT_MOMENTUM_WEIGHTS)
    try:
        row = DynamicSetting.objects.filter(key="momentum_weights").first()
        if row and isinstance(row.value, dict):
            for k in weights:
                if k in row.value and isinstance(row.value[k], (int, float)):
                    weights[k] = float(row.value[k])
    except Exception:
        pass
    tot = sum(weights.values()) or 1.0
    return {k: round(v / tot, 4) for k, v in weights.items()}


class StoryMomentumService:
    """Service to compute, aggregate and record real-time momentum for Stories."""

    @classmethod
    def compute_metrics(
        cls,
        story: Story,
        *,
        now: Any | None = None,
    ) -> MomentumMetrics:
        """Calculate complete momentum and propagation metrics for a Story."""
        current_time = now or timezone.now()
        memberships = list(
            story.memberships.filter(is_current=True).select_related("source_item__source")
        )
        if not memberships:
            return MomentumMetrics(
                observed_sources_count=story.observed_source_count,
                independent_sources_count=story.independent_source_count,
            )

        items = [m.source_item for m in memberships]

        # 1. Member series analysis
        member_series: list[dict[str, Any]] = []
        for item in items:
            s_metrics = ItemMetricSeriesService.compute_item_series_metrics(item)
            member_series.append(s_metrics)

        # Aggregate velocities
        def _max_or_none(key: str) -> float | None:
            vals = [s[key] for s in member_series if s.get(key) is not None]
            return max(vals) if vals else None

        v_view = _max_or_none("view_velocity")
        v_fwd = _max_or_none("forward_velocity")
        v_share = _max_or_none("share_velocity")
        v_react = _max_or_none("reaction_velocity")
        v_reply = _max_or_none("reply_velocity")
        v_eng = _max_or_none("engagement_velocity")
        norm_v = _max_or_none("normalized_velocity") or 0.50
        ewma_v = _max_or_none("ewma_velocity") or norm_v

        # Acceleration across members
        acc_vals = [s["acceleration"] for s in member_series if s.get("acceleration") is not None]
        acc = max(acc_vals) if acc_vals else 0.0
        # Normalize acceleration into 0..1 (raw acceleration centered around 0 in range -1..+1)
        norm_acc = round(max(0.0, min(1.0, 0.5 + (acc * 0.5))), 4)

        # 2. Source Arrival Rates (5m, 15m, 30m)
        t5 = current_time - timedelta(minutes=5)
        t15 = current_time - timedelta(minutes=15)
        t30 = current_time - timedelta(minutes=30)

        arr_5m = sum(1 for it in items if (it.published_at or it.collected_at) >= t5)
        arr_15m = sum(1 for it in items if (it.published_at or it.collected_at) >= t15)
        arr_30m = sum(1 for it in items if (it.published_at or it.collected_at) >= t30)

        indep_15m = sum(
            1
            for m in memberships
            if m.independence == "independent"
            and (m.source_item.published_at or m.source_item.collected_at) >= t15
        )
        indep_30m = sum(
            1
            for m in memberships
            if m.independence == "independent"
            and (m.source_item.published_at or m.source_item.collected_at) >= t30
        )

        # 3. Source & Platform Diversity
        distinct_sources = len({it.source_id for it in items})
        distinct_platforms = len({it.source.platform for it in items})
        source_div = round(min(1.0, distinct_sources / max(1, len(items))), 4)
        plat_div = round(min(1.0, distinct_platforms / 4.0), 4)

        # 4. Propagation Breadth (saturating independent count curve)
        indep_count = story.independent_source_count
        prop_breadth = round(1.0 - math.exp(-0.7 * max(0, indep_count)), 4)

        # 5. Propagation Depth
        depth = None
        for it in items:
            origin = (it.raw_payload or {}).get("forward_origin")
            if origin:
                depth = 1  # 1 hop observed

        # 6. Freshness & Trend Age
        pub_time = story.first_published_at or story.created_at
        trend_age_sec = max(0, int((current_time - pub_time).total_seconds()))
        age_hours = trend_age_sec / 3600.0
        freshness = round(0.5 ** (age_hours / 24.0), 4)

        # 7. Growth Consistency & Persistence
        total_snaps = sum(s["sample_count"] for s in member_series)
        consistency = 1.0 if ewma_v >= 0.50 else max(0.0, ewma_v * 2.0)
        persistence = min(10, total_snaps)

        # 8. Burst factor (recent arrivals vs overall average rate)
        burst = 0.0
        if arr_15m > 0 and len(items) > 0:
            burst = round(min(1.0, (arr_15m * 4) / max(1, len(items))), 4)

        # 9. Time to double (seconds)
        time_to_double = None
        if v_view and v_view > 10.0:
            latest_views = sum(
                (it.snapshots.order_by("-captured_at").first().views or 0)
                for it in items
                if it.snapshots.exists()
            )
            if latest_views > 50:
                time_to_double = round((latest_views / v_view) * 3600.0, 1)

        # 10. Momentum Confidence
        # Higher confidence when multiple snapshots, multiple independent sources, and active baseline
        snap_factor = min(1.0, total_snaps / 6.0)
        indep_factor = min(1.0, indep_count / 3.0)
        confidence = round(max(0.1, 0.40 * snap_factor + 0.40 * indep_factor + 0.20 * freshness), 4)

        # 11. Composite Normalized Momentum (0..1)
        weights = get_momentum_weights()
        norm_indep_arrival = round(min(1.0, indep_15m / 3.0), 4)
        norm_persistence = round(min(1.0, persistence / 6.0), 4)

        raw_momentum = (
            weights["normalized_velocity"] * (norm_v or 0.5)
            + weights["acceleration"] * norm_acc
            + weights["propagation_breadth"] * prop_breadth
            + weights["independent_arrival"] * norm_indep_arrival
            + weights["growth_persistence"] * norm_persistence
            + weights["freshness"] * freshness
        )
        final_momentum = round(max(0.0, min(1.0, raw_momentum)), 4)

        breakdown = {
            "normalized_velocity": round(norm_v or 0.5, 4),
            "acceleration_component": norm_acc,
            "propagation_breadth": prop_breadth,
            "independent_arrival_component": norm_indep_arrival,
            "growth_persistence": norm_persistence,
            "freshness": freshness,
            "raw_acceleration": acc,
            "burst_factor": burst,
            "confidence": confidence,
        }

        return MomentumMetrics(
            view_velocity=v_view,
            forward_velocity=v_fwd,
            share_velocity=v_share,
            reaction_velocity=v_react,
            reply_velocity=v_reply,
            engagement_velocity=v_eng,
            normalized_velocity=norm_v,
            ewma_velocity=ewma_v,
            acceleration=acc,
            source_arrival_5m=arr_5m,
            source_arrival_15m=arr_15m,
            source_arrival_30m=arr_30m,
            independent_arrival_15m=indep_15m,
            independent_arrival_30m=indep_30m,
            observed_sources_count=len(items),
            independent_sources_count=indep_count,
            source_diversity=source_div,
            platform_diversity=plat_div,
            propagation_breadth=prop_breadth,
            propagation_depth=depth,
            freshness_score=freshness,
            trend_age_seconds=trend_age_sec,
            growth_consistency=round(consistency, 4),
            growth_persistence=persistence,
            burst_factor=burst,
            time_to_double_seconds=time_to_double,
            momentum_confidence=confidence,
            normalized_momentum=final_momentum,
            component_breakdown=breakdown,
        )

    @classmethod
    def recompute_and_persist(
        cls,
        story_id: int,
        *,
        lifecycle_state: str | None = None,
        trend_state: str | None = None,
        transition_reason: str = "",
        transition_metrics: dict | None = None,
        now: Any | None = None,
    ) -> StoryMomentumSnapshot | None:
        """Compute momentum, record snapshot and update observation state."""
        current_time = now or timezone.now()
        try:
            story = Story.objects.get(pk=story_id)
        except Story.DoesNotExist:
            return None

        metrics = cls.compute_metrics(story, now=current_time)

        # Idempotency hash: story + metric bucket + minute timestamp
        idemp_raw = (
            f"{story.pk}:{metrics.momentum_score_decimal}:{current_time.strftime('%Y%m%d%H%M')}"
        )
        idemp_hash = hashlib.sha256(idemp_raw.encode()).hexdigest()

        with transaction.atomic():
            obs, _ = StoryObservationState.objects.select_for_update().get_or_create(
                story=story,
                defaults={
                    "lifecycle_state": LifecycleState.DISCOVERED,
                    "trend_state": TrendState.NORMAL,
                },
            )

            prev_state = obs.lifecycle_state
            new_lifecycle = lifecycle_state or obs.lifecycle_state
            new_trend = trend_state or obs.trend_state

            snapshot, _ = StoryMomentumSnapshot.objects.update_or_create(
                story=story,
                idempotency_hash=idemp_hash,
                defaults={
                    "captured_at": current_time,
                    "lifecycle_state": new_lifecycle,
                    "trend_state": new_trend,
                    "previous_lifecycle_state": prev_state,
                    "transition_reason": transition_reason,
                    "view_velocity": metrics.view_velocity,
                    "forward_velocity": metrics.forward_velocity,
                    "share_velocity": metrics.share_velocity,
                    "reaction_velocity": metrics.reaction_velocity,
                    "reply_velocity": metrics.reply_velocity,
                    "engagement_velocity": metrics.engagement_velocity,
                    "normalized_velocity": metrics.normalized_velocity,
                    "ewma_velocity": metrics.ewma_velocity,
                    "acceleration": metrics.acceleration,
                    "source_arrival_5m": metrics.source_arrival_5m,
                    "source_arrival_15m": metrics.source_arrival_15m,
                    "source_arrival_30m": metrics.source_arrival_30m,
                    "independent_arrival_15m": metrics.independent_arrival_15m,
                    "independent_arrival_30m": metrics.independent_arrival_30m,
                    "observed_sources_count": metrics.observed_sources_count,
                    "independent_sources_count": metrics.independent_sources_count,
                    "source_diversity": metrics.source_diversity,
                    "platform_diversity": metrics.platform_diversity,
                    "propagation_breadth": metrics.propagation_breadth,
                    "propagation_depth": metrics.propagation_depth,
                    "freshness_score": metrics.freshness_score,
                    "trend_age_seconds": metrics.trend_age_seconds,
                    "growth_consistency": metrics.growth_consistency,
                    "growth_persistence": metrics.growth_persistence,
                    "burst_factor": metrics.burst_factor,
                    "time_to_double_seconds": metrics.time_to_double_seconds,
                    "momentum_score": metrics.momentum_score_decimal,
                    "confidence": metrics.confidence_decimal,
                    "component_breakdown": metrics.component_breakdown,
                    "transition_metrics": transition_metrics or {},
                    "algorithm_version": MOMENTUM_ALGORITHM_VERSION,
                },
            )

            # Update observation state
            obs.sample_count += 1
            obs.last_observed_at = current_time
            obs.latest_momentum = metrics.momentum_score_decimal
            obs.latest_confidence = metrics.confidence_decimal
            obs.lifecycle_state = new_lifecycle
            obs.trend_state = new_trend
            obs.save()

            return snapshot
