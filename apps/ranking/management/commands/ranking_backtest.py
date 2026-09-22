"""Ranking backtest management command. Replays ranking decisions on historical stories."""

from __future__ import annotations

import json
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.ranking.services.selection import SelectionService
from apps.stories.models import Story


class Command(BaseCommand):
    help = "Replay ranking and selection decisions on historical stories without publishing."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7, help="Lookback window in days")
        parser.add_argument("--limit", type=int, default=50, help="Max stories to replay")
        parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
        parser.add_argument(
            "--ranking-version",
            type=str,
            default="ranking-v1",
            help="Algorithm version tag to replay",
        )
        parser.add_argument(
            "--momentum-version",
            type=str,
            default="momentum-v1",
            help="Momentum version tag to replay",
        )
        parser.add_argument(
            "--from",
            dest="from_date",
            type=str,
            default=None,
            help="ISO start datetime filter",
        )
        parser.add_argument(
            "--to",
            dest="to_date",
            type=str,
            default=None,
            help="ISO end datetime filter",
        )

    def handle(self, *args, **options):
        days = options["days"]
        limit = options["limit"]
        output_json = options["json"]

        from_date = options.get("from_date")
        to_date = options.get("to_date")
        ranking_ver = options.get("ranking_version", "ranking-v1")
        momentum_ver = options.get("momentum_version", "momentum-v1")

        qs = Story.objects.exclude(status="merged")
        if from_date:
            try:
                qs = qs.filter(
                    latest_source_update_at__gte=timezone.datetime.fromisoformat(from_date)
                )
            except Exception:
                pass
        else:
            cutoff = timezone.now() - timedelta(days=days)
            qs = qs.filter(latest_source_update_at__gte=cutoff)

        if to_date:
            try:
                qs = qs.filter(
                    latest_source_update_at__lte=timezone.datetime.fromisoformat(to_date)
                )
            except Exception:
                pass

        stories = list(qs.order_by("-latest_source_update_at")[:limit])

        total = len(stories)
        actions: dict[str, int] = {"publish": 0, "skip": 0, "hold": 0}
        gate_reasons: dict[str, int] = {}
        scores: list[float] = []
        records = []

        for story in stories:
            res = SelectionService.evaluate_story(story)
            act = res["action"]
            actions[act] = actions.get(act, 0) + 1
            if act == "hold":
                reason = res["reason"]
                gate_reasons[reason] = gate_reasons.get(reason, 0) + 1
            scores.append(res["adjusted_score"])
            records.append(
                {
                    "story_id": story.pk,
                    "title": story.canonical_title[:60],
                    "action": act,
                    "editorial_action": res.get("editorial_action", act),
                    "newsworthiness_score": res.get("newsworthiness_score", 0.0),
                    "publish_priority_score": res.get(
                        "publish_priority_score", res["adjusted_score"]
                    ),
                    "adjusted_score": res["adjusted_score"],
                    "final_score": res["scored"]["final_score"],
                    "reason": res["reason"],
                    "ranking_version": ranking_ver,
                    "momentum_version": momentum_ver,
                }
            )

        report = {
            "total_stories": total,
            "actions": actions,
            "gate_reasons": gate_reasons,
            "avg_adjusted_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
            "max_adjusted_score": max(scores) if scores else 0.0,
            "min_adjusted_score": min(scores) if scores else 0.0,
            "records": records,
        }

        if output_json:
            self.stdout.write(json.dumps(report, indent=2))
            return

        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS(f"RANKING BACKTEST REPORT (Last {days} days)"))
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(f"Total Evaluated: {total}")
        self.stdout.write(f"  - Publish: {actions.get('publish', 0)}")
        self.stdout.write(f"  - Skip:    {actions.get('skip', 0)}")
        self.stdout.write(f"  - Hold:    {actions.get('hold', 0)}")
        if gate_reasons:
            self.stdout.write("\nGate Triggers:")
            for reason, count in gate_reasons.items():
                self.stdout.write(f"    * {reason}: {count}")
        self.stdout.write(
            f"\nScores: Min={report['min_adjusted_score']:.4f}, "
            f"Avg={report['avg_adjusted_score']:.4f}, Max={report['max_adjusted_score']:.4f}"
        )
        self.stdout.write(self.style.SUCCESS("=" * 60))
