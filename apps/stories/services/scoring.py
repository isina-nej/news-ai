"""MatchScore fusion: lexical + semantic + temporal + metadata.

Weights are configurable/versioned (settings + DynamicSettings override),
never hardcoded in call sites. Starting point only — tuned via benchmark.
"""

from __future__ import annotations

import re

from django.conf import settings

DEFAULT_WEIGHTS = {
    "semantic": 0.45,
    "lexical": 0.25,
    "time": 0.15,
    "metadata": 0.15,
}

# Small closed sets of decisive antonym/action swaps that flip event meaning
# even when the rest of the sentence is near-identical. Kept tiny on purpose;
# genuinely ambiguous cases stay AMBIGUOUS for the Phase 5 AI judge.
_ANTONYM_PAIRS = frozenset(
    {
        ("passes", "rejects"),
        ("pass", "reject"),
        ("passed", "rejected"),
        ("approve", "reject"),
        ("approved", "rejected"),
        ("win", "lose"),
        ("wins", "loses"),
        ("won", "lost"),
        ("victory", "defeat"),
        ("launch", "delay"),
        ("launches", "delays"),
        ("launched", "delayed"),
        ("cut", "hire"),
        ("cuts", "hires"),
        ("cutting", "hiring"),
        ("raise", "cut"),
        ("raises", "cuts"),
        ("increase", "decrease"),
        ("rise", "fall"),
        ("rises", "falls"),
        ("hits", "falls"),
        ("approve", "delay"),
        ("hold", "raise"),
        ("holds", "raises"),
        ("steady", "raise"),
        ("found", "lost"),
    }
)

_ACTION_WORDS = re.compile(r"[a-z]{3,}")


def _content_words(text: str) -> set[str]:
    return set(_ACTION_WORDS.findall((text or "").lower()))


def _antonym_swap(a_title: str, a_text: str, b_title: str, b_text: str) -> str | None:
    """Detect a decisive action-word swap between two near-identical reports."""
    a_words = _content_words(f"{a_title} {a_text}")
    b_words = _content_words(f"{b_title} {b_text}")
    only_a = a_words - b_words
    only_b = b_words - a_words
    # Both sides must be small diffs (1-3 distinctive words each); otherwise
    # this is a genuinely different story, not a swapped-action pair.
    if not (1 <= len(only_a) <= 3 and 1 <= len(only_b) <= 3):
        return None
    for word_a in only_a:
        for word_b in only_b:
            if (word_a, word_b) in _ANTONYM_PAIRS or (word_b, word_a) in _ANTONYM_PAIRS:
                return f"{word_a}<->{word_b}"
    return None


def scoring_weights() -> dict:
    import logging

    weights = dict(DEFAULT_WEIGHTS)
    try:
        from apps.ops.models import DynamicSetting

        row = DynamicSetting.objects.filter(key="cluster_weights").first()
        if row and isinstance(row.value, dict):
            for key in weights:
                if key in row.value:
                    weights[key] = float(row.value[key])
    except Exception as exc:  # noqa: S110 — DB override is best-effort
        logging.getLogger(__name__).debug("cluster_weights lookup failed: %s", exc)
    total = sum(weights.values()) or 1.0
    return {k: round(v / total, 4) for k, v in weights.items()}


def _avg(*values) -> float:
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return 0.0
    return sum(nums) / len(nums)


def match_score(features: dict, *, weights: dict | None = None) -> tuple[float, dict]:
    """Fuse pair features into a 0..1 MatchScore. Returns (score, components)."""
    weights = weights or scoring_weights()
    lexical = _avg(
        features.get("title_fuzzy_similarity"),
        features.get("token_set_similarity"),
        features.get("minhash_similarity"),
    )
    semantic = float(features.get("semantic_similarity") or 0.0)
    # Cross-language: lexical is unreliable, lean on semantic but cap confidence.
    # Missing language labels ("") mean the detector was unsure — treat like a
    # potential cross-language pair rather than crushing the score to zero.
    language_match = features.get("language_match")
    if language_match == 0.0:
        lexical = lexical * 0.5
        semantic = min(semantic, 0.95)
    elif language_match != 1.0:
        lexical = lexical * 0.75
        semantic = min(semantic, 0.9)
    temporal = float(features.get("time_proximity") or 0.0)
    metadata = _avg(
        features.get("url_overlap"),
        features.get("domain_overlap"),
        features.get("number_overlap"),
        features.get("hashtag_overlap"),
    )
    score = (
        weights["semantic"] * semantic
        + weights["lexical"] * lexical
        + weights["time"] * temporal
        + weights["metadata"] * metadata
    )
    score = round(max(0.0, min(1.0, score)), 4)
    components = {
        "lexical": round(lexical, 4),
        "semantic": round(semantic, 4),
        "temporal": round(temporal, 4),
        "metadata": round(metadata, 4),
        "weights": dict(weights),
    }
    return score, components


def thresholds() -> tuple[float, float, str]:
    import logging

    high = float(getattr(settings, "CLUSTER_HIGH_THRESHOLD", 0.78))
    low = float(getattr(settings, "CLUSTER_LOW_THRESHOLD", 0.52))
    version = str(getattr(settings, "CLUSTER_ALGORITHM_VERSION", "cluster-v1"))
    try:
        from apps.ops.models import DynamicSetting

        row = DynamicSetting.objects.filter(key="cluster_thresholds").first()
        if row and isinstance(row.value, dict):
            high = float(row.value.get("high", high))
            low = float(row.value.get("low", low))
            version = str(row.value.get("version", version))
    except Exception as exc:  # noqa: S110 — DB override is best-effort
        logging.getLogger(__name__).debug("cluster_thresholds lookup failed: %s", exc)
    return high, low, version


def decide(score: float, *, high: float, low: float) -> str:
    if score >= high:
        return "match"
    if score >= low:
        return "ambiguous"
    return "new_story"


def evidence_veto(
    features: dict,
    *,
    item_meta: dict,
    story_meta: dict,
    item_title: str = "",
    item_text: str = "",
    story_title: str = "",
    story_text: str = "",
) -> str | None:
    """Hard vetoes for clearly incompatible events. Returns reason or None.

    Two narrow, deterministic rules (complex cases stay AMBIGUOUS for Phase 5):
    1. conflicting_numbers: both sides carry concrete figures, share none, yet
       look similar (price vs release, Q2 vs Q3, 2.1B vs 4.8B).
    2. antonym_action_swap: near-identical reports differing only in a
       decisive action verb (passes<->rejects, launches<->delays, cuts<->hires).
    """
    item_numbers = set(str(n) for n in (item_meta.get("numbers", []) or []))
    story_numbers = set(str(n) for n in (story_meta.get("numbers", []) or []))
    if item_numbers and story_numbers and not (item_numbers & story_numbers):
        lexical = float(features.get("title_fuzzy_similarity") or 0.0)
        semantic = float(features.get("semantic_similarity") or 0.0)
        # Both sides carry concrete figures but share none, yet look similar:
        # likely same entity, different event (price vs release, Q2 vs Q3).
        if lexical >= 0.55 and semantic >= 0.55:
            return "conflicting_numbers"
    swap = _antonym_swap(item_title, item_text, story_title, story_text)
    if swap is not None:
        lexical = float(features.get("title_fuzzy_similarity") or 0.0)
        if lexical >= 0.55:
            return f"antonym_action_swap:{swap}"
    return None
