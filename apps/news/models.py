"""News intake: SourceItem + EngagementSnapshot. Cross-source items never deleted."""

from __future__ import annotations

import hashlib

from django.core.exceptions import ValidationError
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
    """One fetched unit from one source. Same event from N sources = N rows, one Story.

    Current assignment lives on `story` FK (nullable = unassigned) plus a single
    `StoryMembership(is_current=True)` row. Change assignment by flipping the
    membership, never by diverging the two — `save()` keeps them consistent.
    """

    source = models.ForeignKey("sources.Source", on_delete=models.PROTECT, related_name="items")
    external_id = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        default=None,
        help_text="Platform-native id (message id, post id). Null when unavailable.",
    )
    canonical_url = models.URLField(max_length=2048, blank=True, default="")
    url_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        default=None,
        help_text="SHA-256 of canonical_url. NULL when URL is absent so empty URLs never collide.",
    )
    title = models.CharField(max_length=1024, blank=True, default="")
    raw_text = models.TextField(blank=True, default="")
    normalized_text = models.TextField(blank=True, default="")
    raw_content_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        default=None,
        help_text="SHA-256 of raw_text. NULL when text is absent.",
    )
    content_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        default=None,
        help_text="SHA-256 hash of normalized_text for exact deduplication. NULL when absent.",
    )
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
        help_text="Current Story assignment; must match the single current membership.",
    )
    published_at = models.DateTimeField(
        null=True, blank=True, help_text="Real publish time at the source, not fetch time."
    )
    collected_at = models.DateTimeField(default=timezone.now)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    # Platform-native edit timestamp (e.g. Telegram edit_date). Never derived locally.
    source_updated_at = models.DateTimeField(null=True, blank=True, default=None)
    # Soft deletion marker (platform-confirmed only; absence in one poll never implies deletion).
    source_deleted_at = models.DateTimeField(null=True, blank=True, default=None)
    raw_payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=SourceItemStatus.choices, default=SourceItemStatus.COLLECTED
    )

    class Meta:
        constraints = [
            # NULLs stay distinct on MySQL 8.4 & SQLite -> uniqueness enforced when present
            models.UniqueConstraint(
                fields=["source", "external_id"], name="uniq_item_source_external"
            ),
            models.UniqueConstraint(
                fields=["source", "url_hash"], name="uniq_item_source_url_hash"
            ),
            models.UniqueConstraint(
                fields=["source", "content_hash"], name="uniq_item_source_content_hash"
            ),
        ]
        indexes = [
            models.Index(fields=["source", "collected_at"]),
            models.Index(fields=["status", "collected_at"]),
            models.Index(fields=["story", "collected_at"]),
            models.Index(fields=["published_at"]),
            models.Index(fields=["content_hash"]),
            models.Index(fields=["raw_content_hash"]),
            models.Index(fields=["url_hash"]),
        ]

    def clean(self) -> None:
        if not self.external_id:
            self.external_id = None
        if self.subtopic and self.topic and self.subtopic.topic_id != self.topic_id:
            raise ValidationError({"subtopic": "subtopic does not belong to the selected topic."})

    def save(self, *args, **kwargs):
        # 1. External ID normalization
        if not self.external_id:
            self.external_id = None

        # 2. Recompute URL hash (never allow stale hash on update)
        if self.canonical_url and self.canonical_url.strip():
            self.url_hash = hashlib.sha256(self.canonical_url.strip().encode()).hexdigest()
        else:
            self.url_hash = None

        # 3. Recompute raw content hash
        if self.raw_text and self.raw_text.strip():
            self.raw_content_hash = hashlib.sha256(self.raw_text.strip().encode()).hexdigest()
        else:
            self.raw_content_hash = None

        # 4. Recompute normalized content hash
        target_text = (
            self.normalized_text.strip()
            if self.normalized_text
            else (self.raw_text.strip() if self.raw_text else "")
        )
        if target_text:
            self.content_hash = hashlib.sha256(target_text.encode()).hexdigest()
        else:
            self.content_hash = None

        self.full_clean(exclude=None, validate_unique=False)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        ref = self.external_id or self.pk
        return f"{self.source_id}#{ref}: {self.title[:60]}"


class SourceItemRevision(models.Model):
    """Immutable snapshot of a SourceItem's material state at observation time.

    One row is created only when material fields actually changed
    (title / raw_text / normalized_text / content_hash). The live
    SourceItem row always carries the latest version; this table preserves
    the history needed for corrections, novelty and republication decisions.
    """

    source_item = models.ForeignKey(SourceItem, on_delete=models.CASCADE, related_name="revisions")
    revision_number = models.PositiveIntegerField()
    source_updated_at = models.DateTimeField(null=True, blank=True, default=None)
    observed_at = models.DateTimeField(default=timezone.now)
    title = models.CharField(max_length=1024, blank=True, default="")
    raw_text = models.TextField(blank=True, default="")
    normalized_text = models.TextField(blank=True, default="")
    content_hash = models.CharField(max_length=64, null=True, blank=True, default=None)
    change_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_item", "revision_number"],
                name="uniq_revision_item_number",
            ),
        ]
        indexes = [
            models.Index(fields=["source_item", "revision_number"]),
        ]
        ordering = ["revision_number"]

    def __str__(self) -> str:
        return f"rev{self.revision_number} of item {self.source_item_id}"


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

    target_age_seconds = models.PositiveIntegerField(
        null=True,
        blank=True,
        default=None,
        help_text="Scheduled milestone bucket (e.g. 600, 1800, 3600). "
        "NULL = ad-hoc/immediate capture, not a milestone.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_item", "captured_at"], name="uniq_snap_item_captured"
            ),
            # Milestone idempotency: same item + same scheduled milestone at most once.
            # NULL target_age_seconds rows are ad-hoc and excluded from this constraint
            # (MySQL treats NULLs as distinct, SQLite too).
            models.UniqueConstraint(
                fields=["source_item", "target_age_seconds"],
                name="uniq_snap_item_target_age",
            ),
        ]
        indexes = [
            models.Index(fields=["source_item", "post_age_seconds"]),
            models.Index(fields=["source_item", "target_age_seconds"]),
        ]

    def save(self, *args, **kwargs):
        if self.post_age_seconds is None and self.captured_at:
            published = getattr(self.source_item, "published_at", None)
            if published:
                self.post_age_seconds = max(0, int((self.captured_at - published).total_seconds()))
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"snap {self.source_item_id}@{self.captured_at:%H:%M}"
