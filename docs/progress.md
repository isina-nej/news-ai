# Project Progress

## Phase 0..4
- Status: Completed previously
- Last SHA before Phase 4.1: `2d3916f`

---

## Phase 4.1 — Clustering Correctness & Real Benchmark
- Status: Completed (offline/CI regression green; real-model download skipped due to sandbox network limits; push pending host credentials)
- SHA: `2a40455`
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

---

## Phase 5 — AI Intelligence Layer
- Status: Completed (offline/CI regression green; live provider calls skipped without external credentials)
- SHA: Pending commit
- CI Run: Pending push (blocked on external GitHub credentials in sandbox)
- Tests: 152 passed, 1 skipped
- Migrations: `ai.0001_initial`
- Important Architecture Decisions:
  - Provider abstraction with default `FakeAIProvider` and optional `OpenAICompatibleProvider`. Domain depends only on the application service.
  - Prompt registry in `apps/ai/prompts/` addressing versioned text templates.
  - Machine outputs validated via Pydantic; malformed responses repaired once, then recorded as `invalid_response`.
  - Audited `AICallLog` records latency, token counts, cache status, model, and sanitized errors. No secrets or raw texts stored.
  - Result caching keyed on `(task, prompt_version, model, input_hash)`.
  - Cheap model handles topic/subtopic mapping; strong model handles judge, conflict, novelty, and draft tasks.
  - Combinatorial credibility blends source trust, independent spread, copy network, conflict penalties, and AI credibility.
  - Ambiguous clustering decisions are judged conservatively; low confidence leaves stories split and reversible.
  - AI tasks ledger tracks `pending/processing/done/failed` asynchronously without blocking ingestion.
- Known Limitations:
  - External live OpenAI/LiteLLM call skipped (no live credentials in sandbox).
- Skipped Live Integrations:
  - Live OpenAI-compatible provider calls.

---

## Phase 6 — Ranking, Baselines, Selection
- Status: Completed (offline/CI regression green)
- SHA: Pending commit
- CI Run: Pending push (blocked on external GitHub credentials in sandbox)
- Tests: 164 passed, 1 skipped
- Migrations: none
- Important Architecture Decisions:
  - Source-relative percentiles against robust `SourceBaseline` (median, percentiles, fallback hierarchy) eliminate source-size bias.
  - Momentum combines `relative_performance`, velocity, acceleration, saturating multi-source spread, and cross-source consistency.
  - `CredibilityGate` holds low credibility (<0.25), unresolved conflicts, and single low-trust source rumors.
  - Freshness uses half-life decay (default 24 hours).
  - Selection applies anti-repeat, topic saturation, repetition penalties, and correction boosts.
  - `ranking_backtest` command replays decisions without live publishing.
- Known Limitations:
  - Baselines need real engagement history before becoming meaningful; fallback uses deterministic defaults.
- Skipped Live Integrations:
  - None required for Phase 6.
