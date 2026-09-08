"""AI queue. Never blocks ingestion; retries async with explicit failure states."""

from __future__ import annotations

import random
from typing import Any

from celery import shared_task
from django.utils import timezone


@shared_task(
    bind=True,
    max_retries=3,
    acks_late=True,
    queue="intelligence",
    autoretry_for=(),
)
def analyze_story_task(self, story_id: int) -> dict[str, Any]:
    from apps.ai.analysis import (
        classify_topic,
        combined_credibility,
        detect_conflicts,
        extract_news_value,
    )
    from apps.ai.models import AITask
    from apps.ai.service import feature_enabled
    from apps.stories.models import Story

    if not feature_enabled():
        return {"status": "skipped", "reason": "ai_disabled"}
    try:
        story = Story.objects.get(pk=story_id)
    except Story.DoesNotExist:
        return {"status": "skipped", "reason": "story_not_found"}

    results: dict[str, Any] = {}
    for task_name, runner in (
        ("topic_classification", classify_topic),
        ("news_value", extract_news_value),
        ("conflict_detection", detect_conflicts),
    ):
        ledger, _ = AITask.objects.get_or_create(
            story=story,
            task=task_name,
            input_hash=f"{story_id}:{task_name}:{story.updated_at.isoformat()}",
            defaults={"state": "pending"},
        )
        # Exact duplicates are skipped before any provider call.
        if ledger.state == "done":
            results[task_name] = "cached-ledger"
            continue
        ledger.state = "processing"
        ledger.attempts += 1
        ledger.last_attempt_at = timezone.now()
        ledger.save(update_fields=["state", "attempts", "last_attempt_at", "updated_at"])
        try:
            runner(story)
            ledger.state = "done"
            ledger.error_type = ""
            ledger.save(update_fields=["state", "error_type", "updated_at"])
            results[task_name] = "done"
        except Exception as exc:
            ledger.state = "failed"
            ledger.error_type = type(exc).__name__[:64]
            ledger.save(update_fields=["state", "error_type", "updated_at"])
            countdown = int((2**self.request.retries) * 30 + random.uniform(2, 8))  # noqa: S311
            raise self.retry(exc=exc, countdown=countdown) from exc
    results["credibility"] = combined_credibility(story)
    return {"status": "done", "story_id": story.pk, "results": results}


@shared_task(
    bind=True,
    max_retries=2,
    acks_late=True,
    queue="intelligence",
    autoretry_for=(),
)
def judge_ambiguous_task(self, decision_id: int) -> dict[str, Any]:
    from apps.ai.judge import apply_judge_to_decision
    from apps.ai.service import feature_enabled

    if not feature_enabled():
        return {"status": "skipped", "reason": "ai_disabled"}
    try:
        return apply_judge_to_decision(decision_id)
    except Exception as exc:
        countdown = int((2**self.request.retries) * 20 + random.uniform(1, 5))  # noqa: S311
        raise self.retry(exc=exc, countdown=countdown) from exc
