# Sources and Ingestion Architecture

Source ingestion uses an adapter-based architecture. Every source is represented by a `Source` record in the database and handled by an adapter registered in `adapter_registry`.

## Adapter Contract

All adapters implement `SourceAdapter(Protocol)` in `apps/sources/adapters/base.py`:

```python
class SourceAdapter(Protocol):
    async def fetch(self, context: FetchContext) -> FetchResult: ...
```

Pure Python DTOs:
- `FetchContext`: contains `url`, `configuration`, `etag`, `last_modified`, `timeout_seconds`, `max_bytes`, `correlation_id`.
- `FetchedItem`: normalized container for `external_id`, `url`, `title`, `raw_text`, `published_at`, `author`, `media`, `raw_payload`.
- `FetchResult`: `items`, `status_code`, `etag`, `last_modified`, `not_modified` (bool), `duration_ms`.
- `AdapterError` hierarchy: `NetworkError`, `TimeoutError`, `RateLimitError` (transient, retryable), `AuthenticationError`, `ParseError`, `ExtractionError`, `PermanentSourceError`, `SSRFError`, `PayloadTooLargeError` (permanent, fast-fail).

## Defining Sources

### 1. Adding an RSS / Atom Source
Create a `Source` with `platform="rss"`:
```python
Source.objects.create(
    name="TechCrunch",
    platform="rss",
    identifier="https://techcrunch.com/feed/",
    url="https://techcrunch.com/feed/",
    fetch_interval_seconds=300,
)
```
The `RSSSourceAdapter` parses RSS 2.0, Atom, and variants using `feedparser`, preserves genuine `published_at` in UTC (or `None` if missing), and handles `304 Not Modified` via HTTP cache validators (`ETag`, `Last-Modified`).

### 2. Adding an RSSHub Source
Create a `Source` with `platform="rsshub"`:
```python
Source.objects.create(
    name="GitHub Trending",
    platform="rsshub",
    identifier="github-trending-python",
    url="github/trending/daily/python",  # or in configuration["route"]
    configuration={
        "route": "github/trending/daily/python",
        "params": {"limit": "20"},
        # "base_url": "http://rsshub:1200",  # defaults to settings.RSSHUB_BASE_URL
    },
    fetch_interval_seconds=900,
)
```
`RSSHubAdapter` resolves `RSSHUB_ACCESS_KEY` from environment variables, generates the parameterized route, allows internal container access safely, and delegates feed parsing to the RSS parser.

### 3. Adding a Static HTML Source
Create a `Source` with `platform="website_html"`:
```python
Source.objects.create(
    name="Example Blog",
    platform="website_html",
    identifier="https://example.com/blog/latest",
    url="https://example.com/blog/latest",
    fetch_interval_seconds=3600,
)
```
`HTMLSourceAdapter` fetches via `SafeHttpClient`, runs `trafilatura` article extraction (title, author, published date, canonical URL, main body), and falls back to semantic HTML container extraction if necessary.

## Writing a New Adapter

1. Subclass `SourceAdapter`:
```python
from apps.sources.adapters.base import FetchContext, FetchResult, SourceAdapter


class CustomAdapter(SourceAdapter):
    async def fetch(self, context: FetchContext) -> FetchResult:
        # 1. Fetch raw data safely
        # 2. Parse into list[FetchedItem]
        # 3. Return FetchResult
        ...
```

2. Register in `apps/sources/adapters/registry.py`:
```python
adapter_registry.register("custom_platform", "custom_type", factory=CustomAdapter)
```

## Normalization and Exact Deduplication

Flow:
`Adapter` → `FetchedItem` → `NormalizationService` / `CanonicalURLService` → `IngestionPersistenceService` → `SourceItem`

1. **Canonical URL Service** (`apps/news/services/canonical_url.py`):
   - Lowercases scheme and host, removes default ports.
   - Strips fragments (`#...`) and trailing slashes.
   - Strips tracking query parameters (`utm_*`, `fbclid`, `gclid`, etc.).
   - Sorts and preserves meaningful query parameters.
   - Generates deterministic `url_hash` (SHA-256).

2. **Text Normalization Service** (`apps/news/services/normalization.py`):
   - **Never overwrites `raw_text`**.
   - Generates `normalized_text` (versioned as `v1`).
   - Unicode NFKC normalization.
   - Safe Persian/Arabic standardization (Arabic Yeh/Kaf to Persian equivalents, tatweel removal, ZWNJ cleanup).
   - Horizontal and vertical whitespace normalization.
   - Preserves emojis, hashtags, and punctuation.
   - Generates `raw_content_hash` and `content_hash` (normalized SHA-256).

3. **Deduplication Rules** (`apps/news/services/persistence.py`):
   - For items from the **same source**:
     1. Exact `external_id` match → `DUPLICATE`
     2. Exact `url_hash` match → `DUPLICATE`
     3. Exact `content_hash` match → `DUPLICATE`
   - **Cross-source items**:
     - Identical content from different sources is **never discarded**.
     - Each is saved as a separate `SourceItem` for independent multi-source verification and ranking.

## Security & SSRF Protection

- All external URLs pass through `validate_url_for_ssrf` in `apps/sources/adapters/ssrf.py`.
- Blocks loopback (`127.0.0.0/8`, `::1`), private ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), link-local / cloud metadata (`169.254.169.254`), and non-HTTP schemes.
- Redirects are re-validated at each hop.
- Response payloads are guarded by byte limits (default 5MB) to prevent memory exhaustion.
- Database payloads (`raw_payload`) are capped at 64KB with an explicit `_truncated: True` flag.

## Observability & Health

- Every fetch run produces a `FetchRun` audit record containing counts (`fetched`, `created`, `duplicate`, `rejected`), status, duration in milliseconds, HTTP status code, and correlation ID.
- Source health (`last_success_at`, `last_failure_at`, `consecutive_failures`) is updated transactionally.
- Distributed locking (`fetch:source:{id}`) in Redis ensures no duplicate overlapping fetches per source.
