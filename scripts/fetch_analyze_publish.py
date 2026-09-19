#!/usr/bin/env python3
"""Fetch last 20 messages from each channel, analyze, rank, and publish best."""
import os, sys, time
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

import django
django.setup()

from django.utils import timezone
from apps.core.choices import Platform
from apps.sources.models import Source
from apps.sources.services.fetcher import source_fetch_service
from apps.stories.tasks import cluster_source_item_task
from apps.ranking.services.scoring import StoryScoringService
from apps.stories.models import Story, StoryMembership
from apps.news.models import SourceItem
from apps.publishing.telegram import render_post
from apps.ai.analysis import extract_news_value, classify_topic, detect_conflicts, combined_credibility
import httpx, json

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")

def fetch_all(limit=20):
    """Fetch last N messages from each active Telegram channel."""
    channels = Source.objects.filter(
        platform=Platform.TELEGRAM, enabled=True,
        identifier__in=[
            '@smartainewss','@ainews_fa','@NabzeAINews','@techhub_x',
            '@vigiatonet','@MatinSenPaii','@iaghapour','@AISpecialists_ir',
            '@technews_fa'
        ]
    )
    total_fetched = 0
    for src in channels:
        print(f"  Fetching {src.identifier}...", end=" ", flush=True)
        try:
            result = source_fetch_service.fetch_source(src.pk)
            n = result.get("created_count", 0)
            total_fetched += n
            print(f"new={n}")
        except Exception as e:
            print(f"error: {str(e)[:60]}")
    return total_fetched

def cluster_new():
    """Cluster all unassigned source items."""
    unassigned = SourceItem.objects.filter(story__isnull=True, status="collected")
    count = 0
    for item in unassigned[:200]:
        try:
            result = cluster_source_item(item.pk)
            if result.get("status") == "new_story":
                count += 1
        except Exception:
            pass
    return count

def analyze_and_rank():
    """Analyze stories with AI and rank them."""
    stories = Story.objects.filter(status="emerging").order_by("-latest_source_update_at")[:30]
    scored = []
    for s in stories:
        try:
            # Run AI analysis
            extract_news_value(s)
            classify_topic(s)
            detect_conflicts(s)
            cred = combined_credibility(s)
            # Get score
            result = StoryScoringService.score_story(s)
            final = float(result.get("final_score", 0.5))
            scored.append({
                "story": s,
                "credibility": float(cred) if cred else 0.5,
                "final_score": final,
                "title": s.canonical_title,
            })
        except Exception as e:
            # Score without AI
            try:
                result = StoryScoringService.score_story(s)
                final = float(result.get("final_score", 0.5))
                scored.append({
                    "story": s,
                    "credibility": 0.5,
                    "final_score": final,
                    "title": s.canonical_title,
                })
            except Exception:
                pass
    scored.sort(key=lambda x: x["final_score"], reverse=True)
    return scored

def publish_to_telegram(story_data):
    """Publish a story to the Telegram channel."""
    if not BOT_TOKEN or not CHANNEL_ID:
        print("    [SKIP] No bot token or channel configured")
        return False

    story = story_data["story"]
    items = SourceItem.objects.filter(story=story)[:3]
    title = story.canonical_title or "خبر جدید"
    body_parts = []
    for item in items:
        text = (item.raw_text or item.title or "")[:300]
        if text:
            body_parts.append(text)
    body = "\n\n".join(body_parts) if body_parts else title

    rendered = render_post(headline=title, body=body, source_urls=[
        item.canonical_url for item in items if item.canonical_url
    ][:3])

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": rendered.payload,
        "parse_mode": "HTML",
    }
    try:
        r = httpx.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            data = r.json()
            if data.get("ok"):
                print(f"    Published: {title[:50]} (msg #{data['result']['message_id']})")
                return True
            else:
                print(f"    [ERROR] {data.get('description', 'unknown')}")
                return False
        else:
            print(f"    [HTTP {r.status_code}] {r.text[:100]}")
            return False
    except Exception as e:
        print(f"    [ERROR] {str(e)[:80]}")
        return False

def main():
    print("=" * 60)
    print("NewsAI - Fetch, Analyze, Rank & Publish")
    print("=" * 60)

    print("\n[1/4] Fetching last messages from channels...")
    fetched = fetch_all(limit=20)
    print(f"  Total new items: {fetched}")

    print("\n[2/4] Clustering new items...")
    new_stories = cluster_new()
    print(f"  New stories created: {new_stories}")

    print("\n[3/4] Analyzing and ranking stories...")
    scored = analyze_and_rank()
    print(f"  Stories evaluated: {len(scored)}")
    if scored:
        print("\n  Top 10 by score:")
        for i, s in enumerate(scored[:10], 1):
            print(f"    {i}. [{s['final_score']:.3f}] {s['title'][:60]}")

    print("\n[4/4] Publishing best stories...")
    published = 0
    for s in scored[:3]:  # Publish top 3
        if s["final_score"] >= 0.5 and s["credibility"] >= 0.4:
            if publish_to_telegram(s):
                published += 1
                time.sleep(1)  # Rate limit

    print(f"\n{'=' * 60}")
    print(f"Done! Published {published} stories to Telegram channel")
    print(f"{'=' * 60}")

if __name__ == "__main__":
    main()
