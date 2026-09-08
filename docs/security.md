# Security

## Secrets

Env-only, never git/logs. RedactFilter on logs. TELEGRAM_API_ID/HASH/BOT_TOKEN from env.

## Telegram sessions (single source of truth)

`TELEGRAM_API_ID` / `TELEGRAM_API_HASH` / `TELEGRAM_SESSION_STRING` in env only.
No DB column. The encrypted-at-rest DB column for multi-account rotation is
explicitly deferred until key-rotation is designed (see
`docs/decisions/phase1-datamodel.md`). `apps/ops/models.py` docstring states the
same; if they diverge, ops/models.py wins.

Session logic isolated behind `TelegramSourceAdapter` (+ `telegram_client.py` /
`telegram_accounts.py` / `telegram_serializers.py`); nothing else imports
Kurigram. Expired/revoked sessions surface as `AuthenticationError`, never an
interactive login inside workers. `telegram_check_session` prints identity only.
`Source.configuration` / `raw_payload` / `FetchRun` / logs never carry the
session, api_hash, access_hash or file_reference (central redaction + explicit
serializer allowlist).

## Other

- Publication idempotency via `idempotency_key` + `UNIQUE(story,channel,content_hash)` +
  `UNIQUE(story,channel,version)`.
- CheckConstraints back DB validators so bulk UPDATE paths cannot break 0..1.
