# ADR: Phase 2.1 Production Hardening Decisions

Date: 2026-09-07. Status: accepted.

## 1. Async HTTP Client Lifecycle

**Context:** Celery worker tasks are synchronous functions that invoke `asyncio.run(adapter.fetch(context))` to run async network I/O. Each call to `asyncio.run()` creates a new event loop and closes it upon return. A global `httpx.AsyncClient` singleton bound to the initial loop causes `RuntimeError: Event loop is closed` when subsequent tasks execute.

**Decision:**
- `SafeHttpClient` tracks `asyncio.get_running_loop()`.
- If the current loop differs from the client's bound loop or the loop is closed, the client reference is discarded and a new `httpx.AsyncClient` is created for the active loop.
- Connection pooling is fully preserved for all requests within the same fetch execution (including manual redirect hops and pagination).
- `fetch_source` keeps Django ORM queries strictly synchronous on the worker thread, isolating async I/O cleanly to the adapter execution step.

## 2. Database-Level Exact Deduplication Guarantees

**Context:** Pre-insert queries (`filter(...).exists()`) are vulnerable to race conditions when multiple Celery workers ingest identical items concurrently. The database must provide the final, unbreakable invariant.

**Decision:**
- `url_hash`, `content_hash`, and `raw_content_hash` on `SourceItem` are nullable (`CharField(max_length=64, null=True, blank=True, default=None)`).
- In MySQL 8.4 and SQLite, `NULL` values are treated as distinct in unique indexes. Empty strings (`""`) are converted to `None` on save so items without a URL or without text never collide with each other.
- Added explicit per-source DB constraints:
  - `UniqueConstraint(fields=["source", "external_id"], name="uniq_item_source_external")`
  - `UniqueConstraint(fields=["source", "url_hash"], name="uniq_item_source_url_hash")`
  - `UniqueConstraint(fields=["source", "content_hash"], name="uniq_item_source_content_hash")`
- `IngestionPersistenceService` wraps creation in a transaction catching `IntegrityError` to safely record concurrent duplicates.
- Cross-source identical items are preserved across all constraints because `source` is part of every unique constraint.

## 3. Distributed Lock Ownership

**Context:** The previous pattern `cache.add(key, token); ... cache.delete(key)` is unsafe under lock expiration. If worker A's lock TTL expires and worker B acquires the lock, worker A's `finally` block would delete worker B's active lock.

**Decision:**
- `apps.core.lock.DistributedLock` assigns a unique ownership token (`uuid.uuid4().hex`) on acquisition.
- `release()` uses an atomic Redis Lua script checking that the stored token matches before deletion:
  ```lua
  if redis.call('get', KEYS[1]) == ARGV[1] then
      return redis.call('del', KEYS[1])
  else
      return 0
  end
  ```
- Non-Redis backends (such as LocMemCache during testing) verify the token before deleting.
- Heartbeat TTL extension is supported via `extend()`.

## 4. SSRF & RSSHub Trust Boundary

**Context:** Blanket inclusion of `localhost` and `127.0.0.1` in adapter allowlists permitted arbitrary source configurations to target internal services.

**Decision:**
- Blanket localhost/loopback entries removed.
- Only the specific hostname defined in deployment settings (`RSSHUB_TRUSTED_HOSTS` and `RSSHUB_BASE_URL`, default `rsshub`) is trusted for containerized inter-service communication.
- User-supplied source configurations cannot override SSRF protections.

## 5. Secret Redaction on All Logging and Error Paths

**Context:** Query parameters (such as `key` or `access_key` in RSSHub routes) could leak into exception messages, database `FetchRun.error_message`, or logs during timeouts, 429s, or network failures.

**Decision:**
- Central `apps.core.redaction` utility masks sensitive query parameters (`key`, `access_key`, `token`, `api_key`, `secret`, `password`, `auth`) in all URLs and text strings.
- `AdapterError.__init__` sanitizes error messages at instantiation so all adapter-raised exceptions are clean by default.
- `FetchRun.error_message` is sanitized before database persistence.

## 6. Chunked Streaming Response Size Guard

**Context:** Reading the full response with `response.content` before checking size permits malicious or misconfigured servers to exhaust worker RAM via chunked responses without `Content-Length`.

**Decision:**
- `SafeHttpClient` uses `client.stream("GET", ...)` and reads chunks incrementally via `response.aiter_bytes()`.
- If cumulative bytes exceed `max_bytes`, the stream is immediately aborted and `PayloadTooLargeError` is raised.
