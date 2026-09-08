# Ranking & Selection (Phase 6)

`FinalScore = w_news * NewsValue + w_aud * AudienceFit + w_mom * Momentum + w_fresh * Freshness`

All scores are standardized to 0.0000..1.0000 (Decimal). NaN, Inf, and out-of-range values are strictly forbidden.

## 1. Source Baselines & Fallback Hierarchy

Baselines are stored in `SourceBaseline` per `(source, platform, topic, subtopic, content_type, age_bucket_minutes, metric)`.
Robust statistics (p25, p50, p75, p90, p95, p99) are used; mean is never the primary ranking baseline.

When sample count is low, `SourceBaselineService.find_baseline` follows a strict fallback hierarchy:
- Level 1: `source + topic + subtopic + content_type` (confidence ~0.95)
- Level 2: `source + topic + content_type` (confidence ~0.85)
- Level 3: `source + content_type` (confidence ~0.70)
- Level 4: `source only` (confidence ~0.55)
- Level 5: `platform global` (confidence ~0.35)

## 2. Eliminating Source Size Bias

Raw engagement (views, forwards, etc.) never directly ranks stories. An item with 5,000 views on a source whose median is 100 views reaches ~0.99 percentile, whereas 700,000 views on a source whose median is 1,000,000 views reaches <0.50 percentile.
Missing metrics remain NULL/None and are never converted to 0.0.

## 3. Score Families

- **NewsValue**: `importance`, `utility`, `impact`, `novelty`, `urgency`, `credibility`. Sourced from Phase 5 `StoryNewsValue` or deterministic structural fallback.
- **AudienceFit**: Editorial topic weight (`Topic.weight`), historical topic preference (`AudiencePreference`), content type preference, language fit.
- **Momentum**: `relative_performance` across members, velocity, acceleration, and saturating multi-source spread (`1.0 - exp(-0.7 * independent_sources)`). 100 copy-network items do not scale linearly.
- **Freshness**: Exponential half-life decay (`0.5 ** (age / half_life)`), default 24h.

## 4. Credibility Gate

Before selection, `CredibilityGate` stops:
- Credibility below 0.25 (`low_credibility`)
- Unresolved factual conflicts with confidence >= 0.60 (`unresolved_conflict`)
- Single low-trust source rumors with trust < 0.30 (`single_low_trust_rumor`)

## 5. Selection & Anti-Repeat

- Stories already published are skipped (`already_published_no_update`) unless a `MaterialUpdateDecision` marks a `CORRECTION` (boosted +0.10) or `MATERIAL_UPDATE`.
- `topic_saturation_penalty`: Penalizes stories if 2+ stories on the same primary topic were published within the saturation window (4 hours).
- `repetition_penalty`: Penalizes stories sharing >= 0.85 lexical similarity with recent publications.
- Decisions are logged in `DecisionLog` (type `what`, `algorithm_version="ranking-v1"`).

## 6. Backtesting

`python manage.py ranking_backtest --days 7 --limit 50` replays historical decisions and outputs audit tables without performing any live publishing.
