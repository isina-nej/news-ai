"""Own-channel normalized reward from relative publication performance."""

from __future__ import annotations

REWARD_ALGORITHM_VERSION = "reward-v1"
REWARD_WEIGHTS = {
    "forwards": 0.50,
    "views": 0.25,
    "reactions": 0.15,
    "replies": 0.10,
}


def _percentile_or_none(value, percentiles: dict | None) -> float | None:
    if value is None or not percentiles:
        return None
    points = [
        (percentiles.get("p25", 0), 0.25),
        (percentiles.get("p50", 0), 0.50),
        (percentiles.get("p75", 0), 0.75),
        (percentiles.get("p90", 0), 0.90),
        (percentiles.get("p95", 0), 0.95),
        (percentiles.get("p99", 0), 0.99),
    ]
    for threshold, pct in points:
        if float(value) <= float(threshold or 0):
            return pct
    return 0.9999


def normalized_reward(
    snapshot, *, baselines: dict[str, dict] | None = None
) -> dict[str, float | str | None]:
    """Compare an own-channel snapshot to similar historical posts.

    Missing metrics redistribute their weight across available metrics.
    """
    baselines = baselines or {}
    scored: dict[str, float] = {}
    for metric, weight in REWARD_WEIGHTS.items():
        value = getattr(snapshot, metric, None)
        pct = _percentile_or_none(value, baselines.get(metric))
        if pct is not None:
            scored[metric] = pct * weight

    if not scored:
        return {"reward": None, "algorithm_version": REWARD_ALGORITHM_VERSION}
    total_weight = sum(REWARD_WEIGHTS[m] for m in scored)
    reward = sum(scored.values()) / total_weight if total_weight else None
    return {
        "reward": round(reward, 4) if reward is not None else None,
        "algorithm_version": REWARD_ALGORITHM_VERSION,
    }
