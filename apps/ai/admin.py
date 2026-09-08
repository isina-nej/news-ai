from django.contrib import admin

from apps.ai.models import (
    AICallLog,
    AIResultCache,
    AITask,
    ClusteringJudgeDecision,
    MaterialUpdateDecision,
    StoryConflict,
    StoryNewsValue,
    TopicClassification,
)


@admin.register(AITask)
class AITaskAdmin(admin.ModelAdmin):
    list_display = ("created_at", "task", "story", "state", "provider", "model", "attempts")
    list_filter = ("task", "state", "provider")
    search_fields = ("input_hash",)
    readonly_fields = ("created_at", "updated_at", "last_attempt_at")


@admin.register(AICallLog)
class AICallLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "task", "provider", "model", "status", "cached", "latency_ms")
    list_filter = ("task", "status", "provider", "cached")
    readonly_fields = ("provider", "model", "task", "prompt_version", "created_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AIResultCache)
class AIResultCacheAdmin(admin.ModelAdmin):
    list_display = ("created_at", "task", "model", "prompt_version", "expires_at")
    list_filter = ("task",)
    readonly_fields = ("created_at",)


@admin.register(TopicClassification)
class TopicClassificationAdmin(admin.ModelAdmin):
    list_display = ("updated_at", "story", "topic", "subtopic", "confidence")
    readonly_fields = ("created_at", "updated_at")


@admin.register(StoryNewsValue)
class StoryNewsValueAdmin(admin.ModelAdmin):
    list_display = ("updated_at", "story", "algorithm_version")
    readonly_fields = ("created_at", "updated_at", "component_detail")
    exclude = ("component_detail",)


@admin.register(StoryConflict)
class StoryConflictAdmin(admin.ModelAdmin):
    list_display = ("updated_at", "story", "has_conflict", "confidence")
    list_filter = ("has_conflict",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(ClusteringJudgeDecision)
class ClusteringJudgeDecisionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "label", "source_item", "candidate_story", "applied")
    list_filter = ("label", "applied")
    readonly_fields = ("created_at", "updated_at")


@admin.register(MaterialUpdateDecision)
class MaterialUpdateDecisionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "label", "story", "source_item")
    list_filter = ("label",)
    readonly_fields = ("created_at", "updated_at")
