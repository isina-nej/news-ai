"""Branding and channel signature configuration."""

from __future__ import annotations

from typing import Any

from django.conf import settings

from apps.ops.models import DynamicSetting

DEFAULT_BRANDING = {
    "channel_name": "NewsAI",
    "channel_username": "@newsai_channel",
    "signature_text": "📰 @newsai_channel",
    "signature_style": "separator",
    "show_brand_signature": True,
    "show_source": True,
    "show_source_link": True,
    "show_update_time": True,
    "max_emojis": 3,
}


class BrandingService:
    """Provides configurable branding and channel signatures."""

    @classmethod
    def get_settings(cls) -> dict[str, Any]:
        cfg = dict(DEFAULT_BRANDING)
        try:
            row = DynamicSetting.objects.filter(key="branding_settings").first()
            if row and isinstance(row.value, dict):
                cfg.update(row.value)
        except Exception:
            pass

        # Fallback to Django settings if available
        if hasattr(settings, "CHANNEL_USERNAME"):
            cfg["channel_username"] = str(settings.CHANNEL_USERNAME)
        if hasattr(settings, "CHANNEL_NAME"):
            cfg["channel_name"] = str(settings.CHANNEL_NAME)

        return cfg

    @classmethod
    def render_signature(cls) -> str:
        cfg = cls.get_settings()
        if not cfg.get("show_brand_signature"):
            return ""
        sig = cfg.get("signature_text") or cfg.get("channel_username") or ""
        return f"────────────\n{sig}".strip()
