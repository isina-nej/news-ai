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
    MERGED = "merged", "Merged"


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
    # Representative member for deterministic primary selection.
    primary_item = models.ForeignKey(
        "news.SourceItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="Deterministic representative member (earliest reliable publication).",
    )
    # Merge target: set when this story was merged into another (status=MERGED).
    merged_into = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="merged_stories",
        help_text="Target story after merge. Set only when status is MERGED.",
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
    # Total distinct configured sources observed (raw membership count).
    observed_source_count = models.PositiveIntegerField(
        default=0,
        help_text="Distinct configured sources with current membership. Raw count.",
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
        if self.status == StoryStatus.MERGED and self.merged_into_id is None:
            raise ValidationError({"merged_into": "MERGED stories must set merged_into."})
        if self.status != StoryStatus.MERGED and self.merged_into_id is not None:
            raise ValidationError({"merged_into": "Only MERGED stories may set merged_into."})

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
    # Copy-network classification for independence accounting.
    independence = models.CharField(
        max_length=16,
        choices=(
            ("independent", "Independent"),
            ("likely_copy", "Likely copy/syndication"),
            ("unknown", "Unknown"),
        ),
        default="unknown",
    )
    independence_score = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        null=True,
        blank=True,
        default=None,
        help_text="0..1 confidence that this member is an independent confirmation.",
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
            models.Index(fields=["story", "independence"]),
        ]

    def clean(self) -> None:
        if self.similarity_score is not None and not (0 <= float(self.similarity_score) <= 1):
            raise ValidationError({"similarity_score": "must be in 0..1."})
        if self.independence_score is not None and not (0 <= float(self.independence_score) <= 1):
            raise ValidationError({"independence_score": "must be in 0..1."})

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


class ClusteringDecisionType(models.TextChoices):
    MATCH = "match", "Match to existing story"
    NEW_STORY = "new_story", "Create new story"
    AMBIGUOUS = "ambiguous", "Ambiguous, needs review/AI judge"
    REASSIGN = "reassign", "Reassign item to another story"
    MERGE = "merge", "Merge two stories"


class ClusteringDecision(models.Model):
    """Audit record for every story-assignment decision (replay/backtest/AI-judge eval)."""

    source_item = models.ForeignKey(
        "news.SourceItem", on_delete=models.CASCADE, related_name="clustering_decisions"
    )
    candidate_story = models.ForeignKey(
        Story,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="clustering_decisions",
        help_text="Best candidate considered. NULL when no candidate existed.",
    )
    decision = models.CharField(max_length=16, choices=ClusteringDecisionType.choices)
    match_score = models.DecimalField(
        max_digits=5, decimal_places=4, null=True, blank=True, default=None
    )
    feature_snapshot = models.JSONField(default=dict, blank=True)
    algorithm_version = models.CharField(max_length=32)
    threshold_version = models.CharField(max_length=32, blank=True, default="")
    embedding_model = models.CharField(max_length=128, blank=True, default="")
    embedding_version = models.CharField(max_length=64, blank=True, default="")
    method = models.CharField(
        max_length=32, blank=True, default="", help_text="e.g. lexical/semantic/evidence."
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["source_item", "-created_at"]),
            models.Index(fields=["candidate_story", "-created_at"]),
            models.Index(fields=["algorithm_version", "decision", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.decision} item={self.source_item_id} story={self.candidate_story_id}"


class ItemEmbedding(models.Model):
    """MySQL-side metadata for vector index entries. Qdrant holds vectors, never truth.

    Rebuild of the whole Qdrant collection from MySQL (+ recompute) is always
    possible using these rows. Vectors themselves are NOT stored here.
    """

    STATUS_CHOICES = (
        ("pending", "Pending"),
        ("indexed", "Indexed"),
        ("failed", "Failed"),
        ("stale", "Stale"),
    )

    source_item = models.OneToOneField(
        "news.SourceItem", on_delete=models.CASCADE, related_name="item_embedding"
    )
    content_hash = models.CharField(
        max_length=64,
        help_text="SourceItem.content_hash at embed time; mismatch means STALE.",
    )
    provider = models.CharField(max_length=32, default="fastembed")
    model = models.CharField(max_length=128)
    model_version = models.CharField(max_length=64, default="")
    dimension = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    point_id = models.CharField(
        max_length=64,
        unique=True,
        help_text="Deterministic Qdrant point id (item pk based).",
    )
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "updated_at"]),
            models.Index(fields=["model", "model_version"]),
        ]

    def __str__(self) -> str:
        return f"emb item={self.source_item_id} {self.model} [{self.status}]"


class ItemMinHash(models.Model):
    """Versioned MinHash sketch storage (safe ints, never raw pickle).

    Stores digest as a JSON int list with explicit num_perm + scheme so a
    datasketch 2.x upgrade never silently mis-compares sketches.
    """

    source_item = models.OneToOneField(
        "news.SourceItem", on_delete=models.CASCADE, related_name="item_minhash"
    )
    num_perm = models.PositiveIntegerField()
    scheme = models.CharField(max_length=16, default="affine32")
    digest = models.JSONField(help_text="List of uint32 digest values.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"minhash item={self.source_item_id} perm={self.num_perm} {self.scheme}"
