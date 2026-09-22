"""Item metric time-series processing: velocity, EWMA smoothing, baseline normalization, acceleration."""

from __future__ import annotations

from typing import Any

from django.conf import settings

from apps.news.models import EngagementSnapshot, SourceItem
from apps.ops.models import DynamicSetting
from apps.ranking.services.baselines import STANDARD_AGE_BUCKETS, SourceBaselineService

DEFAULT_EWMA_ALPHA = 0.30
METRIC_PERCENTILE_WEIGHTS = {
    "views": 0.40,
    "forwards": 0.30,
    "reactions": 0.15,
    "replies": 0.10,
    "shares": 0.05,
}


def get_ewma_alpha() -> float:
    try:
        row = DynamicSetting.objects.filter(key="MOMENTUM_EWMA_ALPHA").first()
        if row and isinstance(row.value, (int, float)):
            return max(0.01, min(1.0, float(row.value)))
    except Exception:
        pass
    return getattr(settings, "MOMENTUM_EWMA_ALPHA", DEFAULT_EWMA_ALPHA)


def _nearest_age_bucket(post_age_minutes: int | float) -> int:
    return min(STANDARD_AGE_BUCKETS, key=lambda b: abs(b - post_age_minutes))


class ItemMetricSeriesService:
    """Computes time-series velocities, accelerations and baseline-relative scores for SourceItems."""

    @classmethod
    def compute_item_series_metrics(
        cls,
        item: SourceItem,
        *,
        snapshots: list[EngagementSnapshot] | None = None,
    ) -> dict[str, Any]:
        """Compute comprehensive multi-point velocity, EWMA and acceleration for an item."""
        snaps = (
            snapshots
            if snapshots is not None
            else list(item.snapshots.all().order_by("captured_at"))
        )
        if not snaps:
            return {
                "sample_count": 0,
                "view_velocity": None,
                "forward_velocity": None,
                "share_velocity": None,
                "reaction_velocity": None,
                "reply_velocity": None,
                "engagement_velocity": None,
                "normalized_velocity": None,
                "ewma_velocity": None,
                "acceleration": None,
                "latest_percentiles": {},
            }

        latest_snap = snaps[-1]
        post_age_min = max(1, (latest_snap.post_age_seconds or 60) // 60)
        age_bucket = _nearest_age_bucket(post_age_min)

        # Baseline percentiles for latest point
        latest_pcts: dict[str, float | None] = {}
        for metric_name in ("views", "forwards", "shares", "reactions", "replies"):
            val = getattr(latest_snap, metric_name, None)
            if val is None:
                latest_pcts[metric_name] = None
                continue
            baseline, _level, _conf = SourceBaselineService.find_baseline(
                source=item.source,
                platform=item.source.platform,
                topic_id=item.topic_id,
                subtopic_id=item.subtopic_id,
                content_type=item.content_type,
                age_bucket_minutes=age_bucket,
                metric=metric_name,
            )
            latest_pcts[metric_name] = SourceBaselineService.estimate_percentile(val, baseline)

        # If only 1 snapshot, velocity cannot be directly measured
        if len(snaps) < 2:
            norm_vel = cls._blend_percentiles(latest_pcts)
            return {
                "sample_count": 1,
                "view_velocity": None,
                "forward_velocity": None,
                "share_velocity": None,
                "reaction_velocity": None,
                "reply_velocity": None,
                "engagement_velocity": None,
                "normalized_velocity": norm_vel,
                "ewma_velocity": norm_vel,
                "acceleration": None,
                "latest_percentiles": latest_pcts,
            }

        # Compute interval velocities across all snapshot pairs
        alpha = get_ewma_alpha()
        velocities_views: list[float] = []
        velocities_fwds: list[float] = []
        velocities_shares: list[float] = []
        velocities_reactions: list[float] = []
        velocities_replies: list[float] = []
        velocities_eng: list[float] = []
        norm_velocities: list[float] = []

        for i in range(1, len(snaps)):
            s_prev, s_curr = snaps[i - 1], snaps[i]
            t_diff = (s_curr.captured_at - s_prev.captured_at).total_seconds()
            delta_hours = max(0.001, t_diff / 3600.0)

            # Views delta
            if s_curr.views is not None and s_prev.views is not None:
                v_diff = s_curr.views - s_prev.views
                # Anomaly check: counter reset / correction
                v_diff = max(0, v_diff)
                velocities_views.append(v_diff / delta_hours)

            # Forwards delta
            if s_curr.forwards is not None and s_prev.forwards is not None:
                f_diff = max(0, s_curr.forwards - s_prev.forwards)
                velocities_fwds.append(f_diff / delta_hours)

            # Shares delta
            if s_curr.shares is not None and s_prev.shares is not None:
                sh_diff = max(0, s_curr.shares - s_prev.shares)
                velocities_shares.append(sh_diff / delta_hours)

            # Reactions delta
            if s_curr.reactions is not None and s_prev.reactions is not None:
                r_diff = max(0, s_curr.reactions - s_prev.reactions)
                velocities_reactions.append(r_diff / delta_hours)

            # Replies delta
            if s_curr.replies is not None and s_prev.replies is not None:
                rp_diff = max(0, s_curr.replies - s_prev.replies)
                velocities_replies.append(rp_diff / delta_hours)

            # Engagement velocity (sum of interactive signals)
            e_prev = sum(
                v
                for v in (s_prev.forwards, s_prev.shares, s_prev.reactions, s_prev.replies)
                if v is not None
            )
            e_curr = sum(
                v
                for v in (s_curr.forwards, s_curr.shares, s_curr.reactions, s_curr.replies)
                if v is not None
            )
            if e_curr >= e_prev:
                velocities_eng.append((e_curr - e_prev) / delta_hours)

            # Normalized rate at step i
            step_age_min = max(1, (s_curr.post_age_seconds or 60) // 60)
            step_bucket = _nearest_age_bucket(step_age_min)
            step_pcts: dict[str, float | None] = {}
            for metric_name in ("views", "forwards", "shares", "reactions", "replies"):
                val = getattr(s_curr, metric_name, None)
                if val is not None:
                    baseline, _, _ = SourceBaselineService.find_baseline(
                        source=item.source,
                        platform=item.source.platform,
                        topic_id=item.topic_id,
                        subtopic_id=item.subtopic_id,
                        content_type=item.content_type,
                        age_bucket_minutes=step_bucket,
                        metric=metric_name,
                    )
                    step_pcts[metric_name] = SourceBaselineService.estimate_percentile(
                        val, baseline
                    )
            step_norm = cls._blend_percentiles(step_pcts)
            if step_norm is not None:
                norm_velocities.append(step_norm)

        # EWMA of normalized velocity
        ewma = None
        if norm_velocities:
            ewma = norm_velocities[0]
            for v in norm_velocities[1:]:
                ewma = alpha * v + (1.0 - alpha) * ewma

        # Acceleration: rate of change of normalized velocity
        acceleration = None
        if len(norm_velocities) >= 2:
            t_span = max(
                0.01, (snaps[-1].captured_at - snaps[-2].captured_at).total_seconds() / 3600.0
            )
            raw_acc = (norm_velocities[-1] - norm_velocities[-2]) / t_span
            # Clamp acceleration into a reasonable bounded range
            acceleration = round(max(-2.0, min(2.0, raw_acc)), 4)

        current_norm = (
            norm_velocities[-1] if norm_velocities else cls._blend_percentiles(latest_pcts)
        )

        return {
            "sample_count": len(snaps),
            "view_velocity": round(velocities_views[-1], 2) if velocities_views else None,
            "forward_velocity": round(velocities_fwds[-1], 2) if velocities_fwds else None,
            "share_velocity": round(velocities_shares[-1], 2) if velocities_shares else None,
            "reaction_velocity": round(velocities_reactions[-1], 2)
            if velocities_reactions
            else None,
            "reply_velocity": round(velocities_replies[-1], 2) if velocities_replies else None,
            "engagement_velocity": round(velocities_eng[-1], 2) if velocities_eng else None,
            "normalized_velocity": round(current_norm, 4) if current_norm is not None else None,
            "ewma_velocity": round(ewma, 4) if ewma is not None else None,
            "acceleration": acceleration,
            "latest_percentiles": latest_pcts,
        }

    @staticmethod
    def _blend_percentiles(pcts: dict[str, float | None]) -> float | None:
        valid_items = [
            (pcts[m], METRIC_PERCENTILE_WEIGHTS[m])
            for m in METRIC_PERCENTILE_WEIGHTS
            if pcts.get(m) is not None
        ]
        if not valid_items:
            return None
        total_w = sum(w for _, w in valid_items)
        if total_w <= 0:
            return None
        return sum(p * w for p, w in valid_items) / total_w
