"""Source attribution service: truth-grounded attribution blocks from database records."""

from __future__ import annotations

import html

from apps.publishing.branding import BrandingService
from apps.stories.models import Story


class SourceAttributionService:
    """Builds truth-grounded source attribution blocks from verified story members."""

    @classmethod
    def render_attribution_block(
        cls,
        story: Story,
        *,
        include_links: bool = True,
    ) -> str:
        """Render deterministic Telegram HTML source block."""
        cfg = BrandingService.get_settings()
        if not cfg.get("show_source", True):
            return ""

        primary = story.primary_item
        members = list(
            story.memberships.filter(is_current=True).select_related("source_item__source")
        )

        source_name = "نامشخص"
        source_url = ""

        if primary and primary.source:
            source_name = primary.source.name or primary.source.identifier
            source_url = primary.canonical_url or primary.source.url
        elif members:
            first_src = members[0].source_item.source
            source_name = first_src.name or first_src.identifier
            source_url = members[0].source_item.canonical_url or first_src.url

        # Format source name and safe link
        safe_name = html.escape(source_name)
        if (
            include_links
            and cfg.get("show_source_link", True)
            and source_url
            and source_url.startswith("http")
        ):
            safe_url = html.escape(source_url, quote=True)
            source_display = f'<a href="{safe_url}">{safe_name}</a>'
        else:
            source_display = safe_name

        lines = [f"<b>منبع:</b> {source_display}"]

        # Independent confirmation count
        indep_count = story.independent_source_count
        if indep_count >= 2:
            lines.append(f"<b>تأیید مستقل:</b> {indep_count} منبع")

        # Update time
        if cfg.get("show_update_time", True) and story.latest_source_update_at:
            time_str = story.latest_source_update_at.strftime("%H:%M UTC")
            lines.append(f"<b>آخرین بروزرسانی:</b> {time_str}")

        return "\n".join(lines)
