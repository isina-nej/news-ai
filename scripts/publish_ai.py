#!/usr/bin/env python3
"""Fetch, AI-rewrite, AI-score, and publish news to Telegram."""
import os, sys, time
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

import django
django.setup()

from apps.core.choices import Platform
from apps.sources.models import Source
from apps.sources.services.fetcher import source_fetch_service
from apps.stories.models import Story
from apps.news.models import SourceItem
from apps.publishing.telegram import render_post
from apps.ai.providers import get_provider
import httpx, json, re

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
PROXY = os.environ.get("HTTPS_PROXY", "")

def ai_rewrite(headline: str, body: str) -> dict:
    """Use AI to rewrite news in a professional style."""
    provider = get_provider()
    prompt = f"""این خبر را بازنویسی کن. قوانین:
1. منبع یا لینک ذکر نکن
2. متن باید روان و حرفه‌ای باشد
3. حداکثر ۳ جمله
4. اگر فارسی است، فارسی بنویس
5. اگر انگلیسی است، فارسی بنویس
6. فقط محتوای خبر را بنویس، نظر شخصی اضافه نکن

قوانین خروجی JSON:
{{"title": "عنوان خبر", "body": "متن بازنویسی شده", "importance": 0.8, "topic": "tech/ai/finance"}}

خبر:
عنوان: {headline}
متن: {body[:500]}"""

    try:
        result = provider.generate(task="rewrite", prompt=prompt, model="gpt", timeout=30)
        text = result.text.strip()
        # Extract JSON from response
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return data
    except Exception as e:
        pass
    return {"title": headline[:100], "body": body[:300], "importance": 0.5, "topic": "news"}

def publish(title: str, body: str) -> bool:
    """Publish a story to Telegram channel."""
    if not BOT_TOKEN or not CHANNEL_ID:
        print("  [SKIP] No bot token or channel")
        return False

    rendered = render_post(headline=title, body=body)
    try:
        with httpx.Client(proxy=PROXY, timeout=20) as client:
            r = client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHANNEL_ID, "text": rendered.payload, "parse_mode": "HTML"}
            )
        if r.status_code == 200 and r.json().get("ok"):
            msg_id = r.json()["result"]["message_id"]
            print(f"  Published (msg #{msg_id}): {title[:50]}")
            return True
        else:
            print(f"  Error: {r.text[:80]}")
            return False
    except Exception as e:
        print(f"  Error: {str(e)[:50]}")
        return False

def main():
    print("=" * 60)
    print("NewsAI - AI-Powered News Publishing")
    print("=" * 60)

    # Step 1: Fetch all channels
    print("\n[1/4] Fetching channels...")
    channels = Source.objects.filter(
        platform=Platform.TELEGRAM, enabled=True,
        identifier__in=['@smartainewss','@ainews_fa','@NabzeAINews','@techhub_x',
                       '@vigiatonet','@MatinSenPaii','@iaghapour','@AISpecialists_ir']
    )
    for src in channels:
        try:
            r = source_fetch_service.fetch_source(src.pk)
            print(f"  {src.identifier}: new={r.get('created_count',0)}")
        except: pass

    # Step 2: Get latest stories
    print("\n[2/4] Getting stories...")
    stories = Story.objects.filter(status='emerging').order_by('-latest_source_update_at')[:20]
    print(f"  Found {stories.count()} stories")

    # Step 3: AI rewrite and score
    print("\n[3/4] AI rewriting and scoring...")
    published = 0
    for s in stories:
        items = SourceItem.objects.filter(story=s)[:3]
        if not items:
            continue

        # Get raw text
        headline = s.canonical_title or ""
        body_parts = []
        for item in items:
            text = (item.raw_text or item.title or "")[:300]
            if text:
                body_parts.append(text)
        raw_body = " ".join(body_parts)

        if not headline and not raw_body:
            continue

        # AI rewrite
        print(f"  Rewriting: {headline[:40]}...", end=" ", flush=True)
        rewritten = ai_rewrite(headline, raw_body)
        new_title = rewritten.get("title", headline[:100])
        new_body = rewritten.get("body", raw_body[:300])
        importance = rewritten.get("importance", 0.5)

        # AI score
        if importance >= 0.6:
            print(f"[score={importance:.1f}] -> Publish")
            if publish(new_title, new_body):
                published += 1
                time.sleep(2)
        else:
            print(f"[score={importance:.1f}] -> Skip")

        if published >= 5:
            break

    print(f"\n{'=' * 60}")
    print(f"Done! Published {published} stories")
    print(f"{'=' * 60}")

if __name__ == "__main__":
    main()
