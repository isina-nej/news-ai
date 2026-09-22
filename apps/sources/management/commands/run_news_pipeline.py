"""Run end-to-end news pipeline from fetch and clustering to momentum, intelligence, and publication."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.ai.analysis import (
    analyze_story_intelligence,
    classify_topic,
    detect_conflicts,
    extract_news_value,
)
from apps.news.models import SourceItem
from apps.ops.services.bandit import ContextualBanditService
from apps.publishing.services import publish_story
from apps.ranking.services.selection import SelectionService
from apps.sources.models import Source
from apps.sources.services.fetcher import source_fetch_service
from apps.stories.models import Story
from apps.stories.services.clustering import story_clustering_service
from apps.stories.services.coordinator import StoryReanalysisCoordinator


class Command(BaseCommand):
    help = "Run the full NewsAI intelligence and publishing pipeline."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Simulate full pipeline without sending external publication requests.",
        )
        parser.add_argument(
            "--live",
            action="store_true",
            default=False,
            help="Send live publication requests to Telegram channel.",
        )
        parser.add_argument(
            "--fetch",
            action="store_true",
            default=False,
            help="Fetch latest items from all enabled sources first.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            default=False,
            help="Force publish regardless of auto-publish flag.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Maximum items/stories to process per step.",
        )

    def handle(self, *args, **options):
        is_live = options["live"]
        dry_run = options["dry_run"] or (not is_live)
        force = options["force"] or is_live
        fetch_first = options["fetch"]
        limit = options["limit"]

        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS("NEWS-AI PIPELINE EXECUTION"))
        self.stdout.write(self.style.SUCCESS(f"Mode: {'LIVE' if is_live else 'DRY RUN'}"))
        self.stdout.write(self.style.SUCCESS("=" * 60))

        # 0. Fetch (Optional)
        if fetch_first:
            enabled_sources = list(Source.objects.filter(enabled=True))
            self.stdout.write(f"\nStep 0: Fetching from {len(enabled_sources)} enabled sources...")
            total_new = 0
            for src in enabled_sources:
                try:
                    f_res = source_fetch_service.fetch_source(src.pk)
                    new_cnt = f_res.get("created_count", 0)
                    total_new += new_cnt
                    self.stdout.write(f"  - {src.name or src.identifier}: {new_cnt} new items")
                except Exception as exc:
                    self.stdout.write(
                        self.style.WARNING(f"  Fetch error for {src.identifier}: {exc}")
                    )
            self.stdout.write(f"  -> Total new items fetched: {total_new}")

        # 1. Clustering
        unclustered = list(SourceItem.objects.filter(story__isnull=True)[:limit])
        self.stdout.write(f"\nStep 1: Clustering {len(unclustered)} unassigned items...")
        clustered_count = 0
        for item in unclustered:
            res = story_clustering_service.cluster_item(item.pk)
            if res.get("status") in ("matched", "new_story"):
                clustered_count += 1
        self.stdout.write(f"  -> Successfully clustered {clustered_count} items.")

        # 2. Real-Time Momentum & Lifecycle Tracking
        stories = list(
            Story.objects.exclude(status="merged").order_by("-latest_source_update_at")[:limit]
        )
        self.stdout.write(
            f"\nStep 2: Tracking Momentum and Lifecycle for {len(stories)} stories..."
        )
        for story in stories:
            try:
                m_res = StoryReanalysisCoordinator.process_story(story.pk)
                self.stdout.write(
                    f"  - Story #{story.pk}: {m_res.get('lifecycle_state')}/{m_res.get('trend_state')} "
                    f"(mom={m_res.get('momentum_score'):.3f})"
                )
            except Exception as exc:
                self.stdout.write(
                    self.style.WARNING(f"  Momentum tracking warning for #{story.pk}: {exc}")
                )

        # 3. AI Intelligence Layer
        self.stdout.write(
            f"\nStep 3: Running AI Intelligence Analysis on {len(stories)} stories..."
        )
        for story in stories:
            try:
                classify_topic(story)
                extract_news_value(story)
                detect_conflicts(story)
                analyze_story_intelligence(story)
            except Exception as exc:
                self.stdout.write(
                    self.style.WARNING(f"  AI analysis warning for story #{story.pk}: {exc}")
                )
        self.stdout.write("  -> AI analysis complete.")

        # 4. Ranking & Selection
        self.stdout.write(
            f"\nStep 4: Ranking and Evaluating Selection for {len(stories)} stories..."
        )
        selected_stories = []
        for story in stories:
            eval_res = SelectionService.evaluate_story(story)
            action = eval_res["action"]
            editorial_action = eval_res.get("editorial_action", action)
            adj_score = eval_res["adjusted_score"]
            title_preview = story.canonical_title[:40]
            self.stdout.write(
                f"  - Story #{story.pk} '{title_preview}': {editorial_action.upper()} (Priority={adj_score})"
            )
            if action == "publish" or force:
                selected_stories.append((story, eval_res))

        # 5. Publication Dispatch
        self.stdout.write(f"\nStep 5: Publishing Dispatch ({len(selected_stories)} candidates)...")
        for story, _eval_res in selected_stories:
            pub_res = publish_story(story.pk, dry_run=dry_run, force=force)
            status = pub_res.get("status")
            pub_id = pub_res.get("publication_id")
            msg_id = pub_res.get("message_id")
            self.stdout.write(
                f"  - Story #{story.pk} -> {status.upper()} (Pub #{pub_id}, Msg #{msg_id})"
            )

            # 6. Feedback simulation for dry run
            action, is_exp, _ = ContextualBanditService.select_action(story=story)
            self.stdout.write(
                f"    Style selected: {action.get('headline_style')} (exploration={is_exp})"
            )

        self.stdout.write(self.style.SUCCESS("\n" + "=" * 60))
        self.stdout.write(self.style.SUCCESS("PIPELINE COMPLETED SUCCESSFULLY."))
        self.stdout.write(self.style.SUCCESS("=" * 60))
