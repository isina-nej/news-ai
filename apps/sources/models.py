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
    # Stable platform-native peer id resolved after first contact
    # (e.g. Telegram channel id). Non-secret; survives username renames.
    platform_external_id = models.CharField(max_length=128, blank=True, default="")
    # Runtime FloodWait/rate-limit cooldown; scheduler must skip while in future.
    cooldown_until = models.DateTimeField(null=True, blank=True, default=None)

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
    updated_count = models.PositiveIntegerField(default=0)
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
            f"+{self.created_count}u{self.updated_count}d{self.duplicate_count}"
        )


class SourceCheckpoint(models.Model):
    """Persistent per-source ingestion cursor.

    One row per (source, adapter). The cursor advances ONLY after items
    have been persisted successfully (fetch -> persist -> advance).
    A crash between fetch and advance is safe: the next run re-reads with
    overlap and idempotent persistence drops true duplicates.
    """

    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="checkpoints")
    adapter = models.CharField(max_length=32, default="telegram")
    # Telegram semantics: highest message id observed (inclusive).
    last_external_id = models.CharField(max_length=512, null=True, blank=True, default=None)
    last_published_at = models.DateTimeField(null=True, blank=True, default=None)
    state = models.JSONField(
        default=dict,
        blank=True,
        help_text="Adapter-scoped cursor state, e.g. {'last_message_id': 1234}. "
        "Never secrets. Small and JSON-serializable.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "adapter"], name="uniq_checkpoint_source_adapter"
            ),
        ]
        indexes = [models.Index(fields=["source", "adapter"])]

    def __str__(self) -> str:
        return f"checkpoint {self.source_id}/{self.adapter} @{self.last_external_id}"


class EngagementTrackingState(models.Model):
    """Lean per-item engagement milestone scheduler state.

    Avoids thousands of individual Celery ETA tasks: a periodic scheduler
    scans due rows in batches. ``next_due_at`` is the wall-clock time the
    milestone becomes observable; ``next_target_age_seconds`` is the metric
    bucket (10m/30m/60m/...). ``active=False`` stops tracking (e.g. max age
    reached or item deleted).
    """

    source_item = models.OneToOneField(
        "news.SourceItem", on_delete=models.CASCADE, related_name="engagement_tracking"
    )
    next_due_at = models.DateTimeField(null=True, blank=True, default=None, db_index=True)
    next_target_age_seconds = models.PositiveIntegerField(null=True, blank=True, default=None)
    active = models.BooleanField(default=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True, default=None)
    failure_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["active", "next_due_at"]),
        ]

    def __str__(self) -> str:
        return (
            f"tracking item={self.source_item_id} "
            f"next={self.next_target_age_seconds}s active={self.active}"
        )
