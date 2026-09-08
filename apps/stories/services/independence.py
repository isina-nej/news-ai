"""Copy-network vs independent-confirmation classification (v1, conservative).

Signals: near-identical normalized text, shared URLs, shared forward origin,
near-simultaneous timestamps, known source relations (opt-in config).
UNKNOWN is a first-class outcome — never forced to INDEPENDENT.
"""

from __future__ import annotations


def classify_membership(
    *,
    item_text: str,
    item_meta: dict,
    forward_origin: dict | None,
    story_texts: list[str],
    story_urls: list[str],
    time_gap_hours: float | None,
) -> tuple[str, float, dict]:
    """Return (label, score, evidence). Label in independent/likely_copy/unknown."""
    evidence: dict = {}
    item_urls = set(item_meta.get("urls", []) or [])
    story_url_set = set(story_urls or [])
    url_shared = bool(item_urls & story_url_set)
    evidence["url_shared"] = url_shared

    fwd = dict(forward_origin or {})
    fwd_present = bool(fwd.get("origin_type") or fwd.get("origin_id") or fwd.get("origin_title"))
    evidence["forward_present"] = fwd_present

    # Near-identical text check (cheap containment both directions).
    norm_item = (item_text or "").strip()
    near_identical = False
    for other in story_texts:
        other = (other or "").strip()
        if not norm_item or not other:
            continue
        shorter, longer = (norm_item, other) if len(norm_item) <= len(other) else (other, norm_item)
        if len(longer) > 0 and len(shorter) / len(longer) >= 0.95:
            near_identical = True
            break
    evidence["near_identical"] = near_identical
    evidence["time_gap_hours"] = time_gap_hours

    if fwd_present:
        return "likely_copy", 0.2, {**evidence, "reason": "forward_origin"}
    if near_identical and (url_shared or (time_gap_hours is not None and time_gap_hours <= 3)):
        return "likely_copy", 0.25, {**evidence, "reason": "near_identical_plus_signal"}
    if near_identical or url_shared:
        return "unknown", 0.5, {**evidence, "reason": "single_copy_signal"}
    return "independent", 0.85, {**evidence, "reason": "paraphrase_or_distinct"}


def aggregate_counts(labels: list[str]) -> tuple[int, int, str]:
    """Return (observed, independent, algorithm_version)."""
    observed = len(labels)
    independent = sum(1 for label in labels if label == "independent")
    return observed, independent, "indep-v1"
