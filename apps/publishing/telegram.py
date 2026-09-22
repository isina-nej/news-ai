"""Telegram Bot API sender and content renderer. Library behind a port."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MAX_CAPTION = 1024
MAX_TEXT = 4096
TEMPLATE_VERSION = "tg-v1"


@dataclass(frozen=True)
class RenderedPost:
    headline: str
    body: str
    footer: str
    payload: str
    parse_mode: str = "HTML"


def render_post(*, headline: str, body: str, source_urls: list[str] | None = None) -> RenderedPost:
    """Render and escape a versioned Telegram HTML payload."""
    clean_headline = (headline or "").strip()[:250]
    clean_body = (body or "").strip()[:3500]
    valid_urls = [
        u for u in (source_urls or []) if u.startswith("http://") or u.startswith("https://")
    ][:5]
    link_lines = "".join(f'\n<a href="{html.escape(u, quote=True)}">Source</a>' for u in valid_urls)

    # Format with nice structure
    payload = "━━━━━━━━━━━━━━━━\n"
    payload += f"<b>{html.escape(clean_headline)}</b>\n"
    payload += "━━━━━━━━━━━━━━━━\n\n"
    payload += f"{html.escape(clean_body)}\n\n"
    if link_lines:
        payload += f"{link_lines.strip()}\n\n"
    payload += "━━━━━━━━━━━━━━━━"

    if len(payload) > MAX_TEXT:
        payload = payload[: MAX_TEXT - 1] + "…"
    return RenderedPost(
        headline=clean_headline, body=clean_body, footer=link_lines.strip(), payload=payload
    )


class PublisherPort:
    def send(
        self,
        *,
        chat_id: str,
        payload: str,
        caption: str | None = None,
        photo: str | None = None,
        photos: list[str] | None = None,
        reply_to_message_id: str | None = None,
        parse_mode: str = "HTML",
    ) -> dict:
        raise NotImplementedError


class TelegramBotPublisher(PublisherPort):
    def __init__(self, *, bot_token: str, timeout: float = 15.0) -> None:
        from django.conf import settings
        from telegram import Bot

        if not bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not configured")
        proxy_url = str(getattr(settings, "HTTPS_PROXY", "") or "")
        if proxy_url:
            self._bot = Bot(token=bot_token, proxy=proxy_url)
        else:
            self._bot = Bot(token=bot_token)
        self._timeout = timeout

    def send(
        self,
        *,
        chat_id: str,
        payload: str,
        caption: str | None = None,
        photo: str | None = None,
        photos: list[str] | None = None,
        reply_to_message_id: str | None = None,
        parse_mode: str = "HTML",
    ) -> dict:
        import asyncio

        from telegram.error import RetryAfter, TimedOut

        reply_to_int = (
            int(reply_to_message_id)
            if reply_to_message_id and reply_to_message_id.isdigit()
            else None
        )

        async def _send():
            use_caption = caption or (
                payload if len(payload) <= MAX_CAPTION else payload[:1000] + "…"
            )
            media_urls = [p for p in (photos or ([photo] if photo else [])) if p][:10]
            single_target_photo = photo

            # 1. Media Group (Album) path for 2+ items
            if len(media_urls) >= 2:
                try:
                    from telegram import InputMediaPhoto

                    media_group = [
                        InputMediaPhoto(
                            media=m,
                            caption=use_caption if idx == 0 else None,
                            parse_mode=parse_mode,
                        )
                        for idx, m in enumerate(media_urls)
                    ]
                    msgs = await self._bot.send_media_group(
                        chat_id=chat_id,
                        media=media_group,
                        reply_to_message_id=reply_to_int,
                    )
                    file_ids = [m.photo[-1].file_id for m in msgs if m.photo]
                    first_msg = msgs[0]

                    follow_up_id = None
                    if len(payload) > MAX_CAPTION:
                        full_msg = await self._bot.send_message(
                            chat_id=chat_id,
                            text=payload,
                            parse_mode=parse_mode,
                            reply_to_message_id=first_msg.message_id,
                        )
                        follow_up_id = str(full_msg.message_id)

                    return {
                        "message_id": str(first_msg.message_id),
                        "file_id": file_ids[0] if file_ids else "",
                        "file_ids": file_ids,
                        "is_media_group": True,
                        "follow_up_message_id": follow_up_id,
                    }
                except Exception as album_err:
                    logger.warning(
                        "Telegram send_media_group failed, falling back to single photo: %s",
                        album_err,
                    )
                    single_target_photo = media_urls[0]

            # 2. Single Photo path
            if single_target_photo or (media_urls and len(media_urls) == 1):
                target_img = single_target_photo or media_urls[0]
                try:
                    msg = await self._bot.send_photo(
                        chat_id=chat_id,
                        photo=target_img,
                        caption=use_caption,
                        parse_mode=parse_mode,
                        reply_to_message_id=reply_to_int,
                    )
                    file_id = msg.photo[-1].file_id if msg.photo else ""
                    follow_up_id = None
                    if len(payload) > MAX_CAPTION:
                        full_msg = await self._bot.send_message(
                            chat_id=chat_id,
                            text=payload,
                            parse_mode=parse_mode,
                            reply_to_message_id=msg.message_id,
                        )
                        follow_up_id = str(full_msg.message_id)

                    return {
                        "message_id": str(msg.message_id),
                        "file_id": file_id,
                        "file_ids": [file_id] if file_id else [],
                        "follow_up_message_id": follow_up_id,
                    }
                except Exception as photo_err:
                    logger.warning(
                        "Telegram photo send failed, falling back to text: %s", photo_err
                    )
                    text_msg = await self._bot.send_message(
                        chat_id=chat_id,
                        text=payload,
                        parse_mode=parse_mode,
                        reply_to_message_id=reply_to_int,
                        disable_web_page_preview=False,
                    )
                    return {
                        "message_id": str(text_msg.message_id),
                        "photo_fallback": True,
                        "photo_error": str(photo_err)[:250],
                    }

            # 3. Text-only path
            message = await self._bot.send_message(
                chat_id=chat_id,
                text=payload,
                parse_mode=parse_mode,
                reply_to_message_id=reply_to_int,
                disable_web_page_preview=False,
            )
            return {"message_id": str(message.message_id)}

        try:
            return asyncio.run(_send())
        except RetryAfter as exc:
            return {"error": "retry_after", "retry_after": int(exc.retry_after)}
        except TimedOut as exc:
            raise TimeoutError(str(exc)) from exc


class FakePublisher(PublisherPort):
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(
        self,
        *,
        chat_id: str,
        payload: str,
        caption: str | None = None,
        photo: str | None = None,
        photos: list[str] | None = None,
        reply_to_message_id: str | None = None,
        parse_mode: str = "HTML",
    ) -> dict:
        media_list = photos or ([photo] if photo else [])
        entry = {
            "chat_id": chat_id,
            "payload": payload,
            "caption": caption,
            "photo": photo or (media_list[0] if media_list else None),
            "photos": media_list,
            "is_media_group": len(media_list) >= 2,
            "reply_to_message_id": reply_to_message_id,
            "parse_mode": parse_mode,
        }
        self.sent.append(entry)
        msg_id = f"fake-{len(self.sent)}"
        return {
            "message_id": msg_id,
            "file_id": f"fake-file-{len(self.sent)}" if media_list else "",
            "file_ids": [f"fake-file-{len(self.sent)}-{i}" for i in range(len(media_list))],
            "is_media_group": len(media_list) >= 2,
        }
