from django.contrib import admin

from .models import Story, StoryMembership


class StoryMembershipInline(admin.TabularInline):
    model = StoryMembership
    extra = 0
    readonly_fields = ("added_at", "created_at", "updated_at")
    exclude = ("detail",)


@admin.register(Story)
class StoryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "canonical_title_short",
        "status",
        "language",
        "independent_source_count",
        "first_published_at",
        "latest_update_at",
    )
    list_filter = ("status", "language")
    search_fields = ("canonical_title",)
    readonly_fields = ("created_at", "updated_at", "first_seen_at", "latest_update_at")
    inlines = [StoryMembershipInline]

    @admin.display(description="title")
    def canonical_title_short(self, obj: Story) -> str:
        t = obj.canonical_title or ""
        return (t[:80] + "…") if len(t) > 80 else t
