import json

from django.contrib import admin

from .models import EngagementSnapshot, SourceItem


def _trunc(value: str, n: int = 80) -> str:
    return (value[:n] + "…") if value and len(value) > n else (value or "—")


@admin.register(SourceItem)
class SourceItemAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source",
        "status",
        "title_short",
        "language",
        "published_at",
        "collected_at",
    )
    list_filter = ("status", "source__platform", "language", "content_type")
    search_fields = ("title", "external_id", "canonical_url")
    readonly_fields = (
        "created_at",
        "updated_at",
        "first_seen_at",
        "url_hash",
        "content_hash",
        "payload_size",
    )
    exclude = ("raw_payload",)

    @admin.display(description="title")
    def title_short(self, obj: SourceItem) -> str:
        return _trunc(obj.title)

    @admin.display(description="payload bytes")
    def payload_size(self, obj: SourceItem) -> int:
        return len(json.dumps(obj.raw_payload or {}, default=str))


@admin.register(EngagementSnapshot)
class EngagementSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "source_item",
        "captured_at",
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
