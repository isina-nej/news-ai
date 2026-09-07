"""Deterministic text normalization service.

Preserves raw_text intact while producing a clean, normalized representation
for deduplication and NLP. Strips HTML tags, cleans Persian/Arabic, collapses whitespace.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata

NORMALIZATION_VERSION = "v1"

# Safe Persian/Arabic character translations
CHAR_MAP = {
    ord("ي"): "ی",  # Arabic Yeh -> Persian Yeh
    ord("ى"): "ی",  # Arabic Alif Maksura -> Persian Yeh
    ord("ك"): "ک",  # Arabic Kaf -> Persian Keheh
    ord("ۀ"): "هٔ",  # Heh with isolated yeh -> Heh + Hamza
}

# Regex to detect HTML tags
RE_HTML_DETECT = re.compile(r"<[a-zA-Z\/][^>]*>")
RE_HTML_BREAKS = re.compile(r"(?i)<br\s*/?>|</p>|</div>|</li>")
RE_HTML_TAGS = re.compile(r"<[^>]+>")

# Regex to remove Tatweel/Kashida (ـ)
RE_TATWEEL = re.compile(r"ـ+")

# Regex for control characters (keep \n, \t, \r)
RE_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# ZWNJ (‌) cleanup: collapse multiples, strip if next to whitespace
RE_MULTI_ZWNJ = re.compile(r"‌{2,}")
RE_ZWNJ_WHITESPACE = re.compile(r"(\s+‌|‌\s+)")

# Invisible direction marks (‎ LTR, ‏ RTL)
RE_DIR_MARKS = re.compile(r"[‎‏‪-‮]")

# Whitespace normalization
RE_SPACES = re.compile(r"[^\S\n\r]+")  # Horizontal spaces and tabs
RE_NEWLINES = re.compile(r"\r\n|\r")
RE_MULTI_NEWLINES = re.compile(r"\n{3,}")


def strip_html_tags(text: str) -> str:
    """Convert HTML paragraphs/breaks to newlines and strip remaining markup."""
    if not text or not RE_HTML_DETECT.search(text):
        return text
    # Convert block endings and breaks to newlines
    converted = RE_HTML_BREAKS.sub("\n", text)
    # Strip all remaining HTML tags
    stripped = RE_HTML_TAGS.sub("", converted)
    # Unescape HTML entities (&amp; -> &, &quot; -> ", etc.)
    return html.unescape(stripped)


class NormalizationService:
    """Service producing standardized, deterministic normalized text."""

    version = NORMALIZATION_VERSION

    def normalize(self, text: str) -> str:
        """Produce clean normalized plain text from raw source text.

        Invariants:
        - Never mutates raw input or strips emojis/hashtags/punctuation.
        - Strips HTML markup cleanly if present (e.g. from RSS descriptions).
        - Unicode NFKC normalized.
        - Persian/Arabic Yeh/Kaf safely standardized.
        - Tatweel and redundant ZWNJs cleaned.
        - Horizontal and vertical whitespace collapsed cleanly.
        """
        if not text or not isinstance(text, str):
            return ""

        # 1. Clean HTML tags if present
        norm = strip_html_tags(text)

        # 2. Unicode NFKC normalization
        norm = unicodedata.normalize("NFKC", norm)

        # 3. Strip non-printable control characters
        norm = RE_CONTROL_CHARS.sub("", norm)

        # 4. Strip directional marks
        norm = RE_DIR_MARKS.sub("", norm)

        # 5. Safe Persian/Arabic character translations
        norm = norm.translate(CHAR_MAP)

        # 6. Remove Tatweel (Kashida)
        norm = RE_TATWEEL.sub("", norm)

        # 7. Clean ZWNJ
        norm = RE_MULTI_ZWNJ.sub("‌", norm)
        norm = RE_ZWNJ_WHITESPACE.sub(" ", norm)

        # 8. Normalize line endings to \n
        norm = RE_NEWLINES.sub("\n", norm)

        # 9. Normalize horizontal spaces (tabs, repeated spaces)
        norm = RE_SPACES.sub(" ", norm)

        # 10. Clean up each line and collapse 3+ newlines to 2
        lines = [line.strip() for line in norm.split("\n")]
        norm = "\n".join(lines)
        norm = RE_MULTI_NEWLINES.sub("\n\n", norm)

        return norm.strip()

    @staticmethod
    def compute_hash(text: str) -> str:
        """Compute SHA-256 hex digest of text."""
        if not text:
            return ""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def normalize_and_hash(self, raw_text: str) -> tuple[str, str, str]:
        """Convenience returning (normalized_text, raw_hash, normalized_hash)."""
        raw_hash = self.compute_hash(raw_text)
        normalized = self.normalize(raw_text)
        normalized_hash = self.compute_hash(normalized)
        return normalized, raw_hash, normalized_hash


normalization_service = NormalizationService()
