"""Stories: one Story per real-world event. All SourceItems kept, linked via membership."""

from __future__ import annotations

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
    latest_update_at = models.DateTimeField(auto_now=True)
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
            models.Index(fields=["status", "-latest_update_at"]),
            models.Index(fields=["primary_topic", "status"]),
            models.Index(fields=["-first_published_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.canonical_title[:60]} [{self.status}]"


class StoryMembership(TimeStampedModel):
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
    detail = models.JSONField(default=dict, blank=True)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["story", "source_item"], name="uniq_membership_story_item"
            ),
        ]
        indexes = [
            models.Index(fields=["story", "is_primary"]),
            models.Index(fields=["source_item"]),
        ]

    def __str__(self) -> str:
        return f"{self.story_id}<-{self.source_item_id} ({self.match_method})"
