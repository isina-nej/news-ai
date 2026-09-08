# ADR: Phase 3 Telegram User-Session Ingestion

Date: 2026-09-08. Status: accepted. Amended by Phase 3.1 (2026-09-08).

## 1. Kurigram client lifecycle (dedicated queue + loop-bound manager)

**Context:** Kurigram's `Client` owns an asyncio event loop, MTProto session
state and a connection pool; sharing one handle across Celery tasks running
`asyncio.run()` in different loops reproduces the Phase 2.1 HTTPX cross-loop
failure, and concurrent MTProto calls from one account invite FloodWait.

**Decision:**
- `TelegramClientManager` (per `account_key`) owns exactly one client,
  records the owning loop, and rebuilds on loop mismatch or close.
- `fetch_telegram_source_task` / `refresh_telegram_engagement_task` route to
  the dedicated `telegram` Celery queue; `docker-compose` runs
  `telegram-worker` with `-Q telegram -c 1` (concurrency 1 per account).
- Explicit `health_check()` (`get_me`), `disconnect()` and `reconnect()`;
  `start()` failures map auth problems to `AuthenticationError` (never an
  interactive login inside workers).
- Verified against Kurigram 2.2.25 (`pyrogram` top-level package):
  `Client(name, api_id, api_hash, session_string, in_memory=True,
  no_updates=True)`, `await start()` / `await stop()`,
  `get_chat_history(chat_id, limit, min_id, max_id)` async generator,
  `get_messages(chat_id, message_ids)` (batched, plain read),
  `get_chat(chat_id)`, `export_session_string()`.

## 2. Cursor / checkpoint semantics (fetch -> persist -> advance)

**Context:** Re-fetching full history every run wastes quota; advancing the
cursor before persistence loses messages on crash.

**Decision:**
- `SourceCheckpoint(source, adapter="telegram")` with
  `state = {"last_message_id": N}` (+ `last_external_id`,
  `last_published_at`). Strict order: load checkpoint -> fetch newer than
  checkpoint (+ bounded edit-lookback overlap) -> persist (creates/updates/
  duplicates) -> advance checkpoint.
- Crash between fetch and advance is safe: next run re-reads the overlap and
  idempotent persistence absorbs duplicates (at-least-once + dedupe).
- `Source.platform_external_id` caches the resolved numeric peer id
  (non-secret) so username renames never break the cursor.

## 3. SourceItem revision strategy (edit history, not overwrite-only)

**Context:** Telegram edits keep the same message id. Treating every
re-observation as a duplicate loses corrections; treating every re-observation
as new breaks identity.

**Decision:**
- `FetchedItem.source_updated_at` carries `message.edit_date` (UTC, None when
  unedited). On same-`external_id` re-observation with changed material
  fields (title/raw_text/normalized_text) and a newer-or-unknown edit stamp,
  the live `SourceItem` row is refreshed and the previous state is appended
  to immutable `SourceItemRevision(source_item, revision_number,
  source_updated_at, observed_at, material snapshot, change_metadata)`.
- Unchanged re-fetches are plain duplicates with no revision. Revisions feed
  future Story material-update, correction, novelty and republication logic.
- `Source.source_deleted_at`-style soft deletion is prepared on
  `SourceItem.source_deleted_at` but only set on platform-confirmed evidence;
  absence from one bounded poll never implies deletion.

## 4. Engagement snapshot semantics (honest, milestone-idempotent)

**Context:** Telegram exposes views/forwards/reactions/replies but no
independent shares/saves; naive copying double-counts ranking signals, and
timestamp-keyed snapshots collide across workers.

**Decision:**
- Mapping: `views -> views`, `forwards -> forwards`, reaction total ->
  `reactions` (+ `raw_metrics.reaction_breakdown`, no user identities),
  discussion reply counts -> `replies` (refresh path only);
  `shares`/`saves` stay `NULL` (unknown, never 0, never copied from forwards).
- Refresh reads via batched `get_messages` per peer (<=100 ids/call) — a
  plain read that never increments view counters.
- `EngagementSnapshot.target_age_seconds` names the milestone bucket with
  `UNIQUE(source_item, target_age_seconds)`; `captured_at` stays the real
  capture time. Same milestone from two workers = one row (idempotent).
- `EngagementTrackingState` schedules future-only milestones from
  `SNAPSHOT_SCHEDULE_MIN`; past buckets are never backfilled. A periodic
  batch scheduler scans due rows instead of thousands of 24h ETA tasks.
- FloodWait (`FloodWait.seconds`) maps to `RateLimitError(retry_after)` with
  Celery backoff+jitter plus auditable `Source.cooldown_until` so the
  scheduler backs off instead of busy-looping.

## 5. Phase 3.1 amendments (stable runtime, idempotent milestones)

**Lifecycle (replaces per-fetch `asyncio.run()`):** `apps/sources/telegram_runtime.py`
owns one asyncio loop on a background thread per worker process. Sync Celery
code enters via `run_sync()` (`run_coroutine_threadsafe`, stdlib only — no
`nest_asyncio`, no monkey patching). One `TelegramClientManager` per account
binds to that loop; loop mismatch rebuilds instead of reusing. Celery
`worker_shutdown` signal + `atexit` stop all clients cleanly; a stopped handle
is never reused without `start()`. Fetch and refresh share the same runtime
object, verified by loop-identity regression tests.

**Peer persist:** `platform_external_id` is written immediately
(`update_fields=["platform_external_id", "updated_at"]`) when first resolved,
not deferred to `record_fetch_success()` — which now writes health fields
only, so unrelated in-memory mutations are never silently dropped. Web fetcher
persists `_http_etag`/`_http_last_modified` explicitly for the same reason.

**Milestone retry safety:** `record_milestone_snapshot()` returns
`(snapshot, created)`; on `IntegrityError` retry it reuses the existing row
(`created=False`) and the caller still advances tracking with
`completed_target_age_seconds`, so a crash between insert and advance can
never wedge tracking on one bucket. `capture_reason`
(initial/milestone/edit_observation/manual) lets future baselines exclude
ad-hoc observations; routine re-polls write no ad-hoc snapshot, only genuine
edits do.

**Due-state claiming:** `_claim_due_states()` takes
`select_for_update(skip_locked=True)` row locks in a transaction (MySQL 8.4
InnoDB); concurrent refresh workers skip each other's rows. The milestone
`UNIQUE` remains the final guard on backends without skip-locked.

**Replies:** `_extract_reply_count()` reads stable inline containers
(`replies`/`reply_info` dicts or objects with `replies_count`/`reply_count`/
`count`; `get_discussion_replies_count` is a separate extra RPC, not scraped
bulk). Known counts (incl. real 0) stored; unavailable stays `NULL`. No reply
text or user identities collected.

**Checkpoint:** `last_published_at` is the monotonic max over ALL batches
(`max(prev, batch_max)`), never first-batch-only.

**Account cooldown:** `TelegramAccountRuntimeState(account_key,
cooldown_until, last_error_*)` holds account-wide FloodWait state; the ingest
service checks it before every dispatch and skips with `account_cooldown`.
Per-source `Source.cooldown_until` stays for source-specific backoff — the two
are stored and checked separately, never conflated.
