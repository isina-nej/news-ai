# Phase 6 Ranking and Selection Decision

## Context
Raw view counts bias rankings towards mega-channels, irrespective of whether an item was exceptional or routine. Furthermore, copy-networks republishing the same event across dozens of satellite channels should not artificially boost a story's importance 100-fold.

## Decision
1. **Source-Relative Percentiles**: All engagement metrics are evaluated against the source's own historical baselines (`SourceBaseline`), eliminating source-size bias. An unexpected 5,000 views on a small blog outranks 700,000 expected views on a mega-publisher.
2. **Robust Statistics**: Baselines store p25, p50 (median), p75, p90, p95, p99. Mean is not used.
3. **Fallback Hierarchy**: Scarcity of observations falls back gracefully: `source+topic+subtopic+type` -> `source+topic+type` -> `source+type` -> `source` -> `platform global`.
4. **Saturating Spread Function**: Multi-source confirmation uses `1.0 - exp(-0.7 * independent_sources)` so copy cascades saturate rather than inflating rankings linearly.
5. **Credibility Gate**: Low credibility (<0.25), unresolved conflicts, and single low-trust rumors are held by `CredibilityGate` regardless of raw momentum.
6. **Anti-Repeat & Material Updates**: Already-published stories are skipped unless new evidence qualifies as `CORRECTION` (boosted +0.10) or `MATERIAL_UPDATE`.
7. **Backtesting**: `ranking_backtest` management command enables offline decision replay without external publishing calls.
