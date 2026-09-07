# NewsAI — AI-Powered News Intelligence & Publishing Platform

Production-grade, modular Django platform: collect → normalize → cluster → analyze → rank → publish (Telegram first).

## Architecture (revised, 15 changes applied)

`External → Adapter → Service → Domain/Core`. Core knows nothing about Telegram/X/Web.

- `sources/`: `Source` profile + adapters `rss, html, dynamic(playwright fallback), rsshub, kurigram_telegram, twitter_stub`. Kurigram fully swappable behind `TelegramSourceAdapter`. Twitter stub only in phase 1.
- `news/`: `SourceItem` (never deleted cross-source; only `same source + external_id` ignored idempotently) with `published_at` (real source time), `collected_at`, `first_seen_at`, `updated_at`. `EngagementSnapshot` per item: views/forwards/shares/reactions/replies/saves + `captured_at` + `post_age_seconds`. Schedule configurable (`SNAPSHOT_SCHEDULE_MIN=10,30,60,180,360,720,1440`).
- `stories/`: `StoryCluster` groups all SourceItems of one event. Cross-source count is a ranking feature.
- `ranking/`: `SourceBaselineService` (median/percentile per source×platform×topic×subtopic×content-type×age-bucket; time-of-day/weekday future). Story features in 3 groups: NewsValue (importance/utility/impact/novelty/urgency/credibility), AudienceFit (topic/subtopic/history/content-type preference), Momentum (source-relative engagement, velocity, acceleration, independent-source-count, cross-source consistency).
- `ops/`: `Topic/Subtopic` taxonomy in DB, `DecisionLog` (algorithm_version, feature_snapshot, score_breakdown, predicted_reward, action, actual_reward) for replay/backtesting.
- `publishing/`: WHAT/WHEN/HOW separate decisions. Queue: score, urgency, min-spacing, max-posts/hour, breaking override, topic-saturation + repetition penalties.
- `ai/`: provider-agnostic, prompt registry, Pydantic validation, cache, cheap/strong models.
- Own channel learned like external sources: `AudienceLearningService` (topic, subtopic, content-type, story-value, headline-style, tone, length, emoji, depth, hour, weekday). Topic vs style vs timing vs momentum vs source effects separated.
- Learning ladder: Phase A rolling stats/percentile/EWMA/Bayes → Phase B predictive model → Phase C contextual bandit (VowpalWabbit `cb_explore_adf`, `vowpalwabbit==9.11.2`). Exploration rate configurable (`EXPLORATION_RATE`).

## Setup

```bash
cp .env.example .env
docker compose up --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
```

Local (sqlite, eager celery): `DJANGO_SETTINGS_MODULE=config.settings.local pytest`.

## Env vars

See `.env.example`. Secrets never in git/logs. Session encrypted (`SESSION_ENCRYPTION_KEY`).

## Docs

- `docs/architecture.md`, `docs/development.md`, `docs/deployment.md`
- `docs/sources.md`, `docs/ai.md`, `docs/ranking.md`, `docs/security.md`, `docs/learning.md`, `docs/publishing.md`
- `docs/decisions/*`: kurigram, rsshub, vowpalwabbit
