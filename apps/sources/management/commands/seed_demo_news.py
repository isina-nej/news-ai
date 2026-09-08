"""Seed realistic demo news dataset for local development and E2E validation."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.core.choices import Platform
from apps.news.models import EngagementSnapshot, SourceItem
from apps.ops.models import Subtopic, Topic
from apps.sources.models import Source


class Command(BaseCommand):
    help = "Seed demo sources and source items for local development and dry-run pipeline."

    def handle(self, *args, **options):
        now = timezone.now()

        # 1. Topics
        tech, _ = Topic.objects.get_or_create(
            slug="tech", defaults={"name": "Technology", "weight": Decimal("1.2")}
        )
        ai_sub, _ = Subtopic.objects.get_or_create(
            topic=tech, slug="ai", defaults={"name": "Artificial Intelligence"}
        )
        finance, _ = Topic.objects.get_or_create(
            slug="finance", defaults={"name": "Finance & Markets", "weight": Decimal("1.0")}
        )

        # 2. Sources
        reuters, _ = Source.objects.get_or_create(
            platform=Platform.RSS,
            identifier="https://reuters.example.com/tech",
            defaults={
                "name": "Reuters Tech",
                "trust_score": Decimal("0.90"),
                "reliability_score": Decimal("0.90"),
            },
        )
        bloomberg, _ = Source.objects.get_or_create(
            platform=Platform.RSS,
            identifier="https://bloomberg.example.com/markets",
            defaults={
                "name": "Bloomberg Markets",
                "trust_score": Decimal("0.92"),
                "reliability_score": Decimal("0.90"),
            },
        )
        tg_channel, _ = Source.objects.get_or_create(
            platform=Platform.TELEGRAM,
            identifier="@technews_fa",
            defaults={
                "name": "TechNews Persian",
                "trust_score": Decimal("0.75"),
                "reliability_score": Decimal("0.70"),
                "language": "fa",
            },
        )

        # 3. Source Items
        demo_items = [
            (
                reuters,
                "OpenAI announces next generation flagship model GPT-5",
                (
                    "OpenAI officially unveiled GPT-5 today featuring breakthrough reasoning "
                    "capabilities across STEM fields and 50 percent lower inference latency."
                ),
                now - timedelta(hours=3),
                tech,
                ai_sub,
                15000,
            ),
            (
                tg_channel,
                "اوپن‌ای‌آی از مدل جدید خود GPT-5 رونمایی کرد",
                (
                    "شرکت OpenAI دقایقی پیش مدل پرچمدار جدید خود با نام GPT-5 را با قدرت "
                    "استدلال شگفت‌انگیز و کاهش تأخیر معرفی نمود."
                ),
                now - timedelta(hours=2),
                tech,
                ai_sub,
                4200,
            ),
            (
                bloomberg,
                "Federal Reserve holds interest rates steady amid balanced labor data",
                (
                    "The Federal Reserve decided to maintain benchmark borrowing costs at "
                    "5.25 percent following its two-day policy meeting in Washington."
                ),
                now - timedelta(hours=4),
                finance,
                None,
                89000,
            ),
        ]

        created_count = 0
        for src, title, text, pub_time, top, subtop, views in demo_items:
            item, created = SourceItem.objects.get_or_create(
                source=src,
                title=title,
                defaults={
                    "raw_text": text,
                    "normalized_text": text,
                    "published_at": pub_time,
                    "topic": top,
                    "subtopic": subtop,
                    "language": src.language or "en",
                },
            )
            if created:
                created_count += 1
                EngagementSnapshot.objects.create(
                    source_item=item,
                    views=views,
                    forwards=int(views * 0.08),
                    reactions=int(views * 0.05),
                    post_age_seconds=3600,
                )

        self.stdout.write(
            self.style.SUCCESS(f"Demo news seeded successfully. Created {created_count} items.")
        )
