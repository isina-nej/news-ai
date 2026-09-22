"""Information Units & Fact Diffing Service for tracking material story updates."""

from __future__ import annotations

import hashlib
import re

from rapidfuzz import fuzz

from apps.news.services.normalization import normalization_service


def normalize_fact(text: str) -> str:
    """Normalize a single fact string into canonical representation."""
    cleaned = (text or "").strip()
    norm, _, _ = normalization_service.normalize_and_hash(cleaned)
    # Remove punctuation & extra spaces
    norm = re.sub(r"[^\w\s؀-ۿ]", " ", norm)
    return re.sub(r"\s+", " ", norm).strip().lower()


def fact_hash(text: str) -> str:
    """Deterministic hash for a normalized fact string."""
    return hashlib.sha256(normalize_fact(text).encode("utf-8")).hexdigest()[:16]


class FactDiffService:
    """Compares factual information units across story revisions."""

    @classmethod
    def diff_facts(
        cls,
        *,
        prior_facts: list[str],
        current_facts: list[str],
    ) -> dict[str, list[str]]:
        """Identify new, repeated, and changed facts between prior and current observations."""
        if not prior_facts:
            return {
                "new_facts": list(current_facts),
                "repeated_facts": [],
                "changed_facts": [],
            }

        norm_prior = [(f, normalize_fact(f), fact_hash(f)) for f in prior_facts]
        prior_hashes = {h for _, _, h in norm_prior}

        new_facts: list[str] = []
        repeated_facts: list[str] = []
        changed_facts: list[str] = []

        for curr in current_facts:
            c_norm = normalize_fact(curr)
            c_hash = fact_hash(curr)

            # Exact hash match -> repeated
            if c_hash in prior_hashes:
                repeated_facts.append(curr)
                continue

            # Semantic fuzzy similarity check
            is_changed = False
            for _orig_p, p_norm, _ in norm_prior:
                ratio = fuzz.token_sort_ratio(c_norm, p_norm)
                if 75 <= ratio < 98:
                    changed_facts.append(curr)
                    is_changed = True
                    break

            if not is_changed:
                new_facts.append(curr)

        return {
            "new_facts": new_facts,
            "repeated_facts": repeated_facts,
            "changed_facts": changed_facts,
        }
