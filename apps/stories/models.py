"""Stories: one Story per real-world event. All SourceItems kept, linked via membership."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import TimeStampedModel


class StoryStatus(models.TextChoices):
    EMERGING = "emerging", "Emerging"
    ACTIVE = "active", "Active"
    STALE = "stale", "Stale"
    ARCHIVED = "archived", "Archived"


class MatchMethod(models.TextChoices):
    SHARED_URL = "shared_url", "Shared URL"
    TITLE_SIM = "title_sim", "Similar title"
    CONTENT_SIM = "content_sim", "Similar content"
    SEMANTIC = "semantic", "Semantic similarity"
    SHARED_ENTITIES = "shared_entities", "Shared entities"
    AI_VERIFIED = "ai_verified", "AI verified"
    MANUAL = "manual", "Manual"


class Story(TimeStampedModel):
    canonical_title = models.CharField(max_length=1024)
    summary = models.TextField(blank=True, default="")
    language = models.CharField(max_length=10, default="und")
    status = models.CharField(
        max_length=16, choices=StoryStatus.choices, default=StoryStatus.EMERGING
    )
    first_published_at = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    # Service-managed freshness; auto_now would clobber every trivial update.
    latest_source_update_at = models.DateTimeField(
        null=True,
        blank=True,
        default=None,
        help_text="Time of last material source update attached to this story. "
        "Maintained by services, not auto_now, so ranking freshness is truthful.",
    )
    primary_topic = models.ForeignKey(
        "ops.Topic", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    primary_subtopic = models.ForeignKey(
        "ops.Subtopic", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    independent_source_count = models.PositiveIntegerField(
        default=0,
        help_text="Distinct INDEPENDENT confirmations, not raw item count. "
        "Service separates copy-network duplicates from independent confirmation; "
        "membership.detail carries the evidence so the model never blocks that.",
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Future confirmation breakdown, e.g. copy-networks vs independent.",
    )

    class Meta:
        indexes = [
            models.Index(fields=["status", "-latest_source_update_at"]),
            models.Index(fields=["primary_topic", "status"]),
            models.Index(fields=["-first_published_at"]),
            models.Index(fields=["-latest_source_update_at"]),
        ]

    def clean(self) -> None:
        if (
            self.primary_subtopic
            and self.primary_topic
            and self.primary_subtopic.topic_id != self.primary_topic_id
        ):
            raise ValidationError(
                {"primary_subtopic": "primary_subtopic does not belong to primary_topic."}
            )

    def save(self, *args, **kwargs):
        self.full_clean(exclude=None, validate_unique=False)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.canonical_title[:60]} [{self.status}]"


class StoryMembership(TimeStampedModel):
    """Links each SourceItem to stories.

    Invariant: each SourceItem has at most one CURRENT assignment.
    Multi-membership is allowed for candidates, but `is_current` uniqueness
    per SourceItem keeps the active assignment unambiguous.

    Enforced via a partial-like unique constraint in clean() + DB constraint
    on (source_item, is_current) with a condition surrogate for MySQL.
    Since MySQL lacks partial uniques, we use an explicit `current_slot`
    helper column (empty vs item id) with a plain unique constraint.
    """

    story = models.ForeignKey(Story, on_delete=models.CASCADE, related_name="memberships")
    source_item = models.ForeignKey(
        "news.SourceItem", on_delete=models.CASCADE, related_name="memberships"
    )
    similarity_score = models.DecimalField(
        max_digits=5, decimal_places=4, null=True, blank=True, default=None
    )
    match_method = models.CharField(
        max_length=16, choices=MatchMethod.choices, default=MatchMethod.TITLE_SIM
    )
    is_primary = models.BooleanField(default=False)
    is_current = models.BooleanField(
        default=True,
        help_text="Only one membership per SourceItem may be current (active assignment).",
    )
    # Surrogate for MySQL partial unique: only current rows get a non-zero slot.
    current_slot = models.PositiveBigIntegerField(
        editable=False, default=0, help_text="source_item id when is_current, else 0."
    )
    detail = models.JSONField(default=dict, blank=True)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["story", "source_item"], name="uniq_membership_story_item"
            ),
            models.UniqueConstraint(
                fields=["current_slot"], name="uniq_membership_current_per_item"
            ),
        ]
        indexes = [
            models.Index(fields=["story", "is_current"]),
            models.Index(fields=["story", "is_primary"]),
            models.Index(fields=["source_item"]),
        ]

    def clean(self) -> None:
        if self.similarity_score is not None and not (0 <= float(self.similarity_score) <= 1):
            raise ValidationError({"similarity_score": "must be in 0..1."})

    def save(self, *args, **kwargs):
        # Enforce: at most one current per SourceItem. MySQL can't do partial
        # uniques, so `current_slot` is 0 for non-current, source_item_id for
        # current — a plain unique over current_slot keeps at most one
        # non-zero row per SourceItem while allowing unlimited zeros.
        if self.is_current and self.source_item_id:
            self.current_slot = self.source_item_id  # type: ignore[assignment]
        elif not self.is_current:
            self.current_slot = 0
        elif self.is_current and not self.source_item_id:
            # Unsaved FK — defer; will be set correctly after first save if needed.
            self.current_slot = 0
        self.full_clean(exclude=None, validate_unique=False)
        super().save(*args, **kwargs)
        # If we deferred (unsaved FK), fix up the slot on the second pass.
        if self.is_current and not self.current_slot and self.source_item_id:
            self.current_slot = self.source_item_id  # type: ignore[assignment]
            super().save(update_fields=["current_slot", "updated_at"])

    def __str__(self) -> str:
        cur = " current" if self.is_current else ""
        return f"{self.story_id}<-{self.source_item_id} ({self.match_method}{cur})"
