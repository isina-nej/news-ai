"""Phase 10: session-based X ingestion. Fake client only; no live credentials."""

from __future__ import annotations

import asyncio

import pytest

from apps.core.choices import Platform
from apps.sources.adapters import FetchContext, adapter_registry
from apps.sources.adapters.base import AuthenticationError
from apps.sources.adapters.twitter import (
    TwitterSourceAdapter,
    tweet_to_fetched_item,
)
from apps.sources.adapters.twitter_clients import FakeTwitterClient
from apps.sources.models import Source

pytestmark = [pytest.mark.django_db]


def _tweet(tweet_id="123", text="Breaking news", **kwargs):
    payload = {
        "id": tweet_id,
        "text": text,
        "author": {"username": "testuser"},
        "created_at": "2026-09-08T10:00:00Z",
        "public_metrics": {
            "like_count": 10,
            "retweet_count": 2,
            "reply_count": 1,
            "impression_count": 500,
        },
    }
    payload.update(kwargs)
    return payload


def test_tweet_mapping_preserves_nulls_and_metadata():
    mapped = tweet_to_fetched_item(_tweet())
    assert mapped is not None
    assert mapped.external_id == "123"
    assert mapped.published_at is not None
    assert mapped.views == 500
    assert mapped.saves is None
    assert mapped.raw_payload["tweet_id"] == "123"

    no_metrics = tweet_to_fetched_item({"id": "1", "text": "hello"})
    assert no_metrics is not None
    assert no_metrics.views is None
    assert no_metrics.reactions is None


def test_retweet_is_observational_not_independent():
    mapped = tweet_to_fetched_item(_tweet(is_retweet=True, retweeted_id="999"))
    assert mapped is not None
    assert mapped.raw_payload["independent_confirmation"] is False


def test_quote_tweet_keeps_referenced_metadata():
    mapped = tweet_to_fetched_item(
        _tweet(
            is_quote=True,
            quoted_tweet={"id": "777", "author": {"username": "orig"}, "text": "Original claim"},
        )
    )
    assert mapped is not None
    assert mapped.raw_payload["referenced_tweet"]["id"] == "777"


def test_adapter_registered_and_requires_session():
    adapter = adapter_registry.get(Platform.TWITTER_X, "twitter")
    assert isinstance(adapter, TwitterSourceAdapter)
    context = FetchContext(
        source_id=1, url="@testuser", platform=Platform.TWITTER_X, configuration={}
    )
    with pytest.raises(AuthenticationError):
        asyncio.run(adapter.fetch(context))


def test_adapter_fetch_with_fake_client_and_cursor():
    client = FakeTwitterClient(
        pages=[
            {"cursor": None, "tweets": [_tweet("1", "First")], "next_cursor": "c1"},
            {"cursor": "c1", "tweets": [_tweet("2", "Second")], "next_cursor": None},
        ]
    )
    adapter = TwitterSourceAdapter(client=client)
    context = FetchContext(
        source_id=1,
        url="@testuser",
        platform=Platform.TWITTER_X,
        configuration={"username": "testuser", "limit": 10},
    )
    result = asyncio.run(adapter.fetch(context))
    assert len(result.items) == 1
    assert result.items[0].external_id == "1"


def test_twitter_ingest_service_crash_safe_cursor():
    from apps.ops.models import FeatureFlag
    from apps.sources.services.twitter_ingest import twitter_ingest_service

    FeatureFlag.objects.create(key="ENABLE_TWITTER_SOURCE", enabled=True)
    source = Source.objects.create(
        platform=Platform.TWITTER_X,
        name="X Source",
        identifier="@testuser",
        url="https://x.com/testuser",
        configuration={"username": "testuser"},
    )
    fake_items = []
    mapped = tweet_to_fetched_item(_tweet("10", "Hello X world"))
    assert mapped is not None
    fake_items.append(mapped)

    class StubAdapter:
        async def fetch(self, context):
            from apps.sources.adapters.base import FetchResult

            return FetchResult(items=fake_items)

    with (
        pytest.MonkeyPatch.context() as mp,
        pytest.MonkeyPatch.context() as _unused,
    ):
        mp.setattr(
            "apps.sources.services.twitter_ingest.adapter_registry.get",
            lambda *a, **k: StubAdapter(),
        )
        out = twitter_ingest_service.fetch_source(source.pk)
        assert out["status"] == "success"
        assert out["fetched_count"] == 1
        source.refresh_from_db()
        checkpoint = source.checkpoints.filter(adapter="twitter").first()
        assert checkpoint is not None
        assert checkpoint.last_external_id == "10"


def test_twitter_disabled_without_explicit_flag():
    from apps.sources.services.twitter_ingest import twitter_ingest_service

    source = Source.objects.create(
        platform=Platform.TWITTER_X,
        name="Disabled X",
        identifier="@disabled",
        url="https://x.com/disabled",
    )
    out = twitter_ingest_service.fetch_source(source.pk)
    assert out["status"] == "skipped"
