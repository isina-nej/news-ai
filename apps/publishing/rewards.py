"""Own-channel normalized reward from relative publication performance with decomposed signals and quality guardrails."""

from __future__ import annotations

REWARD_ALGORITHM_VERSION = "reward-v2"
REWARD_WEIGHTS = {
    "forwards": 0.45,
    "views": 0.25,
    "reactions": 0.15,
    "replies": 0.15,
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
    snapshot,
    *,
    baselines: dict[str, dict] | None = None,
    credibility_score: float | None = 1.0,
) -> dict[str, float | str | None]:
    """Compare an own-channel snapshot to similar historical posts with decomposed components and clickbait guardrails."""
    baselines = baselines or {}
    scored: dict[str, float] = {}
    decomposed: dict[str, float | None] = {}

    for metric, weight in REWARD_WEIGHTS.items():
        value = getattr(snapshot, metric, None)
        pct = _percentile_or_none(value, baselines.get(metric))
        decomposed[f"{metric}_reward"] = pct
        if pct is not None:
            scored[metric] = pct * weight

    if not scored:
        return {
            "reward": None,
            "view_reward": None,
            "forward_reward": None,
            "reaction_reward": None,
            "reply_reward": None,
            "quality_guardrail": 1.0,
            "algorithm_version": REWARD_ALGORITHM_VERSION,
        }

    total_weight = sum(REWARD_WEIGHTS[m] for m in scored)
    raw_reward = sum(scored.values()) / total_weight if total_weight else 0.50

    # Clickbait / Quality Guardrail: low credibility caps engagement reward
    cred = float(credibility_score if credibility_score is not None else 1.0)
    guardrail_multiplier = 1.0
    if cred < 0.40:
        guardrail_multiplier = max(0.20, cred * 1.5)

    bounded_reward = round(max(0.0, min(1.0, raw_reward * guardrail_multiplier)), 4)

    return {
        "reward": bounded_reward,
        "view_reward": decomposed.get("views_reward"),
        "forward_reward": decomposed.get("forwards_reward"),
        "reaction_reward": decomposed.get("reactions_reward"),
        "reply_reward": decomposed.get("replies_reward"),
        "quality_guardrail": round(guardrail_multiplier, 4),
        "algorithm_version": REWARD_ALGORITHM_VERSION,
    }
