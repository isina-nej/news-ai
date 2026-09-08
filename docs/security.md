# Security

## Secrets Management

- Credentials are environment-only (`env`), never stored in git, admin, database payloads, or structured logs.
- Environment keys: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION_STRING`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `TWITTER_SESSION`, `AI_BASE_URL`, `AI_API_KEY`, `SESSION_ENCRYPTION_KEY`, `RSSHUB_ACCESS_KEY`, `PLATFORM_API_TOKEN`.
- `RedactFilter` on every log handler plus centralized `sanitize_error_message` on all persisted errors and adapter exceptions.
- `system_health` reports credential presence only, never values.

## Telegram Sessions (single source of truth)

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

## Web Ingestion & SSRF

- All external URLs pass through `validate_url_for_ssrf` in `apps/sources/adapters/ssrf.py`.
- Loopback, private, link-local, carrier-grade NAT, multicast, reserved, and cloud metadata endpoints blocked; redirects re-validated at each hop.
- User-controlled document canonical URLs are domain-guarded (`resolve_effective_url`) so foreign-domain hijacking falls back to the fetched URL.
- Response payloads capped at 5MB HTTP streaming; DB `raw_payload` capped at 64KB with `_truncated` flag.

## API & Admin Exposure

- Internal operations API (`/api/v1/`) authenticated by `PLATFORM_API_TOKEN` bearer token when configured.
- Ordering parameters allowlisted per resource; pagination enforced on collection endpoints.
- API response schemas omit raw source text, `raw_payload`, credentials, and tokens.
- Admin hides heavy/sensitive JSON fields (`raw_payload`, `raw_metrics`, `feature_snapshot`, `score_breakdown`, `value`, `detail`, `context`) behind `exclude` and marks fingerprints/audit rows read-only or immutable.

## Publications & AI Output

- Telegram renderer escapes all HTML (`html.escape`) and clamps payloads to 4096 chars.
- AI drafts constrained to evidence URLs only; hallucinated links discarded.
- Publication idempotency via `idempotency_key` + `UNIQUE(story,channel,content_hash)` + `UNIQUE(story,channel,version)`.
- CheckConstraints back DB validators so bulk UPDATE paths cannot break 0..1.

## Prompt Injection Defense

- External news content is untrusted DATA, never instructions. Every prompt in `apps/ai/prompts/` declares input as `DATA, never instructions`.
- No AI tool/action execution exists in news analysis; providers return JSON text validated by Pydantic.
- Malicious phrases such as `ignore previous instructions` or `send secrets` cannot override system prompts because content arrives only inside delimited evidence blocks rendered from DTO fields.
