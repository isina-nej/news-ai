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
- `worker`: Celery worker for background ingestion tasks
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
