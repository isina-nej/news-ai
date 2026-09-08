"""Source baseline calculation, storage, and fallback retrieval.

Uses robust statistics: median, percentiles (p25, p50, p75, p90, p95, p99).
Mean is not the primary ranking baseline.
"""

from __future__ import annotations

import hashlib
import math
from decimal import Decimal

from django.db import transaction

from apps.core.choices import ContentType
from apps.ranking.models import SourceBaseline
from apps.sources.models import Source

STANDARD_AGE_BUCKETS = [10, 30, 60, 180, 360, 720, 1440]  # in minutes
BASELINE_ALGORITHM_VERSION = "baseline-v1"


def compute_context_hash(
    source_id: int | None,
    platform: str,
    topic_id: int | None,
    subtopic_id: int | None,
    content_type: str,
    age_bucket_minutes: int,
    metric: str,
) -> str:
    parts = [
        str(source_id or 0),
        platform or "",
        str(topic_id or 0),
        str(subtopic_id or 0),
        content_type or "",
        str(age_bucket_minutes),
        metric or "",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def calculate_percentiles(values: list[float]) -> dict[str, float]:
    """Calculate p25, p50 (median), p75, p90, p95, p99 from raw observations."""
    if not values:
        return {"p25": 0.0, "p50": 0.0, "p75": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0}
    sorted_vals = sorted(values)
    n = len(sorted_vals)

    def _get_pct(p: float) -> float:
        if n == 1:
            return float(sorted_vals[0])
        k = (n - 1) * p
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return float(sorted_vals[int(k)])
        return float(sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f))

    return {
        "p25": round(_get_pct(0.25), 2),
        "p50": round(_get_pct(0.50), 2),
        "p75": round(_get_pct(0.75), 2),
        "p90": round(_get_pct(0.90), 2),
        "p95": round(_get_pct(0.95), 2),
        "p99": round(_get_pct(0.99), 2),
    }


class SourceBaselineService:
    @staticmethod
    def estimate_percentile(
        value: float | int | None, baseline: SourceBaseline | None
    ) -> float | None:
        """Estimate 0..1 percentile of a value against baseline percentiles.

        If value is None or baseline is None, returns None. Never conflates NULL with 0.
        """
        if value is None or baseline is None:
            return None
        val = float(value)
        p25 = float(baseline.p25 or 0.0)
        p50 = float(baseline.p50 or 0.0)
        p75 = float(baseline.p75 or 0.0)
        p90 = float(baseline.p90 or 0.0)
        p95 = float(baseline.p95 or 0.0)
        p99 = float(baseline.p99 or 0.0)

        # Stepwise linear interpolation across percentile intervals
        if val <= p25:
            if p25 <= 0.0:
                return 0.25
            return round(max(0.0, min(0.25, 0.25 * (val / p25))), 4)
        if val <= p50:
            denom = p50 - p25 or 1.0
            return round(0.25 + 0.25 * ((val - p25) / denom), 4)
        if val <= p75:
            denom = p75 - p50 or 1.0
            return round(0.50 + 0.25 * ((val - p50) / denom), 4)
        if val <= p90:
            denom = p90 - p75 or 1.0
            return round(0.75 + 0.15 * ((val - p75) / denom), 4)
        if val <= p95:
            denom = p95 - p90 or 1.0
            return round(0.90 + 0.05 * ((val - p90) / denom), 4)
        if val <= p99:
            denom = p99 - p95 or 1.0
            return round(0.95 + 0.04 * ((val - p95) / denom), 4)
        # Above p99
        return 0.9999

    @staticmethod
    def find_baseline(
        *,
        source: Source | None,
        platform: str,
        topic_id: int | None,
        subtopic_id: int | None,
        content_type: str,
        age_bucket_minutes: int,
        metric: str,
    ) -> tuple[SourceBaseline | None, str, float]:
        """Find best baseline using fallback hierarchy.

        Returns: (baseline, matched_level, confidence)
        Level 1: source + topic + subtopic + type (conf ~0.95)
        Level 2: source + topic + type (conf ~0.85)
        Level 3: source + type (conf ~0.70)
        Level 4: source (conf ~0.55)
        Level 5: platform global (conf ~0.35)
        """
        source_id = source.pk if source else None
        # Level 1: full context
        if source_id and topic_id and subtopic_id:
            b = SourceBaseline.objects.filter(
                source_id=source_id,
                topic_id=topic_id,
                subtopic_id=subtopic_id,
                content_type=content_type,
                age_bucket_minutes=age_bucket_minutes,
                metric=metric,
            ).first()
            if b and b.sample_count >= 10:
                return b, "source_topic_subtopic_type", 0.95

        # Level 2: source + topic + type
        if source_id and topic_id:
            b = SourceBaseline.objects.filter(
                source_id=source_id,
                topic_id=topic_id,
                content_type=content_type,
                age_bucket_minutes=age_bucket_minutes,
                metric=metric,
            ).first()
            if b and b.sample_count >= 10:
                return b, "source_topic_type", 0.85

        # Level 3: source + type
        if source_id:
            b = SourceBaseline.objects.filter(
                source_id=source_id,
                content_type=content_type,
                age_bucket_minutes=age_bucket_minutes,
                metric=metric,
            ).first()
            if b and b.sample_count >= 5:
                return b, "source_type", 0.70

        # Level 4: source only
        if source_id:
            b = SourceBaseline.objects.filter(
                source_id=source_id,
                age_bucket_minutes=age_bucket_minutes,
                metric=metric,
            ).first()
            if b and b.sample_count >= 5:
                return b, "source", 0.55

        # Level 5: platform global
        b = SourceBaseline.objects.filter(
            source_id__isnull=True,
            platform=platform,
            age_bucket_minutes=age_bucket_minutes,
            metric=metric,
        ).first()
        if b:
            return b, "platform", 0.35

        return None, "none", 0.0

    @classmethod
    def update_baseline_from_observations(
        cls,
        *,
        source: Source | None,
        platform: str,
        topic_id: int | None = None,
        subtopic_id: int | None = None,
        content_type: str = ContentType.OTHER,
        age_bucket_minutes: int = 60,
        metric: str = "views",
        observations: list[float],
    ) -> SourceBaseline:
        """Create or update a SourceBaseline with calculated percentiles."""
        pcts = calculate_percentiles(observations)
        count = len(observations)
        # Confidence increases with sample size up to 1.0 (min 30 samples for full confidence)
        conf = Decimal(str(min(1.0, count / 30.0))).quantize(Decimal("0.0001"))

        with transaction.atomic():
            baseline, _ = SourceBaseline.objects.update_or_create(
                source=source,
                platform=platform,
                topic_id=topic_id,
                subtopic_id=subtopic_id,
                content_type=content_type,
                age_bucket_minutes=age_bucket_minutes,
                metric=metric,
                defaults={
                    "sample_count": count,
                    "p25": pcts["p25"],
                    "p50": pcts["p50"],
                    "p75": pcts["p75"],
                    "p90": pcts["p90"],
                    "p95": pcts["p95"],
                    "p99": pcts["p99"],
                    "confidence": conf,
                },
            )
            return baseline
