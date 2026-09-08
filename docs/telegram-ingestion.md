# Telegram User-Session Ingestion (Kurigram)

Phase 3 implements read-only Telegram channel/group ingestion through a
Kurigram (Pyrogram-fork 2.2.25, top-level `pyrogram` package) user account.
Bot publishing is out of scope for this phase.

## Environment setup

```bash
TELEGRAM_API_ID=<your-api-id>
TELEGRAM_API_HASH=<your-api-hash>
TELEGRAM_SESSION_STRING=<your-session-string>   # user session string, env-only
TELEGRAM_ACCOUNT_KEY=default
TELEGRAM_WORKER_CONCURRENCY=1
```

Rules:

- Credentials live **only** in environment/settings. They are never stored in
  `Source.configuration`, never written to `raw_payload`, `FetchRun`, audit
  logs or structured logs (central `apps.core.redaction` sanitizes all error
  paths; `AdapterError` sanitizes on construction).
- No interactive login ever runs inside Celery workers. Missing or revoked
  sessions fail fast with `AuthenticationError`.
- Verify connectivity without leaking secrets:

```bash
python manage.py telegram_check_session [--account-key default]
```

## Source setup

```python
Source.objects.create(
    name="Tech Insider",
    platform="telegram",
    identifier="@techinsider",
    url="https://t.me/techinsider",
    configuration={
        "chat_identifier": "@techinsider",  # @username, t.me URL or numeric peer id
        "account_key": "default",  # non-secret registry key only
        "initial_backfill_limit": 50,
        "incremental_limit": 100,
        "edit_lookback_messages": 25,
    },
)
```

The adapter resolves `chat_identifier` via `get_chat` and caches the stable
numeric peer id on `Source.platform_external_id` (non-secret), so username
renames do not break the cursor. Only chats the account can already access
are read; the adapter **never** joins channels, imports invites or requests
access.

## Cursor semantics

`SourceCheckpoint(source, adapter="telegram")` stores
`state = {"last_message_id": N}` plus `last_external_id`/`last_published_at`.

- First run: fetch latest `initial_backfill_limit` messages, persist, then
  advance the checkpoint.
- Later runs: `get_chat_history(..., min_id=last_id, limit=incremental_limit)`
  plus a configurable `edit_lookback_messages` overlap window re-read for
  edits. Order is always **fetch -> persist -> advance**; a crash before the
  advance is safe because re-reads are idempotent (at-least-once + dedupe).
- Checkpoints advance only after persistence succeeded. Message loss is
  impossible by construction; duplicates are absorbed.

## Edits and revisions

Telegram edits keep the same message id, so `external_id` alone cannot
distinguish them. `FetchedItem.source_updated_at` carries `message.edit_date`
(UTC, `None` when never edited). On a same-`external_id` re-observation with
changed material fields and a newer (or previously unknown) edit timestamp,
persistence refreshes the live `SourceItem` row and appends an immutable
`SourceItemRevision` snapshot of the previous state. Unchanged re-fetches are
plain duplicates with no revision.

## Engagement snapshots

Initial capture happens at ingest time from the message fields:

| Telegram field | Snapshot column |
|---|---|
| `views` | `views` |
| `forwards` | `forwards` |
| reactions total | `reactions` (+ `raw_metrics.reaction_breakdown`) |
| discussion replies | `replies` (refresh path; counts only, never text) |
| shares / saves | always `NULL` (no independent Telegram metric; never copied from forwards) |

Refresh uses batched `client.get_messages(peer, message_ids=[...])` (a plain
read; view counters are **never** incremented by monitoring). Milestones come
from `SNAPSHOT_SCHEDULE_MIN` (default 10m/30m/60m/180m/360m/720m/1440m):

- Milestones are future-only: a 3-hour-old message at ingest gets an
  immediate current snapshot plus future 6h/12h/24h tracking, never faked
  10m/30m history.
- `EngagementSnapshot.target_age_seconds` identifies the milestone;
  `UNIQUE(source_item, target_age_seconds)` makes re-runs idempotent while
  `captured_at` records the real capture time.
- `EngagementTrackingState(source_item, next_due_at, next_target_age_seconds,
  active, ...)` lets a periodic scheduler batch due rows instead of creating
  thousands of 24h ETA Celery tasks.

## FloodWait and cooldown

Kurigram `FloodWait.seconds` maps to `RateLimitError(retry_after=...)`.
Celery retries with backoff+jitter; the owning `Source.cooldown_until` is set
so the scheduler skips the source/account during the server-imposed delay.
No rate-limit bypass is attempted anywhere.

## Dedicated worker

```yaml
telegram-worker:
  command: celery -A config.celery worker -l info -Q telegram -c 1
```

- `fetch_telegram_source_task` and `refresh_telegram_engagement_task` route
  to the `telegram` queue (`CELERY_TASK_ROUTES`).
- The web `worker` consumes only the default `celery` queue, so web and
  Telegram ingestion never share MTProto state.
- The Kurigram client manager owns one long-lived client per account inside
  the worker's stable event loop and rebuilds it on loop mismatch; explicit
  `disconnect()`/`reconnect()` exist for shutdown and tests.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `AuthenticationError` on every fetch | revoked/expired session string | re-issue `TELEGRAM_SESSION_STRING`, run `telegram_check_session` |
| `ChannelPrivate` / `PeerIdInvalid` | account not a member / typo | fix `chat_identifier`; never auto-join |
| Frequent `FloodWait` | interval too aggressive | raise `fetch_interval_seconds`, check `cooldown_until` in admin |
| Stale cursor after crash | normal: re-read + dedupe | no action; verify `SourceCheckpoint` advanced on next success |
| Missing old milestones | by design (no backfill of past buckets) | use immediate snapshot + future milestones |
