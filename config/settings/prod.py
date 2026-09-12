from .base import *  # noqa
from .base import DATABASES as _DATABASES  # noqa: F401
from .base import CACHES as _CACHES  # noqa: F401
from .base import QDRANT_URL as _QDRANT_URL  # noqa: F401

DEBUG = False
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_SSL_REDIRECT = True

if not ALLOWED_HOSTS or ALLOWED_HOSTS == ["*"]:  # noqa: F405
    raise RuntimeError("config.settings.prod requires explicit ALLOWED_HOSTS")
if not PLATFORM_API_TOKEN:  # noqa: F405
    raise RuntimeError("config.settings.prod requires PLATFORM_API_TOKEN")
if _DATABASES["default"]["ENGINE"] != "django.db.backends.mysql":
    raise RuntimeError("config.settings.prod requires a MySQL DATABASE_URL")
if not _CACHES["default"]["BACKEND"].endswith(".redis.RedisCache"):
    raise RuntimeError("config.settings.prod requires a Redis REDIS_URL")
if not str(_QDRANT_URL).startswith("http"):
    raise RuntimeError("config.settings.prod requires QDRANT_URL")
