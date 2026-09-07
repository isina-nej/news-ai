from django.contrib import admin

from .models import Source


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "platform",
        "identifier",
        "enabled",
        "priority",
        "trust_score",
        "consecutive_failures",
        "last_success_at",
    )
    list_filter = ("platform", "enabled")
    search_fields = ("name", "identifier")
    readonly_fields = ("created_at", "updated_at", "last_success_at", "last_failure_at")
