"""News intake: SourceItem + EngagementSnapshot. Cross-source items never deleted."""

from __future__ import annotations

import hashlib

from django.db import models
from django.utils import timezone

from apps.core.choices import ContentType
from apps.core.models import TimeStampedModel


class SourceItemStatus(models.TextChoices):
    COLLECTED = "collected", "Collected"
    PARSED = "parsed", "Parsed"
    ANALYZED = "analyzed", "Analyzed"
    RANKED = "ranked", "Ranked"
    SELECTED = "selected", "Selected"
    CONTENT_READY = "content_ready", "Content ready"
    APPROVED = "approved", "Approved"
    AUTO_APPROVED = "auto_approved", "Auto-approved"
    SCHEDULED = "scheduled", "Scheduled"
    PUBLISHED = "published", "Published"
    FAILED = "failed", "Failed"
    REJECTED = "rejected", "Rejected"


class SourceItem(TimeStampedModel):
    """One fetched unit from one source. Same event from N sources = N rows, one Story."""

    source = models.ForeignKey("sources.Source", on_delete=models.PROTECT, related_name="items")
    external_id = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        default=None,
        help_text="Platform-native id (message id, post id). Null when unavailable.",
    )
    canonical_url = models.URLField(max_length=2048, blank=True, default="")
    url_hash = models.CharField(max_length=64, blank=True, default="")
    title = models.CharField(max_length=1024, blank=True, default="")
    raw_text = models.TextField(blank=True, default="")
    normalized_text = models.TextField(blank=True, default="")
    content_hash = models.CharField(max_length=64, blank=True, default="")
    media = models.JSONField(default=dict, blank=True)
    language = models.CharField(max_length=10, default="und")
    content_type = models.CharField(
        max_length=16, choices=ContentType.choices, default=ContentType.OTHER
    )
    topic = models.ForeignKey(
        "ops.Topic", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    subtopic = models.ForeignKey(
        "ops.Subtopic", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    story = models.ForeignKey(
        "stories.Story",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="items",
        help_text="Primary story cache. Membership table is the rich link.",
    )
    published_at = models.DateTimeField(
        null=True, blank=True, help_text="Real publish time at the source, not fetch time."
    )
    collected_at = models.DateTimeField(default=timezone.now)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=SourceItemStatus.choices, default=SourceItemStatus.COLLECTED
    )

    class Meta:
        constraints = [
            # NULL external_ids stay distinct on MySQL/SQLite -> uniqueness only when present.
            models.UniqueConstraint(
                fields=["source", "external_id"], name="uniq_item_source_external"
            ),
        ]
        indexes = [
            models.Index(fields=["source", "collected_at"]),
            models.Index(fields=["status", "collected_at"]),
            models.Index(fields=["published_at"]),
            models.Index(fields=["content_hash"]),
            models.Index(fields=["url_hash"]),
        ]

    def save(self, *args, **kwargs):
        if not self.external_id:
            self.external_id = None
        if not self.url_hash and self.canonical_url:
            self.url_hash = hashlib.sha256(self.canonical_url.encode()).hexdigest()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        ref = self.external_id or self.pk
        return f"{self.source_id}#{ref}: {self.title[:60]}"


class EngagementSnapshot(TimeStampedModel):
    """Point-in-time metrics. NULL = unknown platform value, never conflated with 0."""

    source_item = models.ForeignKey(SourceItem, on_delete=models.CASCADE, related_name="snapshots")
    captured_at = models.DateTimeField(default=timezone.now)
    post_age_seconds = models.PositiveIntegerField(null=True, blank=True, default=None)
    views = models.PositiveBigIntegerField(null=True, blank=True, default=None)
    forwards = models.PositiveBigIntegerField(null=True, blank=True, default=None)
    shares = models.PositiveBigIntegerField(null=True, blank=True, default=None)
    reactions = models.PositiveBigIntegerField(null=True, blank=True, default=None)
    replies = models.PositiveBigIntegerField(null=True, blank=True, default=None)
    saves = models.PositiveBigIntegerField(null=True, blank=True, default=None)
    raw_metrics = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_item", "captured_at"], name="uniq_snap_item_captured"
            ),
        ]
        indexes = [models.Index(fields=["source_item", "post_age_seconds"])]

    def save(self, *args, **kwargs):
        if self.post_age_seconds is None and self.captured_at:
            published = getattr(self.source_item, "published_at", None)
            if published:
                self.post_age_seconds = max(0, int((self.captured_at - published).total_seconds()))
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"snap {self.source_item_id}@{self.captured_at:%H:%M}"
