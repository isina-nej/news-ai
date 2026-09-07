# ADR Phase 1 — Data model decisions

Date: 2026-09-07. Status: accepted.

## 1. Publication idempotency without blocking material updates

Rejected: `UNIQUE(story, channel)`. It would forbid legitimate updates.

Chosen triple-guard:

1. `idempotency_key` UNIQUE — one row per publish intent. Retries reuse the
   row (`FAILED -> PUBLISHING`), never insert.
2. `UNIQUE(story, channel, content_hash)` — identical rendered content for the
   same story+channel rejected at model `full_clean` AND DB level
   (race-proof). `content_hash` NULL while draft has no content; NULLs stay
   distinct under UNIQUE on MySQL/SQLite, so empty drafts never collide.
3. `UNIQUE(story, channel, publication_version)` + `UpdateType` rules —
   material updates are new rows with bumped version, new hash,
   `update_type != INITIAL`.

State machine: DRAFT -> READY -> APPROVED -> SCHEDULED/PUBLISHING ->
PUBLISHED, with FAILED retry loop and CANCELLED sinks. PUBLISHED/CANCELLED
terminal (no silent re-publish).

## 2. Scores normalized 0..1, not 0..100

Chosen: every score, probability, confidence stored 0..1 `Decimal(5,4)`,
enforced by `UNIT_INTERVAL` validators + `save()` calling `full_clean`.
Raw counts stay integer columns; percentiles `Float`. Percent display lives
only in presentation layer. `algorithm_version` mandatory on ScoreRecord
and DecisionLog; breakdown shape enforced (`news_value/audience_fit/momentum`).

## 3. Baseline storage: narrow metric-rows, hash uniqueness

Tradeoff: wide table (one row per context, columns per metric) needs a
migration per new metric. Narrow rows (one row per context x metric) keep the
schema stable at the cost of one indexed lookup per metric.

MySQL treats NULLs as distinct inside unique indexes, so a multi-column
`UniqueConstraint` over nullable (source, topic, subtopic) would allow silent
duplicate baselines. Fix: `context_hash` (sha256 of canonical context parts)
with single-column UNIQUE. Same pattern reused for `AudiencePreference`
(feature + canonical-context hash) so new learning features need no migration.

## 4. Own-channel feedback: separate table, no GenericForeignKey

`PublicationEngagementSnapshot` duplicates a few columns from
`EngagementSnapshot` deliberately. Typed FKs preserve referential integrity
and indexable queries; the two metric sets evolve independently. GFK
convenience rejected.

## 5. Cross-source duplicates kept; independent count is a service concern

Only `UNIQUE(source, external_id)` (NULLs distinct) dedupes. Same event from
N sources = N `SourceItem` rows linked to one `Story` via `StoryMembership`
(method, similarity, `is_primary`, evidence `detail`). `Story` keeps
`independent_source_count` denormalized + `metadata` for the future
copy-network vs independent split — the model never blocks that service.

## 6. Unknown vs zero engagement preserved

All snapshot metric columns nullable; NULL = platform did not expose the
metric. Default `None`, never `0`. Same for style fields on Publication.

## 7. Verification notes

- `manage.py check`: clean. `pytest`: 17 passed. `ruff check` + `format`: clean.
- Migrations applied on real MySQL 8.4 (throwaway `newsai-mysql-test`
  container, `newsai_verify` db): all 17 tables InnoDB/utf8mb4; smoke test
  confirmed same-source dup blocked, null-external rows allowed, content dup
  blocked.
- `pyright` (no django-stubs): 197 advisory-only false positives on standard
  Django idioms (TextChoices tuples, field defaults, `*_id`, `objects`).
  Recorded in pyproject; revisit with django-stubs later. Not a gate.
