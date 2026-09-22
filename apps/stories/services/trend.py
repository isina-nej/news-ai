"""Deterministic TrendDetector with hysteresis and confidence guardrails."""

from __future__ import annotations

from apps.core.choices import TrendState
from apps.ops.models import DynamicSetting
from apps.stories.services.momentum_metrics import MomentumMetrics

DEFAULT_THRESHOLDS = {
    "breaking_enter": 0.85,
    "breaking_exit": 0.75,
    "surging_enter": 0.75,
    "surging_exit": 0.65,
    "rising_enter": 0.60,
    "rising_exit": 0.50,
    "early_signal_accel": 0.15,
    "early_signal_indep": 2,
    "early_signal_conf": 0.35,
}


def get_trend_thresholds() -> dict[str, float]:
    thresholds = dict(DEFAULT_THRESHOLDS)
    try:
        row = DynamicSetting.objects.filter(key="trend_thresholds").first()
        if row and isinstance(row.value, dict):
            for k in thresholds:
                if k in row.value and isinstance(row.value[k], (int, float)):
                    thresholds[k] = float(row.value[k])
    except Exception:
        pass
    return thresholds


class TrendDetector:
    """Classifies real-time trend state based on statistical metrics and hysteresis."""

    @classmethod
    def detect_state(
        cls,
        metrics: MomentumMetrics,
        *,
        current_state: str = TrendState.NORMAL,
        sample_count: int = 0,
    ) -> tuple[str, str]:
        """Returns (new_trend_state, detection_reason)."""
        th = get_trend_thresholds()
        mom = float(metrics.normalized_momentum)
        acc = float(metrics.acceleration or 0.0)
        conf = float(metrics.momentum_confidence)
        indep = metrics.independent_sources_count

        # 1. Breaking state (with hysteresis)
        if current_state == TrendState.BREAKING:
            if mom >= th["breaking_exit"]:
                return TrendState.BREAKING, "breaking_maintained"
        else:
            if mom >= th["breaking_enter"] and conf >= 0.40 and sample_count >= 2:
                return TrendState.BREAKING, "momentum_breaking_threshold_crossed"
            if acc >= 0.40 and indep >= 3 and mom >= 0.75:
                return TrendState.BREAKING, "acceleration_breakout_confirmed"

        # 2. Surging state
        if current_state == TrendState.SURGING:
            if mom >= th["surging_exit"]:
                return TrendState.SURGING, "surging_maintained"
        else:
            if mom >= th["surging_enter"] and acc >= 0.05:
                return TrendState.SURGING, "momentum_surging_threshold_crossed"

        # 3. Rising state
        if current_state == TrendState.RISING:
            if mom >= th["rising_exit"]:
                return TrendState.RISING, "rising_maintained"
        else:
            if mom >= th["rising_enter"] and acc >= 0.0:
                return TrendState.RISING, "momentum_rising_threshold_crossed"

        # 4. Early Signal: small story accelerating rapidly in independent sources
        if (
            acc >= th["early_signal_accel"]
            and indep >= th["early_signal_indep"]
            and conf >= th["early_signal_conf"]
            and metrics.observed_sources_count <= 5
        ):
            return TrendState.EARLY_SIGNAL, "early_signal_acceleration_detected"

        # 5. Saturated: high source count but deceleration
        if metrics.observed_sources_count >= 8 and acc <= -0.15:
            return TrendState.SATURATED, "high_coverage_decelerating"

        # 6. Cooling: deceleration and dropping momentum
        if acc <= -0.10 and mom < 0.45:
            return TrendState.COOLING, "momentum_decay_cooling"

        return TrendState.NORMAL, "baseline_momentum"
