# Security

## Secrets

Env-only, never git/logs. RedactFilter on logs. TELEGRAM_API_ID/HASH/BOT_TOKEN from env.

## Telegram sessions (single source of truth)

Phase 1: `TELEGRAM_SESSION_STRING` in env only. No DB column. The encrypted-at-rest
DB column for multi-account rotation is explicitly deferred until key-rotation is
designed (see `docs/decisions/phase1-datamodel.md`). `apps/ops/models.py` docstring
states the same; if they diverge, ops/models.py wins.

Masked in admin, session logic isolated behind platform adapter, distinct
`AuthenticationExpiredError` on expiry, no silent credential swap.

## Other

- Publication idempotency via `idempotency_key` + `UNIQUE(story,channel,content_hash)` +
  `UNIQUE(story,channel,version)`.
- CheckConstraints back DB validators so bulk UPDATE paths cannot break 0..1.
