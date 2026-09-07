from django.contrib import admin

from . import metrics as metric_registry
from .models import DecisionLog, RankingWeight, ScoreRecord, SourceBaseline


@admin.register(RankingWeight)
class RankingWeightAdmin(admin.ModelAdmin):
    list_display = ("key", "value", "enabled", "updated_at")
    list_filter = ("enabled",)
    search_fields = ("key",)


@admin.register(ScoreRecord)
class ScoreRecordAdmin(admin.ModelAdmin):
    list_display = (
        "story",
        "algorithm_version",
        "news_value",
        "audience_fit",
        "momentum",
        "final_score",
        "created_at",
    )
    list_filter = ("algorithm_version",)
    readonly_fields = ("created_at", "updated_at")
    exclude = ("breakdown",)


@admin.register(SourceBaseline)
class SourceBaselineAdmin(admin.ModelAdmin):
    list_display = (
        "source",
        "platform",
        "metric",
        "is_derived",
        "content_type",
        "age_bucket_minutes",
        "sample_count",
        "p50",
        "confidence",
    )
    list_filter = ("platform", "content_type")
    readonly_fields = ("context_hash", "created_at", "updated_at")

    @admin.display(boolean=True, description="derived")
    def is_derived(self, obj: SourceBaseline) -> bool:
        return metric_registry.is_derived_metric(obj.metric)


@admin.register(DecisionLog)
class DecisionLogAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "decision_type",
        "story",
        "selected_action",
        "algorithm_version",
        "is_exploration",
        "predicted_reward",
        "actual_reward",
    )
    list_filter = ("decision_type", "algorithm_version", "is_exploration")
    readonly_fields = (
        "story",
        "publication",
        "algorithm_version",
        "decision_type",
        "selected_action",
        "is_exploration",
        "exploration_probability",
        "predicted_reward",
        "actual_reward",
        "reward_calculated_at",
        "created_at",
    )
    exclude = ("feature_snapshot", "score_breakdown", "action_detail")
