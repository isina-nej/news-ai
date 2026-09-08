"""Telegram user-session ingestion adapter (Kurigram) behind the SourceAdapter contract.

Flow: Telegram -> TelegramSourceAdapter -> FetchedItem -> NormalizationService
-> IngestionPersistenceService. No ORM, no Kurigram imports outside this module
(plus telegram_client.py / telegram_serializers.py / telegram_accounts.py).
"""

from __future__ import annotations

import time
from typing import Any

from apps.core.redaction import redact_url
from apps.sources.adapters.base import (
    AuthenticationError,
    FetchContext,
    FetchedItem,
    FetchResult,
    NetworkError,
    PermanentSourceError,
    RateLimitError,
    SourceAdapter,
)
from apps.sources.adapters.telegram_accounts import telegram_account_registry
from apps.sources.adapters.telegram_client import get_telegram_client_manager
from apps.sources.adapters.telegram_serializers import (
    is_skippable_service_message,
    message_author_name,
    message_content_type,
    message_date_utc,
    message_edit_date_utc,
    serialize_forward_origin,
    serialize_media,
    serialize_reactions,
)

# Defaults; every one overridable per-Source via configuration.
DEFAULT_INITIAL_BACKFILL_LIMIT = 50
DEFAULT_INCREMENTAL_LIMIT = 100
DEFAULT_EDIT_LOOKBACK_MESSAGES = 25
MAX_FETCH_LIMIT = 200


def _coerce_positive_int(value: Any, default: int, *, cap: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if number <= 0:
        return default
    return min(number, cap)


def _resolve_chat_identifier(configuration: dict[str, Any], url: str, identifier: str = "") -> str:
    """Accept @username, t.me URL or numeric peer id from configuration."""
    for key in ("chat_identifier", "chat", "channel", "peer"):
        value = configuration.get(key)
        if value:
            text = str(value).strip()
            if text:
                return text
    for candidate in (url or "", identifier or ""):
        text = (candidate or "").strip()
        if not text:
            continue
        if (
            text.startswith("https://t.me/")
            or text.startswith("http://t.me/")
            or text.startswith("t.me/")
        ):
            tail = text.split("t.me/", 1)[1].split("?", 1)[0].strip("/")
            if tail:
                first = tail.split("/")[0]
                return f"@{first}" if not first.startswith("@") else first
        return text
    return ""


def _public_message_url(chat_username: str | None, message_id: int) -> str:
    if chat_username:
        clean = chat_username.lstrip("@").strip()
        if clean:
            return f"https://t.me/{clean}/{message_id}"
    return ""


def _safe_int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _map_flood_wait(exc: BaseException, *, chat_ref: str) -> RateLimitError:
    seconds = getattr(exc, "seconds", None)
    if seconds is None:
        seconds = getattr(exc, "value", None)
    try:
        retry_after = int(seconds) if seconds is not None else None
    except (TypeError, ValueError):
        retry_after = None
    if retry_after is not None and retry_after < 0:
        retry_after = None
    safe_chat = redact_url(chat_ref) if chat_ref.startswith("http") else chat_ref
    return RateLimitError(
        f"Telegram FloodWait for {safe_chat}",
        retry_after=retry_after,
        details={"retry_after": retry_after},
    )


def _map_peer_error(exc: BaseException, *, chat_ref: str) -> PermanentSourceError:
    return PermanentSourceError(
        f"Telegram chat is not accessible ({type(exc).__name__}): {chat_ref}. "
        "The account must already be a member; auto-join is never attempted."
    )


class TelegramSourceAdapter(SourceAdapter):
    """Polling adapter: fetch latest N, then incremental fetch + edit lookback."""

    async def fetch(self, context: FetchContext) -> FetchResult:
        started = time.monotonic()
        configuration = dict(context.configuration or {})
        account_key = str(configuration.get("account_key", "") or "default")
        # Fail fast on unknown/misconfigured accounts (no network, no secrets in message).
        telegram_account_registry.get(account_key)

        chat_ref = _resolve_chat_identifier(configuration, context.url)
        if not chat_ref:
            raise PermanentSourceError(
                "Telegram source is missing chat_identifier/channel configuration."
            )

        checkpoint = configuration.get("_checkpoint") or {}
        try:
            last_message_id = int(checkpoint.get("last_message_id") or 0)
        except (TypeError, ValueError):
            last_message_id = 0

        if last_message_id > 0:
            limit = _coerce_positive_int(
                configuration.get("incremental_limit", DEFAULT_INCREMENTAL_LIMIT),
                DEFAULT_INCREMENTAL_LIMIT,
                cap=MAX_FETCH_LIMIT,
            )
            min_id = last_message_id  # inclusive; overlap re-checked by persistence
        else:
            limit = _coerce_positive_int(
                configuration.get("initial_backfill_limit", DEFAULT_INITIAL_BACKFILL_LIMIT),
                DEFAULT_INITIAL_BACKFILL_LIMIT,
                cap=MAX_FETCH_LIMIT,
            )
            min_id = 0
        edit_lookback = _coerce_positive_int(
            configuration.get("edit_lookback_messages", DEFAULT_EDIT_LOOKBACK_MESSAGES),
            DEFAULT_EDIT_LOOKBACK_MESSAGES,
            cap=MAX_FETCH_LIMIT,
        )

        manager = get_telegram_client_manager(account_key)

        async def _load(client: Any) -> tuple[Any, list[Any], list[Any]]:
            try:
                chat = await client.get_chat(chat_ref)
            except Exception as exc:
                if type(exc).__name__ in {
                    "PeerIdInvalid",
                    "ChannelPrivate",
                    "ChannelInvalid",
                    "UsernameNotOccupied",
                    "UsernameInvalid",
                }:
                    raise _map_peer_error(exc, chat_ref=chat_ref) from exc
                raise
            try:
                messages = [
                    m async for m in client.get_chat_history(chat_ref, limit=limit, min_id=min_id)
                ]
            except Exception as exc:
                if type(exc).__name__ in {"FloodWait", "FloodPremiumWait", "SlowmodeWait"}:
                    raise _map_flood_wait(exc, chat_ref=chat_ref) from exc
                raise
            lookback_messages: list[Any] = []
            if edit_lookback and last_message_id:
                floor_id = max(0, last_message_id - edit_lookback)
                try:
                    lookback_messages = [
                        m
                        async for m in client.get_chat_history(
                            chat_ref, limit=edit_lookback, max_id=last_message_id, min_id=floor_id
                        )
                    ]
                except Exception as exc:
                    if type(exc).__name__ in {"FloodWait", "FloodPremiumWait", "SlowmodeWait"}:
                        raise _map_flood_wait(exc, chat_ref=chat_ref) from exc
                    raise
            return chat, messages, lookback_messages

        try:
            chat, messages, lookback_messages = await manager.run(_load)
        except (RateLimitError, PermanentSourceError, AuthenticationError, NetworkError):
            raise
        except Exception as exc:
            if type(exc).__name__ in {"FloodWait", "FloodPremiumWait", "SlowmodeWait"}:
                raise _map_flood_wait(exc, chat_ref=chat_ref) from exc
            raise NetworkError(f"Telegram fetch failed for {chat_ref}: {exc}") from exc

        chat_username = getattr(chat, "username", None)
        chat_id = getattr(chat, "id", None)
        chat_title = getattr(chat, "title", None) or getattr(chat, "first_name", None) or ""

        items: list[FetchedItem] = []
        seen_ids: set[str] = set()
        # Newest-first from get_chat_history; merge lookback without duplicates.
        for msg in list(messages) + list(lookback_messages):
            message_id = getattr(msg, "id", None)
            if message_id is None:
                continue
            external_id = f"tg-{chat_id}-{message_id}" if chat_id else f"tg-{message_id}"
            if external_id in seen_ids:
                continue
            seen_ids.add(external_id)

            if is_skippable_service_message(msg):
                continue
            text = getattr(msg, "text", None)
            caption = getattr(msg, "caption", None)
            raw_text = ""
            if text:
                raw_text = str(text)
            elif caption:
                raw_text = str(caption)
            if not raw_text.strip():
                # Media-only posts still ingest (caption may be empty).
                media_kind = getattr(msg, "media", None)
                has_media = media_kind is not None or any(
                    getattr(msg, attr, None) is not None
                    for attr in (
                        "photo",
                        "video",
                        "document",
                        "audio",
                        "voice",
                        "animation",
                        "poll",
                    )
                )
                if not has_media:
                    continue

            published_at = message_date_utc(msg)
            edit_date = message_edit_date_utc(msg)
            total_reactions, breakdown = serialize_reactions(msg)
            media = serialize_media(msg)
            media_group_id = getattr(msg, "media_group_id", None)
            if media_group_id is not None and "media_group_id" not in media:
                media["media_group_id"] = str(media_group_id)
            forward_origin = serialize_forward_origin(msg)

            canonical_url = _public_message_url(chat_username, int(message_id))
            views = _safe_int_or_none(getattr(msg, "views", None))
            forwards = _safe_int_or_none(getattr(msg, "forwards", None))

            items.append(
                FetchedItem(
                    url=canonical_url or chat_ref,
                    external_id=external_id,
                    canonical_url=canonical_url,
                    title=(raw_text.strip().split("\n", 1)[0] if raw_text.strip() else "")[:512],
                    raw_text=raw_text,
                    published_at=published_at,
                    source_updated_at=edit_date,
                    author=message_author_name(msg),
                    language="und",
                    content_type=message_content_type(msg),
                    views=views,
                    forwards=forwards,
                    shares=None,  # Telegram exposes no independent shares metric
                    reactions=total_reactions,
                    replies=None,  # discussion replies counted during refresh, not here
                    saves=None,
                    reaction_breakdown=breakdown,
                    media=media,
                    raw_payload={
                        "message_id": int(message_id),
                        "chat_id": chat_id,
                        "chat_title": str(chat_title)[:256] if chat_title else "",
                        "chat_username": str(chat_username or "")[:128],
                        "author": message_author_name(msg),
                    },
                    source_metadata={
                        "peer_id": chat_id,
                        "chat_title": str(chat_title)[:256] if chat_title else "",
                        "chat_username": str(chat_username or "")[:128],
                        "message_id": int(message_id),
                        "media_group_id": str(media_group_id)
                        if media_group_id is not None
                        else None,
                        "forward_origin": forward_origin,
                        "is_edit": bool(edit_date),
                    },
                )
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        new_last_id = last_message_id
        for item in items:
            try:
                mid = int(str(item.external_id).rsplit("-", 1)[-1])
                new_last_id = max(new_last_id, mid)
            except (TypeError, ValueError):
                continue
        return FetchResult(
            items=items,
            status_code=200,
            duration_ms=duration_ms,
            raw_preview="",
        )
