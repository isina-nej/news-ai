"""Headline evaluation: multi-candidate selection with clickbait prevention and format alignment."""

from __future__ import annotations

import re
from typing import Any

CLICKBAIT_PATTERNS = [
    r"باورنکردنی",
    r"شوکه‌کننده",
    r"شوکه کننده",
    r"عجیب ولی واقعی",
    r"فوری و وحشتناک",
    r"حتما بخوانید",
    r"لو رفت",
    r"انفجار خبری",
]


class HeadlineEvaluator:
    """Evaluates headline candidates and selects the most accurate, information-dense option."""

    @classmethod
    def evaluate_candidates(
        cls,
        *,
        direct: str,
        breaking: str,
        contextual: str,
        target_format: str = "STANDARD",
    ) -> tuple[str, dict[str, Any]]:
        """Select best candidate headline. Returns (selected_headline, audit_metadata)."""
        candidates = {
            "DIRECT": (direct or "").strip(),
            "BREAKING": (breaking or "").strip(),
            "CONTEXTUAL": (contextual or "").strip(),
        }

        scores: dict[str, float] = {}
        flags: dict[str, list[str]] = {}

        for style, h in candidates.items():
            score = 1.0
            style_flags: list[str] = []

            # 1. Length penalty
            length = len(h)
            if length < 10:
                score -= 0.50
                style_flags.append("too_short")
            elif length > 120:
                score -= 0.20
                style_flags.append("too_long")

            # 2. Clickbait penalty
            for pattern in CLICKBAIT_PATTERNS:
                if re.search(pattern, h):
                    score -= 0.60
                    style_flags.append(f"clickbait_detected:{pattern}")

            # 3. Format alignment bonus
            if target_format == "BREAKING" and style == "BREAKING":
                score += 0.20
            elif target_format in ("ANALYSIS", "DATA") and style == "CONTEXTUAL":
                score += 0.20
            elif target_format == "STANDARD" and style == "DIRECT":
                score += 0.15

            scores[style] = round(max(0.0, score), 4)
            flags[style] = style_flags

        # Select highest-scoring candidate
        sorted_cands = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        best_style, best_score = sorted_cands[0]
        selected_text = candidates[best_style] or direct or breaking or contextual or "خبر جدید"

        audit = {
            "selected_style": best_style,
            "selected_score": best_score,
            "candidates": candidates,
            "scores": scores,
            "flags": flags,
        }

        return selected_text, audit
