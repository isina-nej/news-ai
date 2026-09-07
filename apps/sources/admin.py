"""Django Admin configuration for Sources and FetchRuns."""

from __future__ import annotations

from django.contrib import admin

from .models import FetchRun, Source


class FetchRunInline(admin.TabularInline):
    model = FetchRun
    extra = 0
    can_delete = False
    readonly_fields = (
        "started_at",
        "finished_at",
        "status",
        "http_status",
        "fetched_count",
        "created_count",
        "duplicate_count",
        "duration_ms",
        "error_type",
    )
    exclude = ("error_message", "correlation_id")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "platform",
        "adapter_type_display",
        "enabled",
        "priority",
        "trust_score",
        "consecutive_failures",
        "last_success_at",
        "last_failure_at",
    )
    list_filter = ("platform", "enabled")
    search_fields = ("name", "identifier")
    readonly_fields = ("created_at", "updated_at", "last_success_at", "last_failure_at")
    inlines = [FetchRunInline]

    @admin.display(description="adapter")
    def adapter_type_display(self, obj: Source) -> str:
        return str(obj.configuration.get("adapter_type") or obj.platform)


@admin.register(FetchRun)
class FetchRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source",
        "started_at",
        "status",
        "http_status",
        "fetched_count",
        "created_count",
        "duplicate_count",
        "duration_ms",
        "error_type",
    )
    list_filter = ("status", "source__platform")
    search_fields = ("source__name", "correlation_id", "error_type")
    readonly_fields = (
        "source",
        "started_at",
        "finished_at",
        "status",
        "http_status",
        "fetched_count",
        "created_count",
        "duplicate_count",
        "rejected_count",
        "error_type",
        "error_message",
        "duration_ms",
        "correlation_id",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
