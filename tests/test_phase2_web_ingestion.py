"""Comprehensive test suite for Phase 2: Web Ingestion Pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from django.core.cache import cache

from apps.core.choices import Platform
from apps.news.models import SourceItem
from apps.news.services.canonical_url import CanonicalURLService, canonical_url_service
from apps.news.services.normalization import NormalizationService, normalization_service
from apps.news.services.persistence import (
    ingestion_persistence_service,
)
from apps.sources.adapters import (
    ExtractionError,
    FetchContext,
    FetchedItem,
    HTMLSourceAdapter,
    NetworkError,
    ParseError,
    PayloadTooLargeError,
    PermanentSourceError,
    RateLimitError,
    RSSHubAdapter,
    RSSSourceAdapter,
    SafeHttpClient,
    SSRFError,
)
from apps.sources.adapters.ssrf import is_ip_disallowed, validate_url_for_ssrf
from apps.sources.models import FetchRun, FetchRunStatus, Source
from apps.sources.services.fetcher import SourceFetchService

pytestmark = pytest.mark.django_db

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _read_fixture(filename: str) -> bytes:
    return (FIXTURES_DIR / filename).read_bytes()


def _make_source(**kwargs) -> Source:
    kwargs.setdefault("platform", Platform.RSS)
    kwargs.setdefault("name", "Test News Source")
    kwargs.setdefault("identifier", "https://example.com/feed.xml")
    kwargs.setdefault("url", "https://example.com/feed.xml")
    return Source.objects.create(**kwargs)


# ---------------------------------------------------------------------------
# 1. SSRF Protection Tests
# ---------------------------------------------------------------------------


def test_ssrf_disallowed_ip_ranges():
    # Loopback
    assert is_ip_disallowed("127.0.0.1")
    assert is_ip_disallowed("127.0.0.2")
    assert is_ip_disallowed("::1")
    # Private
    assert is_ip_disallowed("10.0.0.1")
    assert is_ip_disallowed("172.16.0.1")
    assert is_ip_disallowed("192.168.1.1")
    # Link-local / Cloud metadata
    assert is_ip_disallowed("169.254.169.254")
    # Carrier-grade NAT
    assert is_ip_disallowed("100.64.0.1")
    # Public IPs are allowed
    assert not is_ip_disallowed("8.8.8.8")
    assert not is_ip_disallowed("1.1.1.1")


def test_ssrf_blocks_private_and_localhost_urls():
    with pytest.raises(SSRFError):
        validate_url_for_ssrf("http://localhost:8000/feed")

    with pytest.raises(SSRFError):
        validate_url_for_ssrf("http://127.0.0.1/rss")

    with pytest.raises(SSRFError):
        validate_url_for_ssrf("http://169.254.169.254/latest/meta-data")

    with pytest.raises(SSRFError):
        validate_url_for_ssrf("file:///etc/passwd")

    with pytest.raises(SSRFError):
        validate_url_for_ssrf("ftp://example.com/feed")


def test_ssrf_allows_explicit_allowlist():
    # Containerized internal host allowed when explicit
    validate_url_for_ssrf("http://rsshub:1200/feed", allow_hosts=["rsshub"])


# ---------------------------------------------------------------------------
# 2. Canonical URL Service Tests
# ---------------------------------------------------------------------------


def test_canonical_url_strips_utm_and_tracking_params():
    raw = "https://EXAMPLE.COM:443/news/article/?utm_source=twitter&utm_medium=social&id=123&fbclid=xyz#section-2"
    canonical, url_hash = canonical_url_service.canonicalize_and_hash(raw)
    assert canonical == "https://example.com/news/article?id=123"
    assert url_hash == CanonicalURLService.compute_hash(canonical)


def test_canonical_url_preserves_meaningful_params_sorted():
    raw = "https://example.com/search?q=ai&page=2&b=1&a=2"
    canonical = canonical_url_service.normalize(raw)
    assert canonical == "https://example.com/search?a=2&b=1&page=2&q=ai"


def test_canonical_url_trailing_slash_normalization():
    assert (
        canonical_url_service.normalize("https://example.com/path/") == "https://example.com/path"
    )
    assert canonical_url_service.normalize("https://example.com/") == "https://example.com/"


# ---------------------------------------------------------------------------
# 3. Text Normalization Service Tests
# ---------------------------------------------------------------------------


def test_normalization_persian_arabic_safe_translation():
    # Arabic Yeh (ي) -> Persian Yeh (ی), Arabic Kaf (ك) -> Persian Keheh (ک)
    arabic_text = "شركت فناوري اطلاعات"
    expected = "شرکت فناوری اطلاعات"
    assert normalization_service.normalize(arabic_text) == expected


def test_normalization_removes_tatweel_and_cleans_zwnj():
    text_with_tatweel = "اطـــــلاعات"
    assert normalization_service.normalize(text_with_tatweel) == "اطلاعات"

    # Redundant ZWNJs collapsed, and ZWNJ next to space stripped
    text_with_zwnj = "می‌‌‌رویم  و  می‌ دانیم"
    normalized = normalization_service.normalize(text_with_zwnj)
    assert "می‌رویم" in normalized
    assert "می دانیم" in normalized


def test_normalization_collapses_whitespace_and_preserves_raw():
    raw = "  Hello   world!  \n\n\n\nNew   paragraph.  "
    norm = normalization_service.normalize(raw)
    assert norm == "Hello world!\n\nNew paragraph."
    # Raw is unchanged
    assert raw.startswith("  Hello")


def test_normalization_produces_deterministic_hashes():
    raw = "  تیتر اول  "
    norm, raw_hash, norm_hash = normalization_service.normalize_and_hash(raw)
    assert norm == "تیتر اول"
    assert raw_hash != norm_hash
    assert norm_hash == NormalizationService.compute_hash("تیتر اول")


# ---------------------------------------------------------------------------
# 4. SafeHttpClient & Error Mapping Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_client_maps_status_codes():
    client = SafeHttpClient()

    with patch.object(client, "get_client") as mock_get_client:
        mock_async_client = AsyncMock()
        mock_get_client.return_value = mock_async_client

        # 304 Not Modified
        mock_res_304 = AsyncMock(
            status_code=304, is_redirect=False, headers={"etag": "abc"}, content=b""
        )
        mock_async_client.get.return_value = mock_res_304
        with patch("apps.sources.adapters.http_client.validate_url_for_ssrf"):
            status, headers, body, _ = await client.fetch("https://example.com/feed")
            assert status == 304

        # 429 Rate Limit
        mock_res_429 = AsyncMock(
            status_code=429, is_redirect=False, headers={"retry-after": "45"}, content=b""
        )
        mock_async_client.get.return_value = mock_res_429
        with patch("apps.sources.adapters.http_client.validate_url_for_ssrf"):
            with pytest.raises(RateLimitError) as exc_info:
                await client.fetch("https://example.com/feed")
            assert exc_info.value.retry_after == 45
            assert exc_info.value.is_transient is True

        # 404 Permanent
        mock_res_404 = AsyncMock(
            status_code=404, is_redirect=False, headers={}, content=b"Not Found"
        )
        mock_async_client.get.return_value = mock_res_404
        with patch("apps.sources.adapters.http_client.validate_url_for_ssrf"):
            with pytest.raises(PermanentSourceError) as exc_info:
                await client.fetch("https://example.com/feed")
            assert exc_info.value.is_transient is False

        # 500 Network / Server Error
        mock_res_500 = AsyncMock(status_code=500, is_redirect=False, headers={}, content=b"Error")
        mock_async_client.get.return_value = mock_res_500
        with patch("apps.sources.adapters.http_client.validate_url_for_ssrf"):
            with pytest.raises(NetworkError) as exc_info:
                await client.fetch("https://example.com/feed")
            assert exc_info.value.is_transient is True


@pytest.mark.asyncio
async def test_http_client_rejects_oversized_response():
    client = SafeHttpClient(max_bytes=100)
    with patch.object(client, "get_client") as mock_get_client:
        mock_async_client = AsyncMock()
        mock_get_client.return_value = mock_async_client
        mock_res = AsyncMock(status_code=200, is_redirect=False, headers={}, content=b"X" * 150)
        mock_async_client.get.return_value = mock_res
        with patch("apps.sources.adapters.http_client.validate_url_for_ssrf"):
            with pytest.raises(PayloadTooLargeError):
                await client.fetch("https://example.com/feed")


# ---------------------------------------------------------------------------
# 5. RSS & Atom Adapter Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rss_adapter_parses_rss2_fixture():
    rss_bytes = _read_fixture("sample_rss2.xml")
    mock_http = AsyncMock()
    mock_http.fetch.return_value = (200, {"etag": "etag-123"}, rss_bytes, 45)

    adapter = RSSSourceAdapter(http_client=mock_http)
    context = FetchContext(
        source_id=1, url="https://techchronicle.example.com/feed.xml", platform="rss"
    )

    result = await adapter.fetch(context)

    assert result.status_code == 200
    assert result.etag == "etag-123"
    assert len(result.items) == 2

    item1 = result.items[0]
    assert item1.title == "Breakthrough in Neuromorphic Architecture"
    assert item1.external_id == "tc-2026-09-001"
    assert "neuromorphic" in item1.raw_text.lower()
    assert item1.published_at == datetime(2026, 9, 7, 8, 30, 0, tzinfo=UTC)
    assert item1.media.get("enclosures")


@pytest.mark.asyncio
async def test_rss_adapter_parses_atom_fixture():
    atom_bytes = _read_fixture("sample_atom.xml")
    mock_http = AsyncMock()
    mock_http.fetch.return_value = (200, {}, atom_bytes, 30)

    adapter = RSSSourceAdapter(http_client=mock_http)
    context = FetchContext(
        source_id=2, url="https://engdigest.example.com/atom.xml", platform="rss"
    )

    result = await adapter.fetch(context)
    assert len(result.items) == 1

    item = result.items[0]
    assert item.title == "Scaling Distributed Cache Tier Under High Concurrency"
    assert item.author == "Alex Rivera"
    assert item.published_at == datetime(2026, 9, 7, 6, 30, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_rss_adapter_missing_date_yields_none():
    feed_bytes = _read_fixture("sample_feed_missing_date.xml")
    mock_http = AsyncMock()
    mock_http.fetch.return_value = (200, {}, feed_bytes, 20)

    adapter = RSSSourceAdapter(http_client=mock_http)
    context = FetchContext(source_id=3, url="https://wire.example.com/feed", platform="rss")

    result = await adapter.fetch(context)
    assert len(result.items) == 1
    # Critical invariant: published_at MUST NOT be spoofed to now
    assert result.items[0].published_at is None


@pytest.mark.asyncio
async def test_rss_adapter_malformed_feed_raises_parse_error():
    broken_bytes = _read_fixture("sample_feed_malformed.xml")
    mock_http = AsyncMock()
    mock_http.fetch.return_value = (200, {}, broken_bytes, 10)

    adapter = RSSSourceAdapter(http_client=mock_http)
    context = FetchContext(source_id=4, url="https://bad.example.com/feed", platform="rss")

    with pytest.raises(ParseError):
        await adapter.fetch(context)


# ---------------------------------------------------------------------------
# 6. RSSHub Adapter Tests
# ---------------------------------------------------------------------------


def test_rsshub_adapter_builds_endpoint_url_with_access_key():
    adapter = RSSHubAdapter()
    context = FetchContext(
        source_id=10,
        url="https://rsshub.example.com",
        platform=Platform.RSSHUB,
        configuration={
            "base_url": "http://rsshub:1200",
            "route": "telegram/channel/techinsider",
            "params": {"limit": "15"},
        },
    )

    with patch("apps.sources.adapters.rsshub.settings") as mock_settings:
        mock_settings.RSSHUB_BASE_URL = "http://rsshub:1200"
        mock_settings.RSSHUB_ACCESS_KEY = "supersecretkey"

        url = adapter.build_endpoint_url(context)
        assert url.startswith("http://rsshub:1200/telegram/channel/techinsider?")
        assert "key=supersecretkey" in url
        assert "limit=15" in url


# ---------------------------------------------------------------------------
# 7. HTML Article Extraction Adapter Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_html_adapter_extracts_article_and_canonical_url():
    html_bytes = _read_fixture("sample_article.html")
    mock_http = AsyncMock()
    mock_http.fetch.return_value = (200, {}, html_bytes, 50)

    adapter = HTMLSourceAdapter(http_client=mock_http)
    context = FetchContext(
        source_id=5,
        url="https://cleanenergy.example.com/reports/global-grid-milestone-2026?utm_source=news",
        platform=Platform.WEBSITE_HTML,
    )

    result = await adapter.fetch(context)
    assert len(result.items) == 1

    item = result.items[0]
    assert "Global Renewable Grid Milestone Achieved" in item.title
    assert "Marcus Vance" in item.author
    assert "ninety-eight percent" in item.raw_text
    assert (
        item.canonical_url == "https://cleanenergy.example.com/reports/global-grid-milestone-2026"
    )
    assert item.published_at == datetime(2026, 9, 7, 10, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_html_adapter_empty_page_raises_extraction_error():
    empty_bytes = _read_fixture("sample_empty_article.html")
    mock_http = AsyncMock()
    mock_http.fetch.return_value = (200, {}, empty_bytes, 10)

    adapter = HTMLSourceAdapter(http_client=mock_http)
    context = FetchContext(
        source_id=6, url="https://empty.example.com", platform=Platform.WEBSITE_HTML
    )

    with pytest.raises(ExtractionError):
        await adapter.fetch(context)


# ---------------------------------------------------------------------------
# 8. Ingestion Persistence & Exact Dedupe Tests
# ---------------------------------------------------------------------------


def test_persistence_creates_and_exact_dedupes():
    source = _make_source()
    items = [
        FetchedItem(
            url="https://example.com/post-1?utm_source=twitter",
            external_id="p-1",
            title="First Post",
            raw_text="The quick brown fox jumps over the lazy dog.",
        ),
        FetchedItem(
            url="https://example.com/post-2",
            external_id="p-2",
            title="Second Post",
            raw_text="Different story entirely.",
        ),
    ]

    # First ingest: creates 2 items
    res1 = ingestion_persistence_service.persist_items(source, items)
    assert res1.created_count == 2
    assert res1.duplicate_count == 0
    assert SourceItem.objects.filter(source=source).count() == 2

    # Second ingest of identical items: both flagged as duplicates, 0 created
    res2 = ingestion_persistence_service.persist_items(source, items)
    assert res2.created_count == 0
    assert res2.duplicate_count == 2
    assert SourceItem.objects.filter(source=source).count() == 2


def test_cross_source_identical_content_both_preserved():
    s1 = _make_source(name="Source 1", identifier="s1")
    s2 = _make_source(name="Source 2", identifier="s2")

    item1 = [
        FetchedItem(
            url="https://news1.example.com/breaking",
            external_id="ext-1",
            title="Breaking News Event",
            raw_text="Global leaders have concluded the international summit.",
        )
    ]
    item2 = [
        FetchedItem(
            url="https://news2.example.com/breaking",
            external_id="ext-2",
            title="Breaking News Event",
            raw_text="Global leaders have concluded the international summit.",
        )
    ]

    res1 = ingestion_persistence_service.persist_items(s1, item1)
    res2 = ingestion_persistence_service.persist_items(s2, item2)

    assert res1.created_count == 1
    assert res2.created_count == 1
    # Both items must be saved in database!
    assert SourceItem.objects.count() == 2


def test_persistence_sanitizes_huge_payload():
    source = _make_source()
    huge_data = {"key_" + str(i): "x" * 2000 for i in range(100)}  # ~200KB
    items = [
        FetchedItem(
            url="https://example.com/huge",
            title="Huge Payload Item",
            raw_text="Body",
            raw_payload=huge_data,
        )
    ]

    res = ingestion_persistence_service.persist_items(source, items)
    assert res.created_count == 1
    saved_item = SourceItem.objects.get(pk=res.created_items[0].pk)
    assert saved_item.raw_payload.get("_truncated") is True


# ---------------------------------------------------------------------------
# 9. SourceFetchService & Health / Audit Tests
# ---------------------------------------------------------------------------


def test_source_fetch_service_records_fetch_run_and_updates_health():
    source = _make_source()
    service = SourceFetchService()

    fake_items = [
        FetchedItem(
            url="https://example.com/item1", external_id="id-1", title="Title 1", raw_text="Text 1"
        )
    ]

    mock_adapter = AsyncMock()
    mock_adapter.fetch.return_value = AsyncMock(
        items=fake_items,
        status_code=200,
        etag="etag-abc",
        last_modified=None,
        not_modified=False,
    )

    with patch.object(service.registry, "get", return_value=mock_adapter):
        outcome = service.fetch_source(source.pk)

    assert outcome["status"] == "success"
    assert outcome["created_count"] == 1

    source.refresh_from_db()
    assert source.last_success_at is not None
    assert source.consecutive_failures == 0
    assert source.configuration.get("_http_etag") == "etag-abc"

    run = FetchRun.objects.filter(source=source).first()
    assert run is not None
    assert run.status == FetchRunStatus.SUCCESS
    assert run.fetched_count == 1
    assert run.created_count == 1


def test_source_fetch_service_handles_304_not_modified():
    source = _make_source()
    service = SourceFetchService()

    mock_adapter = AsyncMock()
    mock_adapter.fetch.return_value = AsyncMock(
        items=[],
        status_code=304,
        etag="etag-prev",
        last_modified=None,
        not_modified=True,
    )

    with patch.object(service.registry, "get", return_value=mock_adapter):
        outcome = service.fetch_source(source.pk)

    assert outcome["status"] == "not_modified"

    run = FetchRun.objects.filter(source=source).first()
    assert run is not None
    assert run.status == FetchRunStatus.NOT_MODIFIED
    assert run.http_status == 304


def test_source_fetch_service_distributed_lock_prevents_overlap():
    source = _make_source()
    service = SourceFetchService()

    # Pre-acquire lock
    lock_key = f"fetch:source:{source.pk}"
    cache.set(lock_key, "other-worker-running", timeout=60)

    try:
        outcome = service.fetch_source(source.pk)
        assert outcome["status"] == "skipped"
        assert outcome["reason"] == "already_running"
    finally:
        cache.delete(lock_key)
