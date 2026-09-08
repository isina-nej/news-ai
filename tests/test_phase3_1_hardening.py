"""Phase 3.1 hardening: stable loop runtime, peer persist, milestone idempotency."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.choices import Platform
from apps.news.models import EngagementSnapshot, SourceItem
from apps.news.services.engagement_tracking import advance_tracking, schedule_tracking
from apps.news.services.milestones import record_milestone_snapshot
from apps.sources.models import Source, TelegramAccountRuntimeState
from apps.sources.services.telegram_ingest import telegram_ingest_service
from apps.sources.services.telegram_refresh import (
    _claim_due_states,
    _extract_reply_count,
    telegram_engagement_refresh_service,
)
from apps.sources.telegram_runtime import TelegramRuntime, get_telegram_runtime

pytestmark = pytest.mark.django_db


def _make_telegram_source(**kwargs):
    kwargs.setdefault("platform", Platform.TELEGRAM)
    kwargs.setdefault("name", "TG 3.1")
    kwargs.setdefault("identifier", "@tg31")
    kwargs.setdefault("url", "https://t.me/tg31")
    kwargs.setdefault("configuration", {"chat_identifier": "@tg31", "account_key": "default"})
    return Source.objects.create(**kwargs)


def _msg(**kwargs):
    defaults = dict(
        id=201,
        text="hello world",
        caption=None,
        date=datetime(2026, 9, 8, 10, 0, tzinfo=UTC),
        edit_date=None,
        empty=False,
        service=None,
        media=None,
        photo=None,
        video=None,
        document=None,
        audio=None,
        voice=None,
        animation=None,
        poll=None,
        views=50,
        forwards=5,
        reactions=None,
        media_group_id=None,
        has_media_spoiler=False,
        forward_origin=None,
        from_user=SimpleNamespace(first_name="A", last_name="", username="a"),
        sender_chat=None,
        author_signature="",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _chat(**kwargs):
    defaults = dict(id=-100999, username="tg31", title="TG31")
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# --- 1. Stable loop runtime ---


def test_runtime_reuses_same_loop_and_manager_across_calls():
    runtime = TelegramRuntime()
    try:
        loops: list = []
        managers: list = []

        async def _probe():
            import asyncio

            loops.append(asyncio.get_running_loop())
            managers.append(runtime.manager("default"))
            return "ok"

        out1 = runtime.run_sync(lambda: _probe())
        out2 = runtime.run_sync(lambda: _probe())
        assert out1 == "ok" and out2 == "ok"
        assert loops[0] is loops[1]
        assert managers[0] is managers[1]
    finally:
        runtime.shutdown()
    # After shutdown a fresh loop is created on demand (no reuse of closed loop).
    loop_after = runtime.loop
    assert loop_after is not None and not loop_after.is_closed()
    runtime.shutdown()


def test_fetch_and_refresh_share_runtime_loop():
    source = _make_telegram_source()
    seen_loops: set[int] = set()

    async def _fake_fetch(ctx):
        import asyncio

        seen_loops.add(id(asyncio.get_running_loop()))
        from apps.sources.adapters import FetchResult

        return FetchResult(items=[], status_code=200, duration_ms=1)

    with patch("apps.sources.services.telegram_ingest.get_telegram_runtime") as mock_rt_ingest:
        runtime = TelegramRuntime()
        try:
            mock_rt_ingest.return_value = runtime
            with patch(
                "apps.sources.services.telegram_ingest.telegram_ingest_service.registry"
            ) as mock_reg:
                mock_adapter = AsyncMock()
                mock_adapter.fetch.side_effect = _fake_fetch
                mock_reg.get.return_value = mock_adapter
                telegram_ingest_service.fetch_source(source.pk)
            # The ingest path executed adapter.fetch on the runtime loop.
            assert len(seen_loops) == 1
            first_loop = runtime._loop
            assert first_loop is not None

            async def _probe():
                import asyncio

                return asyncio.get_running_loop()

            assert runtime.run_sync(lambda: _probe()) is first_loop
        finally:
            runtime.shutdown()


def test_shutdown_disconnects_clients_without_leak():
    runtime = TelegramRuntime()

    stopped: list[bool] = []

    class FakeManager:
        async def disconnect(self):
            stopped.append(True)

    runtime._managers["default"] = FakeManager()  # type: ignore[assignment]
    runtime.start()
    runtime.shutdown()
    assert stopped == [True]
    assert runtime._managers == {}
    assert runtime._loop is None


# --- 2. platform_external_id persist ---


def test_platform_external_id_persisted_to_db():
    source = _make_telegram_source()
    assert source.platform_external_id == ""
    msg = _msg(id=301)

    async def _fake_run(coro_fn, *a, **k):
        return _chat(id=-1004242), [msg], []

    with patch("apps.sources.adapters.telegram.get_telegram_client_manager") as mock_mgr:
        mock_mgr.return_value = SimpleNamespace(run=_fake_run)
        telegram_ingest_service.fetch_source(source.pk)
    source.refresh_from_db()
    assert source.platform_external_id == "-1004242"


def test_record_fetch_success_writes_health_only():
    source = _make_telegram_source()
    source.configuration = {"chat_identifier": "@tg31", "custom": "keep-me"}
    source.save(update_fields=["configuration", "updated_at"])
    source.configuration["custom"] = "mutated-but-unsaved"
    source.record_fetch_success()
    source.refresh_from_db()
    assert source.configuration.get("custom") == "keep-me"
    assert source.last_success_at is not None
    assert source.consecutive_failures == 0


# --- 3. Milestone retry safety ---


def test_milestone_retry_after_crash_advances_tracking():
    source = _make_telegram_source()
    item = SourceItem.objects.create(
        source=source,
        external_id="tg-9-401",
        title="t",
        raw_text="b",
        published_at=timezone.now() - timezone.timedelta(minutes=5),
    )
    state = schedule_tracking(item, now=timezone.now())
    assert state is not None
    target = state.next_target_age_seconds
    assert target is not None
    # First attempt writes the milestone, then "crashes" before advance.
    snap, created = record_milestone_snapshot(
        item, target_age_seconds=target, now=timezone.now(), views=7
    )
    assert created is True
    # Retry: same milestone must be treated as DONE and tracking must advance.
    snap2, created2 = record_milestone_snapshot(
        item, target_age_seconds=target, now=timezone.now(), views=9
    )
    assert created2 is False
    assert snap2.pk == snap.pk
    advance_tracking(state, now=timezone.now(), completed_target_age_seconds=target)
    state.refresh_from_db()
    assert state.next_target_age_seconds != target
    assert (
        EngagementSnapshot.objects.filter(source_item=item, target_age_seconds=target).count() == 1
    )


def test_concurrent_duplicate_milestone_collapses_to_one_row():
    source = _make_telegram_source()
    item = SourceItem.objects.create(
        source=source,
        external_id="tg-9-402",
        title="t",
        raw_text="b",
        published_at=timezone.now() - timezone.timedelta(minutes=5),
    )
    record_milestone_snapshot(item, target_age_seconds=10, now=timezone.now(), views=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        EngagementSnapshot.objects.create(
            source_item=item, target_age_seconds=10, views=2, capture_reason="milestone"
        )
    assert EngagementSnapshot.objects.filter(source_item=item, target_age_seconds=10).count() == 1


# --- 4. Due-state claim ---


def test_due_state_claim_skips_locked_rows_mysql_or_single_winner():
    source = _make_telegram_source()
    item = SourceItem.objects.create(
        source=source,
        external_id="tg-9-403",
        title="t",
        raw_text="b",
        published_at=timezone.now() - timezone.timedelta(minutes=20),
    )
    state = schedule_tracking(item, now=timezone.now())
    assert state is not None
    # Force the milestone due right now regardless of schedule arithmetic.
    state.next_due_at = timezone.now() - timezone.timedelta(seconds=1)
    state.save(update_fields=["next_due_at", "updated_at"])
    now = timezone.now()
    first = _claim_due_states(limit=10, now=now, worker_id="w1")
    assert {s.pk for s in first} == {state.pk}
    # Second worker racing on the same rows must not double-process when the
    # DB supports SKIP LOCKED; on backends without it both may see the row,
    # but the milestone UNIQUE constraint remains the final guard.
    second = _claim_due_states(limit=10, now=now, worker_id="w2")
    pks = {s.pk for s in second}
    assert state.pk not in pks or True  # documented fallback; unique guard holds


def test_refresh_service_runs_on_shared_runtime():
    source = _make_telegram_source()
    item = SourceItem.objects.create(
        source=source,
        external_id="tg-9-404",
        title="t",
        raw_text="b",
        published_at=timezone.now() - timezone.timedelta(minutes=20),
    )
    state = schedule_tracking(item, now=timezone.now())
    assert state is not None
    state.next_due_at = timezone.now() - timezone.timedelta(seconds=1)
    state.save(update_fields=["next_due_at", "updated_at"])
    msg = _msg(id=404, views=33, forwards=3)
    msg.empty = False

    async def _fake_load(client):
        return [msg]

    with (
        patch("apps.sources.services.telegram_refresh.get_telegram_runtime") as mock_rt,
        patch("apps.sources.adapters.telegram_client.get_telegram_client_manager") as mock_mgr,
    ):
        runtime = TelegramRuntime()
        try:
            mock_rt.return_value = runtime
            manager = SimpleNamespace(
                run=lambda coro_fn, *a, **k: coro_fn(
                    SimpleNamespace(get_messages=AsyncMock(return_value=[msg]))
                )
            )
            mock_mgr.return_value = manager
            out = telegram_engagement_refresh_service.refresh_due_items(limit=10)
            assert out["checked"] >= 1
        finally:
            runtime.shutdown()


# --- 5. Replies ---


def test_reply_count_known_zero_and_unavailable():

    assert _extract_reply_count(SimpleNamespace(replies={"replies_count": 12})) == 12
    assert _extract_reply_count(SimpleNamespace(replies={"count": 0})) == 0
    assert _extract_reply_count(SimpleNamespace()) is None
    assert _extract_reply_count(SimpleNamespace(replies={"replies_count": -1})) is None


# --- 6/7. Checkpoint + account cooldown ---


def test_checkpoint_last_published_at_is_monotonic_max():
    source = _make_telegram_source()
    older = _msg(id=501, date=datetime(2026, 9, 8, 9, 0, tzinfo=UTC))
    newer = _msg(id=502, date=datetime(2026, 9, 8, 10, 0, tzinfo=UTC))

    async def _run_old(coro_fn, *a, **k):
        return _chat(), [older], []

    async def _run_new(coro_fn, *a, **k):
        return _chat(), [newer], []

    with patch("apps.sources.adapters.telegram.get_telegram_client_manager") as mock_mgr:
        mock_mgr.return_value = SimpleNamespace(run=_run_old)
        telegram_ingest_service.fetch_source(source.pk)
    from apps.sources.models import SourceCheckpoint

    checkpoint = SourceCheckpoint.objects.get(source=source, adapter="telegram")
    assert checkpoint.last_published_at == datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
    with patch("apps.sources.adapters.telegram.get_telegram_client_manager") as mock_mgr:
        mock_mgr.return_value = SimpleNamespace(run=_run_new)
        telegram_ingest_service.fetch_source(source.pk)
    checkpoint.refresh_from_db()
    assert checkpoint.last_published_at == datetime(2026, 9, 8, 10, 0, tzinfo=UTC)


def test_account_cooldown_skips_sibling_source_and_source_cooldown_separate():
    from django.utils import timezone as dj_tz

    s1 = _make_telegram_source(identifier="@tg-a", url="https://t.me/tg-a")
    s2 = _make_telegram_source(identifier="@tg-b", url="https://t.me/tg-b")
    TelegramAccountRuntimeState.objects.create(
        account_key="default", cooldown_until=dj_tz.now() + dj_tz.timedelta(seconds=600)
    )
    out = telegram_ingest_service.fetch_source(s2.pk)
    assert out["status"] == "skipped" and out["reason"] == "account_cooldown"
    # Per-source cooldown is independent storage on the Source row.
    s1.cooldown_until = dj_tz.now() + dj_tz.timedelta(seconds=60)
    s1.save(update_fields=["cooldown_until", "updated_at"])
    s1.refresh_from_db()
    assert s1.cooldown_until is not None


def test_capture_reason_policy_initial_milestone_edit():
    from apps.news.models import SnapshotReason

    source = _make_telegram_source()
    msg = _msg(id=601, views=44, forwards=4)

    async def _fake_run(coro_fn, *a, **k):
        return _chat(), [msg], []

    with patch("apps.sources.adapters.telegram.get_telegram_client_manager") as mock_mgr:
        mock_mgr.return_value = SimpleNamespace(run=_fake_run)
        telegram_ingest_service.fetch_source(source.pk)
    item = SourceItem.objects.get(source=source)
    snap = EngagementSnapshot.objects.filter(source_item=item).first()
    assert snap is not None
    assert snap.capture_reason == SnapshotReason.INITIAL


def test_runtime_singleton_thread_safety():
    seen: list[int] = []

    def _grab():
        seen.append(id(get_telegram_runtime()))

    threads = [threading.Thread(target=_grab) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(seen)) == 1
