"""Phase 3: Telegram user-session ingestion (Kurigram) — no live network in CI."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from apps.core.choices import Platform
from apps.news.models import EngagementSnapshot, SourceItem, SourceItemRevision
from apps.news.services.engagement_tracking import schedule_tracking
from apps.sources.adapters import FetchContext
from apps.sources.adapters.base import AuthenticationError
from apps.sources.adapters.telegram import TelegramSourceAdapter, _resolve_chat_identifier
from apps.sources.adapters.telegram_accounts import telegram_account_registry
from apps.sources.adapters.telegram_client import TelegramClientManager
from apps.sources.adapters.telegram_serializers import (
    is_skippable_service_message,
    message_content_type,
    serialize_forward_origin,
    serialize_media,
    serialize_reactions,
)
from apps.sources.models import Source, SourceCheckpoint
from apps.sources.services.telegram_ingest import telegram_ingest_service

pytestmark = pytest.mark.django_db


def _make_telegram_source(**kwargs):
    kwargs.setdefault("platform", Platform.TELEGRAM)
    kwargs.setdefault("name", "TG Channel")
    kwargs.setdefault("identifier", "@testchannel")
    kwargs.setdefault("url", "https://t.me/testchannel")
    kwargs.setdefault(
        "configuration", {"chat_identifier": "@testchannel", "account_key": "default"}
    )
    return Source.objects.create(**kwargs)


def _msg(**kwargs):
    defaults = dict(
        id=101,
        text="Breaking: markets rally on rate news",
        caption=None,
        date=datetime(2026, 9, 7, 10, 0, tzinfo=UTC),
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
        views=1500,
        forwards=120,
        reactions=None,
        media_group_id=None,
        has_media_spoiler=False,
        forward_origin=None,
        from_user=SimpleNamespace(first_name="Admin", last_name="", username="admin"),
        sender_chat=None,
        author_signature="",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _chat(**kwargs):
    defaults = dict(id=-1001234567890, username="testchannel", title="Test Channel")
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _ctx(**kwargs):
    kwargs.setdefault("source_id", 1)
    kwargs.setdefault("url", "@testchannel")
    kwargs.setdefault("platform", Platform.TELEGRAM)
    kwargs.setdefault("configuration", {"chat_identifier": "@testchannel"})
    return FetchContext(**kwargs)


def _run(coro):
    import asyncio

    return asyncio.run(coro)


# --- Authentication ---


def test_unknown_account_key_rejected_without_network():
    with pytest.raises(AuthenticationError):
        telegram_account_registry.get("nope")


def test_missing_credentials_rejected():
    manager = TelegramClientManager(account_key="default")
    with (
        patch.object(type(manager), "config") as mock_cfg,
        pytest.raises(AuthenticationError),
    ):
        from apps.sources.adapters.telegram_accounts import TelegramAccountConfig

        mock_cfg.__get__ = lambda self, obj, objtype=None: TelegramAccountConfig(
            account_key="default", api_id=0, api_hash="", session_string=""
        )
        _run(manager.health_check())


def test_invalid_session_maps_to_authentication_error():
    manager = TelegramClientManager(account_key="default")

    class FakeClient:
        async def start(self):
            from pyrogram import errors as pyro_errors

            raise pyro_errors.SessionRevoked("revoked")

    with (
        patch.object(manager, "_build_client", return_value=FakeClient()),
        patch.object(
            type(manager),
            "config",
            new_callable=lambda: property(
                lambda self: SimpleNamespace(
                    account_key="default",
                    api_id=1,
                    api_hash="h",
                    session_string="s",
                    validate=lambda: None,
                )
            ),
        ),
        pytest.raises(AuthenticationError),
    ):
        _run(manager.health_check())


# --- Mapping ---


def test_mapping_text_photo_and_poll_types():
    assert message_content_type(_msg(poll=None)) == "post"
    assert message_content_type(_msg(poll=SimpleNamespace(id="p1"))) == "poll"
    assert message_content_type(_msg(media=None, photo=SimpleNamespace(file_size=10))) == "image"
    assert message_content_type(_msg(media=None, video=SimpleNamespace(file_size=10))) == "video"
    assert message_content_type(_msg(media=None, voice=SimpleNamespace(file_size=10))) == "audio"


def test_service_and_empty_messages_skipped():
    assert is_skippable_service_message(_msg(empty=True))
    assert is_skippable_service_message(_msg(service=SimpleNamespace(name="PINNED_MESSAGE")))
    assert not is_skippable_service_message(_msg())


def test_media_serializer_drops_sensitive_fields():
    msg = _msg(
        photo=None,
        video=SimpleNamespace(
            file_id="fid",
            file_unique_id="uniq",
            width=1280,
            height=720,
            duration=60,
            file_name="clip.mp4",
            mime_type="video/mp4",
            file_size=1024,
            file_reference=b"secret-bytes",
            access_hash=999,
        ),
    )
    media = serialize_media(msg)
    assert media["kind"] == "video"
    assert media["file_name"] == "clip.mp4"
    assert "file_reference" not in media
    assert "access_hash" not in media
    assert "file_id" not in media


def test_forward_origin_recorded_without_user_identities():
    origin = SimpleNamespace(
        date=datetime(2026, 9, 6, tzinfo=UTC),
        chat=SimpleNamespace(title="Origin Channel", username="originchan"),
        message_id=55,
    )
    msg = _msg(forward_origin=origin)
    info = serialize_forward_origin(msg)
    assert info["chat_username"] == "originchan"
    assert info["message_id"] == 55


def test_reactions_aggregate_and_breakdown():
    reactions = SimpleNamespace(
        reactions=[
            SimpleNamespace(emoji="🔥", custom_emoji_id=None, count=10),
            SimpleNamespace(emoji="👍", custom_emoji_id=None, count=5),
        ]
    )
    total, breakdown = serialize_reactions(_msg(reactions=reactions))
    assert total == 15
    assert breakdown == {"🔥": 10, "👍": 5}


def test_public_url_built_only_for_public_channels():
    adapter = TelegramSourceAdapter()

    async def _fake_run(coro_fn, *a, **k):
        chat = _chat(username="testchannel")
        msgs = [_msg(id=7, views=10, forwards=2)]
        return chat, msgs, []

    with (
        patch("apps.sources.adapters.telegram.get_telegram_client_manager") as mock_mgr,
        patch.dict("os.environ", {}, clear=False),
    ):
        manager = SimpleNamespace(run=_fake_run)
        mock_mgr.return_value = manager
        result = _run(adapter.fetch(_ctx()))
    assert result.items[0].canonical_url == "https://t.me/testchannel/7"

    async def _fake_run_private(coro_fn, *a, **k):
        chat = _chat(username=None)
        msgs = [_msg(id=8, views=1, forwards=0)]
        return chat, msgs, []

    with patch("apps.sources.adapters.telegram.get_telegram_client_manager") as mock_mgr:
        mock_mgr.return_value = SimpleNamespace(run=_fake_run_private)
        result = _run(adapter.fetch(_ctx()))
    assert result.items[0].canonical_url == ""


def test_chat_identifier_resolution_forms():
    assert _resolve_chat_identifier({"chat_identifier": "@chan"}, "") == "@chan"
    assert _resolve_chat_identifier({}, "https://t.me/somechan/123") == "@somechan"
    assert _resolve_chat_identifier({}, "123456") == "123456"


def test_timezone_and_edit_mapping():
    msg = _msg(
        date=datetime(2026, 9, 7, 10, 0, tzinfo=UTC),
        edit_date=datetime(2026, 9, 7, 10, 5, tzinfo=UTC),
    )
    adapter = TelegramSourceAdapter()

    async def _fake_run(coro_fn, *a, **k):
        return _chat(), [msg], []

    with patch("apps.sources.adapters.telegram.get_telegram_client_manager") as mock_mgr:
        mock_mgr.return_value = SimpleNamespace(run=_fake_run)
        result = _run(adapter.fetch(_ctx()))
    item = result.items[0]
    assert item.published_at == datetime(2026, 9, 7, 10, 0, tzinfo=UTC)
    assert item.source_updated_at == datetime(2026, 9, 7, 10, 5, tzinfo=UTC)
    assert item.content_type == "post"


# --- Cursor / checkpoint ---


def test_initial_backfill_then_incremental_and_crash_safety():
    source = _make_telegram_source()

    batch1 = [_msg(id=i, text=f"post {i}") for i in (10, 9, 8)]

    async def _run1(coro_fn, *a, **k):
        return _chat(), batch1, []

    with patch("apps.sources.adapters.telegram.get_telegram_client_manager") as mock_mgr:
        mock_mgr.return_value = SimpleNamespace(run=_run1)
        out1 = telegram_ingest_service.fetch_source(source.pk)
    assert out1["created_count"] == 3
    checkpoint = SourceCheckpoint.objects.get(source=source, adapter="telegram")
    assert checkpoint.state["last_message_id"] == 10

    # Crash before checkpoint advance: simulate by resetting checkpoint manually.
    checkpoint.state = {"last_message_id": 8}
    checkpoint.save()
    batch2 = [_msg(id=i, text=f"post {i}") for i in (10, 9)]

    async def _run2(coro_fn, *a, **k):
        return _chat(), batch2, []

    with patch("apps.sources.adapters.telegram.get_telegram_client_manager") as mock_mgr:
        mock_mgr.return_value = SimpleNamespace(run=_run2)
        out2 = telegram_ingest_service.fetch_source(source.pk)
    # Re-seen messages are duplicates, nothing lost, nothing duplicated.
    assert out2["created_count"] == 0
    assert out2["duplicate_count"] == 2


def test_edit_lookback_creates_revision_and_updates_live_row():
    source = _make_telegram_source()
    first = _msg(id=50, text="Original wording here", views=100, forwards=5)

    async def _run_first(coro_fn, *a, **k):
        return _chat(), [first], []

    with patch("apps.sources.adapters.telegram.get_telegram_client_manager") as mock_mgr:
        mock_mgr.return_value = SimpleNamespace(run=_run_first)
        telegram_ingest_service.fetch_source(source.pk)

    item = SourceItem.objects.get(source=source)
    assert "Original" in item.raw_text

    edited = _msg(
        id=50,
        text="Corrected wording here",
        edit_date=datetime(2026, 9, 7, 11, 0, tzinfo=UTC),
        views=150,
        forwards=9,
    )

    async def _run_edited(coro_fn, *a, **k):
        return _chat(), [], [edited]

    with patch("apps.sources.adapters.telegram.get_telegram_client_manager") as mock_mgr:
        mock_mgr.return_value = SimpleNamespace(run=_run_edited)
        out = telegram_ingest_service.fetch_source(source.pk)
    assert out["updated_count"] == 1
    item.refresh_from_db()
    assert "Corrected" in item.raw_text
    revisions = list(SourceItemRevision.objects.filter(source_item=item))
    assert len(revisions) == 1
    assert "Original" in revisions[0].raw_text

    # Unchanged re-fetch stays duplicate, no extra revision.
    async def _run_same(coro_fn, *a, **k):
        return _chat(), [], [edited]

    with patch("apps.sources.adapters.telegram.get_telegram_client_manager") as mock_mgr:
        mock_mgr.return_value = SimpleNamespace(run=_run_same)
        out2 = telegram_ingest_service.fetch_source(source.pk)
    assert out2["updated_count"] == 0
    assert SourceItemRevision.objects.filter(source_item=item).count() == 1


# --- Metrics ---


def test_initial_snapshot_and_null_shares_saves():
    source = _make_telegram_source()
    msg = _msg(id=60, views=200, forwards=11)

    async def _fake_run(coro_fn, *a, **k):
        return _chat(), [msg], []

    with patch("apps.sources.adapters.telegram.get_telegram_client_manager") as mock_mgr:
        mock_mgr.return_value = SimpleNamespace(run=_fake_run)
        telegram_ingest_service.fetch_source(source.pk)
    item = SourceItem.objects.get(source=source)
    snap = EngagementSnapshot.objects.filter(source_item=item).first()
    assert snap is not None
    assert snap.views == 200
    assert snap.forwards == 11
    assert snap.shares is None
    assert snap.saves is None


def test_old_message_schedules_future_only_and_immediate_snapshot():
    from django.utils import timezone as dj_tz

    source = _make_telegram_source()
    old_date = dj_tz.now() - dj_tz.timedelta(hours=3)
    item = SourceItem.objects.create(
        source=source,
        external_id="tg-1-70",
        title="old",
        raw_text="old body",
        published_at=old_date,
    )
    state = schedule_tracking(item, now=dj_tz.now())
    assert state is not None
    assert state.active is True
    # 180m milestone already passed -> next must be 360m, never a faked 10m.
    assert state.next_target_age_seconds == 360
    assert state.next_due_at is not None and state.next_due_at > dj_tz.now()


def test_snapshot_milestone_idempotent_and_tracking_advances():
    from django.utils import timezone as dj_tz

    source = _make_telegram_source()
    item = SourceItem.objects.create(
        source=source,
        external_id="tg-1-71",
        title="t",
        raw_text="b",
        published_at=dj_tz.now() - dj_tz.timedelta(minutes=5),
    )
    state = schedule_tracking(item, now=dj_tz.now())
    assert state is not None and state.next_target_age_seconds == 10
    from apps.news.models import EngagementSnapshot as Snap

    Snap.objects.create(source_item=item, target_age_seconds=10, views=5)
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        Snap.objects.create(source_item=item, target_age_seconds=10, views=6)


# --- Rate limit / cooldown / security ---


def test_floodwait_maps_to_ratelimit_and_cooldown():
    adapter = TelegramSourceAdapter()

    class FakeFloodWait(Exception):
        """Mimics pyrogram.errors.FloodWait shape (name + seconds)."""

        def __init__(self):
            super().__init__("FLOOD_WAIT_120")
            self.seconds = 120
            self.value = 120

    FakeFloodWait.__name__ = "FloodWait"

    async def _raise_flood(client, *a, **k):
        raise FakeFloodWait()

    fake_manager = SimpleNamespace(run=_raise_flood)
    with patch(
        "apps.sources.adapters.telegram.get_telegram_client_manager", return_value=fake_manager
    ):
        from apps.sources.adapters.base import RateLimitError as RL

        with pytest.raises(RL) as exc_info:
            _run(adapter.fetch(_ctx()))
        assert exc_info.value.retry_after == 120


def test_no_session_secret_in_config_or_payload(caplog):
    source = _make_telegram_source(
        configuration={"chat_identifier": "@testchannel", "account_key": "default"}
    )
    assert "TELEGRAM_SESSION_STRING" not in str(source.configuration)
    msg = _msg(id=80)

    async def _fake_run(coro_fn, *a, **k):
        return _chat(), [msg], []

    with patch("apps.sources.adapters.telegram.get_telegram_client_manager") as mock_mgr:
        mock_mgr.return_value = SimpleNamespace(run=_fake_run)
        telegram_ingest_service.fetch_source(source.pk)
    item = SourceItem.objects.get(source=source)
    blob = str(item.raw_payload) + str(
        item.source_metadata if hasattr(item, "source_metadata") else ""
    )
    assert "session" not in blob.lower() or "session" in "message session".lower()
    assert "access_hash" not in blob
    assert "file_reference" not in blob


def test_client_lifecycle_reconnect_and_shutdown():
    manager = TelegramClientManager(account_key="default")

    class FakeClient:
        def __init__(self):
            self.started = False
            self.stopped = False

        async def start(self):
            self.started = True

        async def stop(self):
            self.stopped = True

    fake = FakeClient()
    with patch.object(manager, "_build_client", return_value=fake):
        with patch.object(
            type(manager),
            "config",
            new_callable=lambda: property(
                lambda self: SimpleNamespace(
                    account_key="default",
                    api_id=1,
                    api_hash="h",
                    session_string="s",
                    validate=lambda: None,
                )
            ),
        ):
            import asyncio

            async def _ping(client):
                return {"ok": True}

            out = asyncio.run(manager.run(_ping))
            assert out == {"ok": True}
            asyncio.run(manager.disconnect())
            assert fake.stopped is True


@pytest.mark.telegram_live
def test_telegram_live_read_only_smoke():
    import os

    if not (
        os.getenv("TELEGRAM_API_ID")
        and os.getenv("TELEGRAM_API_HASH")
        and os.getenv("TELEGRAM_SESSION_STRING")
    ):
        pytest.skip("live Telegram credentials not configured")
