"""Django Admin configuration for Sources, FetchRuns, checkpoints and tracking."""

from __future__ import annotations

from django.contrib import admin
from django.utils import timezone

from .models import EngagementTrackingState, FetchRun, Source, SourceCheckpoint


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
        "updated_count",
        "duplicate_count",
        "duration_ms",
        "error_type",
    )
    exclude = ("error_message", "correlation_id")

    def has_add_permission(self, request, obj=None):
        return False


class SourceCheckpointInline(admin.TabularInline):
    model = SourceCheckpoint
    extra = 0
    can_delete = False
    readonly_fields = ("adapter", "last_external_id", "last_published_at", "state", "updated_at")

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
        "cooldown_active",
        "last_success_at",
        "last_failure_at",
    )
    list_filter = ("platform", "enabled")
    search_fields = ("name", "identifier", "platform_external_id")
    readonly_fields = (
        "created_at",
        "updated_at",
        "last_success_at",
        "last_failure_at",
        "platform_external_id",
        "cooldown_until",
    )
    inlines = [FetchRunInline, SourceCheckpointInline]

    @admin.display(description="adapter")
    def adapter_type_display(self, obj: Source) -> str:
        return str(obj.configuration.get("adapter_type") or obj.platform)

    @admin.display(description="cooldown?", boolean=True)
    def cooldown_active(self, obj: Source) -> bool:
        return bool(obj.cooldown_until and obj.cooldown_until > timezone.now())


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
        "updated_count",
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
        "updated_count",
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


@admin.register(SourceCheckpoint)
class SourceCheckpointAdmin(admin.ModelAdmin):
    list_display = ("source", "adapter", "last_external_id", "last_published_at", "updated_at")
    list_filter = ("adapter",)
    search_fields = ("source__name", "last_external_id")
    readonly_fields = (
        "source",
        "adapter",
        "last_external_id",
        "last_published_at",
        "state",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(EngagementTrackingState)
class EngagementTrackingStateAdmin(admin.ModelAdmin):
    list_display = (
        "source_item",
        "active",
        "next_target_age_seconds",
        "next_due_at",
        "failure_count",
        "updated_at",
    )
    list_filter = ("active",)
    readonly_fields = (
        "source_item",
        "next_due_at",
        "next_target_age_seconds",
        "active",
        "last_attempt_at",
        "failure_count",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False
