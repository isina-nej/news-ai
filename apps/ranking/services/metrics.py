"""Engagement metric processing: velocity, acceleration, relative percentiles.

Eliminates source-size bias by comparing items to their own source baseline.
Unavailable metrics remain NULL/None — never defaulted to 0.0.
"""

from __future__ import annotations

from typing import Any

from apps.news.models import EngagementSnapshot, SourceItem
from apps.ranking.services.baselines import SourceBaselineService

# Weighting across relative percentiles for composite relative_performance
RELATIVE_WEIGHTS = {
    "views": 0.40,
    "forwards": 0.30,
    "shares": 0.15,
    "reactions": 0.15,
}


def find_nearest_snapshot(item: SourceItem, target_age_minutes: int) -> EngagementSnapshot | None:
    """Find snapshot closest to target post age in minutes."""
    snapshots = list(item.snapshots.all().order_by("captured_at"))
    if not snapshots:
        return None
    target_seconds = target_age_minutes * 60
    return min(
        snapshots,
        key=lambda s: abs((s.post_age_seconds or 0) - target_seconds),
    )


def compute_velocity_and_acceleration(
    snapshots: list[EngagementSnapshot],
) -> dict[str, float | None]:
    """Compute rate of change across consecutive snapshots.

    Velocity = delta count / delta hours.
    Acceleration = delta velocity / delta hours.
    """
    if len(snapshots) < 2:
        return {
            "view_velocity": None,
            "forward_velocity": None,
            "share_velocity": None,
            "engagement_velocity": None,
            "engagement_acceleration": None,
        }

    s1, s2 = snapshots[-2], snapshots[-1]
    age1 = s1.post_age_seconds or 0
    age2 = s2.post_age_seconds or 0
    delta_hours = max(0.01, (age2 - age1) / 3600.0)

    # Views velocity
    v_views = (
        (s2.views - s1.views) / delta_hours
        if s1.views is not None and s2.views is not None
        else None
    )
    v_fwd = (
        (s2.forwards - s1.forwards) / delta_hours
        if s1.forwards is not None and s2.forwards is not None
        else None
    )
    v_share = (
        (s2.shares - s1.shares) / delta_hours
        if s1.shares is not None and s2.shares is not None
        else None
    )

    # Total engagement velocity
    e1 = sum(v for v in [s1.forwards, s1.shares, s1.reactions, s1.replies] if v is not None)
    e2 = sum(v for v in [s2.forwards, s2.shares, s2.reactions, s2.replies] if v is not None)
    v_eng = (e2 - e1) / delta_hours if (e1 or e2) else None

    # Acceleration if 3+ snapshots
    acc = None
    if len(snapshots) >= 3:
        s0 = snapshots[-3]
        d_h_prev = max(0.01, (age1 - (s0.post_age_seconds or 0)) / 3600.0)
        e0 = sum(v for v in [s0.forwards, s0.shares, s0.reactions, s0.replies] if v is not None)
        v_eng_prev = (e1 - e0) / d_h_prev if (e0 or e1) else None
        if v_eng is not None and v_eng_prev is not None:
            acc = round((v_eng - v_eng_prev) / delta_hours, 4)

    return {
        "view_velocity": round(v_views, 2) if v_views is not None else None,
        "forward_velocity": round(v_fwd, 2) if v_fwd is not None else None,
        "share_velocity": round(v_share, 2) if v_share is not None else None,
        "engagement_velocity": round(v_eng, 2) if v_eng is not None else None,
        "engagement_acceleration": acc,
    }


def compute_item_relative_metrics(
    item: SourceItem,
    *,
    target_age_minutes: int = 60,
) -> dict[str, Any]:
    """Compute relative percentiles and velocities for a single SourceItem.

    Outputs 0..1 relative percentiles based on the source's own baseline.
    A small source getting 50k views when expected is 5k scores ~0.99,
    while a large source getting 1M views when expected is 900k scores ~0.55.
    """
    snapshot = find_nearest_snapshot(item, target_age_minutes)
    all_snaps = list(item.snapshots.all().order_by("captured_at"))
    velocities = compute_velocity_and_acceleration(all_snaps)

    metrics_out: dict[str, float | None] = {}
    percentiles_for_blend: list[tuple[float, float]] = []

    if snapshot is not None:
        for metric_name in ("views", "forwards", "shares", "reactions", "replies"):
            val = getattr(snapshot, metric_name, None)
            if val is None:
                metrics_out[f"{metric_name}_percentile"] = None
                continue

            baseline, _level, _conf = SourceBaselineService.find_baseline(
                source=item.source,
                platform=item.source.platform,
                topic_id=item.topic_id,
                subtopic_id=item.subtopic_id,
                content_type=item.content_type,
                age_bucket_minutes=target_age_minutes,
                metric=metric_name,
            )
            pct = SourceBaselineService.estimate_percentile(val, baseline)
            metrics_out[f"{metric_name}_percentile"] = pct

            if pct is not None and metric_name in RELATIVE_WEIGHTS:
                percentiles_for_blend.append((pct, RELATIVE_WEIGHTS[metric_name]))

    # Relative performance: normalized weighted average of available percentiles
    if percentiles_for_blend:
        tot_w = sum(w for _, w in percentiles_for_blend)
        rel_perf = sum(p * w for p, w in percentiles_for_blend) / tot_w
        metrics_out["relative_performance"] = round(rel_perf, 4)
    else:
        metrics_out["relative_performance"] = None

    metrics_out.update(velocities)
    return metrics_out
