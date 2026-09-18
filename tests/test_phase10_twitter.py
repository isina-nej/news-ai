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


def test_parse_session_credentials():
    from apps.sources.adapters.twitter_clients import parse_session_credentials

    assert parse_session_credentials("") == {}
    assert parse_session_credentials("auth123") == {"auth_token": "auth123"}
    assert parse_session_credentials("auth_token=tok; ct0=csrf") == {
        "auth_token": "tok",
        "ct0": "csrf",
    }
    assert parse_session_credentials('{"auth_token": "j1", "ct0": "j2"}') == {
        "auth_token": "j1",
        "ct0": "j2",
    }


def test_env_session_client_auth_error():
    import httpx

    from apps.sources.adapters.base import AuthenticationError
    from apps.sources.adapters.twitter_clients import EnvSessionClient

    transport = httpx.MockTransport(
        lambda request: httpx.Response(401, json={"error": "unauthorized"})
    )
    client = EnvSessionClient(session="auth_token=bad", transport=transport)
    with pytest.raises(AuthenticationError):
        client.fetch_user_tweets(username="target")


def test_env_session_client_rate_limit_error():
    import httpx

    from apps.sources.adapters.base import RateLimitError
    from apps.sources.adapters.twitter_clients import EnvSessionClient

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            429,
            headers={"retry-after": "120"},
            json={"error": "rate limited"},
        )
    )
    client = EnvSessionClient(session="auth_token=ok", transport=transport)
    with pytest.raises(RateLimitError) as exc_info:
        client.fetch_user_tweets(username="target")
    assert exc_info.value.retry_after == 120


def test_env_session_client_not_found_error():
    import httpx

    from apps.sources.adapters.base import PermanentSourceError
    from apps.sources.adapters.twitter_clients import EnvSessionClient

    transport = httpx.MockTransport(
        lambda request: httpx.Response(404, json={"error": "user not found"})
    )
    client = EnvSessionClient(session="auth_token=ok", transport=transport)
    with pytest.raises(PermanentSourceError):
        client.fetch_user_tweets(username="nonexistent")


def test_env_session_client_parses_graphql_timeline():
    import httpx

    from apps.sources.adapters.twitter_clients import EnvSessionClient

    tweet_data = {
        "__typename": "Tweet",
        "rest_id": "98765",
        "core": {"user_results": {"result": {"legacy": {"screen_name": "techreporter"}}}},
        "legacy": {
            "full_text": "Live test tweet from X",
            "created_at": "Tue Sep 08 14:00:00 +0000 2026",
            "favorite_count": 42,
            "retweet_count": 7,
            "reply_count": 3,
            "bookmark_count": 5,
            "lang": "en",
        },
        "views": {"count": "1250"},
    }
    sample_gql = {
        "data": {
            "user": {
                "result": {
                    "timeline_v2": {
                        "timeline": {
                            "instructions": [
                                {
                                    "type": "TimelineAddEntries",
                                    "entries": [
                                        {
                                            "entryId": "tweet-98765",
                                            "content": {
                                                "entryType": "TimelineTimelineItem",
                                                "itemContent": {
                                                    "itemType": "TimelineTweet",
                                                    "tweet_results": {"result": tweet_data},
                                                },
                                            },
                                        },
                                        {
                                            "entryId": "cursor-bottom-123",
                                            "content": {
                                                "entryType": "TimelineTimelineCursor",
                                                "cursorType": "Bottom",
                                                "value": "cursor_token_abc",
                                            },
                                        },
                                    ],
                                }
                            ]
                        }
                    }
                }
            }
        }
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=sample_gql))
    client = EnvSessionClient(session="auth_token=valid_tok; ct0=csrf_tok", transport=transport)
    page = client.fetch_user_tweets(username="techreporter", limit=10)
    assert len(page["tweets"]) == 1
    t = page["tweets"][0]
    assert t["id"] == "98765"
    assert t["text"] == "Live test tweet from X"
    assert t["author"]["username"] == "techreporter"
    assert t["public_metrics"]["like_count"] == 42
    assert t["public_metrics"]["impression_count"] == "1250"
    assert page["next_cursor"] == "cursor_token_abc"
