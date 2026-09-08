# Phase 9 Audience Learning, Scheduler & Observability Decision

## Context
A fixed editorial policy risks becoming stale or trapped in a topic echo chamber. An adaptive learning system must adjust to what audiences respond to over time while strictly respecting safety guardrails (credibility gates, rate limits, anti-spam).

## Decision
1. **Bayesian Smoothing & EWMA**: Preference updates use EWMA (`alpha = 0.15`) with Bayesian shrinkage against a neutral prior (`0.50`, prior weight = 5). A single runaway post cannot distort future recommendations.
2. **Confounding Separation**: Features are learned independently (`feature='topic'`, `feature='style'`, `feature='content_type'`, `feature='hour'`). Viral breaking news does not falsely inflate bullet or emoji styles.
3. **Contextual Bandit with Strict Guardrails**: `ContextualBanditService` implements epsilon-greedy exploration (`EXPLORATION_RATE = 0.05` default). The bandit is only allowed to select among pre-screened safe actions. It can NEVER bypass the `CredibilityGate`, rate limits, or anti-repeat rules.
4. **Celery Beat Pipeline Orchestration**: Periodic tasks wired in `CELERY_BEAT_SCHEDULE`:
   - `dispatch_due_sources_task` (2 min): checks cooldown and intervals
   - `dispatch_pending_clustering_task` (2 min): batches unclustered items
   - `dispatch_ranking_refresh_task` (1 hr): re-evaluates active stories
   - `dispatch_publication_queue_task` (10 min): gated by `ENABLE_AUTO_PUBLISH`
5. **Observability & Health Checks**: `MetricsRegistry` captures end-to-end metrics (success rates, lag, decision counts) and structured logs include correlation IDs (`correlation_id`, `job_id`, `story_id`, `fetch_run_id`).
