# Development

Requires: Docker + Compose. Python 3.12 inside containers.

```bash
cp .env.example .env
docker compose up --build
docker compose exec web python manage.py migrate
docker compose exec web pytest
```

Checks: `ruff check .`, `ruff format .`, `pyright`. Hooks: `pre-commit install`.
