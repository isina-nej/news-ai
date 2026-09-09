"""Docker development / production-like settings.

Unlike ``config.settings.local`` this module intentionally keeps the MySQL,
Redis, and Qdrant configuration from ``base``. Docker Compose sets the service
hostnames through environment variables.
"""

from .base import *  # noqa: F403

DEBUG = env.bool("DEBUG", default=False)  # noqa: F405
CELERY_TASK_ALWAYS_EAGER = False

if DATABASES["default"]["ENGINE"] != "django.db.backends.mysql":  # noqa: F405
    raise RuntimeError("config.settings.docker requires a MySQL DATABASE_URL")
if not CACHES["default"]["BACKEND"].endswith(".redis.RedisCache"):  # noqa: F405
    raise RuntimeError("config.settings.docker requires a Redis REDIS_URL")
if not str(QDRANT_URL).startswith("http://qdrant:"):  # noqa: F405
    raise RuntimeError("config.settings.docker requires QDRANT_URL=http://qdrant:6333")
