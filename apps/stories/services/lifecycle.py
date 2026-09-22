"""Story lifecycle state machine and adaptive observation interval policy."""

from __future__ import annotations

from apps.core.choices import LifecycleState, TrendState
from apps.ops.models import DynamicSetting
from apps.stories.services.momentum_metrics import MomentumMetrics

DEFAULT_OBSERVATION_INTERVALS = {
    LifecycleState.BREAKING: 60,  # 1 minute
    LifecycleState.RISING: 180,  # 3 minutes
    LifecycleState.WATCHING: 600,  # 10 minutes
    LifecycleState.DISCOVERED: 300,  # 5 minutes
    LifecycleState.PEAKING: 600,  # 10 minutes
    LifecycleState.COOLING: 1800,  # 30 minutes
    LifecycleState.STALE: 3600,  # 60 minutes
    LifecycleState.ARCHIVED: 86400,  # 24 hours (inactive)
}


def get_observation_intervals() -> dict[str, int]:
    intervals = dict(DEFAULT_OBSERVATION_INTERVALS)
    try:
        row = DynamicSetting.objects.filter(key="observation_intervals").first()
        if row and isinstance(row.value, dict):
            for k in intervals:
                if k in row.value and isinstance(row.value[k], int) and row.value[k] > 0:
                    intervals[k] = row.value[k]
    except Exception:
        pass
    return intervals


class LifecycleStateMachine:
    """Manages deterministic lifecycle state transitions and adaptive polling schedules."""

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        LifecycleState.DISCOVERED: {
            LifecycleState.WATCHING,
            LifecycleState.RISING,
            LifecycleState.BREAKING,
            LifecycleState.ARCHIVED,
        },
        LifecycleState.WATCHING: {
            LifecycleState.RISING,
            LifecycleState.BREAKING,
            LifecycleState.COOLING,
            LifecycleState.STALE,
            LifecycleState.ARCHIVED,
        },
        LifecycleState.RISING: {
            LifecycleState.BREAKING,
            LifecycleState.PEAKING,
            LifecycleState.COOLING,
            LifecycleState.WATCHING,
        },
        LifecycleState.BREAKING: {
            LifecycleState.PEAKING,
            LifecycleState.COOLING,
            LifecycleState.WATCHING,
        },
        LifecycleState.PEAKING: {
            LifecycleState.COOLING,
            LifecycleState.STALE,
        },
        LifecycleState.COOLING: {
            LifecycleState.STALE,
            LifecycleState.ARCHIVED,
            LifecycleState.WATCHING,
            LifecycleState.RISING,
        },
        LifecycleState.STALE: {
            LifecycleState.ARCHIVED,
            LifecycleState.WATCHING,
            LifecycleState.RISING,
        },
        LifecycleState.ARCHIVED: {
            LifecycleState.WATCHING,
            LifecycleState.RISING,
        },
    }

    @classmethod
    def evaluate_lifecycle(
        cls,
        current_lifecycle: str,
        trend_state: str,
        metrics: MomentumMetrics,
        *,
        has_material_update: bool = False,
    ) -> tuple[str, str]:
        """Determine next lifecycle state and transition reason."""
        # 1. Material update reactivates cooled/stale/archived stories
        if has_material_update and current_lifecycle in (
            LifecycleState.COOLING,
            LifecycleState.STALE,
            LifecycleState.ARCHIVED,
        ):
            return LifecycleState.RISING, "material_update_reactivation"

        # 2. Breaking trend moves story to BREAKING
        if trend_state == TrendState.BREAKING:
            if current_lifecycle != LifecycleState.BREAKING:
                return LifecycleState.BREAKING, "breaking_trend_transition"
            return LifecycleState.BREAKING, "breaking_sustained"

        # 3. Rising / Surging trend
        if trend_state in (TrendState.RISING, TrendState.SURGING):
            if current_lifecycle == LifecycleState.BREAKING:
                return LifecycleState.PEAKING, "breaking_passed_peak"
            if current_lifecycle in (LifecycleState.DISCOVERED, LifecycleState.WATCHING):
                return LifecycleState.RISING, "momentum_growth_rising"
            return LifecycleState.RISING, "rising_sustained"

        # 4. Early signal: keeps in WATCHING with accelerated monitoring
        if trend_state == TrendState.EARLY_SIGNAL:
            if current_lifecycle in (LifecycleState.DISCOVERED, LifecycleState.STALE):
                return LifecycleState.WATCHING, "early_signal_watching"

        # 5. Peak to Cooling
        if current_lifecycle in (LifecycleState.BREAKING, LifecycleState.RISING):
            if metrics.acceleration is not None and metrics.acceleration < -0.15:
                return LifecycleState.PEAKING, "velocity_decelerating_peak"

        if current_lifecycle == LifecycleState.PEAKING:
            if metrics.normalized_momentum < 0.50:
                return LifecycleState.COOLING, "momentum_cooled_below_threshold"

        # 6. Cooling to Stale
        if current_lifecycle == LifecycleState.COOLING:
            # If no updates for over 12 hours
            if metrics.trend_age_seconds > 43200 and metrics.normalized_momentum < 0.30:
                return LifecycleState.STALE, "inactivity_marked_stale"

        # 7. Stale to Archived
        if current_lifecycle == LifecycleState.STALE:
            if metrics.trend_age_seconds > 86400 * 3:
                return LifecycleState.ARCHIVED, "long_term_inactivity_archived"

        # Default fallback: keep current or move DISCOVERED to WATCHING
        if current_lifecycle == LifecycleState.DISCOVERED:
            return LifecycleState.WATCHING, "initial_observation_watching"

        return current_lifecycle, "state_maintained"

    @classmethod
    def get_interval_seconds(cls, lifecycle_state: str, trend_state: str) -> int:
        """Determine adaptive observation interval in seconds based on combined state."""
        intervals = get_observation_intervals()
        # High urgency trend overrides lifecycle interval to faster polling
        if trend_state == TrendState.BREAKING:
            return intervals.get(LifecycleState.BREAKING, 60)
        if trend_state == TrendState.SURGING:
            return intervals.get(LifecycleState.RISING, 180)
        if trend_state == TrendState.EARLY_SIGNAL:
            return 300  # 5 minutes

        return intervals.get(lifecycle_state, 600)
