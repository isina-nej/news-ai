"""Engagement milestone scheduling: future-only snapshots without ETA-task explosion."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from apps.news.models import SourceItem
from apps.sources.models import EngagementTrackingState

DEFAULT_SCHEDULE_MINUTES = (10, 30, 60, 180, 360, 720, 1440)


def snapshot_schedule_minutes() -> list[int]:
    raw = getattr(settings, "SNAPSHOT_SCHEDULE_MIN", None) or list(DEFAULT_SCHEDULE_MINUTES)
    minutes: list[int] = []
    for value in raw:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            minutes.append(number)
    return sorted(set(minutes)) or list(DEFAULT_SCHEDULE_MINUTES)


def _parse_schedule(value: object) -> list[int]:
    if isinstance(value, (list, tuple)):
        out: list[int] = []
        for entry in value:
            try:
                number = int(entry)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if number > 0:
                out.append(number)
        return sorted(set(out))
    return snapshot_schedule_minutes()


def _next_milestone_after(age_seconds: int, schedule: list[int]) -> int | None:
    for minutes in schedule:
        if minutes * 60 > age_seconds:
            return minutes
    return None


def schedule_tracking(
    source_item: SourceItem,
    *,
    now: datetime | None = None,
    schedule_minutes: list[int] | None = None,
) -> EngagementTrackingState | None:
    """Create or refresh tracking state for a newly ingested item.

    - Past milestones are never backfilled/faked.
    - Records an immediate current snapshot separately (persistence layer);
      this only schedules FUTURE milestones.
    - Items without published_at cannot compute age -> no schedule.
    """
    current = now or timezone.now()
    published_at = getattr(source_item, "published_at", None)
    if published_at is None:
        return None
    schedule = (
        _parse_schedule(schedule_minutes) if schedule_minutes else snapshot_schedule_minutes()
    )
    age_seconds = max(0, int((current - published_at).total_seconds()))
    nxt = _next_milestone_after(age_seconds, schedule)
    state, _ = EngagementTrackingState.objects.get_or_create(source_item=source_item)
    if nxt is None:
        state.active = False
        state.next_due_at = None
        state.next_target_age_seconds = None
        state.save(update_fields=["active", "next_due_at", "next_target_age_seconds", "updated_at"])
        return state
    state.active = True
    state.next_target_age_seconds = nxt
    state.next_due_at = published_at + timedelta(seconds=nxt * 60)
    state.save(update_fields=["active", "next_target_age_seconds", "next_due_at", "updated_at"])
    return state


def advance_tracking(
    state: EngagementTrackingState,
    *,
    now: datetime | None = None,
    schedule_minutes: list[int] | None = None,
) -> EngagementTrackingState:
    """Move tracking state to the next future milestone after a capture."""
    current = now or timezone.now()
    schedule = (
        _parse_schedule(schedule_minutes) if schedule_minutes else snapshot_schedule_minutes()
    )
    published_at = state.source_item.published_at
    if published_at is None:
        state.active = False
        state.next_due_at = None
        state.next_target_age_seconds = None
        state.save(update_fields=["active", "next_due_at", "next_target_age_seconds", "updated_at"])
        return state
    age_seconds = max(0, int((current - published_at).total_seconds()))
    nxt = _next_milestone_after(age_seconds, schedule)
    if nxt is None:
        state.active = False
        state.next_due_at = None
        state.next_target_age_seconds = None
    else:
        state.active = True
        state.next_target_age_seconds = nxt
        state.next_due_at = published_at + timedelta(seconds=nxt * 60)
    state.last_attempt_at = current
    state.save(
        update_fields=[
            "active",
            "next_target_age_seconds",
            "next_due_at",
            "last_attempt_at",
            "updated_at",
        ]
    )
    return state
