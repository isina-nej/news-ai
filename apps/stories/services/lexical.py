"""Cheap lexical similarity features (RapidFuzz) + MinHash sketch helpers.

All scores normalized to 0..1. Pure functions, no ORM side effects.
MinHash uses datasketch 2.x with explicit num_perm + scheme pinning so a
future upgrade cannot silently mis-compare sketches.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from apps.news.services.normalization import normalization_service

_WS = re.compile(r"\s+")


def _tokens(text: str) -> list[str]:
    return [t for t in _WS.split((text or "").strip().lower()) if t]


def shingles(text: str, k: int = 5) -> set[str]:
    toks = _tokens(text)
    if not toks:
        return set()
    if len(toks) < k:
        return {" ".join(toks)}
    return {" ".join(toks[i : i + k]) for i in range(len(toks) - k + 1)}


def title_fuzzy(a_title: str, b_title: str) -> float:
    a, b = (a_title or "").strip(), (b_title or "").strip()
    if not a or not b:
        return 0.0
    return round(fuzz.WRatio(a, b) / 100.0, 4)


def token_set(a_text: str, b_text: str) -> float:
    a, b = (a_text or "").strip(), (b_text or "").strip()
    if not a or not b:
        return 0.0
    return round(fuzz.token_set_ratio(a, b) / 100.0, 4)


def partial(a_text: str, b_text: str) -> float:
    a, b = (a_text or "").strip(), (b_text or "").strip()
    if not a or not b:
        return 0.0
    return round(fuzz.partial_ratio(a, b) / 100.0, 4)


def minhash_for_text(text: str, *, num_perm: int = 128, scheme: str = "affine32"):
    """Build a MinHash sketch over word shingles of normalized text."""
    from datasketch import MinHash

    mh = MinHash(num_perm=num_perm, scheme=scheme)  # type: ignore[call-arg]
    normed = normalization_service.normalize(text or "")
    for shingle in shingles(normed):
        mh.update(shingle.encode("utf-8"))
    return mh


def minhash_jaccard(a, b) -> float:
    try:
        return round(float(a.jaccard(b)), 4)
    except Exception:
        return 0.0


def digest_to_list(mh) -> list[int]:
    return [int(v) for v in mh.digest()]


def minhash_from_digest(values: list[int], *, num_perm: int = 128, scheme: str = "affine32"):
    from datasketch import MinHash

    mh = MinHash(num_perm=num_perm, scheme=scheme)  # type: ignore[call-arg]
    import numpy as np

    mh.hashvalues = np.array([int(v) for v in values], dtype=np.uint64)
    return mh
