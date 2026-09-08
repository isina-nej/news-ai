"""Ranking: weights, score records, baselines, decision logs.

Score standard: ALL scores are 0..1 (Decimal). Probabilities/confidence also 0..1,
so one standard across the system. Display as percent only at presentation layer.
"""

from __future__ import annotations

import hashlib
import math
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinLengthValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from apps.core.choices import ContentType, Platform
from apps.core.models import TimeStampedModel
from apps.ranking import metrics as metric_registry


def to_score_decimal(val: float | Decimal | None) -> Decimal:
    """Standardize any score to 0.0000..1.0000 Decimal. Shared by Ranking & Learning."""
    import math

    if val is None:
        return Decimal("0.0000")
    try:
        f = float(val)
    except (TypeError, ValueError):
        return Decimal("0.0000")
    if not math.isfinite(f):
        return Decimal("0.0000")
    clamped = max(0.0, min(1.0, f))
    return Decimal(str(clamped)).quantize(Decimal("0.0001"))


UNIT_INTERVAL = [MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))]


def _validate_breakdown_unit_values(breakdown: dict) -> None:
    """Every numeric leaf inside breakdown must be a finite 0..1 value."""
    for group_name, group in breakdown.items():
        if not isinstance(group, dict):
            continue
        for key, value in group.items():
            if isinstance(value, dict):
                continue
            if isinstance(value, bool):
                continue
            try:
                num = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(num) or not (0 <= num <= 1):
                raise ValidationError(
                    f"breakdown['{group_name}']['{key}'] must be a finite number in 0..1, "
                    f"got {value!r}."
                )


def validate_breakdown(value: dict) -> None:
    required_groups = ("news_value", "audience_fit", "momentum")
    if not isinstance(value, dict):
        raise ValidationError("score_breakdown must be an object.")
    for group in required_groups:
        if group not in value:
            raise ValidationError(f"score_breakdown missing group '{group}'.")
        if not isinstance(value[group], dict):
            raise ValidationError(f"score_breakdown['{group}'] must be an object.")
    _validate_breakdown_unit_values(value)


class RankingWeight(TimeStampedModel):
    key = models.CharField(max_length=64, unique=True)
    value = models.DecimalField(max_digits=5, decimal_places=4, validators=UNIT_INTERVAL)
    description = models.TextField(blank=True, default="")
    enabled = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(value__gte=Decimal("0"), value__lte=Decimal("1")),
                name="chk_rankingweight_value_0_1",
            ),
        ]
        indexes = [models.Index(fields=["enabled"])]

    def save(self, *args, **kwargs):
        self.full_clean(exclude=None, validate_unique=False)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.key}={self.value}"


class ScoreRecord(TimeStampedModel):
    story = models.ForeignKey("stories.Story", on_delete=models.CASCADE, related_name="scores")
    algorithm_version = models.CharField(max_length=32, validators=[MinLengthValidator(1)])
    news_value = models.DecimalField(max_digits=5, decimal_places=4, validators=UNIT_INTERVAL)
    audience_fit = models.DecimalField(max_digits=5, decimal_places=4, validators=UNIT_INTERVAL)
    momentum = models.DecimalField(max_digits=5, decimal_places=4, validators=UNIT_INTERVAL)
    final_score = models.DecimalField(max_digits=5, decimal_places=4, validators=UNIT_INTERVAL)
    breakdown = models.JSONField(
        default=dict,
        validators=[validate_breakdown],
        help_text="{news_value: {...}, audience_fit: {...}, momentum: {...}}",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(news_value__gte=0, news_value__lte=1),
                name="chk_scorerecord_news_value_0_1",
            ),
            models.CheckConstraint(
                condition=Q(audience_fit__gte=0, audience_fit__lte=1),
                name="chk_scorerecord_audience_fit_0_1",
            ),
            models.CheckConstraint(
                condition=Q(momentum__gte=0, momentum__lte=1),
                name="chk_scorerecord_momentum_0_1",
            ),
            models.CheckConstraint(
                condition=Q(final_score__gte=0, final_score__lte=1),
                name="chk_scorerecord_final_0_1",
            ),
        ]
        indexes = [
            models.Index(fields=["story", "-created_at"]),
            models.Index(fields=["algorithm_version", "-final_score"]),
            models.Index(fields=["-final_score", "-created_at"]),
        ]

    def save(self, *args, **kwargs):
        self.full_clean(exclude=None, validate_unique=False)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.story_id} {self.algorithm_version}={self.final_score}"


class SourceBaseline(TimeStampedModel):
    """One row per (context x metric). Metric-as-row avoids migrations per metric.

    Metrics are registry-backed (apps.ranking.metrics) — derived metrics like
    velocity/acceleration need no schema or enum change.

    Percentiles stored per row; NULL percentile = unknown.
    Uniqueness via `context_hash` (not a multi-column UniqueConstraint) because
    source/topic/subtopic are nullable and MySQL treats NULLs as distinct in
    unique indexes, which would allow silent duplicate baselines.
    """

    source = models.ForeignKey(
        "sources.Source",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="baselines",
        help_text="Null = platform-level fallback baseline.",
    )
    platform = models.CharField(max_length=32, choices=Platform.choices)
    topic = models.ForeignKey(
        "ops.Topic", on_delete=models.CASCADE, null=True, blank=True, related_name="+"
    )
    subtopic = models.ForeignKey(
        "ops.Subtopic", on_delete=models.CASCADE, null=True, blank=True, related_name="+"
    )
    content_type = models.CharField(
        max_length=16, choices=ContentType.choices, default=ContentType.OTHER
    )
    age_bucket_minutes = models.PositiveIntegerField(
        help_text="Snapshot schedule bucket, e.g. 10, 30, 60, 180, 360, 720, 1440."
    )
    # Free-form registry key; validated against metrics registry, not TextChoices.
    metric = models.CharField(max_length=32)
    context_hash = models.CharField(max_length=64, unique=True, editable=False)
    sample_count = models.PositiveIntegerField(default=0)
    p25 = models.FloatField(null=True, blank=True, default=None)
    p50 = models.FloatField(null=True, blank=True, default=None)
    p75 = models.FloatField(null=True, blank=True, default=None)
    p90 = models.FloatField(null=True, blank=True, default=None)
    p95 = models.FloatField(null=True, blank=True, default=None)
    p99 = models.FloatField(null=True, blank=True, default=None)
    confidence = models.DecimalField(
        max_digits=5, decimal_places=4, default=Decimal("0"), validators=UNIT_INTERVAL
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(confidence__gte=0, confidence__lte=1),
                name="chk_baseline_confidence_0_1",
            ),
        ]
        indexes = [
            models.Index(
                fields=["source", "metric", "age_bucket_minutes"],
                name="idx_baseline_lookup",
            ),
            models.Index(fields=["platform", "metric"]),
        ]

    def clean(self) -> None:
        if self.metric and not metric_registry.is_valid_metric(self.metric):
            raise ValidationError(
                {"metric": f"Unknown metric '{self.metric}'. Register it in metrics.py."}
            )
        if self.subtopic and self.topic and self.subtopic.topic_id != self.topic_id:
            raise ValidationError({"subtopic": "subtopic does not belong to the selected topic."})

    def save(self, *args, **kwargs):
        self.full_clean(exclude=None, validate_unique=False)
        parts = [
            str(self.source_id or 0),
            self.platform or "",
            str(self.topic_id or 0),
            str(self.subtopic_id or 0),
            self.content_type or "",
            str(self.age_bucket_minutes),
            self.metric or "",
        ]
        self.context_hash = hashlib.sha256("|".join(parts).encode()).hexdigest()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"baseline {self.source_id}/{self.platform}/{self.metric}@{self.age_bucket_minutes}m"


class DecisionType(models.TextChoices):
    WHAT = "what", "What (publish or skip)"
    WHEN = "when", "When (timing)"
    HOW = "how", "How (template/style)"


class DecisionLog(models.Model):
    story = models.ForeignKey("stories.Story", on_delete=models.CASCADE, related_name="decisions")
    publication = models.ForeignKey(
        "publishing.Publication",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decisions",
    )
    algorithm_version = models.CharField(max_length=32, validators=[MinLengthValidator(1)])
    decision_type = models.CharField(max_length=8, choices=DecisionType.choices)
    feature_snapshot = models.JSONField(default=dict)
    score_breakdown = models.JSONField(default=dict, blank=True)
    predicted_reward = models.DecimalField(
        max_digits=5, decimal_places=4, null=True, blank=True, validators=UNIT_INTERVAL
    )
    selected_action = models.CharField(max_length=64)
    action_detail = models.JSONField(default=dict, blank=True)
    is_exploration = models.BooleanField(default=False)
    exploration_probability = models.DecimalField(
        max_digits=5, decimal_places=4, null=True, blank=True, validators=UNIT_INTERVAL
    )
    actual_reward = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True, default=None
    )
    reward_calculated_at = models.DateTimeField(null=True, blank=True, default=None)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(is_exploration=False)
                | (Q(exploration_probability__gte=0) & Q(exploration_probability__lte=1)),
                name="chk_decision_expl_prob_0_1",
            ),
            models.CheckConstraint(
                condition=Q(predicted_reward__isnull=True)
                | (Q(predicted_reward__gte=0) & Q(predicted_reward__lte=1)),
                name="chk_decision_predicted_reward_0_1",
            ),
        ]
        indexes = [
            models.Index(fields=["story", "decision_type", "-created_at"]),
            models.Index(fields=["algorithm_version", "decision_type", "-created_at"]),
            models.Index(fields=["publication", "decision_type"]),
        ]

    def __str__(self) -> str:
        return (
            f"{self.decision_type}:{self.selected_action} {self.story_id} {self.algorithm_version}"
        )
