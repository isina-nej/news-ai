"""StoryReanalysisCoordinator: debouncing, pipeline coordination, and fast-path dispatch."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.core.cache import cache
from django.utils import timezone

from apps.core.choices import LifecycleState, TrendState
from apps.stories.models import Story, StoryObservationState
from apps.stories.services.lifecycle import LifecycleStateMachine
from apps.stories.services.momentum import StoryMomentumService
from apps.stories.services.trend import TrendDetector

logger = logging.getLogger(__name__)

DEBOUNCE_TTL_SECONDS = 10
FAST_PATH_COOLDOWN_SECONDS = 300  # 5 minutes minimum between fast-path ranking dispatches


class StoryReanalysisCoordinator:
    """Coordinates momentum calculation, lifecycle progression, and immediate fast path ranking."""

    @classmethod
    def schedule_reanalysis(
        cls,
        story_id: int,
        *,
        has_material_update: bool = False,
        immediate: bool = False,
    ) -> bool:
        """Debounces incoming story events and triggers momentum recompute.

        Returns True if a task was triggered, False if debounced.
        """
        debounce_key = f"story:debounce:{story_id}"
        if not immediate:
            # Atomic cache.add acts as a token bucket / debounce guard
            acquired = cache.add(debounce_key, True, timeout=DEBOUNCE_TTL_SECONDS)
            if not acquired:
                return False

        # Run or enqueue momentum recompute
        from apps.stories.tasks import recompute_story_momentum_task

        recompute_story_momentum_task.delay(
            story_id=story_id, has_material_update=has_material_update
        )
        return True

    @classmethod
    def process_story(
        cls,
        story_id: int,
        *,
        has_material_update: bool = False,
        now: Any | None = None,
    ) -> dict[str, Any]:
        """Execute full momentum recompute, trend & lifecycle detection, and fast-path check."""
        current_time = now or timezone.now()
        try:
            story = Story.objects.get(pk=story_id)
        except Story.DoesNotExist:
            return {"status": "skipped", "reason": "story_not_found"}

        obs, _ = StoryObservationState.objects.get_or_create(
            story=story,
            defaults={
                "lifecycle_state": LifecycleState.DISCOVERED,
                "trend_state": TrendState.NORMAL,
            },
        )

        # 1. Compute raw statistical metrics
        metrics = StoryMomentumService.compute_metrics(story, now=current_time)

        # 2. Detect trend state
        new_trend, trend_reason = TrendDetector.detect_state(
            metrics,
            current_state=obs.trend_state,
            sample_count=obs.sample_count,
        )

        # 3. Evaluate lifecycle state machine
        new_lifecycle, lifecycle_reason = LifecycleStateMachine.evaluate_lifecycle(
            current_lifecycle=obs.lifecycle_state,
            trend_state=new_trend,
            metrics=metrics,
            has_material_update=has_material_update,
        )

        # 4. Determine adaptive interval and next observation time
        interval = LifecycleStateMachine.get_interval_seconds(new_lifecycle, new_trend)
        next_obs = current_time + timedelta(seconds=interval)

        # 5. Persist momentum snapshot and update observation state
        snap = StoryMomentumService.recompute_and_persist(
            story_id=story.pk,
            lifecycle_state=new_lifecycle,
            trend_state=new_trend,
            transition_reason=f"{trend_reason}|{lifecycle_reason}",
            transition_metrics={
                "interval_seconds": interval,
                "has_material_update": has_material_update,
            },
            now=current_time,
        )

        # Update next observation schedule
        obs.refresh_from_db()
        obs.observation_interval_seconds = interval
        obs.next_observation_at = next_obs
        obs.active = new_lifecycle != LifecycleState.ARCHIVED
        obs.save(
            update_fields=[
                "observation_interval_seconds",
                "next_observation_at",
                "active",
                "updated_at",
            ]
        )

        # 6. Breaking / Early Signal Fast Path Dispatch
        fast_path_triggered = False
        is_breaking = new_trend == TrendState.BREAKING or new_lifecycle == LifecycleState.BREAKING
        is_early_signal = new_trend == TrendState.EARLY_SIGNAL

        if is_breaking or is_early_signal or has_material_update:
            can_trigger = True
            if obs.last_fast_path_at:
                sec_since_fp = (current_time - obs.last_fast_path_at).total_seconds()
                if sec_since_fp < FAST_PATH_COOLDOWN_SECONDS:
                    can_trigger = False

            if can_trigger:
                obs.last_fast_path_at = current_time
                obs.save(update_fields=["last_fast_path_at", "updated_at"])
                from apps.stories.tasks import immediate_rescore_task

                immediate_rescore_task.delay(story_id=story.pk, is_breaking=is_breaking)
                fast_path_triggered = True

        return {
            "status": "processed",
            "story_id": story.pk,
            "lifecycle_state": new_lifecycle,
            "trend_state": new_trend,
            "momentum_score": float(snap.momentum_score) if snap else 0.0,
            "fast_path_triggered": fast_path_triggered,
            "next_observation_at": next_obs.isoformat() if next_obs else None,
        }
