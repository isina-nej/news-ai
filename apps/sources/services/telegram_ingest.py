"""Telegram ingestion orchestration: checkpoint-first incremental fetch.

Order of operations (crash-safe):
  1. load checkpoint (or initialise from backfill policy)
  2. fetch messages newer than checkpoint (+ edit lookback)
  3. persist items (creates / updates / duplicates) + snapshots + tracking
  4. advance checkpoint ONLY after persistence succeeded
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from django.utils import timezone

from apps.core.lock import DistributedLock
from apps.core.redaction import sanitize_error_message
from apps.news.services.engagement_tracking import schedule_tracking
from apps.news.services.persistence import ingestion_persistence_service
from apps.sources.adapters import FetchContext, adapter_registry
from apps.sources.models import FetchRun, FetchRunStatus, Source, SourceCheckpoint

LOCK_PREFIX = "fetch:source:"
LOCK_TIMEOUT = 300

DEFAULT_INITIAL_BACKFILL_LIMIT = 50
DEFAULT_INCREMENTAL_LIMIT = 100
DEFAULT_EDIT_LOOKBACK_MESSAGES = 25


def _positive_int(value: Any, default: int, *, cap: int = 200) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(max(number, 1), cap) if number > 0 else default


def _cooldown_active(source: Source, *, now: Any) -> bool:
    return bool(source.cooldown_until and source.cooldown_until > now)


class TelegramIngestService:
    def __init__(self, *, lock_timeout: int = LOCK_TIMEOUT) -> None:
        self.lock_timeout = lock_timeout
        self.persistence = ingestion_persistence_service
        self.registry = adapter_registry

    def fetch_source(self, source_id: int, *, correlation_id: str | None = None) -> dict[str, Any]:
        cid = correlation_id or uuid.uuid4().hex[:16]
        lock = DistributedLock(
            f"{LOCK_PREFIX}{source_id}", token=cid, ttl_seconds=self.lock_timeout
        )
        if not lock.acquire():
            return {
                "status": "skipped",
                "reason": "already_running",
                "source_id": source_id,
                "correlation_id": cid,
            }
        started = time.monotonic()
        run: FetchRun | None = None
        try:
            try:
                source = Source.objects.get(pk=source_id)
            except Source.DoesNotExist:
                return {"status": "error", "reason": "source_not_found", "source_id": source_id}
            if not source.enabled:
                return {"status": "skipped", "reason": "source_disabled", "source_id": source_id}
            now = timezone.now()
            if _cooldown_active(source, now=now):
                return {
                    "status": "skipped",
                    "reason": "cooldown",
                    "source_id": source_id,
                    "correlation_id": cid,
                }
            run = FetchRun.objects.create(
                source=source, started_at=now, status=FetchRunStatus.RUNNING, correlation_id=cid
            )
            configuration = dict(source.configuration or {})
            checkpoint, _ = SourceCheckpoint.objects.get_or_create(
                source=source, adapter="telegram", defaults={"state": {}}
            )
            try:
                last_id = int((checkpoint.state or {}).get("last_message_id") or 0)
            except (TypeError, ValueError):
                last_id = 0
            if last_id > 0:
                configuration.setdefault("incremental_limit", DEFAULT_INCREMENTAL_LIMIT)
            else:
                configuration.setdefault("initial_backfill_limit", DEFAULT_INITIAL_BACKFILL_LIMIT)
            configuration.setdefault("edit_lookback_messages", DEFAULT_EDIT_LOOKBACK_MESSAGES)
            configuration["_checkpoint"] = {"last_message_id": last_id}

            adapter = self.registry.get(source.platform, str(configuration.get("adapter_type", "")))
            context = FetchContext(
                source_id=source.pk,
                url=source.url or source.identifier,
                platform=source.platform,
                adapter_type=str(configuration.get("adapter_type", "")),
                configuration=configuration,
                timeout_seconds=float(configuration.get("timeout_seconds", 60.0)),
                max_bytes=int(configuration.get("max_bytes", 5 * 1024 * 1024)),
                correlation_id=cid,
            )
            result = asyncio.run(adapter.fetch(context))
            persist_res = self.persistence.persist_items(source, result.items)

            # Advance checkpoint only after successful persistence.
            ids: list[int] = []
            for item in result.items:
                try:
                    ids.append(int(str(item.external_id).rsplit("-", 1)[-1]))
                except (TypeError, ValueError, AttributeError):
                    continue
            if ids:
                new_last = max([last_id, *ids])
                checkpoint.last_external_id = str(new_last)
                checkpoint.state = {**(checkpoint.state or {}), "last_message_id": new_last}
                if not checkpoint.last_published_at:
                    published = [i.published_at for i in result.items if i.published_at]
                    if published:
                        checkpoint.last_published_at = max(published)
                checkpoint.save()

            # Resolve stable peer id once (non-secret cache for renames).
            peer_id = None
            for item in result.items:
                peer_id = (item.source_metadata or {}).get("peer_id")
                if peer_id is not None:
                    break
            if peer_id is not None and not source.platform_external_id:
                source.platform_external_id = str(peer_id)

            for created in persist_res.created_items:
                try:
                    schedule_tracking(created, now=timezone.now())
                except Exception:  # noqa: S112 — tracking is best-effort, never fail ingest
                    continue
            source.record_fetch_success()

            duration_ms = int((time.monotonic() - started) * 1000)
            run.finished_at = timezone.now()
            run.status = FetchRunStatus.SUCCESS
            run.http_status = result.status_code
            run.fetched_count = len(result.items)
            run.created_count = persist_res.created_count
            run.updated_count = persist_res.updated_count
            run.duplicate_count = persist_res.duplicate_count
            run.rejected_count = persist_res.rejected_count
            run.duration_ms = duration_ms
            run.save()
            return {
                "status": "success",
                "source_id": source_id,
                "fetched_count": len(result.items),
                "created_count": persist_res.created_count,
                "updated_count": persist_res.updated_count,
                "duplicate_count": persist_res.duplicate_count,
                "duration_ms": duration_ms,
                "correlation_id": cid,
            }
        except Exception as exc:
            from apps.sources.adapters.base import AdapterError, RateLimitError

            duration_ms = int((time.monotonic() - started) * 1000)
            if run is not None:
                run.finished_at = timezone.now()
                run.status = FetchRunStatus.FAILED
                run.error_type = type(exc).__name__
                run.error_message = sanitize_error_message(str(exc))[:1024]
                run.duration_ms = duration_ms
                if isinstance(exc, RateLimitError):
                    run.http_status = 429
                run.save()
            try:
                source = Source.objects.get(pk=source_id)
                # FloodWait cooldown: honour server delay so the scheduler backs off.
                retry_after = getattr(exc, "retry_after", None)
                if retry_after:
                    try:
                        secs = max(1, min(int(retry_after), 3600))
                    except (TypeError, ValueError):
                        secs = 60
                    source.cooldown_until = timezone.now() + timezone.timedelta(seconds=secs)
                    source.save(update_fields=["cooldown_until", "updated_at"])
                source.record_fetch_failure()
            except Source.DoesNotExist:
                pass
            if isinstance(exc, AdapterError) and exc.is_transient:
                raise
            if isinstance(exc, AdapterError):
                return {
                    "status": "failed",
                    "source_id": source_id,
                    "error_type": type(exc).__name__,
                    "error": sanitize_error_message(str(exc)),
                    "duration_ms": duration_ms,
                    "correlation_id": cid,
                }
            raise
        finally:
            lock.release()


telegram_ingest_service = TelegramIngestService()
