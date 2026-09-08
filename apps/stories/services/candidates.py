"""Bounded candidate generation: time window + language + evidence/lexical/vector union.

Never O(N^2). Per item: filter recent stories by time window, union at most
CLUSTER_MAX_CANDIDATES from URL/domain hits, MinHash prefilter and optional
Qdrant nearest neighbors.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.stories.models import Story
from apps.stories.services import lexical as lex


def lookback_hours(*, extended: bool = False) -> int:
    if extended:
        return int(getattr(settings, "CLUSTER_EXTENDED_LOOKBACK_HOURS", 120))
    return int(getattr(settings, "CLUSTER_DEFAULT_LOOKBACK_HOURS", 48))


def max_candidates() -> int:
    return int(getattr(settings, "CLUSTER_MAX_CANDIDATES", 30) or 30)


EXCLUDED_STATUSES = ("merged", "archived", "stale")


def is_candidate_eligible(story, *, now=None, extended: bool = False) -> bool:
    """Central eligibility policy for every candidate source (URL/MinHash/Qdrant/recency).

    Qdrant hits must pass this gate too — the vector index never bypasses
    status or lookback policy.
    """
    if story is None or story.pk is None:
        return False
    if story.status in EXCLUDED_STATUSES:
        return False
    ref = now or timezone.now()
    cutoff = ref - timedelta(hours=lookback_hours(extended=extended))
    ts = story.latest_source_update_at
    if ts is None:
        return False
    try:
        return ts >= cutoff
    except TypeError:
        return False


def recent_stories(*, now=None, extended: bool = False, limit: int = 200):
    now = now or timezone.now()
    cutoff = now - timedelta(hours=lookback_hours(extended=extended))
    return list(
        Story.objects.filter(latest_source_update_at__gte=cutoff)
        .exclude(status__in=list(EXCLUDED_STATUSES))
        .order_by("-latest_source_update_at")[:limit]
    )


def url_evidence_candidates(item_meta: dict, stories: list) -> list:
    urls = set(item_meta.get("urls", []) or [])
    domains = set(item_meta.get("domains", []) or [])
    if not urls and not domains:
        return []
    hits = []
    for story in stories:
        meta = (story.metadata or {}).get("cluster_meta", {}) if story.metadata else {}
        s_urls = set(meta.get("urls", []) or [])
        s_domains = set(meta.get("domains", []) or [])
        if (urls & s_urls) or (domains & s_domains and urls):
            hits.append(story)
    return hits


def minhash_candidates(item_text: str, stories: list, *, threshold: float = 0.5) -> list:
    item_shingles = lex.shingles(item_text)
    if not item_shingles:
        return []
    scored = []
    for story in stories:
        rep = ((story.metadata or {}).get("cluster_rep") or {}) if story.metadata else {}
        story_shingles = set(rep.get("shingles", []) or [])
        if not story_shingles:
            continue
        inter = len(item_shingles & story_shingles)
        union = len(item_shingles | story_shingles)
        jaccard = (inter / union) if union else 0.0
        if jaccard >= threshold:
            scored.append((jaccard, story))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [story for _, story in scored]


def vector_candidates(
    item_vector: list[float] | None,
    *,
    limit: int = 15,
    provider: str | None = None,
    model: str | None = None,
    model_version: str | None = None,
) -> list:
    """Optional Qdrant nearest neighbors. Failure => [] (never break clustering)."""
    if not item_vector:
        return []
    try:
        from apps.stories.models import Story as StoryModel
        from apps.stories.services import vector_store

        hits = vector_store.search_points(
            vector=item_vector,
            limit=limit,
            provider=provider or "",
            model=model or "",
            model_version=model_version or "",
        )
        ids: list[int] = []
        for hit in hits:
            payload = hit.get("payload", {})
            story_id = payload.get("story_id")
            if story_id:
                try:
                    ids.append(int(story_id))
                except (TypeError, ValueError):
                    continue
        if not ids:
            return []
        found = {story.pk: story for story in StoryModel.objects.filter(pk__in=ids)}
        return [found[i] for i in ids if i in found]
    except Exception:
        return []


def union_candidates(*lists: list, limit: int = 30) -> list:
    seen: set[int] = set()
    out: list = []
    for lst in lists:
        for story in lst:
            if story.pk not in seen:
                seen.add(story.pk)
                out.append(story)
            if len(out) >= limit:
                return out
    return out


def generate_candidates(
    *,
    item_text: str,
    item_meta: dict,
    item_vector: list[float] | None = None,
    item_provider: str | None = None,
    item_model: str | None = None,
    item_model_version: str | None = None,
    now=None,
    extended: bool = False,
) -> dict:
    now = now or timezone.now()
    window = recent_stories(now=now, extended=extended)
    url_hits = url_evidence_candidates(item_meta, window)
    mh_hits = minhash_candidates(item_text, window)
    # Qdrant is an auxiliary candidate source, not a source of truth. Query it
    # even when the SQL recency window is empty, then apply the same central gate.
    raw_vector_hits = vector_candidates(
        item_vector,
        provider=item_provider,
        model=item_model,
        model_version=item_model_version,
    )
    recency = window[:10]
    counts = {
        "url_candidate_count": len(url_hits),
        "minhash_candidate_count": len(mh_hits),
        "vector_candidate_count": len(raw_vector_hits),
        "recency_candidate_count": len(recency),
    }
    merged = union_candidates(url_hits, mh_hits, raw_vector_hits, recency, limit=max_candidates())
    eligible = [s for s in merged if is_candidate_eligible(s, now=now, extended=extended)]
    counts["eligible_candidate_count"] = len(eligible)
    return {"candidates": eligible, "counts": counts}


def _empty_counts() -> dict:
    return {
        "url_candidate_count": 0,
        "minhash_candidate_count": 0,
        "vector_candidate_count": 0,
        "recency_candidate_count": 0,
        "eligible_candidate_count": 0,
    }
