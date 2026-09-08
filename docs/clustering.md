# Story Detection / Clustering (Phase 4)

Pipeline: `SourceItem -> metadata -> candidates -> cheap similarity -> semantic
-> evidence fusion -> decision -> assignment`. No `O(N^2)` global comparison.

## Text representation

`build_clustering_text(title, normalized_text)` = bounded title (500 chars) +
head of body (2000 chars); Telegram posts send full text up to the limit.
Versioned as `ct-v1` (`clustering_text_version` in metadata). Changing the
strategy bumps the version so old embeddings/sketches are visibly stale.

## Language

`detect_language()` (lingua, 8 languages) returns `{language, confidence,
reliable}`. Texts under 20 chars or detector failure yield `und`/0.0 and never
fail ingestion. Source language, detected language and confidence stay
separate; short texts get low confidence by construction.

## Metadata (no LLM)

Deterministic extractors: urls/domains, hashtags, mentions, numbers,
currency values, content length, media presence. Entity/topic AI comes in
Phase 5 behind a stubbed interface — this phase adds no NLP model dependency.

## Candidates (bounded, centrally eligible)

1. Time window: `latest_source_update_at >= now - CLUSTER_DEFAULT_LOOKBACK_HOURS`
   (default 48h; extended 120h available; category-specific windows later).
2. Union of: URL/domain evidence hits + MinHash prefilter + Qdrant nearest
   neighbors + recency tail, capped at `CLUSTER_MAX_CANDIDATES` (default 30).
3. Every source passes `apps.stories.services.candidates.is_candidate_eligible`.
   Vector hits that are missing, merged, archived, stale, or outside lookback are
   filtered after Qdrant lookup. NULL timestamps are rejected.
4. Decision `feature_snapshot.candidate_counts` records URL/MinHash/vector/recency
   and eligible totals.
5. Language mismatch never rejects; it only discounts lexical confidence.

## Similarity features (0..1)

`title_fuzzy_similarity` (RapidFuzz WRatio), `token_set_similarity`,
`partial_similarity`, `minhash_similarity` (datasketch 2.x, num_perm=128,
scheme=affine32 pinned), `semantic_similarity` (cosine, pluggable backend),
`time_proximity` (exp decay, 36h half-life), `url/domain/number/hashtag
overlap`, `language_match`.

## Embedding backends

- `fake` (default in CI): deterministic hash-bucket vectors, no download.
- `fastembed` (production): local ONNX multilingual inference
  (`EMBEDDING_MODEL`, default MiniLM-L12-v2 384d; e5-large supported).
  Every embedding row stores provider/model/version/dimension/input_hash;
  content change marks STALE; model change starts new rows.

## Qdrant (auxiliary only)

MySQL is the source of truth. Qdrant holds vectors with deterministic point
ids (`item-{pk}`) in identity-isolated collections
(`{base}__{model}__d{dimension}__{hash}` for provider/model/version). Writes and
reads require full embedding identity, mismatched payloads are dropped, an
identity mismatch raises instead of silently mixing models, and invalid identity
fails the embedding row without endless retry. `ItemEmbedding(status)` tracks
pending/indexed/failed/stale with a retry task. Qdrant failure never fails
assignment. Full rebuild from MySQL is always possible. Compose runs
`qdrant:v1.19.1` internally (no public port in production) plus an
`intelligence` worker queue and a fastembed model cache volume.

## Scoring, thresholds, vetoes

`MatchScore = 0.45*semantic + 0.25*lexical + 0.15*time + 0.15*metadata`
(starting point; `DynamicSetting(cluster_weights)` overrides, renormalized).
Decisions: `>= high (0.78)` MATCH, `[low, high)` AMBIGUOUS (conservative new
story + decision row for the Phase 5 AI judge), `< low (0.52)` NEW_STORY.
Thresholds versioned (`cluster_thresholds` setting). False merge is treated as
worse than false split. Narrow deterministic vetoes: `conflicting_numbers`
and `antonym_action_swap:<a><-><b>`; everything else complex stays ambiguous.

## Story representation & centroid

Matching uses primary-item text + representative members, never just the
first item. `story.metadata` stores centroid plus identity and membership
evidence (`centroid_provider`, `centroid_model`, `centroid_model_version`,
`centroid_dimension`, `centroid_member_count`, `centroid_member_ids`,
`centroid_updated_at`). Vectors from stale, failed, model-mismatched, or
dimension-mismatched rows never enter the mean. Members are ordered primary
first, then edited/newest first, capped at 8. Tradeoff: capped mean is cheap
and stable; full re-embed of all members on every assignment would be O(n) per
write with no measurable gain at current scale.

## Freshness, sources, primary

`latest_source_update_at = max(published_at, source_updated_at)` across current
members. Telegram edits therefore advance freshness; ordinary saves do not.
`observed_source_count` and `independent_source_count` are distinct configured
sources, not item rows. UNKNOWN contribution policy is DB-configurable and
auditable (`indep-v1+unknown-policy-v1:exclude|include`). Primary selection is
`primary-v1`: items with a publication timestamp first, then earliest
publication, then bounded trust/reliability, then completeness.

## Assignment, merge, reassign

`StoryClusteringService.cluster_item()` runs in a bounded transaction with
`select_for_update` on the candidate story; reruns are idempotent
(`already_clustered` short-circuit). `merge_stories()` moves current
memberships, flips `status=MERGED` + `merged_into` (never hard-deletes),
logs `ClusteringDecision(decision=merge)` + `AuditLog(story.merge)`, and is
idempotent on repeat. `reassign_item()` moves one item with the same audit
trail. Publications/history are untouched (FKs point at stories/items, merge
only retires the source row).

## Counts & copy networks

`observed_source_count` = distinct configured sources with current
membership. `independent_source_count` counts distinct sources with at least one
`independent` member by default. `likely_copy` never counts. `unknown` is
excluded by the conservative DB-overridable policy. Version
`indep-v1+unknown-policy-v1:exclude|include` is stored in metadata.
Forwards remain valuable observations with reduced independence weight — never
double-counted as independent confirmations.

## Benchmark

`scripts/benchmark_clustering.py` reports overall and en/fa/en-fa slices plus
latency/model size. Fake provider is the offline CI regression on
`tests/fixtures/clustering_benchmark.json` (currently 75 pairs). A real run was
attempted with `paraphrase-multilingual-MiniLM-L12-v2`, but the model download
did not complete in this sandbox, so no real-model numbers are reported here.
Do not interpret fake-provider figures as real embedding quality. Priority:
false merge, then precision, then recall; thresholds are not overfit to this
small fixture. `ClusteringDecision.relationship` reserves same/material/related/
unrelated labels for future AI classifiers.

## Observability

Structured logs carry `source_item_id, candidate_count, best_story_id,
best_score, decision, algorithm_version, embedding_model, duration_ms`. Full
text and raw vectors are never logged. Every assignment writes a
`ClusteringDecision` row (feature snapshot + versions) for replay, threshold
tuning and AI-judge evaluation.
