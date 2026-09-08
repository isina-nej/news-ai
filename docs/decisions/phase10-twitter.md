# Phase 10 Twitter/X Ingestion Decision

## Context
Twitter/X ingestion must not use the official API (high cost / restrictive limits) nor fragile unmaintained scraping tools (e.g. `twikit`, which broke on X's 2026 chunk hashing). It must also avoid risky CAPTCHA/challenge-bypass automation that risks legal/ToS escalation.

## Decision
1. **Injected Session Client Architecture**: `TwitterSourceAdapter` depends on a `TwitterSessionClient` protocol rather than raw scraping code. Sessions are provided through environment (`TWITTER_SESSION`) and never stored in the database or logs.
2. **Standard FetchedItem Contract**: Implements the identical ingestion pipeline contract:
   - Tweet ID -> `external_id`
   - Tweet text -> `raw_text` and `normalized_text`
   - Metrics -> `views`, `forwards` (reposts), `reactions` (likes), `replies`, `saves` (bookmarks). Missing metrics stay `None` (never `0`).
3. **Retweet vs Quote Semantics**:
   - Retweets carry observational engagement signal but are flagged `independent_confirmation=False` so they cannot inflate independent source counts.
   - Quote tweets carry new commentary and are ingested as distinct `SourceItem` rows with referenced tweet metadata preserved.
4. **Crash-Safe Checkpoint**: Ingestion progress is committed to `SourceCheckpoint(adapter="twitter")` with `last_external_id` and cursor state only after database persistence succeeds (at-least-once guarantee).
5. **Session Safety & Fast Failure**: If session credentials expire or encounter rate limits, the adapter raises `AuthenticationError` or `RateLimitError` and sets `cooldown_until` on the source, preventing retry storms. No challenge-bypass automation is used.
6. **Feature Flag**: Governed by `ENABLE_TWITTER_SOURCE` (default `False`).
