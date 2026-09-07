import environ

env = environ.Env(
    DEBUG=(bool, False),
    SNAPSHOT_SCHEDULE_MIN=(str, "10,30,60,180,360,720,1440"),
    EXPLORATION_RATE=(float, 0.05),
    ENABLE_AI_CLUSTERING=(bool, False),
    ENABLE_AUTO_PUBLISH=(bool, False),
    ENABLE_TWITTER_SOURCE=(bool, False),
)

BASE_DIR = environ.Path(__file__) - 3

SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-only-change-me")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["*"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_celery_beat",
    "apps.core",
    "apps.sources",
    "apps.news",
    "apps.stories",
    "apps.ranking",
    "apps.ai",
    "apps.publishing",
    "apps.ops",
    "apps.platform_api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.CorrelationIdMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {"default": env.db("DATABASE_URL", default="mysql://newsai:newsai@mysql:3306/newsai")}
DATABASES["default"].setdefault("OPTIONS", {"charset": "utf8mb4"})
DATABASES["default"].setdefault("CONN_MAX_AGE", 60)

CACHES = {"default": env.cache("REDIS_URL", default="redis://redis:6379/0")}

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://redis:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://redis:6379/1")
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# NewsAI knobs (DB-overridable, env = defaults)
SNAPSHOT_SCHEDULE_MIN = [int(x) for x in env("SNAPSHOT_SCHEDULE_MIN").split(",") if x.strip()]
EXPLORATION_RATE = env("EXPLORATION_RATE")
FEATURE_FLAGS = {
    "ENABLE_AI_CLUSTERING": env("ENABLE_AI_CLUSTERING"),
    "ENABLE_AUTO_PUBLISH": env("ENABLE_AUTO_PUBLISH"),
    "ENABLE_TWITTER_SOURCE": env("ENABLE_TWITTER_SOURCE"),
}
RSSHUB_BASE_URL = env("RSSHUB_BASE_URL", default="http://rsshub:1200")
SESSION_ENCRYPTION_KEY = env("SESSION_ENCRYPTION_KEY", default="")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "apps.core.logging.JSONFormatter"}},
    "filters": {"redact": {"()": "apps.core.logging.RedactFilter"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "filters": ["redact"],
        }
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
