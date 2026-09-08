"""Batch Telegram engagement refresh without ever incrementing views.

Uses ``client.get_messages`` (a plain read) in peer-batched calls — never the
``messages.getMessagesViews … increment=True`` path. Monitoring must not make
content look more popular.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from django.utils import timezone

from apps.core.redaction import sanitize_error_message
from apps.news.models import EngagementSnapshot, SourceItem
from apps.news.services.engagement_tracking import advance_tracking
from apps.sources.adapters.base import AuthenticationError, RateLimitError
from apps.sources.adapters.telegram_client import get_telegram_client_manager
from apps.sources.adapters.telegram_serializers import serialize_reactions
from apps.sources.models import EngagementTrackingState

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


class TelegramEngagementRefreshService:
    """Refresh counters for due items, batched per peer."""

    batch_size = TELEGRAM_METRICS_BATCH_SIZE

    def refresh_due_items(
        self, *, limit: int = 200, correlation_id: str | None = None
    ) -> dict[str, Any]:
        cid = correlation_id or uuid.uuid4().hex[:16]
        started = time.monotonic()
        now = timezone.now()
        due = list(
            EngagementTrackingState.objects.filter(active=True, next_due_at__lte=now)
            .select_related("source_item", "source_item__source")
            .order_by("next_due_at")[: max(1, limit)]
        )
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
        for (account_key, peer), states in groups.items():
            for chunk_start in range(0, len(states), self.batch_size):
                chunk = states[chunk_start : chunk_start + self.batch_size]
                try:
                    made = asyncio.run(self._refresh_chunk(account_key, peer, chunk, now=now))
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
            total_reactions, breakdown = serialize_reactions(msg)
            target = state.next_target_age_seconds
            try:
                EngagementSnapshot.objects.create(
                    source_item=item,
                    captured_at=now,
                    target_age_seconds=target,
                    views=views,
                    forwards=forwards,
                    shares=None,
                    reactions=total_reactions,
                    replies=None,
                    saves=None,
                    raw_metrics={"reaction_breakdown": breakdown} if breakdown else {},
                )
                made += 1
            except Exception:  # noqa: S112 — one bad snapshot must not abort the batch
                continue
            advance_tracking(state, now=now)
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
