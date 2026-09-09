"""Regression checks for production-like Docker infrastructure configuration."""

from __future__ import annotations

import importlib
from io import StringIO
from pathlib import Path

import pytest
import yaml
from django.core.management import call_command

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_docker_settings_use_real_mysql_redis_and_qdrant(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql://newsai:test@mysql:3306/newsai")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    import sys

    sys.modules.pop("config.settings.docker", None)
    module = importlib.import_module("config.settings.docker")
    assert module.DATABASES["default"]["ENGINE"] == "django.db.backends.mysql"
    assert module.DATABASES["default"]["HOST"] == "mysql"
    assert module.CACHES["default"]["BACKEND"] == "django.core.cache.backends.redis.RedisCache"
    assert module.CACHES["default"]["LOCATION"] == "redis://redis:6379/0"
    assert module.QDRANT_URL == "http://qdrant:6333"
    assert module.CELERY_TASK_ALWAYS_EAGER is False


def test_host_local_settings_are_explicitly_lightweight():
    from config.settings import local

    assert local.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
    assert local.CACHES["default"]["BACKEND"].endswith("LocMemCache")
    assert local.CELERY_TASK_ALWAYS_EAGER is True


def test_compose_uses_docker_settings_and_internal_infrastructure():
    compose = _compose()
    services = compose["services"]
    for service_name in ("web", "worker", "intelligence-worker", "telegram-worker", "beat"):
        environment = services[service_name].get("environment", {})
        assert environment["DJANGO_SETTINGS_MODULE"] == "config.settings.docker"
    assert "ports" not in services["mysql"]
    assert "ports" not in services["redis"]
    assert "ports" not in services["qdrant"]
    assert services["qdrant"].get("healthcheck") is None
    assert services["web"]["depends_on"]["qdrant"]["condition"] == "service_started"


def test_compose_service_urls_are_unambiguous():
    compose = _compose()
    environment = compose["x-app-environment"]
    assert environment["DATABASE_URL"].endswith("@mysql:3306/newsai_validation")
    assert environment["REDIS_URL"] == "redis://redis:6379/0"
    assert environment["QDRANT_URL"] == "http://qdrant:6333"


def test_system_health_reports_backend_and_cache_backend(settings):
    output = StringIO()
    call_command("system_health", stdout=output, no_color=True)
    rendered = output.getvalue()
    assert settings.DATABASES["default"]["ENGINE"] in rendered
    assert settings.CACHES["default"]["BACKEND"] in rendered
    assert "AI Layer: Provider=fake" in rendered
    assert "Auto-Publish=False" in rendered


def test_seed_demo_news_is_idempotent():
    from apps.news.models import EngagementSnapshot, SourceItem
    from apps.sources.models import Source
    from apps.stories.models import Story

    call_command("seed_demo_news")
    first = (
        Source.objects.count(),
        SourceItem.objects.count(),
        Story.objects.count(),
        EngagementSnapshot.objects.count(),
    )
    call_command("seed_demo_news")
    second = (
        Source.objects.count(),
        SourceItem.objects.count(),
        Story.objects.count(),
        EngagementSnapshot.objects.count(),
    )
    assert first == second


def test_pipeline_dry_run_rerun_is_idempotent():
    from apps.news.models import SourceItem
    from apps.publishing.models import Publication
    from apps.sources.models import Source
    from apps.stories.models import Story

    call_command("seed_demo_news")
    call_command("run_news_pipeline", dry_run=True, limit=10)
    first = (
        Source.objects.count(),
        SourceItem.objects.count(),
        Story.objects.count(),
        Publication.objects.count(),
    )
    call_command("run_news_pipeline", dry_run=True, limit=10)
    second = (
        Source.objects.count(),
        SourceItem.objects.count(),
        Story.objects.count(),
        Publication.objects.count(),
    )
    assert first == second
    assert Publication.objects.count() == 0
