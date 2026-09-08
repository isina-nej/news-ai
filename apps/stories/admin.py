from django.contrib import admin

from .models import (
    ClusteringDecision,
    ItemEmbedding,
    ItemMinHash,
    Story,
    StoryMembership,
)


class StoryMembershipInline(admin.TabularInline):
    model = StoryMembership
    extra = 0
    readonly_fields = ("added_at", "created_at", "updated_at", "current_slot")
    exclude = ("detail",)


class ClusteringDecisionInline(admin.TabularInline):
    model = ClusteringDecision
    fk_name = "candidate_story"
    extra = 0
    can_delete = False
    readonly_fields = (
        "source_item",
        "decision",
        "match_score",
        "algorithm_version",
        "threshold_version",
        "method",
        "created_at",
    )
    exclude = ("feature_snapshot",)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Story)
class StoryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "canonical_title_short",
        "status",
        "language",
        "observed_source_count",
        "independent_source_count",
        "first_published_at",
        "latest_source_update_at",
    )
    list_filter = ("status", "language")
    search_fields = ("canonical_title",)
    readonly_fields = (
        "created_at",
        "updated_at",
        "first_seen_at",
        "latest_source_update_at",
        "observed_source_count",
        "independent_source_count",
    )
    inlines = [StoryMembershipInline, ClusteringDecisionInline]

    @admin.display(description="title")
    def canonical_title_short(self, obj: Story) -> str:
        t = obj.canonical_title or ""
        return (t[:80] + "…") if len(t) > 80 else t

    actions = ["merge_into_primary"]

    @admin.action(description="Merge selected stories into first selected (audit-logged)")
    def merge_into_primary(self, request, queryset):
        from apps.stories.services.merge import merge_stories

        stories = list(queryset.order_by("pk"))
        if len(stories) < 2:
            self.message_user(request, "Select at least two stories to merge.")
            return
        target, rest = stories[0], stories[1:]
        merged = 0
        for source in rest:
            result = merge_stories(
                source_story_id=source.pk,
                target_story_id=target.pk,
                actor=str(request.user),
                reason="admin bulk merge",
            )
            if result.get("status") == "merged":
                merged += 1
        self.message_user(request, f"Merged {merged} storie(s) into #{target.pk}.")


@admin.register(ClusteringDecision)
class ClusteringDecisionAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "decision",
        "source_item",
        "candidate_story",
        "match_score",
        "algorithm_version",
        "method",
    )
    list_filter = ("decision", "algorithm_version", "method")
    readonly_fields = (
        "source_item",
        "candidate_story",
        "decision",
        "match_score",
        "algorithm_version",
        "threshold_version",
        "embedding_model",
        "embedding_version",
        "method",
        "created_at",
    )
    exclude = ("feature_snapshot",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ItemEmbedding)
class ItemEmbeddingAdmin(admin.ModelAdmin):
    list_display = ("source_item", "model", "model_version", "dimension", "status", "updated_at")
    list_filter = ("status", "provider", "model")
    readonly_fields = ("point_id", "content_hash", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False


@admin.register(ItemMinHash)
class ItemMinHashAdmin(admin.ModelAdmin):
    list_display = ("source_item", "num_perm", "scheme", "updated_at")
    list_filter = ("scheme",)
    readonly_fields = ("num_perm", "scheme", "created_at", "updated_at")
    exclude = ("digest",)

    def has_add_permission(self, request):
        return False
