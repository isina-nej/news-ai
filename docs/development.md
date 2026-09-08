# Development Guide

## Prerequisites

- Docker and Docker Compose
- Python 3.12 (managed via `uv` or system Python)
- MySQL 8.4 and Redis 7 (available via Docker Compose)

## Setup

```bash
# 1. Environment configuration
cp .env.example .env

# 2. Virtual environment with uv
uv venv .venv --python 3.12
source .venv/bin/activate
uv pip install -r pyproject.toml --extra dev

# 3. Database migrations (local SQLite default, or MySQL in Docker)
python manage.py migrate

# 4. Run test suite
pytest -q
```

## Running the Services with Docker Compose

```bash
docker compose up --build
```

Services started:
- `web`: Django development server at `http://localhost:8000`
- `worker`: Celery worker for background ingestion tasks (`-Q celery`)
- `telegram-worker`: dedicated Telegram queue worker (`-Q telegram -c 1`)
- `beat`: Celery Beat scheduler for periodic source fetching
- `mysql`: MySQL 8.4 database on port 3306
- `redis`: Redis cache & task queue on port 6379
- `rsshub`: Self-hosted RSSHub service on port 1200

## Code Quality & Linters

```bash
# Linting
ruff check .
ruff check . --fix

# Formatting
ruff format --check .
ruff format .

# Django system checks
python manage.py check

# Migration consistency
python manage.py makemigrations --check --dry-run
```

## Running Web Ingestion Manually

To trigger a source fetch manually from Django shell:
```python
from apps.sources.services.fetcher import source_fetch_service

result = source_fetch_service.fetch_source(source_id=1)
print(result)
```
Or dispatching via Celery task:
```python
from apps.sources.tasks import fetch_source_task

fetch_source_task.delay(source_id=1)
```

## Running Telegram Ingestion Manually

Telegram ingestion is read-only and runs on the dedicated `telegram` queue:

```bash
# 1. Verify the user session (prints identity only, never secrets)
python manage.py telegram_check_session

# 2. Direct service call (synchronous, for debugging)
python manage.py shell -c "
from apps.sources.services.telegram_ingest import telegram_ingest_service
print(telegram_ingest_service.fetch_source(source_id=1))
"

# 3. Via Celery (routes to the telegram queue automatically)
python manage.py shell -c "
from apps.sources.tasks import fetch_telegram_source_task
fetch_telegram_source_task.delay(source_id=1)
"
```

Start the isolated worker with concurrency 1 per account:

```bash
celery -A config.celery worker -l info -Q telegram -c 1
```

See `docs/telegram-ingestion.md` for env setup, cursor semantics, edits,
engagement snapshots, FloodWait handling and troubleshooting.
