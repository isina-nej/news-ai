"""Safe, explicit serializers for Kurigram/Telegram message objects.

Nothing here imports Kurigram at module level: all inputs are duck-typed
so the rest of the system never depends on the vendor SDK. Sensitive and
unstable fields (access_hash, file_reference, auth/session state) are
dropped by construction — only an explicit allowlist is ever emitted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _as_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromtimestamp(float(value), tz=UTC)
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _text_of(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    text = getattr(value, "text", None)
    if isinstance(text, str):
        return text
    try:
        return str(value)
    except Exception:
        return ""


def _message_text(msg: Any) -> str:
    return _text_of(getattr(msg, "text", None)) or _text_of(getattr(msg, "caption", None))


def _media_kind(msg: Any) -> str | None:
    media = getattr(msg, "media", None)
    if media is None:
        for attr in (
            "photo",
            "video",
            "document",
            "audio",
            "voice",
            "animation",
            "poll",
            "sticker",
        ):
            if getattr(msg, attr, None) is not None:
                return attr
        return None
    name = type(media).__name__
    lowered = name.lower()
    for kind in ("photo", "video", "document", "audio", "voice", "animation", "poll", "sticker"):
        if kind in lowered:
            return kind
    raw = str(media).lower()
    if "photo" in raw:
        return "photo"
    if "video" in raw:
        return "video"
    if "poll" in raw:
        return "poll"
    return "document"


def _safe_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def serialize_media(msg: Any) -> dict[str, Any]:
    """Extract safe media metadata. Never downloads bytes or leaks references."""
    media_meta: dict[str, Any] = {}
    kind = _media_kind(msg)
    if kind is None:
        return media_meta
    media_meta["kind"] = kind
    media_group_id = getattr(msg, "media_group_id", None)
    if media_group_id is not None:
        media_meta["media_group_id"] = str(media_group_id)
    if getattr(msg, "has_media_spoiler", None):
        media_meta["has_spoiler"] = True

    blob = None
    for attr in ("photo", "video", "document", "audio", "voice", "animation"):
        candidate = getattr(msg, attr, None)
        if candidate is not None:
            blob = candidate
            break
    if blob is None and kind == "poll":
        poll = getattr(msg, "poll", None)
        if poll is not None:
            media_meta["poll_id"] = str(getattr(poll, "id", "") or "")
            media_meta["question"] = _text_of(getattr(poll, "question", ""))
            media_meta["total_voters"] = _safe_int(getattr(poll, "total_voter_count", None))
        return media_meta
    if blob is None:
        return media_meta

    for field_name in ("mime_type", "file_name", "file_size", "width", "height", "duration"):
        value = getattr(blob, field_name, None)
        if value is None:
            continue
        if field_name in ("file_size", "width", "height", "duration"):
            safe = _safe_int(value)
            if safe is not None:
                media_meta[field_name] = safe
        elif isinstance(value, str) and value:
            media_meta[field_name] = value[:256]
    return media_meta


def serialize_forward_origin(msg: Any) -> dict[str, Any]:
    """Record forward provenance without treating it as an independent source."""
    origin = getattr(msg, "forward_origin", None)
    if origin is None:
        # Legacy compat: older SDKs expose forward_from_* scalar fields.
        legacy: dict[str, Any] = {}
        chat = getattr(msg, "forward_from_chat", None)
        if chat is not None:
            title = getattr(chat, "title", None) or getattr(chat, "username", None)
            if title:
                legacy["chat_title"] = str(title)[:256]
            username = getattr(chat, "username", None)
            if username:
                legacy["chat_username"] = str(username)[:128]
        sender_name = getattr(msg, "forward_sender_name", None)
        if sender_name:
            legacy["sender_name"] = str(sender_name)[:256]
        fwd_date = _as_utc(getattr(msg, "forward_date", None))
        if fwd_date is not None:
            legacy["date"] = fwd_date.isoformat()
        return legacy
    info: dict[str, Any] = {"type": type(origin).__name__}
    fwd_date = _as_utc(getattr(origin, "date", None))
    if fwd_date is not None:
        info["date"] = fwd_date.isoformat()
    for attr in ("chat", "sender_chat"):
        chat = getattr(origin, attr, None)
        if chat is not None:
            title = getattr(chat, "title", None) or getattr(chat, "username", None)
            if title:
                info["chat_title"] = str(title)[:256]
            username = getattr(chat, "username", None)
            if username:
                info["chat_username"] = str(username)[:128]
            break
    for attr in ("sender_user", "sender_user_name", "author_signature"):
        value = getattr(origin, attr, None)
        if value is None:
            continue
        if isinstance(value, str) and value:
            info["sender_name"] = value[:256]
        else:
            name = getattr(value, "first_name", None) or getattr(value, "username", None)
            if name:
                info["sender_name"] = str(name)[:256]
        break
    message_id = _safe_int(getattr(origin, "message_id", None))
    if message_id is not None:
        info["message_id"] = message_id
    return info


def serialize_reactions(msg: Any) -> tuple[int | None, dict[str, int]]:
    """Return (total_reactions, breakdown). Totals only; no user identities."""
    reactions = getattr(msg, "reactions", None)
    if reactions is None:
        return None, {}
    entries = getattr(reactions, "reactions", None) or []
    breakdown: dict[str, int] = {}
    total = 0
    for entry in entries:
        count = _safe_int(getattr(entry, "count", None)) or 0
        label = (
            getattr(entry, "emoji", None)
            or getattr(entry, "custom_emoji_id", None)
            or type(entry).__name__
        )
        label = str(label)[:64]
        if count > 0:
            breakdown[label] = breakdown.get(label, 0) + count
            total += count
    if not breakdown:
        return None, {}
    return total, breakdown


def message_content_type(msg: Any) -> str:
    """Map a Telegram message to a platform-neutral content type string."""
    if getattr(msg, "poll", None) is not None:
        return "poll"
    kind = _media_kind(msg)
    if kind == "video":
        return "video"
    if kind == "photo":
        return "image"
    if kind == "audio" or kind == "voice":
        return "audio"
    return "post"


def is_skippable_service_message(msg: Any) -> bool:
    """Service/system/sponsored-only messages never enter the news pipeline."""
    if getattr(msg, "empty", False):
        return True
    service = getattr(msg, "service", None)
    if service is not None:
        try:
            name = service.name if hasattr(service, "name") else str(service)
        except Exception:
            name = str(service)
        if name and name.upper() not in ("", "NONE"):
            return True
    return False


def message_author_name(msg: Any) -> str:
    for attr in ("author_signature",):
        value = getattr(msg, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()[:256]
    sender = getattr(msg, "from_user", None)
    if sender is not None:
        parts = [getattr(sender, "first_name", "") or "", getattr(sender, "last_name", "") or ""]
        full = " ".join(p for p in parts if p).strip()
        if full:
            return full[:256]
        username = getattr(sender, "username", None)
        if username:
            return str(username)[:256]
    chat = getattr(msg, "sender_chat", None)
    if chat is not None:
        title = getattr(chat, "title", None) or getattr(chat, "username", None)
        if title:
            return str(title)[:256]
    return ""


def message_date_utc(msg: Any) -> datetime | None:
    return _as_utc(getattr(msg, "date", None))


def message_edit_date_utc(msg: Any) -> datetime | None:
    return _as_utc(getattr(msg, "edit_date", None))
