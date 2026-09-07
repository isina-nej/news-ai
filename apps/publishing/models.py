"""Publishing: idempotent publications with material-update support.

Idempotency design (accidental duplicates impossible, material updates allowed):
- `idempotency_key` unique: caller-supplied key per publish intent.
  Retry of the same intent MUST reuse the same row (transition FAILED->PUBLISHING),
  never insert a new row.
- `UNIQUE(story, channel, content_hash)`: same rendered payload for the same
  story+channel cannot be inserted twice, even with a different key.
- `UNIQUE(story, channel, publication_version)`: material updates are new rows
  with bumped version + new content_hash + update_type != INITIAL.

content_hash is a fingerprint of the FINAL RENDERED payload
(headline + content + template/style fields), not body alone, and is always
recomputed on save so headline/style changes never go stale.

Invariant for Phase 2 patch:
- Content/style mutation and state transition must not be silently bundled.
  `transition()` only changes `status` (and keeps `content_hash` consistent
  with already-persisted payload). If caller mutated headline/content/etc.
  without saving first, transition raises ValidationError so hash never goes
  stale and content change is explicit.
"""

from __future__ import annotations

import hashlib
import json

from django.core.exceptions import ValidationError
from django.core.validators import MinLengthValidator
from django.db import models

from apps.core.models import TimeStampedModel


class PublicationStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    READY = "ready", "Ready"
    APPROVED = "approved", "Approved"
    SCHEDULED = "scheduled", "Scheduled"
    PUBLISHING = "publishing", "Publishing"
    PUBLISHED = "published", "Published"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"


TERMINAL_STATES = {PublicationStatus.PUBLISHED, PublicationStatus.CANCELLED}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    PublicationStatus.DRAFT: {PublicationStatus.READY, PublicationStatus.CANCELLED},
    PublicationStatus.READY: {PublicationStatus.APPROVED, PublicationStatus.CANCELLED},
    PublicationStatus.APPROVED: {
        PublicationStatus.SCHEDULED,
        PublicationStatus.PUBLISHING,
        PublicationStatus.CANCELLED,
    },
    PublicationStatus.SCHEDULED: {
        PublicationStatus.PUBLISHING,
        PublicationStatus.CANCELLED,
        PublicationStatus.FAILED,
    },
    PublicationStatus.PUBLISHING: {PublicationStatus.PUBLISHED, PublicationStatus.FAILED},
    PublicationStatus.FAILED: {
        PublicationStatus.PUBLISHING,
        PublicationStatus.SCHEDULED,
        PublicationStatus.CANCELLED,
    },
    PublicationStatus.PUBLISHED: set(),
    PublicationStatus.CANCELLED: set(),
}


class UpdateType(models.TextChoices):
    INITIAL = "initial", "Initial publish"
    MATERIAL_UPDATE = "material_update", "Material update"
    CORRECTION = "correction", "Correction"


FINGERPRINT_FIELDS = (
    "headline",
    "content",
    "template_version",
    "headline_style",
    "tone",
    "emoji_level",
    "technical_depth",
)


def compute_publication_payload_hash(pub: Publication) -> str | None:
    """Fingerprint of the final rendered telegram payload.

    Includes every field that changes what the subscriber sees:
    headline, content, template_version, headline_style, tone,
    emoji_level, technical_depth. Returns None for empty drafts
    (both headline and content blank) so NULL-unique never collides.
    """
    headline = (pub.headline or "").strip()
    content = (pub.content or "").strip()
    if not headline and not content:
        return None
    payload = {
        "headline": headline,
        "content": content,
        "template_version": (pub.template_version or "").strip(),
        "headline_style": (pub.headline_style or "").strip(),
        "tone": (pub.tone or "").strip(),
        "emoji_level": pub.emoji_level,
        "technical_depth": pub.technical_depth,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class Publication(TimeStampedModel):
    story = models.ForeignKey(
        "stories.Story", on_delete=models.PROTECT, related_name="publications"
    )
    channel = models.CharField(
        max_length=255, help_text="Destination, e.g. @channel or channel id."
    )
    status = models.CharField(
        max_length=16, choices=PublicationStatus.choices, default=PublicationStatus.DRAFT
    )
    publication_version = models.PositiveIntegerField(default=1)
    update_type = models.CharField(
        max_length=16, choices=UpdateType.choices, default=UpdateType.INITIAL
    )
    headline = models.CharField(max_length=1024, blank=True, default="")
    content = models.TextField(blank=True, default="")
    content_hash = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        default=None,
        editable=False,
        help_text="SHA-256 of final rendered payload, recomputed on every save. "
        "NULL while draft has no visible content; NULLs stay distinct under UNIQUE.",
    )
    template_version = models.CharField(max_length=32, blank=True, default="")
    headline_style = models.CharField(max_length=32, blank=True, default="")
    tone = models.CharField(max_length=32, blank=True, default="")
    emoji_level = models.PositiveSmallIntegerField(null=True, blank=True, default=None)
    technical_depth = models.PositiveSmallIntegerField(null=True, blank=True, default=None)
    scheduled_at = models.DateTimeField(null=True, blank=True, default=None)
    published_at = models.DateTimeField(null=True, blank=True, default=None)
    external_message_id = models.CharField(max_length=128, blank=True, default="")
    idempotency_key = models.CharField(
        max_length=64, unique=True, validators=[MinLengthValidator(1)]
    )
    last_error = models.TextField(blank=True, default="")
    attempt_count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["story", "channel", "publication_version"],
                name="uniq_pub_story_channel_version",
            ),
            models.UniqueConstraint(
                fields=["story", "channel", "content_hash"],
                name="uniq_pub_story_channel_content",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "scheduled_at"]),
            models.Index(fields=["story", "channel"]),
            models.Index(fields=["channel", "-published_at"]),
            models.Index(fields=["status", "-created_at"]),
        ]

    def clean(self) -> None:
        if not self.idempotency_key:
            raise ValidationError({"idempotency_key": "idempotency_key is required."})
        if self.publication_version < 1:
            raise ValidationError({"publication_version": "must be >= 1."})
        if self.update_type == UpdateType.INITIAL and self.publication_version != 1:
            raise ValidationError("INITIAL publications must have version 1.")
        if self.publication_version > 1 and self.update_type == UpdateType.INITIAL:
            raise ValidationError("version > 1 requires MATERIAL_UPDATE or CORRECTION.")

    def save(self, *args, **kwargs):
        if self.content is None:
            self.content = ""
        if self.headline is None:
            self.headline = ""
        # Always recompute so headline/style edits never leave a stale hash.
        self.content_hash = compute_publication_payload_hash(self)
        self.full_clean(exclude=None, validate_unique=False)
        super().save(*args, **kwargs)

    def can_transition(self, target: str) -> bool:
        return target in ALLOWED_TRANSITIONS.get(self.status, set())

    def transition(self, target: str, *, save: bool = True) -> None:
        if not self.can_transition(target):
            raise ValidationError(f"Illegal transition {self.status} -> {target}.")
        # Invariant: do not silently bundle content/style mutation with status change.
        # If caller mutated fingerprint fields without saving, force explicit save first
        # so content_hash never goes stale and intent is unambiguous.
        if self.pk is not None:
            persisted = (
                type(self)
                .objects.filter(pk=self.pk)
                .only(*FINGERPRINT_FIELDS, "content_hash")
                .first()
            )
            if persisted is not None:
                dirty = [f for f in FINGERPRINT_FIELDS if getattr(self, f) != getattr(persisted, f)]
                if dirty:
                    raise ValidationError(
                        f"Cannot transition with unsaved content changes in {dirty}. "
                        "Save content separately before transitioning so content_hash "
                        "stays consistent."
                    )
                # Keep hash consistent with persisted payload (recomputed anyway, but be explicit).
                expected = compute_publication_payload_hash(persisted)
                if self.content_hash != expected:
                    self.content_hash = expected
        self.status = target
        if save:
            # Only status (+ hash consistency) should be written; fingerprint fields
            # are intentionally excluded so they cannot piggyback on a transition.
            self.save(update_fields=["status", "content_hash", "updated_at"])

    def __str__(self) -> str:
        return f"pub {self.story_id}@{self.channel} v{self.publication_version} [{self.status}]"


class PublicationEngagementSnapshot(TimeStampedModel):
    """Own-channel feedback. Separate table, no GenericForeignKey.

    Chosen over reusing EngagementSnapshot via GFK because: typed FKs keep
    referential integrity + queryable indexes, and source-item metrics vs
    own-channel metrics evolve independently. Duplication of a few columns
    is cheaper than losing FK guarantees.
    """

    publication = models.ForeignKey(Publication, on_delete=models.CASCADE, related_name="snapshots")
    captured_at = models.DateTimeField(auto_now_add=True)
    post_age_seconds = models.PositiveIntegerField(null=True, blank=True, default=None)
    views = models.PositiveBigIntegerField(null=True, blank=True, default=None)
    forwards = models.PositiveBigIntegerField(null=True, blank=True, default=None)
    reactions = models.PositiveBigIntegerField(null=True, blank=True, default=None)
    replies = models.PositiveBigIntegerField(null=True, blank=True, default=None)
    raw_metrics = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["publication", "captured_at"], name="uniq_pub_snap_pub_captured"
            ),
        ]
        indexes = [models.Index(fields=["publication", "post_age_seconds"])]

    def __str__(self) -> str:
        return f"pubsnap {self.publication_id}@{self.captured_at:%H:%M}"
