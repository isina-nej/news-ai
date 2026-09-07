from django.contrib import admin

from .models import Publication, PublicationEngagementSnapshot


@admin.register(Publication)
class PublicationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "story",
        "channel",
        "status",
        "publication_version",
        "update_type",
        "scheduled_at",
        "published_at",
        "attempt_count",
    )
    list_filter = ("status", "update_type", "channel")
    search_fields = ("channel", "headline", "idempotency_key", "external_message_id")
    readonly_fields = (
        "created_at",
        "updated_at",
        "content_hash",
        "idempotency_key",
        "external_message_id",
        "published_at",
        "attempt_count",
    )
    exclude = ("content",)


@admin.register(PublicationEngagementSnapshot)
class PublicationEngagementSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "publication",
        "captured_at",
        "post_age_seconds",
        "views",
        "forwards",
        "reactions",
        "replies",
    )
    readonly_fields = ("created_at", "updated_at")
    exclude = ("raw_metrics",)
