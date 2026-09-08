# NewsAI — AI-Powered News Intelligence & Publishing Platform

Production-ready modular news intelligence platform: collect → normalize → cluster → analyze with AI → rank → publish (Telegram & multi-channel).

## Architecture

`External Platform → Adapter → Application Service → Domain / Core`. Core intelligence is decoupled from external platforms and libraries.

- **`apps/sources`**: Source profile registry and ingestion adapters:
  - RSS / Atom feeds (`feedparser`)
  - Self-hosted RSSHub bridge
  - Static Web HTML extraction (`trafilatura`)
  - Telegram user-session ingestion (`kurigram` user-session, dedicated worker queue)
  - Twitter/X session-based read-only ingestion
- **`apps/news`**: `SourceItem` store with strict same-source exact deduplication (`external_id`, `url_hash`, `content_hash`), Unicode NFKC / Persian-Arabic safe text normalization, immutable `SourceItemRevision` history, and `EngagementSnapshot` time-series metrics. Cross-source identical items are preserved for independent multi-source verification.
- **`apps/stories`**: Real-world event clustering (`Story`, `StoryMembership`). Central candidate eligibility, representative vector centroids, truthful freshness tracking, distinct source counts, and deterministic versioned primary selection (`primary-v1`).
- **`apps/ai`**: Pydantic-structured AI layer behind `AIProvider` port. Versioned prompt registry in `apps/ai/prompts/`, response caching, cheap/strong model routing, news value extraction (importance, utility, impact, novelty, urgency, credibility), conflict detection, ambiguous clustering judge, and material update detection.
- **`apps/ranking`**: Source-relative baseline engine (`SourceBaseline`) with median and percentiles to eliminate source-size bias. Weighted composite scoring (`NewsValue`, `AudienceFit`, `Momentum`, `Freshness`), saturating multi-source confirmation curve, and `CredibilityGate` safety checks.
- **`apps/publishing`**: Modular publishing engine with WHAT / WHEN / HOW separation, strict HTML escaping, 4096-char clamping, idempotent retry, and own-channel normalized engagement reward. `ENABLE_AUTO_PUBLISH=false` by default.
- **`apps/ops`**: Taxonomy (`Topic`, `Subtopic`), `DynamicSetting`, `FeatureFlag`, immutable `AuditLog`, `AudienceLearningService` with EWMA and Bayesian smoothing, and guarded `ContextualBanditService`.
- **`apps/platform_api`**: Versioned Django Ninja operations API at `/api/v1/` with OpenAPI documentation, safe pagination, ordering allowlists, and audited manual actions.

---

## Quickstart & Local Bootstrap

### 1. Clone & Environment Setup
```bash
git clone https://github.com/isina-nej/news-ai.git
cd news-ai
cp .env.example .env
```

### 2. Run with Docker Compose (Recommended)
```bash
# Build and launch MySQL, Redis, Qdrant, Web, Workers, Beat
docker compose up --build -d

# Run database migrations
docker compose exec web python manage.py migrate

# Create superuser
docker compose exec web python manage.py createsuperuser
```

### 3. Local Development (uv / virtualenv)
```bash
uv venv .venv --python 3.12
source .venv/bin/activate
uv pip install -e . --extra dev

# Run migrations (local SQLite default)
DJANGO_SETTINGS_MODULE=config.settings.local python manage.py migrate

# Run system health check
python manage.py system_health
```

---

## Operational Commands

### System Health Probe
Verify database, Redis, Qdrant, AI, Telegram, and Twitter status without printing secrets:
```bash
python manage.py system_health
```

### Seed Demo News Dataset
Seed realistic demo sources, items, topics, and snapshots for local verification:
```bash
python manage.py seed_demo_news
```

### Dry-Run Full Intelligence Pipeline
Execute the full end-to-end pipeline (clustering -> AI analysis -> ranking -> selection -> dry-run publish):
```bash
python manage.py run_news_pipeline --dry-run
```

### Ranking & Selection Replay Backtest
Replay ranking decisions on historical stories without performing any live publishing:
```bash
python manage.py ranking_backtest --days 7 --limit 50
```

### Clustering Quality Benchmark
Evaluate pairwise story clustering metrics on benchmark datasets:
```bash
python scripts/benchmark_clustering.py
```

---

## Running Tests & Linters

```bash
# Run complete test suite (207+ automated tests)
pytest -q -p no:warnings

# Run linter checks
ruff check .

# Run format checks
ruff format --check .

# Django system checks & migration consistency
python manage.py check
python manage.py makemigrations --check --dry-run
```

---

## Documentation

- Architecture: `docs/architecture.md`
- Development Guide: `docs/development.md`
- Production Deployment: `docs/deployment.md`
- Security Architecture: `docs/security.md`
- Data Retention Policy: `docs/data-retention.md`
- Source Ingestion: `docs/sources.md`
- Telegram Ingestion: `docs/telegram-ingestion.md`
- Story Clustering: `docs/clustering.md`
- AI Intelligence Layer: `docs/ai.md`
- Ranking & Scoring: `docs/ranking.md`
- Telegram Publishing & Policy: `docs/publishing.md`
- Audience Learning & Contextual Bandit: `docs/learning.md`
- Architectural Decision Records: `docs/decisions/`
