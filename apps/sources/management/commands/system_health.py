"""System health check command. Tests DB, Redis, Qdrant, AI, Telegram, and Twitter status."""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache
from django.core.cache.backends.base import BaseCache
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Check operational health of all infrastructure components without printing secrets."

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS("NEWSAI SYSTEM HEALTH CHECK"))
        self.stdout.write(self.style.SUCCESS("=" * 60))

        # 1. Database
        db_engine = settings.DATABASES["default"].get("ENGINE", "unknown")
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                row = cursor.fetchone()
                db_status = "OK" if row and row[0] == 1 else "FAILED"
        except Exception as exc:
            db_status = f"FAILED: {exc}"
        self.stdout.write(f"Database ({db_engine}): {db_status}")

        # 2. Redis Cache (set/get/delete round-trip; backend class is explicit so
        # a LocMem fallback can never masquerade as real Redis).
        cache_backend = (
            settings.CACHES["default"].get("BACKEND", type(cache).__name__) or type(cache).__name__
        )
        try:
            cache.set("healthcheck:probe", "probe-value", timeout=30)
            fetched = cache.get("healthcheck:probe")
            cache.delete("healthcheck:probe")
            still_there = cache.get("healthcheck:probe")
            if fetched == "probe-value" and still_there is None:
                redis_status = "OK"
            else:
                redis_status = "FAILED: round-trip mismatch"
        except Exception as exc:
            redis_status = f"FAILED: {exc}"
        self.stdout.write(f"Redis Cache ({cache_backend}): {redis_status}")

        # 3. Qdrant
        qdrant_url = str(getattr(settings, "QDRANT_URL", ""))
        try:
            from qdrant_client import QdrantClient

            client = QdrantClient(url=qdrant_url, timeout=3.0)
            client.get_collections()
            qdrant_status = "OK"
        except Exception as exc:
            qdrant_status = f"UNAVAILABLE ({type(exc).__name__}) - Optional in offline/CI"
        self.stdout.write(f"Qdrant Vector Store ({qdrant_url}): {qdrant_status}")

        # 4. AI Layer
        ai_provider = str(getattr(settings, "AI_PROVIDER", "fake"))
        ai_key_set = bool(str(getattr(settings, "AI_API_KEY", "") or ""))
        cheap_model = str(getattr(settings, "AI_MODEL_CHEAP", ""))
        strong_model = str(getattr(settings, "AI_MODEL_STRONG", ""))
        self.stdout.write(
            f"AI Layer: Provider={ai_provider}, Key Configured={ai_key_set}, "
            f"Models=[{cheap_model} / {strong_model}]"
        )

        # 5. Telegram Ingestion
        tg_id_set = bool(getattr(settings, "TELEGRAM_API_ID", 0))
        tg_session_set = bool(str(getattr(settings, "TELEGRAM_SESSION_STRING", "") or ""))
        tg_state = (
            "NOT CONFIGURED" if not (tg_id_set and tg_session_set) else "CONFIGURED (presence only)"
        )
        self.stdout.write(f"Telegram Ingestion (Kurigram): {tg_state}")

        # 6. Telegram Publishing
        bot_token_set = bool(str(getattr(settings, "TELEGRAM_BOT_TOKEN", "") or ""))
        channel_id = str(getattr(settings, "TELEGRAM_CHANNEL_ID", "") or "")
        channel_set = bool(channel_id)
        auto_pub = bool(getattr(settings, "FEATURE_FLAGS", {}).get("ENABLE_AUTO_PUBLISH", False))
        if not (bot_token_set and channel_set):
            pub_state = "NOT CONFIGURED"
        else:
            pub_state = "CONFIGURED (presence only)"
        self.stdout.write(f"Telegram Bot Publishing: {pub_state}, Auto-Publish={auto_pub}")

        # 7. Twitter / X Ingestion
        twitter_session_set = bool(str(getattr(settings, "TWITTER_SESSION", "") or ""))
        twitter_enabled = bool(
            getattr(settings, "FEATURE_FLAGS", {}).get("ENABLE_TWITTER_SOURCE", False)
        )
        if not twitter_session_set:
            twitter_state = "NOT CONFIGURED"
        else:
            twitter_state = "CONFIGURED (presence only)"
        self.stdout.write(f"Twitter/X Ingestion: {twitter_state}, Enabled={twitter_enabled}")

        self.stdout.write(self.style.SUCCESS("=" * 60))

    @staticmethod
    def check_cache_backend_is_redis() -> bool:
        backend: BaseCache = cache
        return "redis" in type(backend).__module__.lower()
