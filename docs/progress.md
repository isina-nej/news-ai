# Project Progress

## Phase 0..4
- Status: Completed previously
- Last SHA before Phase 4.1: `2d3916f`

---

## Phase 4.1 — Clustering Correctness & Real Benchmark
- Status: Completed (offline/CI regression green; real-model download skipped due to sandbox network limits; push pending host credentials)
- SHA: `fabbec8`
- CI Run: Pending push (blocked on external GitHub credentials in sandbox)
- Tests: 136 passed, 1 skipped (`test_telegram_live_smoke` requires credentials)
- Migrations: `stories.0004_clusteringdecision_relationship_and_more`
- Important Architecture Decisions:
  - Central candidate eligibility (`is_candidate_eligible`) gates lookback, merged, archived, and stale statuses across all candidate sources (URL, MinHash, Qdrant, recency).
  - Story centroid built strictly from valid current members (filtering stale, failed, model-mismatched, or dimension-mismatched vectors) and stores full identity metadata.
  - Story freshness uses `max(published_at, source_updated_at)` over valid members so Telegram edits advance freshness truthfully.
  - Observed and independent source counts are strictly distinct configured sources.
  - Primary item selection is deterministic and versioned (`primary-v1`), prioritizing items with publication timestamps and earlier publication over delayed high-trust sources.
  - Qdrant collections are isolated by model identity and dimension hash.
  - `StoryRelationship` enum added for future Phase 5+ classifier readiness.
- Known Limitations:
  - Live embedding download (`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`) timed out in sandbox; benchmark script ran with deterministic `FakeEmbeddingProvider`.
- Skipped Live Integrations:
  - Hugging Face / FastEmbed live download.
