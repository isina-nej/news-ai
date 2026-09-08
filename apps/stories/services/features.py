"""Pair feature extraction: SourceItem <-> Story candidate.

Pure functions returning 0..1-normalized features. Later phases add
entity_overlap / topic_overlap / ai_match_score without breaking this shape.
"""

from __future__ import annotations

from datetime import datetime

from apps.stories.services import lexical as lex
from apps.stories.services.representation import extract_metadata

TIME_HALF_LIFE_HOURS = 36.0


def _hours_between(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    try:
        return abs((a - b).total_seconds()) / 3600.0
    except Exception:
        return None


def time_proximity(item_published, story_published) -> float:
    hours = _hours_between(item_published, story_published)
    if hours is None:
        return 0.5
    import math

    return round(float(math.exp(-hours / TIME_HALF_LIFE_HOURS)), 4)


def _overlap(a: list, b: list) -> float:
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    denom = min(len(sa), len(sb))
    if denom == 0:
        return 0.0
    return round(len(sa & sb) / denom, 4)


def extract_pair_features(
    *,
    item_title: str,
    item_text: str,
    item_published,
    item_meta: dict,
    story_title: str,
    story_text: str,
    story_published,
    story_meta: dict,
    story_language: str = "",
    item_language: str = "",
    minhash_sim: float | None = None,
    semantic_sim: float | None = None,
) -> dict:
    title_fuzzy = lex.title_fuzzy(item_title, story_title)
    token_set = lex.token_set(
        f"{item_title} {item_text}"[:4000], f"{story_title} {story_text}"[:4000]
    )
    partial = lex.partial(item_title, story_title)
    time_sim = time_proximity(item_published, story_published)
    url_overlap = _overlap(item_meta.get("urls", []), story_meta.get("urls", []))
    domain_overlap = _overlap(item_meta.get("domains", []), story_meta.get("domains", []))
    number_overlap = _overlap(
        [str(n) for n in item_meta.get("numbers", [])],
        [str(n) for n in story_meta.get("numbers", [])],
    )
    hashtag_overlap = _overlap(item_meta.get("hashtags", []), story_meta.get("hashtags", []))
    if item_language and story_language:
        language_match = 1.0 if item_language == story_language else 0.0
    else:
        language_match = 0.5
    return {
        "title_fuzzy_similarity": title_fuzzy,
        "token_set_similarity": token_set,
        "partial_similarity": partial,
        "minhash_similarity": round(minhash_sim, 4) if minhash_sim is not None else None,
        "semantic_similarity": round(semantic_sim, 4) if semantic_sim is not None else None,
        "time_proximity": time_sim,
        "url_overlap": url_overlap,
        "domain_overlap": domain_overlap,
        "number_overlap": number_overlap,
        "hashtag_overlap": hashtag_overlap,
        "language_match": language_match,
    }


def item_meta_for(title: str, normalized_text: str) -> dict:
    return extract_metadata(title, normalized_text)
