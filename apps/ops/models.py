"""Ops: taxonomy, settings, flags, audit, audience learning. No secrets stored or logged.

Session storage policy (env-only vs DB) is documented here as the single source
of truth; architecture.md and security.md must match this. Current choice:
Telegram Kurigram session string in env (TELEGRAM_SESSION_STRING) for phase 1
sync-runner; encrypted-at-rest DB column is reserved for multi-account phase
and MUST NOT appear until the encryption key-rotation story ships. See ADR.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel


class Topic(TimeStampedModel):
    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    enabled = models.BooleanField(default=True)
    weight = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0"))],
    )

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(weight__gte=0), name="chk_topic_weight_ge_0"),
        ]
        indexes = [models.Index(fields=["enabled"])]

    def save(self, *args, **kwargs):
        self.full_clean(exclude=None, validate_unique=False)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name


class Subtopic(TimeStampedModel):
    topic = models.ForeignKey(Topic, on_delete=models.CASCADE, related_name="subtopics")
    slug = models.SlugField(max_length=64)
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    enabled = models.BooleanField(default=True)
    weight = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0"))],
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["topic", "slug"], name="uniq_subtopic_topic_slug"),
            models.CheckConstraint(condition=Q(weight__gte=0), name="chk_subtopic_weight_ge_0"),
        ]
        indexes = [models.Index(fields=["topic", "enabled"])]

    def save(self, *args, **kwargs):
        self.full_clean(exclude=None, validate_unique=False)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.topic.name} / {self.name}"


class DynamicSetting(models.Model):
    key = models.CharField(max_length=128, unique=True)
    value = models.JSONField(default=dict)
    description = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.key


class FeatureFlag(models.Model):
    key = models.CharField(max_length=128, unique=True)
    enabled = models.BooleanField(default=False)
    description = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.key}={'on' if self.enabled else 'off'}"


class AuditLog(models.Model):
    """Sensitive actions/decisions. Must never contain secrets (review + RedactFilter)."""

    actor = models.CharField(max_length=255, default="system")
    action = models.CharField(max_length=128)
    entity_type = models.CharField(max_length=128)
    entity_id = models.CharField(max_length=64)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["entity_type", "entity_id"]),
            models.Index(fields=["action", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.entity_type}:{self.entity_id}"


class AudiencePreference(TimeStampedModel):
    """AudienceLearningService state. Feature/context as data, never columns.

    No migration needed for new features: feature='topic', context='{"topic":"tech"}',
    value=relative-performance EWMA. context_hash gives MySQL-safe uniqueness
    (MySQL lacks partial unique constraints and treats NULLs as distinct).
    """

    feature = models.CharField(max_length=64)
    context = models.JSONField(default=dict, blank=True)
    context_hash = models.CharField(max_length=64, default="", blank=True, editable=False)
    value = models.DecimalField(max_digits=9, decimal_places=6, default=Decimal("0"))
    sample_count = models.PositiveIntegerField(default=0)
    confidence = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))],
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["feature", "context_hash"], name="uniq_pref_feature_ctx"
            ),
            models.CheckConstraint(
                check=Q(confidence__gte=0, confidence__lte=1),
                name="chk_audiencepref_confidence_0_1",
            ),
        ]
        indexes = [
            models.Index(fields=["feature", "updated_at"]),
        ]

    def save(self, *args, **kwargs):
        canonical = json.dumps(self.context or {}, sort_keys=True, separators=(",", ":"))
        self.context_hash = hashlib.sha256(canonical.encode()).hexdigest()
        self.full_clean(exclude=None, validate_unique=False)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.feature} {self.context} -> {self.value}"
