# Learning ladder

## Phase A (ship first): rolling stats, percentiles, EWMA, Bayesian smoothing

- Baselines per source×platform×topic×subtopic×content-type×age-bucket. Median/p90, never mean.
- AudienceFit from own-channel snapshot history with Bayes smoothing for cold topics.
- All debug-visible: feature_snapshot stored per decision.

## Phase B: predictive expected-performance model

- Trigger: >1k own-channel snapshots with rewards. Regression on context → expected relative performance.

## Phase C: contextual bandit (VW)

- `vowpalwabbit==9.11.2`, `--cb_explore_adf`. Context: topic/time/story-value. Actions: publish/skip/style/timing. Reward: relative channel performance.
- Alternatives if VW unwanted: mabwiser, contextualbandits. No hand-rolled bandit.

## Exploration

Epsilon-greedy, `EXPLORATION_RATE` (default 0.05), logged in DecisionLog. Covers topic/style/time.
