# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Runtime profiles

Python is pinned to 3.12.

- `config.settings.local` is the default for `manage.py`, Celery, and pytest. It uses SQLite, `LocMemCache`, fake embeddings by default, and eager Celery. Use it for fast host-side development, not infrastructure validation.
- `config.settings.docker` keeps the MySQL, Redis, Qdrant, and asynchronous Celery configuration from `base.py`. Docker Compose injects this profile.
- `config.settings.prod` adds startup guards: explicit `ALLOWED_HOSTS`, an internal API token, MySQL, Redis, and Qdrant are required.

## Setup and common commands

```bash
cp .env.example .env
uv venv .venv --python 3.12
source .venv/bin/activate
uv pip install -e . --extra dev
python manage.py migrate
python manage.py runserver
```

Quality gates mirror `.github/workflows/ci.yml`:

```bash
ruff check .
ruff format --check .
python manage.py check
python manage.py makemigrations --check --dry-run
pytest -q -p no:warnings
```

Useful focused test forms:

```bash
pytest -q tests/test_phase8_api.py
pytest -q tests/test_phase8_api.py::test_api_health_endpoint
pytest -q -k api_health
```

Apply automated formatting only when intended:

```bash
ruff check . --fix
ruff format .
pre-commit run --all-files
```

Production-like development uses the complete Compose stack:

```bash
docker compose up --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py system_health
docker compose exec web pytest -q
docker compose down
```

The queue topology in Compose is deliberate:

```bash
celery -A config.celery worker -l info -Q celery
celery -A config.celery worker -l info -Q intelligence -c 2 --prefetch-multiplier 1
celery -A config.celery worker -l info -Q telegram -c 1
celery -A config.celery beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
```

Telegram MTProto work must remain on the single-concurrency `telegram` queue per account. CPU-heavy clustering, embedding, and AI work belongs on `intelligence`.

Safe operational checks:

```bash
python manage.py system_health
python manage.py seed_demo_news
python manage.py run_news_pipeline --dry-run
python manage.py ranking_backtest --days 7 --limit 50
python scripts/benchmark_clustering.py
python manage.py telegram_check_session
```

Do not use `./start.sh` as a routine development bootstrap: it kills matching local processes and schedules `scripts/publish_ai.py` after 30 seconds. That script can send directly to Telegram and bypass the canonical publication audit/idempotency path. Prefer `run_news_pipeline --dry-run` or explicit services/tasks.

## Architecture and data flow

The dependency direction is `external platform -> adapter -> application service -> domain/core`. Celery tasks should remain thin service entry points.

```text
Source / Beat / manual trigger
  -> apps.sources adapters and fetch services
  -> FetchedItem DTOs
  -> apps.news canonicalization, normalization, persistence
  -> SourceItem + revisions + engagement snapshots
  -> apps.stories bounded candidate search and clustering
  -> Story + current StoryMemberships
  -> apps.ai structured analysis
  -> apps.ranking scoring and selection
  -> apps.publishing draft, policy, Telegram send
  -> publication snapshots, normalized reward
  -> apps.ops audience learning / contextual bandit
```

### Domain boundaries

- `apps.sources` owns external acquisition, adapter registration, fetch audits, cursors, cooldowns, Telegram runtime state, and source-level locking. Add source types through `SourceAdapter`/`adapter_registry`, returning ORM-free `FetchedItem` values.
- `apps.news` is the normalized source-item store. `IngestionPersistenceService` is the write boundary for canonical URLs, text normalization, exact dedupe, revisions, and initial engagement observations.
- `apps.stories` groups source items into real-world events. `StoryClusteringService` combines bounded URL, lexical/MinHash, vector, metadata, and recency evidence. Merge/reassignment services preserve history rather than deleting stories.
- `apps.ai` is a provider-agnostic semantic layer. `run_structured_task()` owns prompt loading, model routing, Pydantic validation, cache keys, bounded repair/retries, and call audit. Prompts live in `apps/ai/prompts/`; register a version in the prompt registry and task/schema map instead of embedding prompts in Python.
- `apps.ranking` owns source-relative baselines, normalized features, credibility gating, score records, selection penalties, and `DecisionLog`. `apps.ops` owns taxonomy, DB-overridable settings/flags, scheduler dispatch, learning, and immutable audit records.
- `apps.publishing` owns WHAT/WHEN/HOW policy, grounded drafting, Telegram rendering/sending, publication state transitions, idempotency, and own-channel rewards. `apps.platform_api` exposes the internal Django Ninja API at `/api/v1/`; mutating endpoints write `AuditLog`.

### Cross-cutting invariants

- MySQL is authoritative. Redis is coordination/cache/broker state. Qdrant is an auxiliary, rebuildable vector index; Qdrant failure must not corrupt or block story assignment.
- Same-source exact identity is checked in order: `external_id`, canonical `url_hash`, normalized `content_hash`. Never discard identical cross-source items; they are evidence for propagation and independent confirmation.
- `SourceItem.story` must agree with its single current `StoryMembership`. Change assignment through clustering/merge/reassignment services, which also refresh primary selection, source counts, freshness, independence metadata, and audit rows.
- Distinguish `observed_source_count` from `independent_source_count`. Forwarded/syndicated/near-copy members remain stored but must not become independent confirmations.
- Missing engagement values remain `None`, never zero. Ranking uses source/topic/content/age baselines and 0..1 finite score components rather than raw engagement counts.
- AI is advisory and evidence-bound. Deterministic credibility, clustering, gates, and source attribution remain outside the model. AI failure must leave ingestion and deterministic fallbacks operational.
- `Publication` payload fields determine `content_hash`. Save content/style changes before calling `transition()`; retries reuse the idempotency row. Network sends occur outside open DB transactions.
- Telegram user-session credentials are environment-only. Checkpoints advance only after persistence. All web fetches go through the existing SSRF-safe HTTP path, including redirect and payload-size checks.
- Feature flags use a DB row when present, then settings defaults. Cluster weights/thresholds and independence policy use `DynamicSetting`; ranking family weights use `RankingWeight`. Preserve algorithm, prompt, and template versions in persisted audit data.

## Orchestration details

`config.settings.base.CELERY_BEAT_SCHEDULE` dispatches due source fetches every two minutes, pending clustering every two minutes, ranking refresh hourly, and publication selection every ten minutes. The DB remains the source of truth for work state; periodic dispatchers scan bounded due/recent rows rather than relying on long-lived ETA tasks.

Telegram ingestion has a separate crash-safe path: load checkpoint, fetch incrementally with edit overlap, persist/update revisions and snapshots, then advance the checkpoint. FloodWait applies source and account cooldowns. Engagement milestones are represented by `EngagementTrackingState`, avoiding one ETA task per snapshot.

The internal API is unauthenticated only when `PLATFORM_API_TOKEN` is empty in local development; production refuses that configuration. OpenAPI is exposed by Django Ninja under `/api/v1/`.

## Where to read next

Start with `README.md` and `docs/architecture.md`. Use `docs/sources.md`, `docs/telegram-ingestion.md`, `docs/clustering.md`, `docs/ai.md`, `docs/ranking.md`, `docs/publishing.md`, and `docs/learning.md` for subsystem rationale. Phase ADRs under `docs/decisions/` explain historical trade-offs; current models, services, settings, and tests are authoritative when older phase documentation disagrees.
