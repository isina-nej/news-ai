"""Host-only quick development: SQLite, LocMem cache, eager Celery.

Do not use this module for Docker or production-like validation. Docker Compose
uses ``config.settings.docker`` with real MySQL, Redis, and Qdrant.
"""

from .base import *  # noqa

DEBUG = True
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR("db.sqlite3"),
    }
}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CELERY_TASK_ALWAYS_EAGER = True
