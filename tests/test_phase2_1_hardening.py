"""Phase 2.1 Production Hardening regression test suite."""

from __future__ import annotations

import asyncio
import http.server
import socketserver
import threading
from unittest.mock import AsyncMock, patch

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.core.choices import ContentType, Platform
from apps.core.lock import DistributedLock
from apps.core.redaction import redact_text, redact_url
from apps.news.models import SourceItem
from apps.news.services.canonical_url import canonical_url_service
from apps.news.services.normalization import strip_html_tags
from apps.news.services.persistence import (
    ingestion_persistence_service,
    resolve_effective_url,
)
from apps.sources.adapters import (
    FetchContext,
    FetchedItem,
    PayloadTooLargeError,
    RSSHubAdapter,
    SafeHttpClient,
    SSRFError,
    TimeoutError,
)
from apps.sources.adapters.http_client import parse_retry_after
from apps.sources.models import FetchRun, FetchRunStatus, Source
from apps.sources.services.fetcher import SourceFetchService

pytestmark = pytest.mark.django_db


def _make_source(**kwargs) -> Source:
    kwargs.setdefault("platform", Platform.RSS)
    kwargs.setdefault("name", "Hardening Source")
    kwargs.setdefault("identifier", "https://news.example.com/feed")
    kwargs.setdefault("url", "https://news.example.com/feed")
    return Source.objects.create(**kwargs)


# ---------------------------------------------------------------------------
# 1. HTTPX Event-Loop Lifecycle
# ---------------------------------------------------------------------------


def test_httpx_cross_event_loop_reuse():
    """Verify consecutive fetches across distinct asyncio.run() loops work without RuntimeError."""

    class QuietHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"pong")

        def log_message(self, format, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), QuietHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}/ping"
    client = SafeHttpClient(allow_hosts=["127.0.0.1"])

    try:
        # Loop 1
        def _run_fetch_1():
            return asyncio.run(client.fetch(url))

        status1, _, body1, _ = _run_fetch_1()
        assert status1 == 200
        assert body1 == b"pong"

        # Loop 2: separate event loop, must NOT raise 'Event loop is closed'
        def _run_fetch_2():
            return asyncio.run(client.fetch(url))

        status2, _, body2, _ = _run_fetch_2()
        assert status2 == 200
        assert body2 == b"pong"

    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# 2. DB-Level Exact Dedupe Race Safety
# ---------------------------------------------------------------------------


def test_db_unique_constraints_on_url_hash_and_content_hash():
    s = _make_source()

    # 1. Same source + same url_hash -> DB IntegrityError
    SourceItem.objects.create(source=s, canonical_url="https://example.com/a", title="A")
    with pytest.raises((IntegrityError, ValidationError)), transaction.atomic():
        SourceItem.objects.create(
            source=s, canonical_url="https://example.com/a", title="Duplicate URL"
        )

    # 2. Same source + same content_hash -> DB IntegrityError
    SourceItem.objects.create(source=s, raw_text="Identical Body Content", title="B")
    with pytest.raises((IntegrityError, ValidationError)), transaction.atomic():
        SourceItem.objects.create(
            source=s, raw_text="Identical Body Content", title="Duplicate Content"
        )

    # 3. Cross-source identical content -> BOTH ALLOWED
    s2 = _make_source(name="Other Source", identifier="https://other.example.com")
    item_cross = SourceItem.objects.create(source=s2, raw_text="Identical Body Content", title="C")
    assert item_cross.pk is not None


def test_empty_hashes_stay_null_and_do_not_collide():
    s = _make_source()
    # Multiple items without URL or without content must not collide
    item1 = SourceItem.objects.create(source=s, title="No URL 1")
    item2 = SourceItem.objects.create(source=s, title="No URL 2")
    assert item1.url_hash is None
    assert item2.url_hash is None
    assert item1.content_hash is None
    assert item2.content_hash is None


def test_persistence_service_concurrent_race_safety():
    s = _make_source()
    item = FetchedItem(
        url="https://example.com/race-test",
        title="Race Title",
        raw_text="Race Content",
        external_id="race-1",
    )

    # Ingest once
    res1 = ingestion_persistence_service.persist_items(s, [item])
    assert res1.created_count == 1

    # Simulate concurrent worker inserting identical item
    res2 = ingestion_persistence_service.persist_items(s, [item])
    assert res2.created_count == 0
    assert res2.duplicate_count == 1
    assert SourceItem.objects.filter(source=s).count() == 1


# ---------------------------------------------------------------------------
# 3. Distributed Lock Ownership
# ---------------------------------------------------------------------------


def test_distributed_lock_atomic_token_release():
    key = "test:lock:ownership"
    cache.delete(key)

    lock_a = DistributedLock(key, token="tok-a", ttl_seconds=60)  # noqa: S106
    assert lock_a.acquire() is True
    assert cache.get(key) == "tok-a"

    # Simulate lock expiration and acquisition by worker B
    cache.set(key, "tok-b", timeout=60)

    # Worker A attempts to release, must NOT delete worker B's lock
    released = lock_a.release()
    assert released is False
    # Worker B's token is still intact in cache!
    assert cache.get(key) == "tok-b"

    # Worker B releases cleanly
    lock_b = DistributedLock(key, token="tok-b")  # noqa: S106
    assert lock_b.release() is True
    assert cache.get(key) is None


def test_distributed_lock_extension():
    key = "test:lock:heartbeat"
    cache.delete(key)

    lock = DistributedLock(key, token="tok-owner", ttl_seconds=30)  # noqa: S106
    assert lock.acquire() is True
    assert lock.extend(60) is True

    # Cannot extend if token was stolen
    cache.set(key, "other-token")
    assert lock.extend(60) is False
    cache.delete(key)


# ---------------------------------------------------------------------------
# 4. Secret Redaction
# ---------------------------------------------------------------------------


def test_secret_redaction_utility():
    url_with_key = "https://rsshub:1200/telegram/channel/test?key=supersecrettoken123&limit=20"
    redacted = redact_url(url_with_key)
    assert "supersecrettoken123" not in redacted
    assert "key=%5BREDACTED%5D" in redacted or "key=[REDACTED]" in redacted
    assert "limit=20" in redacted

    msg = "Error: https://rsshub:1200/feed?access_key=mysecretpass with token=xyz987"
    cleaned = redact_text(msg)
    assert "mysecretpass" not in cleaned
    assert "xyz987" not in cleaned


def test_secret_redaction_in_fetch_run_and_errors():
    s = _make_source()
    service = SourceFetchService()

    secret_key = "TOP_SECRET_RSSHUB_KEY_999"  # noqa: S105
    secret_url = f"https://example.com/rss?key={secret_key}&token=secret_abc"

    mock_adapter = AsyncMock()
    mock_adapter.fetch.side_effect = TimeoutError(f"Connection timed out for {secret_url}")

    with patch.object(service.registry, "get", return_value=mock_adapter):
        with pytest.raises(TimeoutError) as exc_info:
            service.fetch_source(s.pk)

        # Exception message must be redacted
        assert secret_key not in str(exc_info.value)
        assert "secret_abc" not in str(exc_info.value)

    # FetchRun error_message in database must be redacted
    run = FetchRun.objects.filter(source=s).last()
    assert run is not None
    assert secret_key not in run.error_message
    assert "secret_abc" not in run.error_message


# ---------------------------------------------------------------------------
# 5. RSSHub SSRF Trust Boundary
# ---------------------------------------------------------------------------


def test_rsshub_rejects_untrusted_internal_host():
    adapter = RSSHubAdapter()
    context = FetchContext(
        source_id=1,
        url="https://example.com",
        platform=Platform.RSSHUB,
        configuration={
            "base_url": "http://192.168.1.50:8000",
            "route": "test",
        },
    )

    with patch("apps.sources.adapters.rsshub.settings") as mock_settings:
        mock_settings.RSSHUB_TRUSTED_HOSTS = ["rsshub"]
        mock_settings.RSSHUB_BASE_URL = "http://rsshub:1200"
        mock_settings.RSSHUB_ACCESS_KEY = ""

        # Unconfigured internal IP should be blocked by SSRF validation
        with pytest.raises(SSRFError):
            asyncio.run(adapter.fetch(context))


# ---------------------------------------------------------------------------
# 6. Streaming Response Size Guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_size_guard_aborts_chunked_response():
    client = SafeHttpClient(max_bytes=200)

    class MockStreamResponse:
        status_code = 200
        is_redirect = False
        headers = {"transfer-encoding": "chunked"}

        async def aiter_bytes(self):
            for _ in range(10):
                yield b"A" * 50

        async def aclose(self):
            pass

    with patch.object(client, "get_client") as mock_get_client:
        mock_async = AsyncMock()
        mock_get_client.return_value = mock_async
        mock_async.build_request.return_value = "req"
        mock_async.send.return_value = MockStreamResponse()

        with patch("apps.sources.adapters.http_client.validate_url_for_ssrf"):
            with pytest.raises(PayloadTooLargeError) as exc_info:
                await client.fetch("https://example.com/big-stream")
            assert "exceeded limit of 200 bytes" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 7. Canonical URL Correctness & Document Preference
# ---------------------------------------------------------------------------


def test_canonical_url_preserves_encoded_path_characters():
    raw = "https://example.com/tag/c%2B%2B/post%2F1"
    normalized = canonical_url_service.normalize(raw)
    assert "%2F" in normalized or "%2f" in normalized
    assert "%2B" in normalized or "%2b" in normalized


def test_canonical_url_preserves_meaningful_source_and_ref_params():
    raw = "https://example.com/article?source=homepage&ref=breaking_header&utm_medium=email"
    normalized = canonical_url_service.normalize(raw)
    assert "source=homepage" in normalized
    assert "ref=breaking_header" in normalized
    assert "utm_medium" not in normalized


def test_resolve_effective_url_relative_and_domain_safety():
    fetched = "https://news.example.com/world/article-100"

    # 1. Relative canonical URL -> resolved correctly
    rel = "/world/article-100-canonical"
    assert (
        resolve_effective_url(fetched, rel)
        == "https://news.example.com/world/article-100-canonical"
    )

    # 2. Same domain absolute -> accepted
    abs_url = "https://news.example.com/world/article-100-final"
    assert resolve_effective_url(fetched, abs_url) == abs_url

    # 3. Foreign domain canonical -> rejected (prevents canonical hijacking)
    foreign = "https://spammer.example.org/hijack"
    assert resolve_effective_url(fetched, foreign) == fetched


# ---------------------------------------------------------------------------
# 8. SourceItem Hash Invariants & Mutability
# ---------------------------------------------------------------------------


def test_source_item_hashes_recomputed_on_mutation():
    s = _make_source()
    item = SourceItem.objects.create(
        source=s,
        title="Original Title",
        raw_text="Original Raw Content",
        canonical_url="https://example.com/post-orig",
    )

    orig_raw_hash = item.raw_content_hash
    orig_norm_hash = item.content_hash
    orig_url_hash = item.url_hash

    assert orig_raw_hash is not None
    assert orig_norm_hash is not None
    assert orig_url_hash is not None

    # Mutate raw text and save -> hashes must recompute, never stay stale
    item.raw_text = "Updated Content Completely Different"
    item.save()
    assert item.raw_content_hash != orig_raw_hash
    assert item.content_hash != orig_norm_hash

    # Mutate canonical_url and save
    item.canonical_url = "https://example.com/post-new"
    item.save()
    assert item.url_hash != orig_url_hash


# ---------------------------------------------------------------------------
# 9. FetchRun Lifecycle & Unexpected Errors
# ---------------------------------------------------------------------------


def test_fetch_run_status_lifecycle_and_unexpected_error():
    s = _make_source()
    service = SourceFetchService()

    mock_adapter = AsyncMock()
    mock_adapter.fetch.side_effect = ZeroDivisionError("Unexpected math bug in adapter")

    with patch.object(service.registry, "get", return_value=mock_adapter):
        with pytest.raises(ZeroDivisionError):
            service.fetch_source(s.pk)

    # FetchRun must be in FAILED status, NOT misleading SUCCESS or stuck in RUNNING
    run = FetchRun.objects.filter(source=s).last()
    assert run is not None
    assert run.status == FetchRunStatus.FAILED
    assert run.error_type == "ZeroDivisionError"
    assert "Unexpected math bug" in run.error_message
    assert run.finished_at is not None

    # Source health consecutive_failures must increment
    s.refresh_from_db()
    assert s.consecutive_failures == 1


# ---------------------------------------------------------------------------
# 10. Retry-After Parsing
# ---------------------------------------------------------------------------


def test_retry_after_delta_seconds_and_http_date():
    assert parse_retry_after("120") == 120
    assert parse_retry_after("0") == 0
    assert parse_retry_after("999999") == 3600

    future_date = "Wed, 21 Oct 2030 07:28:00 GMT"
    assert parse_retry_after(future_date) == 3600

    assert parse_retry_after("invalid") is None
    assert parse_retry_after(None) is None


# ---------------------------------------------------------------------------
# 11. RSS Content Cleanup
# ---------------------------------------------------------------------------


def test_rss_content_html_tag_cleanup():
    dirty = "<p>First paragraph with <a href='https://example.com'>link</a>.</p><br>Second."
    clean = strip_html_tags(dirty)
    assert "<p>" not in clean
    assert "<a" not in clean
    assert "<br>" not in clean
    assert "First paragraph with link." in clean
    assert "Second." in clean


# ---------------------------------------------------------------------------
# 12. ContentType in DTO
# ---------------------------------------------------------------------------


def test_persistence_respects_custom_content_type():
    s = _make_source()
    item = FetchedItem(
        url="https://t.me/channel/123",
        external_id="msg-123",
        title="Telegram Post",
        raw_text="Short update",
        content_type=ContentType.POST,
    )

    res = ingestion_persistence_service.persist_items(s, [item])
    assert res.created_count == 1
    saved = SourceItem.objects.get(pk=res.created_items[0].pk)
    assert saved.content_type == ContentType.POST
