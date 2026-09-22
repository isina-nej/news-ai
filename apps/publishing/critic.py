"""DraftCriticService: post-generation quality gate, Persian editorial cleanup, and repair."""

from __future__ import annotations

import re
from typing import Any

from apps.ai.service import run_structured_task

FILLER_PHRASES = [
    r"لازم به ذکر است(?: که)?",
    r"شایان ذکر است(?: که)?",
    r"در دنیای امروز",
    r"بدون شک",
    r"همانطور که می‌دانید",
]


def clean_persian_text(text: str) -> str:
    """Deterministic Persian editorial cleanup: filler removal, spacing and ZWNJ standardization."""
    cleaned = (text or "").strip()
    for pattern in FILLER_PHRASES:
        cleaned = re.sub(pattern, "", cleaned)

    # Standardize spaces before punctuation
    cleaned = re.sub(r"\s+([.،؛!؟])", r"\1", cleaned)
    # Remove multiple spaces
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    # Normalize multiple newlines
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


class DraftCriticService:
    """Quality review service for AI-generated drafts."""

    @classmethod
    def review_draft(
        cls,
        *,
        headline: str,
        lead: str,
        body_points: list[str],
        evidence_summary: str,
        provider=None,
    ) -> dict[str, Any]:
        """Runs the draft critic task and returns validation outcome and repaired text."""
        evidence_payload = {
            "headline": headline,
            "lead": lead,
            "body_points": body_points,
            "evidence": evidence_summary,
        }
        import json

        ev_json = json.dumps(evidence_payload, ensure_ascii=False)[:10000]

        result = run_structured_task(
            task="draft_critic",
            evidence=evidence_payload,
            prompt_replacements={"__EVIDENCE__": ev_json},
            provider=provider,
        )
        payload = result["payload"]
        severity = payload.get("severity", "pass")
        is_approved = bool(payload.get("is_approved", True)) and severity != "reject"

        # Apply deterministic Persian cleanups
        cleaned_lead = clean_persian_text(lead)
        cleaned_points = [clean_persian_text(p) for p in body_points if clean_persian_text(p)]

        return {
            "is_approved": is_approved,
            "severity": severity,
            "issue_codes": payload.get("issue_codes", []),
            "unsupported_claims": payload.get("unsupported_claims", []),
            "persian_quality_score": payload.get("persian_quality_score", 1.0),
            "cleaned_lead": cleaned_lead,
            "cleaned_body_points": cleaned_points,
            "repair_instructions": payload.get("repair_instructions", ""),
        }
