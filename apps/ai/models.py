"""Phase 5: AI task ledger and audited call logs."""

from __future__ import annotations

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel

STATE_CHOICES = (
    ("pending", "Pending"),
    ("processing", "Processing"),
    ("done", "Done"),
    ("failed", "Failed"),
)

STATUS_CHOICES = (
    ("success", "Success"),
    ("cached", "Cached"),
    ("failure", "Failure"),
    ("timeout", "Timeout"),
    ("invalid_response", "Invalid Response"),
)

AI_VERSION = "ai-v1"


class AITask(TimeStampedModel):
    story = models.ForeignKey("stories.Story", on_delete=models.CASCADE, related_name="ai_tasks")
    task = models.CharField(
        max_length=32,
        help_text="e.g. topic_classification, news_value, conflict_detection, "
        "clustering_judge, material_update_detection, post_draft.",
    )
    provider = models.CharField(max_length=32, default="fake")
    model = models.CharField(max_length=128, blank=True, default="")
    prompt_version = models.CharField(max_length=32, blank=True, default="")
    input_hash = models.CharField(max_length=64, db_index=True)
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default="pending", db_index=True)
    error_type = models.CharField(max_length=64, blank=True, default="")
    attempts = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        indexes = [
            models.Index(fields=["story", "task"]),
            models.Index(fields=["state", "created_at"]),
            models.Index(fields=["task", "state"]),
        ]

    def __str__(self) -> str:
        return f"{self.task} story={self.story_id} [{self.state}]"


class AICallLog(models.Model):
    """One audited row per provider call. Never stores prompts/raw source text."""

    provider = models.CharField(max_length=32, db_index=True)
    model = models.CharField(max_length=128, blank=True, default="")
    task = models.CharField(max_length=32, db_index=True)
    prompt_version = models.CharField(max_length=32, blank=True, default="")
    input_hash = models.CharField(max_length=64, db_index=True)
    latency_ms = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cached = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="success")
    error_type = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["task", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["provider", "model"]),
        ]

    def __str__(self) -> str:
        return f"{self.task} {self.model} [{self.status}]"


class AIResultCache(models.Model):
    task = models.CharField(max_length=32, db_index=True)
    prompt_version = models.CharField(max_length=32, blank=True, default="")
    model = models.CharField(max_length=128, blank=True, default="")
    input_hash = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField(default=dict)
    algorithm_version = models.CharField(max_length=32, default=AI_VERSION)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["task", "prompt_version", "model", "input_hash"],
                name="uniq_ai_cache_task_prompt_model_input",
            ),
        ]
        indexes = [
            models.Index(fields=["input_hash"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.task} {self.model}@{self.input_hash[:8]}"


class TopicClassification(TimeStampedModel):
    story = models.OneToOneField(
        "stories.Story", on_delete=models.CASCADE, related_name="topic_classification"
    )
    topic = models.ForeignKey(
        "ops.Topic", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    subtopic = models.ForeignKey(
        "ops.Subtopic", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    candidate_topic = models.CharField(max_length=128, blank=True, default="")
    confidence = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    reason_codes = models.JSONField(default=list, blank=True)
    provider = models.CharField(max_length=32, default="fake")
    model = models.CharField(max_length=128, blank=True, default="")
    prompt_version = models.CharField(max_length=32, blank=True, default="")
    algorithm_version = models.CharField(max_length=32, default=AI_VERSION)

    def __str__(self) -> str:
        return f"topic story={self.story_id}"


class StoryNewsValue(TimeStampedModel):
    story = models.OneToOneField(
        "stories.Story", on_delete=models.CASCADE, related_name="news_value"
    )
    importance = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    utility = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    impact = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    novelty = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    urgency = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    credibility = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    component_detail = models.JSONField(default=dict, blank=True)
    provider = models.CharField(max_length=32, default="fake")
    model = models.CharField(max_length=128, blank=True, default="")
    prompt_version = models.CharField(max_length=32, blank=True, default="")
    algorithm_version = models.CharField(max_length=32, default=AI_VERSION)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(importance__gte=0)
                    & Q(importance__lte=1)
                    & Q(utility__gte=0)
                    & Q(utility__lte=1)
                    & Q(impact__gte=0)
                    & Q(impact__lte=1)
                    & Q(novelty__gte=0)
                    & Q(novelty__lte=1)
                    & Q(urgency__gte=0)
                    & Q(urgency__lte=1)
                    & Q(credibility__gte=0)
                    & Q(credibility__lte=1)
                ),
                name="chk_newsvalue_components_0_1",
            ),
        ]
        indexes = [models.Index(fields=["algorithm_version"])]

    def __str__(self) -> str:
        return f"news_value story={self.story_id}"


class StoryConflict(TimeStampedModel):
    story = models.OneToOneField("stories.Story", on_delete=models.CASCADE, related_name="conflict")
    has_conflict = models.BooleanField(default=False)
    summary = models.TextField(blank=True, default="")
    confidence = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    reason_codes = models.JSONField(default=list, blank=True)
    provider = models.CharField(max_length=32, default="fake")
    model = models.CharField(max_length=128, blank=True, default="")
    prompt_version = models.CharField(max_length=32, blank=True, default="")
    algorithm_version = models.CharField(max_length=32, default=AI_VERSION)

    def __str__(self) -> str:
        return f"conflict story={self.story_id}={self.has_conflict}"


class ClusteringJudgeDecision(TimeStampedModel):
    source_item = models.ForeignKey(
        "news.SourceItem",
        on_delete=models.CASCADE,
        related_name="ai_judge_decisions",
    )
    candidate_story = models.ForeignKey(
        "stories.Story",
        on_delete=models.CASCADE,
        related_name="ai_judge_decisions",
    )
    decision = models.ForeignKey(
        "stories.ClusteringDecision",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_judge_decisions",
    )
    label = models.CharField(
        max_length=24,
        help_text="SAME_EVENT, MATERIAL_UPDATE, RELATED_BUT_DISTINCT, UNRELATED.",
    )
    confidence = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    applied = models.BooleanField(default=False)
    provider = models.CharField(max_length=32, default="fake")
    model = models.CharField(max_length=128, blank=True, default="")
    prompt_version = models.CharField(max_length=32, blank=True, default="")
    algorithm_version = models.CharField(max_length=32, default=AI_VERSION)
    reason_codes = models.JSONField(default=list, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["candidate_story", "-created_at"]),
            models.Index(fields=["label", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.label} item={self.source_item_id} story={self.candidate_story_id}"


class MaterialUpdateDecision(TimeStampedModel):
    story = models.ForeignKey(
        "stories.Story", on_delete=models.CASCADE, related_name="material_updates"
    )
    source_item = models.ForeignKey(
        "news.SourceItem",
        on_delete=models.CASCADE,
        related_name="material_update_decisions",
    )
    label = models.CharField(
        max_length=24,
        help_text="NO_NEW_INFORMATION, MINOR_UPDATE, MATERIAL_UPDATE, "
        "CORRECTION, MAJOR_BREAKING_UPDATE.",
    )
    confidence = models.DecimalField(
        max_digits=5,
        decimal_places=4,
        validators=[MinValueValidator(0), MaxValueValidator(1)],
    )
    provider = models.CharField(max_length=32, default="fake")
    model = models.CharField(max_length=128, blank=True, default="")
    prompt_version = models.CharField(max_length=32, blank=True, default="")
    algorithm_version = models.CharField(max_length=32, default=AI_VERSION)
    reason_codes = models.JSONField(default=list, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["story", "-created_at"]),
            models.Index(fields=["label", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.label} story={self.story_id} item={self.source_item_id}"
