# Phase 4.1 Clustering Correctness

Keep Qdrant and model scores auxiliary. MySQL stays the source of truth, and
false merges remain more expensive than false splits.

## Candidate eligibility

One central gate handles URL, MinHash, Qdrant, and recency candidates. Excluded
statuses are merged, archived, and stale; NULL freshness and outside-lookback
stories are also rejected. Vector search is queried independently and then gated,
so a stale or retired Qdrant hit cannot re-enter a story.

## Centroid and primary

Centroid uses representative current members only, ordered primary first and
otherwise by edit/recency. Stale, failed, model-mismatched, and
dimension-mismatched vectors are rejected. Membership metadata carries identity
plus member IDs for replay. Single valid members legitimately equal their own
vector. Primary stays deterministic under `primary-v1`, with earliest reliable
publication ahead of later-but-more-trusted sources.

## Freshness and counts

Freshness is the maximum of valid publication/edit times, preserving the
published/collected/first-seen/edit distinction. Merge and ordinary saves do not
fabricate freshness. Source counts measure distinct sources; UNKNOWN inclusion
needs an explicit DB setting and remains versioned.

## Qdrant isolation

Collections include provider/model/version/dimension identity; callers must pass
identity; payload mismatches are filtered; a changed identity gets a different
collection. Invalid identity fails the embedding row immediately. MySQL rows can
recompute and rebuild that versioned collection.

## Benchmark honesty

The fixture is intentionally small (currently 75 pairs) and only prevents false
merges. The real MiniLM benchmark could not be completed because the ONNX model
failed to download completely in this sandbox. Thresholds were therefore left
unchanged; do not tune production thresholds from the fake-provider result.
