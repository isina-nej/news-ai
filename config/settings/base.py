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
# Dedicated telegram queue: telegram tasks route here; run a worker with
# `-Q telegram -c 1` for single-account sequential MTProto access.
CELERY_TASK_ROUTES = {
    "apps.sources.tasks.fetch_telegram_source_task": {"queue": "telegram"},
    "apps.sources.tasks.refresh_telegram_engagement_task": {"queue": "telegram"},
}
CELERY_TASK_DEFAULT_QUEUE = "celery"

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
RSSHUB_TRUSTED_HOSTS = env.list("RSSHUB_TRUSTED_HOSTS", default=["rsshub"])
SESSION_ENCRYPTION_KEY = env("SESSION_ENCRYPTION_KEY", default="")
# Telegram user-session ingestion (Kurigram/MTProto). Env-only; never DB.
TELEGRAM_API_ID = env.int("TELEGRAM_API_ID", default=0)
TELEGRAM_API_HASH = env("TELEGRAM_API_HASH", default="")
TELEGRAM_SESSION_STRING = env("TELEGRAM_SESSION_STRING", default="")
TELEGRAM_ACCOUNT_KEY = env("TELEGRAM_ACCOUNT_KEY", default="default")
# Dedicated worker concurrency per Telegram account (default 1).
TELEGRAM_WORKER_CONCURRENCY = env.int("TELEGRAM_WORKER_CONCURRENCY", default=1)

# Phase 4 clustering / intelligence settings (env defaults, DB-overridable).
EMBEDDING_PROVIDER = env("EMBEDDING_PROVIDER", default="fake")
EMBEDDING_MODEL = env(
    "EMBEDDING_MODEL", default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)
EMBEDDING_MODEL_VERSION = env("EMBEDDING_MODEL_VERSION", default="phase4-default-v1")
EMBEDDING_DIMENSION = env.int("EMBEDDING_DIMENSION", default=384)
EMBEDDING_BATCH_SIZE = env.int("EMBEDDING_BATCH_SIZE", default=32)
QDRANT_URL = env("QDRANT_URL", default="http://qdrant:6333")
QDRANT_COLLECTION = env("QDRANT_COLLECTION", default="newsai_items")
CLUSTER_ALGORITHM_VERSION = env("CLUSTER_ALGORITHM_VERSION", default="cluster-v1")
CLUSTER_HIGH_THRESHOLD = env.float("CLUSTER_HIGH_THRESHOLD", default=0.78)
CLUSTER_LOW_THRESHOLD = env.float("CLUSTER_LOW_THRESHOLD", default=0.52)
CLUSTER_MAX_CANDIDATES = env.int("CLUSTER_MAX_CANDIDATES", default=30)
CLUSTER_DEFAULT_LOOKBACK_HOURS = env.int("CLUSTER_DEFAULT_LOOKBACK_HOURS", default=48)
CLUSTER_EXTENDED_LOOKBACK_HOURS = env.int("CLUSTER_EXTENDED_LOOKBACK_HOURS", default=120)
MINHASH_NUM_PERM = env.int("MINHASH_NUM_PERM", default=128)
MINHASH_SCHEME = env("MINHASH_SCHEME", default="affine32")

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
