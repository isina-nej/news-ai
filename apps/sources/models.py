"""Source registry. One row per external origin (channel, feed, account, site)."""

from __future__ import annotations

from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

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
                check=Q(reliability_score__gte=0, reliability_score__lte=1),
                name="chk_source_reliability_0_1",
            ),
            models.CheckConstraint(
                check=Q(trust_score__gte=0, trust_score__lte=1),
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

    def __str__(self) -> str:
        return f"{self.name} [{self.platform}]"
