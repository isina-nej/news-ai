"""Source fetch service orchestrating distributed locking, adapters, persistence, and health."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from django.utils import timezone

from apps.core.lock import DistributedLock
from apps.core.redaction import sanitize_error_message
from apps.news.services.persistence import ingestion_persistence_service
from apps.sources.adapters import (
    AdapterError,
    FetchContext,
    FetchResult,
    RateLimitError,
    adapter_registry,
)
from apps.sources.models import FetchRun, FetchRunStatus, Source

LOCK_PREFIX = "fetch:source:"
LOCK_TIMEOUT = 180  # 3 minutes default TTL


class SourceFetchService:
    """Service to execute a safe fetch for a single Source."""

    def __init__(self, *, lock_timeout: int = LOCK_TIMEOUT) -> None:
        self.lock_timeout = lock_timeout
        self.persistence = ingestion_persistence_service
        self.registry = adapter_registry

    def fetch_source(self, source_id: int, *, correlation_id: str | None = None) -> dict[str, Any]:
        """Synchronous entry point called by Celery tasks or management commands.

        Keeps Django ORM database interactions synchronous while executing
        the async adapter network I/O cleanly inside an event loop.
        """
        cid = correlation_id or uuid.uuid4().hex[:16]
        lock_key = f"{LOCK_PREFIX}{source_id}"

        # 1. Atomic token-checked distributed lock to prevent overlapping fetches
        lock = DistributedLock(lock_key, token=cid, ttl_seconds=self.lock_timeout)
        acquired = lock.acquire()
        if not acquired:
            return {
                "status": "skipped",
                "reason": "already_running",
                "source_id": source_id,
                "correlation_id": cid,
            }

        start_time = time.monotonic()
        run_record: FetchRun | None = None

        try:
            # 2. Lookup source
            try:
                source = Source.objects.get(pk=source_id)
            except Source.DoesNotExist:
                return {"status": "error", "reason": "source_not_found", "source_id": source_id}

            if not source.enabled:
                return {"status": "skipped", "reason": "source_disabled", "source_id": source_id}

            # 3. Create FetchRun audit record with RUNNING status
            run_record = FetchRun.objects.create(
                source=source,
                started_at=timezone.now(),
                status=FetchRunStatus.RUNNING,
                correlation_id=cid,
            )

            # 4. Resolve adapter
            adapter_type = str(source.configuration.get("adapter_type", ""))
            adapter = self.registry.get(source.platform, adapter_type)

            # 5. Build FetchContext with cached HTTP validators
            target_url = source.url or source.identifier
            context = FetchContext(
                source_id=source.pk,
                url=target_url,
                platform=source.platform,
                adapter_type=adapter_type,
                configuration=dict(source.configuration),
                etag=source.configuration.get("_http_etag"),
                last_modified=source.configuration.get("_http_last_modified"),
                timeout_seconds=float(source.configuration.get("timeout_seconds", 30.0)),
                max_bytes=int(source.configuration.get("max_bytes", 5 * 1024 * 1024)),
                correlation_id=cid,
            )

            # 6. Execute network fetch in async event loop
            result: FetchResult = asyncio.run(adapter.fetch(context))
            duration_ms = int((time.monotonic() - start_time) * 1000)

            # 7. Handle 304 Not Modified
            if result.not_modified or result.status_code == 304:
                source.record_fetch_success()
                run_record.finished_at = timezone.now()
                run_record.status = FetchRunStatus.NOT_MODIFIED
                run_record.http_status = 304
                run_record.duration_ms = duration_ms
                run_record.save()
                return {
                    "status": "not_modified",
                    "source_id": source_id,
                    "duration_ms": duration_ms,
                    "correlation_id": cid,
                }

            # 8. Persist and dedupe items (synchronous ORM)
            persist_res = self.persistence.persist_items(source, result.items)

            # 9. Update HTTP cache validators on Source configuration, persisted
            # explicitly BEFORE record_fetch_success() (which writes health only).
            if result.etag or result.last_modified:
                cfg = dict(source.configuration)
                if result.etag:
                    cfg["_http_etag"] = result.etag
                if result.last_modified:
                    cfg["_http_last_modified"] = result.last_modified
                source.configuration = cfg
                source.save(update_fields=["configuration", "updated_at"])

            source.record_fetch_success()

            # 10. Complete FetchRun audit record
            run_record.finished_at = timezone.now()
            run_record.status = FetchRunStatus.SUCCESS
            run_record.http_status = result.status_code
            run_record.fetched_count = len(result.items)
            run_record.created_count = persist_res.created_count
            run_record.duplicate_count = persist_res.duplicate_count
            run_record.rejected_count = persist_res.rejected_count
            run_record.duration_ms = duration_ms
            run_record.save()

            return {
                "status": "success",
                "source_id": source_id,
                "fetched_count": len(result.items),
                "created_count": persist_res.created_count,
                "duplicate_count": persist_res.duplicate_count,
                "duration_ms": duration_ms,
                "correlation_id": cid,
            }

        except AdapterError as err:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            if run_record:
                run_record.finished_at = timezone.now()
                run_record.status = FetchRunStatus.FAILED
                run_record.error_type = type(err).__name__
                run_record.error_message = sanitize_error_message(str(err))[:1024]
                run_record.duration_ms = duration_ms
                if isinstance(err, RateLimitError):
                    run_record.http_status = 429
                run_record.save()

            try:
                source = Source.objects.get(pk=source_id)
                source.record_fetch_failure()
            except Source.DoesNotExist:
                pass

            # Transient errors re-raised so Celery can handle exponential retry
            if err.is_transient:
                raise err

            return {
                "status": "failed",
                "source_id": source_id,
                "error_type": type(err).__name__,
                "error": sanitize_error_message(str(err)),
                "duration_ms": duration_ms,
                "correlation_id": cid,
            }

        except Exception as unexpected:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            if run_record:
                run_record.finished_at = timezone.now()
                run_record.status = FetchRunStatus.FAILED
                run_record.error_type = type(unexpected).__name__
                run_record.error_message = sanitize_error_message(str(unexpected))[:1024]
                run_record.duration_ms = duration_ms
                run_record.save()

            try:
                source = Source.objects.get(pk=source_id)
                source.record_fetch_failure()
            except Source.DoesNotExist:
                pass

            raise unexpected

        finally:
            # Atomic token-checked release ensures we only release OUR lock
            lock.release()


source_fetch_service = SourceFetchService()
