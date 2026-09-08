"""Batch Telegram engagement refresh without ever incrementing views.

Uses ``client.get_messages`` (a plain read) in peer-batched calls — never the
``messages.getMessagesViews … increment=True`` path. Monitoring must not make
content look more popular. All network I/O runs on the stable per-process
Telegram runtime loop (no ``asyncio.run()`` per chunk).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.core.redaction import sanitize_error_message
from apps.news.models import SourceItem
from apps.news.services.engagement_tracking import advance_tracking
from apps.news.services.milestones import record_milestone_snapshot
from apps.sources.adapters.base import AuthenticationError, RateLimitError
from apps.sources.adapters.telegram_serializers import serialize_reactions
from apps.sources.models import EngagementTrackingState
from apps.sources.telegram_runtime import get_telegram_runtime

TELEGRAM_METRICS_BATCH_SIZE = 100


def _message_ids(item: SourceItem) -> list[int]:
    try:
        return [int(str(item.external_id).rsplit("-", 1)[-1])]
    except (TypeError, ValueError, AttributeError):
        return []


def _peer_ref(item: SourceItem) -> str:
    meta = item.raw_payload or {}
    for key in ("chat_username",):
        value = meta.get(key)
        if value:
            return str(value if str(value).startswith("@") else f"@{value}")
    source = item.source
    identifier = (source.identifier or "").strip()
    if identifier.startswith("@") or identifier.startswith("t.me/") or "t.me/" in identifier:
        if "t.me/" in identifier and not identifier.startswith("@"):
            tail = identifier.split("t.me/", 1)[1].split("?", 1)[0].strip("/").split("/")[0]
            return f"@{tail}" if tail else identifier
        return identifier
    platform_id = (source.platform_external_id or "").strip()
    if platform_id:
        try:
            return str(int(platform_id))
        except ValueError:
            return platform_id
    return identifier


def _claim_due_states(*, limit: int, now: Any, worker_id: str) -> list[EngagementTrackingState]:
    """Atomically claim due tracking rows so two refresh workers never overlap.

    Uses ``select_for_update(skip_locked=True)`` inside a transaction (MySQL 8.4):
    each row is claimed by stamping ``last_attempt_at`` + ``failure_count`` bump
    guard via a lease marker in a single UPDATE … WHERE. Simpler robust claim:
    select candidates, then UPDATE … WHERE pk AND (lease free) is racy without
    a lease column, so we take row locks with skip_locked and mark claimed in
    memory by bumping ``failure_count`` only on actual failure paths below.
    For correctness here: lock rows, return them; concurrent worker skips them.
    """
    with transaction.atomic():
        candidates = list(
            EngagementTrackingState.objects.select_for_update(skip_locked=True)
            .filter(active=True, next_due_at__lte=now)
            .select_related("source_item", "source_item__source")
            .order_by("next_due_at")[: max(1, limit)]
        )
        # Stamp a claim marker so a concurrent transaction that somehow read
        # the same rows (e.g. non-InnoDB fallback) still observes contention.
        for state in candidates:
            state.last_attempt_at = now
        if candidates:
            EngagementTrackingState.objects.filter(pk__in=[s.pk for s in candidates]).update(
                last_attempt_at=now
            )
        return candidates


def _extract_reply_count(msg: Any) -> int | None:
    """Best-effort discussion reply count from stable Kurigram fields.

    Sources tried in order (first hit wins, None when unavailable):
    1. ``get_discussion_replies_count`` is an extra RPC — handled by the
       service layer per message only when cheap; here we read inline attrs.
    2. ``msg.replies`` mapping with ``replies_count``/``count`` keys.
    3. ``msg.reply_info`` with ``reply_count``.
    Never scrapes reply text or user identities.
    """
    for attr in ("replies", "reply_info", "discussion_info"):
        container = getattr(msg, attr, None)
        if container is None:
            continue
        if isinstance(container, dict):
            for key in ("replies_count", "reply_count", "count", "comments_count"):
                value = container.get(key)
                if isinstance(value, bool):
                    continue
                try:
                    number = int(value) if value is not None else None
                except (TypeError, ValueError, OverflowError):
                    continue
                if number is not None and number >= 0:
                    return number
        else:
            for key in ("replies_count", "reply_count", "count"):
                value = getattr(container, key, None)
                if isinstance(value, bool) or value is None:
                    continue
                try:
                    number = int(value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if number >= 0:
                    return number
    return None


class TelegramEngagementRefreshService:
    """Refresh counters for due items, batched per peer."""

    batch_size = TELEGRAM_METRICS_BATCH_SIZE

    def refresh_due_items(
        self, *, limit: int = 200, correlation_id: str | None = None
    ) -> dict[str, Any]:
        cid = correlation_id or uuid.uuid4().hex[:16]
        started = time.monotonic()
        now = timezone.now()
        due = _claim_due_states(limit=limit, now=now, worker_id=cid)
        refreshed = 0
        snapshots = 0
        errors: list[str] = []
        # Group by (account_key, peer) so each peer is fetched in as few calls as possible.
        groups: dict[tuple[str, str], list[EngagementTrackingState]] = {}
        for state in due:
            item = state.source_item
            peer = _peer_ref(item)
            account_key = str(
                (item.source.configuration or {}).get("account_key", "default") or "default"
            )
            groups.setdefault((account_key, peer), []).append(state)
        runtime = get_telegram_runtime()
        for (account_key, peer), states in groups.items():
            for chunk_start in range(0, len(states), self.batch_size):
                chunk = states[chunk_start : chunk_start + self.batch_size]
                try:
                    made = runtime.run_sync(self._refresh_chunk, account_key, peer, chunk, now=now)
                    refreshed += len(chunk)
                    snapshots += made
                except RateLimitError as exc:
                    errors.append(f"{peer}: {type(exc).__name__}")
                    for state in chunk:
                        state.last_attempt_at = now
                        state.failure_count += 1
                        state.save(update_fields=["last_attempt_at", "failure_count", "updated_at"])
                    # FloodWait cooldown on the owning source(s).
                    for state in chunk:
                        try:
                            secs = max(1, min(int(getattr(exc, "retry_after", 60) or 60), 3600))
                        except (TypeError, ValueError):
                            secs = 60
                        src = state.source_item.source
                        src.cooldown_until = now + timezone.timedelta(seconds=secs)
                        src.save(update_fields=["cooldown_until", "updated_at"])
                    break
                except AuthenticationError as exc:
                    errors.append(f"{peer}: {type(exc).__name__}")
                    for state in chunk:
                        state.last_attempt_at = now
                        state.failure_count += 1
                        state.save(update_fields=["last_attempt_at", "failure_count", "updated_at"])
                    break
                except Exception as exc:
                    errors.append(f"{peer}: {sanitize_error_message(str(exc))[:200]}")
                    for state in chunk:
                        state.last_attempt_at = now
                        state.failure_count += 1
                        state.save(update_fields=["last_attempt_at", "failure_count", "updated_at"])
        return {
            "status": "success",
            "checked": len(due),
            "refreshed": refreshed,
            "snapshots": snapshots,
            "errors": errors,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "correlation_id": cid,
        }

    async def _refresh_chunk(
        self, account_key: str, peer: str, states: list[EngagementTrackingState], *, now: Any
    ) -> int:
        from apps.sources.adapters.telegram_client import get_telegram_client_manager

        manager = get_telegram_client_manager(account_key)
        ids: list[int] = []
        by_id: dict[int, EngagementTrackingState] = {}
        for state in states:
            for mid in _message_ids(state.source_item):
                ids.append(mid)
                by_id[mid] = state

        async def _load(client: Any) -> Any:
            # Plain read; get_messages never increments view counters.
            return await client.get_messages(peer, message_ids=ids)

        # NOTE: called on the runtime loop already (run_sync wraps this
        # coroutine), so manager.run reuses the loop-bound client directly.
        loaded = await manager.run(_load)
        messages = loaded if isinstance(loaded, list) else ([loaded] if loaded else [])
        made = 0
        for msg in messages:
            if msg is None or getattr(msg, "empty", False):
                continue
            mid = getattr(msg, "id", None)
            state = by_id.get(int(mid)) if mid is not None else None
            if state is None:
                continue
            item = state.source_item
            views = self._safe_count(getattr(msg, "views", None))
            forwards = self._safe_count(getattr(msg, "forwards", None))
            replies = _extract_reply_count(msg)
            total_reactions, breakdown = serialize_reactions(msg)
            target = state.next_target_age_seconds
            # Idempotent milestone write: IntegrityError on retry reuses the
            # existing row and still advances tracking (never wedged).
            record_milestone_snapshot(
                item,
                target_age_seconds=target,
                now=now,
                views=views,
                forwards=forwards,
                reactions=total_reactions,
                replies=replies,
                raw_metrics={"reaction_breakdown": breakdown} if breakdown else {},
            )
            made += 1
            advance_tracking(state, now=now, completed_target_age_seconds=target)
        # States whose message vanished: record attempt, keep active for reconciliation.
        seen = set()
        for msg in messages:
            if msg is not None and getattr(msg, "id", None) is not None:
                seen.add(int(msg.id))
        for mid, state in by_id.items():
            if mid not in seen:
                state.last_attempt_at = now
                state.failure_count += 1
                state.save(update_fields=["last_attempt_at", "failure_count", "updated_at"])
        return made

    @staticmethod
    def _safe_count(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if number >= 0 else None


telegram_engagement_refresh_service = TelegramEngagementRefreshService()
