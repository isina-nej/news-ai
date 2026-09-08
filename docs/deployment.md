# Deployment

## Environments

- Development: `config.settings.local` with SQLite and eager Celery tasks.
- Production: `config.settings.prod` with MySQL 8.4 (utf8mb4), Redis 7, and Qdrant v1.19.1.

## Secrets Management

All secrets are environment-only and never committed to git or logs:

- `DJANGO_SECRET_KEY`
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_STRING`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`
- `TWITTER_SESSION`
- `AI_BASE_URL`, `AI_API_KEY`
- `SESSION_ENCRYPTION_KEY`
- `RSSHUB_ACCESS_KEY`
- `PLATFORM_API_TOKEN`

The log pipeline uses `RedactFilter` and centralized `sanitize_error_message` to remove secrets from errors before persistence.

## Production Compose

- All app workers use `restart: unless-stopped`.
- Internal services (`mysql`, `redis`, `qdrant`) are not exposed publicly; only `web:8000` binds to the host.
- Volumes: `mysql-data`, `redis-data`, `qdrant-data`, `fastembed-cache` (model cache for ONNX inference).
- Worker topology:
  - `worker`: default `celery` queue.
  - `intelligence-worker`: `intelligence` queue (`-c 2`).
  - `telegram-worker`: `telegram` queue (`-c 1` per account).
  - `beat`: `DatabaseScheduler` for periodic dispatch tasks.

## Pre-Production Checklist

```bash
cp .env.example .env
docker compose up --build -d
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py system_health
docker compose exec web python manage.py run_news_pipeline --dry-run
```
