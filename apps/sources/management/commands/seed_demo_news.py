"""Seed realistic demo news dataset for local development and E2E validation."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.choices import Platform
from apps.news.models import EngagementSnapshot, SourceItem
from apps.ops.models import Subtopic, Topic
from apps.sources.models import Source

DEMO_ITEMS = (
    (
        "https://reuters.example.com/tech",
        "OpenAI announces next generation flagship model GPT-5",
        "OpenAI officially unveiled GPT-5 today featuring breakthrough reasoning "
        "capabilities across STEM fields and 50 percent lower inference latency.",
        timedelta(hours=3),
        "tech",
        "ai",
        15000,
        "demo-reuters-gpt5",
    ),
    (
        "@technews_fa",
        "اوپن‌ای‌آی از مدل جدید خود GPT-5 رونمایی کرد",
        "شرکت OpenAI دقایقی پیش مدل پرچمدار جدید خود با نام GPT-5 را با قدرت "
        "استدلال شگفت‌انگیز و کاهش تأخیر معرفی نمود.",
        timedelta(hours=2),
        "tech",
        "ai",
        4200,
        "demo-technewsfa-gpt5",
    ),
    (
        "https://bloomberg.example.com/markets",
        "Federal Reserve holds interest rates steady amid balanced labor data",
        "The Federal Reserve decided to maintain benchmark borrowing costs at "
        "5.25 percent following its two-day policy meeting in Washington.",
        timedelta(hours=4),
        "finance",
        None,
        89000,
        "demo-bloomberg-fed",
    ),
)


class Command(BaseCommand):
    help = "Seed demo sources and source items for local development and dry-run pipeline."

    def handle(self, *args, **options):
        now = timezone.now()

        tech, _ = Topic.objects.get_or_create(
            slug="tech", defaults={"name": "Technology", "weight": Decimal("1.2")}
        )
        ai_sub, _ = Subtopic.objects.get_or_create(
            topic=tech, slug="ai", defaults={"name": "Artificial Intelligence"}
        )
        finance, _ = Topic.objects.get_or_create(
            slug="finance", defaults={"name": "Finance & Markets", "weight": Decimal("1.0")}
        )
        topics = {"tech": tech, "finance": finance}
        subtopics = {"ai": ai_sub}

        sources = {
            "https://reuters.example.com/tech": Source.objects.get_or_create(
                platform=Platform.RSS,
                identifier="https://reuters.example.com/tech",
                defaults={
                    "name": "Reuters Tech",
                    "trust_score": Decimal("0.90"),
                    "reliability_score": Decimal("0.90"),
                },
            )[0],
            "@technews_fa": Source.objects.get_or_create(
                platform=Platform.TELEGRAM,
                identifier="@technews_fa",
                defaults={
                    "name": "TechNews Persian",
                    "trust_score": Decimal("0.75"),
                    "reliability_score": Decimal("0.70"),
                    "language": "fa",
                },
            )[0],
            "https://bloomberg.example.com/markets": Source.objects.get_or_create(
                platform=Platform.RSS,
                identifier="https://bloomberg.example.com/markets",
                defaults={
                    "name": "Bloomberg Markets",
                    "trust_score": Decimal("0.92"),
                    "reliability_score": Decimal("0.90"),
                },
            )[0],
        }

        created_count = 0
        with transaction.atomic():
            for item_spec in DEMO_ITEMS:
                identifier, title, text, age, topic_slug, sub_slug, views, external_id = item_spec
                src = sources[identifier]
                # ponytail: rows may predate demo external_ids (same content_hash
                # with NULL external_id). Look up explicitly; get_or_create on
                # external_id alone raises ValidationError (not IntegrityError)
                # because save() runs full_clean with constraint checks.
                item = SourceItem.objects.filter(source=src, external_id=external_id).first()
                created = False
                if item is None:
                    content_hash = hashlib.sha256(text.strip().encode()).hexdigest()
                    item = SourceItem.objects.filter(source=src, content_hash=content_hash).first()
                    if item is not None:
                        item.external_id = external_id
                        item.save(update_fields=["external_id"])
                    else:
                        item = SourceItem(
                            source=src,
                            external_id=external_id,
                            title=title,
                            raw_text=text,
                            normalized_text=text,
                            published_at=now - age,
                            topic=topics[topic_slug],
                            subtopic=subtopics.get(sub_slug) if sub_slug else None,
                            language=src.language or "en",
                        )
                        item.save()
                        created = True
                if created:
                    created_count += 1
                    EngagementSnapshot.objects.get_or_create(
                        source_item=item,
                        target_age_seconds=3600,
                        defaults={
                            "views": views,
                            "forwards": int(views * 0.08),
                            "reactions": int(views * 0.05),
                            "post_age_seconds": 3600,
                        },
                    )

        self.stdout.write(
            self.style.SUCCESS(f"Demo news seeded successfully. Created {created_count} items.")
        )
