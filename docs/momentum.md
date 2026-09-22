# Story Momentum & Real-Time Trend Detection Engine

## Overview

NewsAI tracks the velocity, acceleration, and cross-source propagation of stories in real time, moving beyond simple static view counting to understand the trajectory and momentum of unfolding events.

```text
Source Item & Engagement Snapshots
  ↓
ItemMetricSeriesService (delta / time, baseline normalization, EWMA smoothing)
  ↓
StoryMomentumService (multi-source fusion, arrival rates, propagation breadth)
  ↓
TrendDetector (NORMAL, EARLY_SIGNAL, RISING, SURGING, BREAKING, SATURATED, COOLING)
  ↓
LifecycleStateMachine (DISCOVERED -> WATCHING -> RISING -> BREAKING -> PEAKING -> COOLING -> STALE)
  ↓
Adaptive Observation Scheduler (60s breaking, 180s rising, 300s early, 600s watching)
  ↓
Fast-Path Dispatcher (immediate rescore & ranking on breaking/spikes)
```

## 1. Velocity & Acceleration Calculation

### Metric Rates of Change
For any consecutive engagement snapshots $s_{t-1}$ and $s_t$:
$$\Delta t = \frac{t - (t-1)}{3600}$$
$$\text{velocity} = \frac{\max(0, \text{metric}_t - \text{metric}_{t-1})}{\Delta t}$$

Counter resets or corrections ($\Delta < 0$) are recorded as measurement anomalies rather than negative growth.

### Source-Relative Normalization
Raw velocity is normalized against the source's historical median ($p_{50}$) and percentiles via `SourceBaselineService`:
$$\text{normalized\_velocity} \in [0.0000, 1.0000]$$

A small channel gaining 500 views/hour with an expected median of 50 views scores $\sim 0.98$, while a massive outlet gaining 5,000 views with an expected median of 50,000 views scores $< 0.40$.

### EWMA Smoothing & Acceleration
To eliminate false alarms caused by noisy one-off spikes, an Exponentially Weighted Moving Average (EWMA) is applied:
$$v_{\text{ewma}, t} = \alpha \cdot v_t + (1 - \alpha) \cdot v_{\text{ewma}, t-1}$$
where $\alpha = 0.30$ by default (configurable via `DynamicSetting(MOMENTUM_EWMA_ALPHA)`).

Acceleration measures whether normalized velocity is speeding up or slowing down:
$$\text{acceleration} = \frac{v_t - v_{t-1}}{\Delta t} \in [-2.0, +2.0]$$

## 2. Cross-Source Propagation & Copy-Network Protection

Raw propagation is differentiated from verified independent confirmation:
- **`observed_sources_count`**: Total number of distinct sources publishing on this Story.
- **`independent_sources_count`**: Number of distinct sources with `independent` status (syndicated or verbatim copy members are excluded).
- **`propagation_breadth`**: Saturating confirmation curve:
  $$P_{\text{breadth}} = 1.0 - e^{-0.7 \cdot \text{independent\_count}}$$
  20 copycat channels copying one source produce $P_{\text{breadth}} = 0.50$ ($N=1$), never inflating confirmation artificially.

## 3. Composite Momentum Score

Composite momentum is normalized to $[0.0000, 1.0000]$ with audit-friendly, DB-overridable component weights:
- Normalized velocity: 25%
- Acceleration component: 20%
- Propagation breadth: 20%
- Independent arrival rate: 15%
- Growth persistence: 10%
- Freshness decay: 10%

Confidence is derived from snapshot count, independent source diversity, and baseline sample size.

## 4. Trend States & Hysteresis

The `TrendDetector` applies directional hysteresis to prevent flip-flopping across boundaries:
- **`BREAKING`**: Enter when momentum $\ge 0.85$ (or acceleration $\ge 0.40$ with $N \ge 3$); exit only when momentum $< 0.75$.
- **`SURGING`**: Enter when momentum $\ge 0.75$ and acceleration $> 0.05$; exit when momentum $< 0.65$.
- **`RISING`**: Enter when momentum $\ge 0.60$; exit when momentum $< 0.50$.
- **`EARLY_SIGNAL`**: Acceleration $\ge 0.15$ with $\ge 2$ independent confirmations on small stories ($\le 5$ sources).
- **`SATURATED`**: High source count ($\ge 8$) with significant deceleration ($\text{acc} \le -0.15$).
- **`COOLING`**: Decelerating with momentum decaying below $0.45$.

## 5. Story Lifecycle State Machine

States: `DISCOVERED`, `WATCHING`, `RISING`, `BREAKING`, `PEAKING`, `COOLING`, `STALE`, `ARCHIVED`.

Transition Rules:
- Early signal or low confirmation keeps a story in `WATCHING`.
- Rising trend transitions `WATCHING` $\to$ `RISING`.
- Breaking trend transitions to `BREAKING`.
- Deceleration from breaking/rising moves to `PEAKING`, then `COOLING`.
- Prolonged inactivity ($\Delta t > 12\text{h}$) moves to `STALE`, then `ARCHIVED`.
- A verified `MATERIAL_UPDATE` reactivates `COOLING`/`STALE`/`ARCHIVED` stories back to `RISING`.

## 6. Adaptive Polling & Fast-Path

Instead of polling all stories at high frequency, the `StoryObservationState` dynamically adjusts polling cadence:
- `BREAKING`: every 60 seconds
- `RISING` / `SURGING`: every 180 seconds (3 minutes)
- `EARLY_SIGNAL`: every 300 seconds (5 minutes)
- `WATCHING` / `PEAKING`: every 600 seconds (10 minutes)
- `COOLING`: every 1800 seconds (30 minutes)
- `STALE`: every 3600 seconds (60 minutes)

When a threshold crossing or acceleration breakout is detected, `StoryReanalysisCoordinator` triggers `immediate_rescore_task` to run the editorial policy immediately without waiting for hourly cron ranking.
