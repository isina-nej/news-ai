"""Domain DTO for story momentum and propagation metrics."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class MomentumMetrics:
    # Individual velocities (units/hour or normalized rate)
    view_velocity: float | None = None
    forward_velocity: float | None = None
    share_velocity: float | None = None
    reaction_velocity: float | None = None
    reply_velocity: float | None = None
    engagement_velocity: float | None = None

    # Normalization & smoothing
    normalized_velocity: float | None = None  # Baseline-relative 0..1 percentile rate
    ewma_velocity: float | None = None
    acceleration: float | None = None

    # Arrival windows
    source_arrival_5m: int = 0
    source_arrival_15m: int = 0
    source_arrival_30m: int = 0
    independent_arrival_15m: int = 0
    independent_arrival_30m: int = 0

    # Propagation & counts
    observed_sources_count: int = 0
    independent_sources_count: int = 0
    source_diversity: float = 0.0
    platform_diversity: float = 0.0
    propagation_breadth: float = 0.0
    propagation_depth: float | None = None

    # Temporal & dynamics
    freshness_score: float = 0.0
    trend_age_seconds: int = 0
    growth_consistency: float = 0.0
    growth_persistence: int = 0
    burst_factor: float = 0.0
    time_to_double_seconds: float | None = None

    # Scores
    momentum_confidence: float = 0.0
    normalized_momentum: float = 0.0
    component_breakdown: dict[str, float] = field(default_factory=dict)

    @property
    def momentum_score_decimal(self) -> Decimal:
        val = max(0.0, min(1.0, float(self.normalized_momentum)))
        return Decimal(str(val)).quantize(Decimal("0.0001"))

    @property
    def confidence_decimal(self) -> Decimal:
        val = max(0.0, min(1.0, float(self.momentum_confidence)))
        return Decimal(str(val)).quantize(Decimal("0.0001"))
