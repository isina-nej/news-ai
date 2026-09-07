"""Source registry and fetch audit history. One row per external origin."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.choices import Platform
from apps.core.models import TimeStampedModel


class Source(TimeStampedModel):
    platform = models.CharField(max_length=32, choices=Platform.choices)
    name = models.CharField(max_length=255)
    identifier = models.CharField(
        max_length=512,
        help_text="Canonical handle per platform: @channel, feed URL, site domain.",
    )
    url = models.URLField(max_length=2048, blank=True, default="")
    enabled = models.BooleanField(default=True)
    priority = models.PositiveSmallIntegerField(default=0)
    reliability_score = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=Decimal("0.5"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))],
        help_text="0..1. Historical accuracy.",
    )
    trust_score = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=Decimal("0.5"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))],
        help_text="0..1. Ranking input, updated by ops.",
    )
    language = models.CharField(max_length=10, default="und")
    fetch_interval_seconds = models.PositiveIntegerField(default=300)
    configuration = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-source adapter settings only. Never secrets.",
    )
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["platform", "identifier"], name="uniq_source_platform_identifier"
            ),
            models.CheckConstraint(
                condition=Q(reliability_score__gte=0, reliability_score__lte=1),
                name="chk_source_reliability_0_1",
            ),
            models.CheckConstraint(
                condition=Q(trust_score__gte=0, trust_score__lte=1),
                name="chk_source_trust_0_1",
            ),
        ]
        indexes = [
            models.Index(fields=["platform", "enabled"]),
            models.Index(fields=["enabled", "priority"]),
        ]

    def save(self, *args, **kwargs):
        self.full_clean(exclude=None, validate_unique=False)
        super().save(*args, **kwargs)

    def record_fetch_success(self, *, now: datetime | None = None) -> None:
        """Update source health after a successful fetch."""
        current_time = now or timezone.now()
        self.last_success_at = current_time
        self.consecutive_failures = 0
        self.save(
            update_fields=["last_success_at", "consecutive_failures", "configuration", "updated_at"]
        )

    def record_fetch_failure(self, *, now: datetime | None = None) -> None:
        """Update source health after a failed fetch."""
        current_time = now or timezone.now()
        self.last_failure_at = current_time
        self.consecutive_failures += 1
        self.save(update_fields=["last_failure_at", "consecutive_failures", "updated_at"])

    def __str__(self) -> str:
        return f"{self.name} [{self.platform}]"


class FetchRunStatus(models.TextChoices):
    RUNNING = "running", "Running"
    SUCCESS = "success", "Success"
    FAILED = "failed", "Failed"
    NOT_MODIFIED = "not_modified", "Not modified (304)"


class FetchRun(models.Model):
    """Execution audit record for each source fetch run."""

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="fetch_runs")
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=FetchRunStatus.choices, default=FetchRunStatus.RUNNING
    )
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    fetched_count = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)
    rejected_count = models.PositiveIntegerField(default=0)
    error_type = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    duration_ms = models.PositiveIntegerField(default=0)
    correlation_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["source", "-started_at"]),
            models.Index(fields=["status", "-started_at"]),
        ]

    def __str__(self) -> str:
        return (
            f"FetchRun #{self.pk} {self.source.name} [{self.status}] "
            f"+{self.created_count}d{self.duplicate_count}"
        )
