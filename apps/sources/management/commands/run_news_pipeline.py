"""Run end-to-end news pipeline from clustering to publication."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.ai.analysis import classify_topic, detect_conflicts, extract_news_value
from apps.news.models import SourceItem
from apps.ops.services.bandit import ContextualBanditService
from apps.publishing.services import publish_story
from apps.ranking.services.selection import SelectionService
from apps.stories.models import Story
from apps.stories.services.clustering import story_clustering_service


class Command(BaseCommand):
    help = "Run the full NewsAI intelligence and publishing pipeline."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=True,
            help="Simulate full pipeline without sending external publication requests.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Maximum items/stories to process per step.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]

        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS("NEWS-AI PIPELINE EXECUTION"))
        self.stdout.write(self.style.SUCCESS(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}"))
        self.stdout.write(self.style.SUCCESS("=" * 60))

        # 1. Clustering
        unclustered = list(SourceItem.objects.filter(story__isnull=True)[:limit])
        self.stdout.write(f"\nStep 1: Clustering {len(unclustered)} unassigned items...")
        clustered_count = 0
        for item in unclustered:
            res = story_clustering_service.cluster_item(item.pk)
            if res.get("status") in ("matched", "new_story"):
                clustered_count += 1
        self.stdout.write(f"  -> Successfully clustered {clustered_count} items.")

        # 2. AI Intelligence Layer
        stories = list(
            Story.objects.exclude(status="merged").order_by("-latest_source_update_at")[:limit]
        )
        self.stdout.write(
            f"\nStep 2: Running AI Intelligence Analysis on {len(stories)} stories..."
        )
        for story in stories:
            try:
                classify_topic(story)
                extract_news_value(story)
                detect_conflicts(story)
            except Exception as exc:
                self.stdout.write(
                    self.style.WARNING(f"  AI analysis warning for story #{story.pk}: {exc}")
                )
        self.stdout.write("  -> AI analysis complete.")

        # 3. Ranking & Selection
        self.stdout.write(
            f"\nStep 3: Ranking and Evaluating Selection for {len(stories)} stories..."
        )
        selected_stories = []
        for story in stories:
            eval_res = SelectionService.evaluate_story(story)
            action = eval_res["action"]
            adj_score = eval_res["adjusted_score"]
            title_preview = story.canonical_title[:40]
            self.stdout.write(
                f"  - Story #{story.pk} '{title_preview}': {action.upper()} ({adj_score})"
            )
            if action == "publish":
                selected_stories.append((story, eval_res))

        # 4. Publication Dispatch
        self.stdout.write(f"\nStep 4: Publishing Dispatch ({len(selected_stories)} candidates)...")
        for story, _eval_res in selected_stories:
            pub_res = publish_story(story.pk, dry_run=dry_run, force=True)
            self.stdout.write(f"  - Story #{story.pk} -> {pub_res.get('status')}")

            # 5. Feedback simulation for dry run
            action, is_exp, _ = ContextualBanditService.select_action(story=story)
            self.stdout.write(
                f"    Style selected: {action.get('headline_style')} (exploration={is_exp})"
            )

        self.stdout.write(self.style.SUCCESS("\n" + "=" * 60))
        self.stdout.write(self.style.SUCCESS("PIPELINE COMPLETED SUCCESSFULLY."))
        self.stdout.write(self.style.SUCCESS("=" * 60))
