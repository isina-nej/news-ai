from django.contrib import admin

from .models import (
    AudiencePreference,
    AuditLog,
    DynamicSetting,
    FeatureFlag,
    Subtopic,
    Topic,
)


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "enabled", "weight", "updated_at")
    list_filter = ("enabled",)
    search_fields = ("slug", "name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Subtopic)
class SubtopicAdmin(admin.ModelAdmin):
    list_display = ("slug", "name", "topic", "enabled", "weight")
    list_filter = ("enabled", "topic")
    search_fields = ("slug", "name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(DynamicSetting)
class DynamicSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "updated_at")
    search_fields = ("key",)
    readonly_fields = ("updated_at",)
    exclude = ("value",)


@admin.register(FeatureFlag)
class FeatureFlagAdmin(admin.ModelAdmin):
    list_display = ("key", "enabled", "updated_at")
    list_filter = ("enabled",)
    search_fields = ("key",)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "actor", "action", "entity_type", "entity_id")
    list_filter = ("action", "entity_type")
    search_fields = ("actor", "action", "entity_id")
    readonly_fields = ("actor", "action", "entity_type", "entity_id", "detail", "created_at")
    exclude = ("detail",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AudiencePreference)
class AudiencePreferenceAdmin(admin.ModelAdmin):
    list_display = ("feature", "value", "sample_count", "confidence", "updated_at")
    list_filter = ("feature",)
    search_fields = ("feature",)
    readonly_fields = ("context_hash", "created_at", "updated_at")
    exclude = ("context",)
