# Architecture (rev 5 — Phase 3 Telegram Ingestion)

Dependency: `External → Adapter → Service → Domain/Core`.

## Sources (`apps/sources`)

`Source` profile: name, platform, url/identifier, enabled, priority, reliability, category, language, fetch_interval, tags, last_success_at, last_failure_at, consecutive_failures, trust_score, `platform_external_id` (stable peer id cache), `cooldown_until` (FloodWait backoff).
`FetchRun`: audit record per fetch execution (source FK, started_at, finished_at, status, http_status, fetched_count, created_count, `updated_count`, duplicate_count, rejected_count, error_type, error_message, duration_ms, correlation_id).
`SourceCheckpoint(source, adapter)`: persistent cursor (`last_external_id`, `last_published_at`, `state.last_message_id`); advances only after persistence.
`EngagementTrackingState`: lean per-item milestone scheduler (`next_due_at`, `next_target_age_seconds`, `active`).

### Ingestion Pipeline (Phase 2)
```text
Source (Beat / manual dispatch)
  ↓
Celery Task (fetch_source_task) with bounded exponential backoff & jitter
  ↓
SourceFetchService (Redis distributed lock: fetch:source:{id})
  ↓
Adapter Registry (resolves by platform / adapter_type)
  ├── RSSSourceAdapter (feedparser, UTC datetime parsing, enclosures)
  ├── RSSHubAdapter (route construction, env ACCESS_KEY, self-host allowlist)
  ├── HTMLSourceAdapter (SafeHttpClient, trafilatura article extraction, metadata)
  └── TelegramSourceAdapter (Kurigram user session; see docs/telegram-ingestion.md)
  ↓
SafeHttpClient (connection pool, timeouts, redirects, size guard, SSRF validation)
  ↓
FetchedItem DTOs (pure Python, ORM-agnostic)
  ↓
CanonicalURLService (scheme/host casing, fragment strip, tracking params strip, sort query, url_hash)
  ↓
NormalizationService (raw_text untouched, normalized_text v1, Unicode NFKC, Persian/Arabic safe, whitespace)
  ↓
IngestionPersistenceService (same-source exact dedupe: external_id -> url_hash -> content_hash; cross-source preserved; payload size guard)
  ↓
SourceItem & Source health update (last_success_at, consecutive_failures reset/increment)
```

## News (`apps/news`)

`SourceItem` never deleted cross-source. Dedupe for same source: `external_id` -> `url_hash` -> `content_hash`.
Fields: source FK, platform, external_id, url, url_hash, title, raw_text, normalized_text, `raw_content_hash`, `content_hash` (normalized SHA-256), media, language, content_type, topic/subtopic (cross-field validated), published_at, `source_updated_at` (platform edit time), `source_deleted_at` (platform-confirmed only), collected_at, first_seen_at, updated_at, story FK (current assignment, must match single `is_current` membership).
`SourceItemRevision`: immutable edit history (`revision_number`, `source_updated_at`, `observed_at`, material snapshot + `change_metadata`).
`EngagementSnapshot`: item FK, views, forwards, shares, reactions, replies, saves nullable (unknown vs zero preserved), `target_age_seconds` milestone (`UNIQUE(source_item, target_age_seconds)`), captured_at, post_age_seconds.

## Stories (`apps/stories`)

`Story`: canonical title/summary, topic/subtopic (validated), first_published_at, `independent_source_count` (denormalized), `latest_source_update_at` service-controlled (no auto_now), updated_at for DB churn.
`StoryMembership`: each SourceItem has at most one `is_current` assignment (MySQL-safe `current_slot` unique). Multi-membership allowed for candidates. Fields: similarity_score, match_method, is_primary, is_current, current_slot, detail.

## Ranking (`apps/ranking` + `apps/ops`)

Raw engagement never ranked directly. `SourceBaselineService`: baseline per source×platform×topic×subtopic×content-type×age-bucket. Median/percentiles, never plain mean. Metrics from `apps/ranking/metrics.py` registry (raw + derived: `*_velocity`, `engagement_acceleration`, `relative_performance`). Baselines validated against registry + topic/subtopic consistency; uniqueness via `context_hash`.
Story features, 3 groups: NewsValue, AudienceFit, Momentum.
`ScoreRecord`: story FK, algorithm_version, breakdown JSON (every numeric leaf 0..1 finite), final. DB `CheckConstraint`s on 0..1.
`DecisionLog`: story FK, publication FK, decision_type (WHAT/WHEN/HOW), feature_snapshot, score_breakdown, predicted_reward, selected_action, is_exploration, exploration_probability, actual_reward, reward_calculated_at.

## Publishing (`apps/publishing`)

WHAT/WHEN/HOW independent. `Publication`: story FK, channel, status machine (DRAFT→PUBLISHED, FAILED retry), `content_hash` fingerprint of final payload (headline+content+style fields, always recomputed, NULL only for empty drafts under UNIQUE), scheduled_at, published_at, external_message_id, idempotency_key. `transition()` enforces that fingerprint fields are not mutated without saving first. `PublicationEngagementSnapshot`: separate table for own-channel feedback, no GenericForeignKey.

## Security

- SSRF protection on all web fetches (`apps/sources/adapters/ssrf.py`): loopback, private, link-local, cloud metadata blocked, redirects re-validated.
- Response size limits (5MB HTTP, 64KB DB raw_payload).
- Telegram sessions env-only; secrets never logged or committed. Dedicated `telegram` Celery queue (`-c 1` per account), long-lived loop-bound Kurigram client, FloodWait -> `RateLimitError` + `Source.cooldown_until`. See `docs/telegram-ingestion.md`.
