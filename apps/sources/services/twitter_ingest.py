"""Twitter/X ingestion orchestration: crash-safe cursor, cooldowns, gates."""

from __future__ import annotations

import time
import uuid
from typing import Any

from django.conf import settings
from django.utils import timezone

from apps.core.lock import DistributedLock
from apps.core.redaction import sanitize_error_message
from apps.news.services.persistence import ingestion_persistence_service
from apps.sources.adapters import FetchContext, adapter_registry
from apps.sources.adapters.base import AdapterError, AuthenticationError, RateLimitError
from apps.sources.adapters.twitter import TwitterSourceAdapter
from apps.sources.models import FetchRun, FetchRunStatus, Source, SourceCheckpoint

LOCK_PREFIX = "fetch:twitter:"


def twitter_enabled() -> bool:
    try:
        from apps.ops.models import FeatureFlag

        row = FeatureFlag.objects.filter(key="ENABLE_TWITTER_SOURCE").first()
        if row is not None:
            return bool(row.enabled)
    except Exception:  # noqa: S110 — flag lookup fallback to settings
        pass
    flags = getattr(settings, "FEATURE_FLAGS", {})
    return bool(flags.get("ENABLE_TWITTER_SOURCE", False))


def twitter_client_from_env():
    """Build a session client from env without persisting secrets."""
    session = str(getattr(settings, "TWITTER_SESSION", "") or "")
    if not session:
        return None
    try:
        from apps.sources.adapters.twitter_clients import EnvSessionClient

        return EnvSessionClient(session=session)
    except Exception:
        return None


class TwitterIngestService:
    def __init__(self) -> None:
        self.persistence = ingestion_persistence_service
        self.registry = adapter_registry

    def fetch_source(self, source_id: int, *, correlation_id: str | None = None) -> dict[str, Any]:
        cid = correlation_id or uuid.uuid4().hex[:16]
        if not twitter_enabled():
            return {"status": "skipped", "reason": "twitter_disabled", "source_id": source_id}
        lock = DistributedLock(f"{LOCK_PREFIX}{source_id}", token=cid, ttl_seconds=180)
        if not lock.acquire():
            return {"status": "skipped", "reason": "already_running", "source_id": source_id}
        started = time.monotonic()
        run: FetchRun | None = None
        try:
            source = Source.objects.get(pk=source_id)
            if not source.enabled:
                return {"status": "skipped", "reason": "source_disabled", "source_id": source_id}
            now = timezone.now()
            if source.cooldown_until and source.cooldown_until > now:
                return {"status": "skipped", "reason": "source_cooldown", "source_id": source_id}
            run = FetchRun.objects.create(
                source=source, started_at=now, status=FetchRunStatus.RUNNING, correlation_id=cid
            )
            checkpoint, _ = SourceCheckpoint.objects.get_or_create(
                source=source, adapter="twitter", defaults={"state": {}}
            )
            configuration = dict(source.configuration or {})
            configuration.setdefault("cursor", (checkpoint.state or {}).get("cursor"))
            configuration.setdefault("limit", 50)

            client = twitter_client_from_env()
            adapter = self.registry.get(source.platform, str(configuration.get("adapter_type", "")))
            if (
                isinstance(adapter, TwitterSourceAdapter)
                and client is not None
                and adapter._client is None
            ):
                adapter = TwitterSourceAdapter(client=client)
            context = FetchContext(
                source_id=source.pk,
                url=source.url or source.identifier,
                platform=source.platform,
                adapter_type=str(configuration.get("adapter_type", "")),
                configuration=configuration,
                timeout_seconds=float(configuration.get("timeout_seconds", 30.0)),
                correlation_id=cid,
            )
            import asyncio

            result = asyncio.run(adapter.fetch(context))
            persist_res = self.persistence.persist_items(source, result.items)

            # Advance cursor only after successful persistence (at-least-once safe).
            next_cursor = None
            for item in result.items:
                cursor_val = (item.raw_payload or {}).get("next_cursor")
                if cursor_val:
                    next_cursor = cursor_val
                    break
            if next_cursor or result.items:
                state = dict(checkpoint.state or {})
                if next_cursor:
                    state["cursor"] = next_cursor
                max_external = None
                for item in result.items:
                    if item.external_id and (
                        max_external is None or str(item.external_id) > str(max_external)
                    ):
                        max_external = item.external_id
                if max_external:
                    checkpoint.last_external_id = str(max_external)
                published = [i.published_at for i in result.items if i.published_at]
                if published:
                    batch_max = max(published)
                    if (
                        checkpoint.last_published_at is None
                        or batch_max > checkpoint.last_published_at
                    ):
                        checkpoint.last_published_at = batch_max
                checkpoint.state = state
                checkpoint.save()

            source.record_fetch_success()
            duration_ms = int((time.monotonic() - started) * 1000)
            run.finished_at = timezone.now()
            run.status = FetchRunStatus.SUCCESS
            run.fetched_count = len(result.items)
            run.created_count = persist_res.created_count
            run.duplicate_count = persist_res.duplicate_count
            run.duration_ms = duration_ms
            run.save()
            return {
                "status": "success",
                "source_id": source_id,
                "fetched_count": len(result.items),
                "created_count": persist_res.created_count,
                "duration_ms": duration_ms,
                "correlation_id": cid,
            }
        except AuthenticationError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            if run:
                run.finished_at = timezone.now()
                run.status = FetchRunStatus.FAILED
                run.error_type = "AuthenticationError"
                run.error_message = sanitize_error_message(str(exc))[:1024]
                run.duration_ms = duration_ms
                run.save()
            try:
                source = Source.objects.get(pk=source_id)
                source.cooldown_until = timezone.now() + timezone.timedelta(hours=6)
                source.save(update_fields=["cooldown_until", "updated_at"])
                source.record_fetch_failure()
            except Source.DoesNotExist:
                pass
            return {"status": "failed", "error_type": "AuthenticationError", "source_id": source_id}
        except RateLimitError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            if run:
                run.finished_at = timezone.now()
                run.status = FetchRunStatus.FAILED
                run.error_type = "RateLimitError"
                run.error_message = sanitize_error_message(str(exc))[:1024]
                run.duration_ms = duration_ms
                run.save()
            try:
                source = Source.objects.get(pk=source_id)
                wait = int(exc.retry_after or 3600)
                source.cooldown_until = timezone.now() + timezone.timedelta(seconds=wait)
                source.save(update_fields=["cooldown_until", "updated_at"])
                source.record_fetch_failure()
            except Source.DoesNotExist:
                pass
            raise exc
        except AdapterError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            if run:
                run.finished_at = timezone.now()
                run.status = FetchRunStatus.FAILED
                run.error_type = type(exc).__name__
                run.error_message = sanitize_error_message(str(exc))[:1024]
                run.duration_ms = duration_ms
                run.save()
            if exc.is_transient:
                raise exc
            return {"status": "failed", "error_type": type(exc).__name__, "source_id": source_id}
        finally:
            lock.release()


twitter_ingest_service = TwitterIngestService()
