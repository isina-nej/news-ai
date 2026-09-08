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
- SHA: `770eb17`
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
- SHA: `acbbfe3`
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

---

## Phase 7 — Telegram Publishing + Publishing Policy
- Status: Completed (offline/CI regression green; live Bot sending skipped without credentials)
- SHA: `13fdf0f`
- CI Run: Pending push (blocked on external GitHub credentials in sandbox)
- Tests: 173 passed, 1 skipped
- Migrations: none
- Important Architecture Decisions:
  - Publisher abstraction behind `PublisherPort` with `TelegramBotPublisher` and `FakePublisher`.
  - WHAT/WHEN/HOW publishing engine separates action, schedule timing (with breaking news override), and formatting.
  - Auto-publish is strictly off by default (`ENABLE_AUTO_PUBLISH=false`). Dry-run mode tests end-to-end rendering without dispatch.
  - Telegram HTML escaping and 4096 character clamping prevent formatting injection and payload rejection.
  - External Telegram API calls execute outside open database transactions, ensuring failures and retry states are safely recorded.
  - Normalized reward function computes own-channel performance across available signals.
- Known Limitations:
  - Live Telegram bot credentials not present in sandbox; live send skipped.
- Skipped Live Integrations:
  - Live Telegram Bot API delivery.

---

## Phase 8 — API + Admin + Operations
- Status: Completed (offline/CI regression green)
- SHA: `165e542`
- CI Run: Pending push (blocked on external GitHub credentials in sandbox)
- Tests: 182 passed, 1 skipped
- Migrations: none
- Important Architecture Decisions:
  - Django Ninja versioned operations API under `/api/v1/` with automatic OpenAPI docs.
  - Strict ordering allowlists and safe pagination on all collection resources.
  - Manual operational actions (rescore, recluster, merge, reassign, approve, reject, dry-run-publish, trigger-fetch) with complete `AuditLog` logging.
  - Omission of raw payloads and secrets from API response models.
  - Feature flags exposed for runtime observability.
- Known Limitations:
  - None.
- Skipped Live Integrations:
  - None required for Phase 8.

---

## Phase 9 — Audience Learning + Scheduler + Observability
- Status: Completed (offline/CI regression green)
- SHA: `e0b7ffe`
- CI Run: Pending push (blocked on external GitHub credentials in sandbox)
- Tests: 197 passed, 1 skipped
- Migrations: none
- Important Architecture Decisions:
  - `AudienceLearningService` tracks feature preferences independently using EWMA and Bayesian smoothing with recency decay, preventing confounding between topic, style, and timing effects.
  - `ContextualBanditService` implements guarded exploration (`EXPLORATION_RATE=0.05`) restricted strictly to pre-screened safe editorial variants; never bypasses gates or rate limits.
  - `CELERY_BEAT_SCHEDULE` orchestrates source fetching, pending clustering, ranking refreshes, and publication queue.
  - `MetricsRegistry` and structured logging with correlation IDs (`correlation_id`, `job_id`, `story_id`, `fetch_run_id`).
  - Documented retention policies for logs, payloads, snapshots, and decisions.
- Known Limitations:
  - Vowpal Wabbit C++ backend optional; bandit runs statistical expected-performance layer by default.
- Skipped Live Integrations:
  - None required for Phase 9.

---

## Phase 10 — Twitter/X Real Ingestion
- Status: Completed (offline/CI regression green; live Twitter session skipped without credentials)
- SHA: `75d8f82`
- CI Run: Pending push (blocked on external GitHub credentials in sandbox)
- Tests: 204 passed, 1 skipped
- Migrations: none
- Important Architecture Decisions:
  - `TwitterSourceAdapter` implements `SourceAdapter` protocol via injected `TwitterSessionClient`.
  - Credentials in `TWITTER_SESSION` environment variable only; never persisted in DB or logged.
  - Standard `FetchedItem` mapping with complete engagement metrics; unavailable metrics stay `None`.
  - Retweets flagged as non-independent confirmation (`independent_confirmation=False`).
  - Quote tweets ingested as independent items with quoted tweet metadata preserved in `raw_payload`.
  - Checkpointing via `SourceCheckpoint(adapter="twitter")` ensures crash-safe at-least-once ingestion.
  - Fast-failing authentication and rate limits set `cooldown_until` on source without retry storms.
  - Governed by feature flag `ENABLE_TWITTER_SOURCE` (default `False`).
- Known Limitations:
  - Live X session cookies not configured in test environment; live network fetch skipped.
- Skipped Live Integrations:
  - Live Twitter/X session network requests.

---

## Final Hardening Phase
- Status: Completed (all 207 automated tests green, full E2E pipeline verified, production-ready MVP)
- SHA: Pending commit
- CI Run: Pending push (blocked on external GitHub credentials in sandbox)
- Tests: 207 passed, 1 skipped
- Migrations: 0 pending, all up-to-date
- Important Architecture Decisions:
  - End-to-end integration test (`tests/test_final_hardening_e2e.py`) verifies intake, clustering, AI analysis, ranking, selection, publication dry-run, feedback, and audience learning without external credentials.
  - Operational management commands implemented: `seed_demo_news`, `system_health`, `run_news_pipeline --dry-run`, `ranking_backtest`.
  - Production Docker Compose and Dockerfile reviewed and hardened: restart policies set to `unless-stopped`, internal infrastructure unexposed, model cache mounted, and Debian build packages added.
  - Security audit verified: SSRF protection, secret redaction filter, prompt injection defense, and strict HTML output escaping.
- Known Limitations:
  - Live external services (Telegram Bot, Kurigram MTProto, Twitter/X, Live OpenAI API) require operator credentials in production `.env`.
- Skipped Live Integrations:
  - External network calls to real Telegram, Twitter, and OpenAI APIs.
