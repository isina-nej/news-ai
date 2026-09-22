"""TelegramRenderer: format-aware, semantic-shortening, whitelist-escaped Telegram HTML rendering."""

from __future__ import annotations

import html
from dataclasses import dataclass

from apps.publishing.attribution import SourceAttributionService
from apps.publishing.branding import BrandingService
from apps.stories.models import Story

MAX_TEXT_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024
RENDERER_TEMPLATE_VERSION = "tg-v2"


@dataclass(frozen=True)
class RenderedTelegramPost:
    format_type: str
    headline: str
    lead: str
    caption: str  # Shortened to fit under 1024 chars for photos
    full_message: str  # Full structured message up to 4096 chars
    parse_mode: str = "HTML"
    template_version: str = RENDERER_TEMPLATE_VERSION


def truncate_at_sentence(text: str, max_chars: int) -> str:
    """Truncate text at the nearest sentence boundary before max_chars without cutting words."""
    if len(text) <= max_chars:
        return text
    target = max_chars - 3
    # Try finding nearest sentence boundary (. or ؟ or !)
    slice_text = text[:target]
    last_punct = max(slice_text.rfind("."), slice_text.rfind("!"), slice_text.rfind("؟"))
    if last_punct > target // 2:
        return slice_text[: last_punct + 1] + " …"
    # Fallback to nearest space
    last_space = slice_text.rfind(" ")
    if last_space > target // 2:
        return slice_text[:last_space] + " …"
    return slice_text + " …"


class TelegramRenderer:
    """Renders professional Telegram-native posts with explicit formats and strict HTML escaping."""

    @classmethod
    def render(
        cls,
        *,
        story: Story,
        headline: str,
        lead: str,
        body_points: list[str] | None = None,
        why_it_matters: str | None = None,
        context: str | None = None,
        update_line: str | None = None,
        format_type: str = "STANDARD",
    ) -> RenderedTelegramPost:
        """Render complete structured Telegram post with caption and full message variants."""
        clean_headline = html.escape((headline or "").strip())
        clean_lead = html.escape((lead or "").strip())
        points = [html.escape(p.strip()) for p in (body_points or []) if p.strip()]

        attr_block = SourceAttributionService.render_attribution_block(story, include_links=True)
        signature = BrandingService.render_signature()

        # Format-specific icon
        icon_map = {
            "BREAKING": "🚨",
            "STANDARD": "📰",
            "QUICK_UPDATE": "🔄",
            "DEVELOPING": "⏳",
            "ANALYSIS": "📊",
            "OFFICIAL_STATEMENT": "📢",
            "FOLLOW_UP": "📌",
        }
        icon = icon_map.get(format_type, "📰")

        # 1. Build Full Structured Message
        parts: list[str] = [f"{icon} <b>{clean_headline}</b>", ""]
        if clean_lead:
            parts.extend([clean_lead, ""])

        if format_type == "BREAKING":
            if update_line:
                parts.extend([f"<i>{html.escape(update_line.strip())}</i>", ""])
            else:
                parts.extend(["<i>جزئیات در حال تکمیل است…</i>", ""])

        elif format_type == "QUICK_UPDATE":
            if update_line:
                parts.extend([f"<b>بروزرسانی:</b> {html.escape(update_line.strip())}", ""])

        elif format_type == "OFFICIAL_STATEMENT":
            if points:
                parts.extend([f"<blockquote>{points[0]}</blockquote>", ""])

        else:
            # STANDARD / ANALYSIS
            if points:
                bullet_lines = "\n".join(f"• {p}" for p in points[:4])
                parts.extend([bullet_lines, ""])

            if why_it_matters and why_it_matters.strip():
                clean_why = html.escape(why_it_matters.strip())
                parts.extend([f"<b>چرا مهم است؟</b>\n{clean_why}", ""])

            if context and context.strip():
                clean_ctx = html.escape(context.strip())
                parts.extend([f"<blockquote>{clean_ctx}</blockquote>", ""])

        if attr_block:
            parts.extend([attr_block, ""])
        if signature:
            parts.append(signature)

        full_message = "\n".join(parts).strip()
        if len(full_message) > MAX_TEXT_LENGTH:
            full_message = truncate_at_sentence(full_message, MAX_TEXT_LENGTH)

        # 2. Build Media Caption (Concise, guaranteed under 1024 chars)
        caption_parts: list[str] = [f"{icon} <b>{clean_headline}</b>", ""]
        if clean_lead:
            # Shorten lead for caption if needed
            short_lead = truncate_at_sentence(clean_lead, 450)
            caption_parts.extend([short_lead, ""])
        if attr_block:
            caption_parts.extend([attr_block, ""])
        if signature:
            caption_parts.append(signature)

        caption = "\n".join(caption_parts).strip()
        if len(caption) > MAX_CAPTION_LENGTH:
            caption = truncate_at_sentence(caption, MAX_CAPTION_LENGTH)

        return RenderedTelegramPost(
            format_type=format_type,
            headline=clean_headline,
            lead=clean_lead,
            caption=caption,
            full_message=full_message,
        )
