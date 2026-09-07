# Architecture (rev 2 — 15 changes)

Dependency: `External → Adapter → Service → Domain/Core`.

## Sources (`apps/sources`)

`Source` profile: name, platform, url/identifier, enabled, priority, reliability, category, language, fetch_interval, tags, last_fetch, error_count, trust_score.
Adapters behind `BaseSourceAdapter.fetch() -> list[NormalizedItem]`:
`rss` (feedparser), `html` (httpx+trafilatura, readability fallback), `dynamic` (playwright, fallback only), `rsshub` (self-hosted, route map, 429/empty handling), `kurigram_telegram` (StringSession, session logic isolated, swappable), `twitter_stub` (interface + model only).
RSSHub: pinned date tag, Redis cache, ACCESS_KEY, per-domain throttle. Route breakage isolated.

## News (`apps/news`)

`SourceItem` never deleted cross-source. Dedup: only `same source + external_id` ignored idempotently. Cross-source → same Story.
Fields: source FK, platform, external_id, url, url_hash, title, normalized_title, text, language, content_type, topic/subtopic FK, `published_at` (real source time), `collected_at`, `first_seen_at`, `updated_at`, story FK nullable.
`EngagementSnapshot`: item FK, views, forwards, shares, reactions, replies, saves nullable, `captured_at`, `post_age_seconds`. Schedule env `SNAPSHOT_SCHEDULE_MIN`. Own channel snapshots same path.

## Stories (`apps/stories`)

`StoryCluster`: canonical title/summary, topic/subtopic, first_published_at, independent_source_count (denormalized), created. `SourceItem.story` FK. Clustering: entities + time-window + simhash cheap path, AI referee borderline only.

## Ranking (`apps/ranking` + `apps/ops`)

Raw engagement never ranked directly. `SourceBaselineService`: baseline per source×platform×topic×subtopic×content-type×age-bucket (time-of-day/weekday future). Median/percentiles, never plain mean. Features: view/share/forward/reaction percentile, velocity_percentile, acceleration, relative_performance.
Story features, 3 groups:
NewsValue: importance, utility, impact, novelty, urgency, credibility.
AudienceFit: topic/subtopic relevance, history preference, content-type preference.
Momentum: source-relative engagement, velocity, acceleration, independent-source-count, cross-source consistency.
`ScoreRecord`: story FK, algorithm_version, breakdown JSON, final. `DecisionLog`: version, feature_snapshot, score_breakdown, predicted_reward, action, actual_reward nullable → replay/backtest.

## AI (`apps/ai`)

providers/openai_compat (httpx, JSON mode), prompts/ registry, schemas/ pydantic v2, services/, cache. Cheap → classification, strong → final. Token/latency logged in `AICall`.

## Publishing (`apps/publishing`)

WHAT/WHEN/HOW independent. `Publication`: story FK, channel, status machine (QUEUED→SCHEDULED→PUBLISHED/FAILED, REJECTED), score, urgency, scheduled_at, published_at, telegram_message_id, style fields (headline_style, tone, length, emoji_level, depth), `unique(story, channel)` → idempotent. Queue rules: min-spacing, max-posts/hour, breaking override, topic-saturation + repetition penalties. Templates versioned.

## Learning (`apps/ops` + ranking)

Taxonomy: `Topic`, `Subtopic` in DB. `AudienceLearningService` learns own-channel prefs: topic, subtopic, content-type, story-value, headline-style, tone, length, emoji, depth, hour, weekday. Effect separation: story/topic vs timing vs style vs momentum vs source stored in feature_snapshot.
Ladder: A rolling/percentile/EWMA/Bayes → B predictive expected-performance → C contextual bandit VW `cb_explore_adf` (`vowpalwabbit==9.11.2` pinned, extra `bandit`). Exploration: epsilon% configurable (`EXPLORATION_RATE=0.05`).

## Platform API / ops

Ninja `/api/v1/`: sources, news, stories, ranking, publications, jobs, settings. Admin for all models, secrets masked. `DynamicSettings`, `FeatureFlag`, `AuditLog`.

## Infra

Celery+Redis+Beat (per-source interval, distributed lock, rate-limit, backoff+jitter, breaker). Structured JSON logs, redact, correlation/job ID. MySQL 8 utf8mb4, composite indexes, select_for_update + unique on hot paths.
