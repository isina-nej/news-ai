"""FinalPublicationPayload: audited, validated contract between editorial engine and publishers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

MAX_TEXT_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024


@dataclass
class FinalPublicationPayload:
    story_id: int
    format: str
    headline: str
    caption: str
    message: str
    publication_id: int | None = None
    media: dict[str, Any] | None = None
    parse_mode: str = "HTML"
    source_block: str = ""
    signature: str = ""
    fingerprint: str = ""
    template_version: str = "tg-v2"
    algorithm_version: str = "publish-v2"
    extra_metadata: dict[str, Any] = field(default_factory=dict)


def validate_publication_payload(payload: FinalPublicationPayload) -> tuple[bool, str]:
    """Validate that the rendered payload complies with Telegram API and editorial safety limits."""
    # 1. Non-empty content
    if not payload.headline or not payload.headline.strip():
        return False, "empty_headline"
    if not payload.message or not payload.message.strip():
        return False, "empty_message"

    # 2. Length limits
    if len(payload.message) > MAX_TEXT_LENGTH:
        return False, f"message_exceeds_limit:{len(payload.message)}>{MAX_TEXT_LENGTH}"
    if payload.caption and len(payload.caption) > MAX_CAPTION_LENGTH:
        return False, f"caption_exceeds_limit:{len(payload.caption)}>{MAX_CAPTION_LENGTH}"

    # 3. Balanced HTML tags (basic check for <b>, <i>, <u>, <blockquote>, <a>)
    for tag in ("b", "i", "u", "blockquote", "a"):
        open_count = len(re.findall(rf"<{tag}(?: [^>]*)?>", payload.message))
        close_count = len(re.findall(rf"</{tag}>", payload.message))
        if open_count != close_count:
            return False, f"unbalanced_html_tag:{tag}:{open_count}!={close_count}"

    return True, "valid"
