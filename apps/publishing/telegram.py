"""Telegram Bot API sender and content renderer. Library behind a port."""

from __future__ import annotations

import html
from dataclasses import dataclass

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


def render_post(*, headline: str, body: str, source_urls: list[str]) -> RenderedPost:
    """Render and escape a versioned Telegram HTML payload."""
    clean_headline = (headline or "").strip()[:250]
    clean_body = (body or "").strip()[:3500]
    urls = [u for u in (source_urls or []) if u.startswith("http")][:5]
    link_lines = "".join(f'\n<a href="{html.escape(u, quote=True)}">Source</a>' for u in urls)
    payload = f"<b>{html.escape(clean_headline)}</b>\n\n{html.escape(clean_body)}"
    if link_lines:
        payload = f"{payload}\n{link_lines}"
    if len(payload) > MAX_TEXT:
        payload = payload[: MAX_TEXT - 1] + "…"
    return RenderedPost(
        headline=clean_headline, body=clean_body, footer=link_lines, payload=payload
    )


class PublisherPort:
    def send(self, *, chat_id: str, payload: str, parse_mode: str) -> dict:
        raise NotImplementedError


class TelegramBotPublisher(PublisherPort):
    def __init__(self, *, bot_token: str, timeout: float = 15.0) -> None:
        from telegram import Bot

        if not bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not configured")
        self._bot = Bot(token=bot_token)
        self._timeout = timeout

    def send(self, *, chat_id: str, payload: str, parse_mode: str) -> dict:
        import asyncio

        from telegram.error import RetryAfter, TimedOut

        async def _send():
            message = await self._bot.send_message(
                chat_id=chat_id,
                text=payload,
                parse_mode=parse_mode,
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

    def send(self, *, chat_id: str, payload: str, parse_mode: str) -> dict:
        self.sent.append({"chat_id": chat_id, "payload": payload, "parse_mode": parse_mode})
        return {"message_id": f"fake-{len(self.sent)}"}
