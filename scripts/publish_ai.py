#!/usr/bin/env python3
"""Fetch, AI-rewrite, AI-score, and publish news to Telegram."""

import os
import sys

# Ensure we're in the project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

# Check if we're in the venv
if "VIRTUAL_ENV" not in os.environ:
    venv_python = os.path.join(PROJECT_ROOT, ".venv", "bin", "python")
    if os.path.exists(venv_python):
        os.execv(venv_python, [venv_python] + sys.argv)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

import django

django.setup()

import json
import re

import httpx

from apps.ai.providers import get_provider
from apps.publishing.telegram import render_post

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
PROXY = os.environ.get("HTTPS_PROXY", "")


def ai_rewrite(headline: str, body: str) -> dict:
    """Use AI to rewrite news in a professional style."""
    provider = get_provider()
    prompt = f"""تو یک ویرایشگر حرفه‌ای اخبار هوش مصنوعی هستی. این خبر رو بازنویسی کن.

قوانین تیتر:
- کوتاه و جذاب (حداکثر ۸ کلمه)
- حس کنجکاوی ایجاد کن
- از ایموجی مناسب استفاده کن
- مثل تیتر رسانه‌های حرفه‌ای باشد

قوانین متن:
- روان، شکیل و حرفه‌ای
- حداکثر ۴ جمله
- لحن خبری رسمی ولی دوستانه
- اطلاعات کلیدی رو مشخص کن
- منبع یا لینک ذکر نکن
- اگر فارسی است فارسی، اگر انگلیسی است فارسی بنویس

خروجی JSON:
{{"title": "تیتر جذاب کوتاه", "body": "متن بازنویسی شده شکیل", "importance": 0.8, "topic": "tech/ai/finance"}}

خبر:
عنوان: {headline}
متن: {body[:500]}"""

    try:
        result = provider.generate(task="rewrite", prompt=prompt, model="gpt", timeout=30)
        text = result.text.strip()
        # Extract JSON from response
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return data
    except Exception:
        pass
    return {"title": headline[:100], "body": body[:300], "importance": 0.5, "topic": "news"}


def publish(title: str, body: str, photo_file_id: str | None = None) -> bool:
    """Publish a story to Telegram channel, with optional photo."""
    if not BOT_TOKEN or not CHANNEL_ID:
        print("  [SKIP] No bot token or channel")
        return False

    rendered = render_post(headline=title, body=body)
    try:
        with httpx.Client(proxy=PROXY, timeout=20) as client:
            if photo_file_id:
                # Send photo with caption
                r = client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                    json={
                        "chat_id": CHANNEL_ID,
                        "photo": photo_file_id,
                        "caption": rendered.payload,
                        "parse_mode": "HTML",
                    },
                )
            else:
                # Send text only
                r = client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": CHANNEL_ID,
                        "text": rendered.payload,
                        "parse_mode": "HTML",
                    },
                )
        if r.status_code == 200 and r.json().get("ok"):
            msg_id = r.json()["result"]["message_id"]
            photo_str = " + photo" if photo_file_id else ""
            print(f"  Published (msg #{msg_id}{photo_str}): {title[:50]}")
            return True
        else:
            print(f"  Error: {r.text[:80]}")
            return False
    except Exception as e:
        print(f"  Error: {str(e)[:50]}")
        return False


def main():
    print("=" * 60)
    print("NewsAI - Canonical Pipeline Execution")
    print("=" * 60)

    from django.core.management import call_command

    # Run canonical newsroom intelligence pipeline with fetch & publication
    call_command("run_news_pipeline", fetch=True, live=True, limit=20)


if __name__ == "__main__":
    main()
