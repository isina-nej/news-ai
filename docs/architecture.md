# Architecture: Real-Time Newsroom Intelligence & Professional Telegram Publishing

Dependency: `External Platform → Adapter → Application Service → Domain / Core`.

## End-to-End System Flow

```text
SOURCE STREAM (RSS, RSSHub, Web HTML, Telegram, Twitter/X)
  ↓
INGEST & PERSIST (Same-source dedupe, cross-source preserved, revisions, milestones)
  ↓
STORY CLUSTER (URL evidence, MinHash, Qdrant vectors, centroid representation)
  ↓
MOMENTUM OBSERVER (ItemMetricSeriesService, StoryMomentumService)
  ├── Velocity (source-relative, normalized, EWMA-smoothed)
  ├── Acceleration (change in normalized velocity over time)
  ├── Arrival Rates (5m, 15m, 30m windows)
  └── Propagation Breadth (saturating independent curve)
  ↓
TREND & LIFECYCLE ENGINE (TrendDetector, LifecycleStateMachine)
  ├── States: DISCOVERED -> WATCHING -> RISING -> BREAKING -> PEAKING -> COOLING -> STALE -> ARCHIVED
  ├── Early Signal Detection (breakout candidates before peak)
  ├── Adaptive Observation Intervals (60s to 1h polling schedules)
  └── Fast-Path Rescoring (immediate Celery ranking on acceleration/breaking spikes)
  ↓
AI STORY INTELLIGENCE (StoryIntelligenceSnapshot)
  ├── Fact extraction & Information Units
  ├── Conflicting claims & uncertainty preservation
  └── Semantic evaluations (importance, impact, novelty, credibility, editorial risk)
  ↓
NEWSWORTHINESS SCORE (NewsworthinessService: intrinsic value 0..1)
  ↓
EDITORIAL POLICY ENGINE (EditorialPolicyEngine)
  ├── Channel Novelty (NEW_STORY, MATERIAL_UPDATE, CORRECTION, MINOR_UPDATE, REPEAT)
  ├── Fact-Level Diff (FactDiffService: new, changed, repeated, contradicted)
  ├── Penalties (Topic saturation, lexical repetition, credibility risk)
  └── Binding Decisions:
        ├── PUBLISH_NOW (breaking override or priority cleared)
        ├── WATCH (promising trend awaiting independent confirmation)
        ├── SCHEDULE (queued for next available slot subject to spacing)
        ├── UPDATE_EXISTING_STORY (material update / correction)
        ├── SKIP (below threshold or duplicate content)
        └── REJECT (failed credibility gate / low-trust rumors)
  ↓
MEDIA PIPELINE (MediaAsset, MediaValidationService, MediaSelectionService)
  ├── SSRF-safe validation & MIME/dimension checks
  └── High-res 16:9 scoring with thumbnail & broken image penalties
  ↓
STRUCTURED DRAFT GENERATION (draft_generation_v2)
  ├── 3 Headline Candidates (DIRECT, BREAKING, CONTEXTUAL)
  ├── HeadlineEvaluator (clickbait filter, format alignment)
  └── DraftCriticService (unsupported claim check, Persian cleanup, filler removal)
  ↓
TELEGRAM-NATIVE RENDERER (TelegramRenderer)
  ├── Strict HTML escaping & whitelist tags (<b>, <i>, <u>, <blockquote>, <a>)
  ├── Format variants (BREAKING, STANDARD, QUICK_UPDATE, DEVELOPING, ANALYSIS)
  ├── Semantic shortening (preserves headline, lead, source, signature before truncation)
  └── Character limits (message <= 4096, caption <= 1024)
  ↓
IDEMPOTENT PUBLISHER (TelegramBotPublisher, FakePublisher)
  ├── sendPhoto + caption delivery
  ├── Long caption splitting (photo + short caption followed by full message)
  ├── Automatic text fallback on image errors
  └── Telegram file_id caching for media reuse
  ↓
POST-PUBLICATION MEASUREMENT (PublicationEngagementSnapshot)
  ↓
AUDIENCE LEARNING & REWARD (AudienceLearningService, normalized_reward)
  ├── Reward decomposition (views, forwards, reactions, replies)
  ├── Quality guardrails (low credibility caps reward to prevent clickbait hacking)
  └── Bayesian-smoothed EWMA feature preferences (topic, style, timing)
```

## Subsystems

1. **`apps/sources`**: Acquisition adapters (RSS, RSSHub, Web, Telegram Kurigram, Twitter/X), connection pools, distributed locks, SSRF protection.
2. **`apps/news`**: Canonical source item store, exact dedupe, revisions, milestones, `MediaAsset` storage and validation.
3. **`apps/stories`**: Story event clustering, MinHash/vector centroids, `StoryObservationState`, `StoryMomentumSnapshot`, `TrendDetector`, `LifecycleStateMachine`, `StoryReanalysisCoordinator`.
4. **`apps/ai`**: Provider port (`FakeAIProvider`, `OpenAICompatibleProvider`), versioned prompt registry (`apps/ai/prompts/`), Pydantic schemas, `StoryIntelligenceSnapshot`, fact diffing.
5. **`apps/ranking`**: Robust source baselines, `NewsworthinessService`, `PublishPriorityService`, `EditorialPolicyEngine`, `ChannelNoveltyService`, `PublicationSelectionRun`, `ScoreRecord`, `DecisionLog`.
6. **`apps/publishing`**: `HeadlineEvaluator`, `DraftCriticService`, `SourceAttributionService`, `BrandingService`, `TelegramRenderer`, `PublisherPort`, `TelegramBotPublisher`, reward decomposition.
7. **`apps/ops`**: Taxonomy, dynamic settings, feature flags, `AudienceLearningService`, scheduler tasks, structured logging.
8. **`apps/platform_api`**: Django Ninja operations API at `/api/v1/` with story inspection (lifecycle, momentum, intelligence, media) and selection runs.
