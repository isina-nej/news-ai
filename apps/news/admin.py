import json

from django.contrib import admin

from .models import EngagementSnapshot, SourceItem, SourceItemRevision


def _trunc(value: str, n: int = 80) -> str:
    return (value[:n] + "…") if value and len(value) > n else (value or "—")


class SourceItemRevisionInline(admin.TabularInline):
    model = SourceItemRevision
    extra = 0
    can_delete = False
    readonly_fields = (
        "revision_number",
        "source_updated_at",
        "observed_at",
        "content_hash",
    )
    exclude = ("title", "raw_text", "normalized_text", "change_metadata")

    def has_add_permission(self, request, obj=None):
        return False


class EngagementSnapshotInline(admin.TabularInline):
    model = EngagementSnapshot
    extra = 0
    can_delete = False
    readonly_fields = (
        "captured_at",
        "target_age_seconds",
        "views",
        "forwards",
        "reactions",
        "replies",
    )

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(SourceItem)
class SourceItemAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source",
        "status",
        "title_short",
        "language",
        "published_at",
        "source_updated_at",
        "revision_count",
        "collected_at",
    )
    list_filter = ("status", "source__platform", "language", "content_type")
    search_fields = ("title", "external_id", "canonical_url")
    readonly_fields = (
        "created_at",
        "updated_at",
        "first_seen_at",
        "source_updated_at",
        "source_deleted_at",
        "url_hash",
        "content_hash",
        "raw_content_hash",
        "payload_size",
    )
    exclude = ("raw_payload",)
    inlines = [SourceItemRevisionInline, EngagementSnapshotInline]

    @admin.display(description="title")
    def title_short(self, obj: SourceItem) -> str:
        return _trunc(obj.title)

    @admin.display(description="payload bytes")
    def payload_size(self, obj: SourceItem) -> int:
        return len(json.dumps(obj.raw_payload or {}, default=str))

    @admin.display(description="revisions")
    def revision_count(self, obj: SourceItem) -> int:
        return obj.revisions.count()


@admin.register(EngagementSnapshot)
class EngagementSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "source_item",
        "captured_at",
        "target_age_seconds",
        "post_age_seconds",
        "views",
        "forwards",
        "shares",
        "reactions",
        "replies",
    )
    list_filter = ("captured_at",)
    readonly_fields = ("created_at", "updated_at")
    exclude = ("raw_metrics",)


@admin.register(SourceItemRevision)
class SourceItemRevisionAdmin(admin.ModelAdmin):
    list_display = (
        "source_item",
        "revision_number",
        "source_updated_at",
        "observed_at",
    )
    list_filter = ()
    search_fields = ("source_item__external_id",)
    readonly_fields = (
        "source_item",
        "revision_number",
        "source_updated_at",
        "observed_at",
        "title",
        "raw_text",
        "normalized_text",
        "content_hash",
        "change_metadata",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
