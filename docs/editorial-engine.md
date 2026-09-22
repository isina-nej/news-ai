# Editorial Engine & Telegram-Native Publishing

## Overview

The Editorial Engine separates intrinsic news value from immediate publishing urgency, enforces editorial policy, selects media, validates drafts, and renders Telegram-native HTML with professional visual hierarchy.

```text
Story + Momentum + Intelligence
  ↓
NewsworthinessService (importance, impact, utility, novelty, urgency, credibility)
  ↓
ChannelNoveltyService (NEW_STORY, MATERIAL_UPDATE, CORRECTION, MINOR_UPDATE, REPEAT)
  ↓
PublishPriorityService (positive signals + topic saturation, repetition & credibility penalties)
  ↓
EditorialPolicyEngine
  ├── PUBLISH_NOW
  ├── WATCH (with recommended_wait_minutes)
  ├── SCHEDULE (safe slot subject to spacing)
  ├── UPDATE_EXISTING_STORY (material update / correction)
  ├── SKIP
  └── REJECT (credibility gate / rumors)
  ↓
MediaSelectionService (resolution, aspect ratio, source reliability, thumbnail penalty)
  ↓
StructuredDraft (3-candidate headlines, lead, body points, why_it_matters)
  ↓
HeadlineEvaluator (clickbait filter, format alignment)
  ↓
DraftCriticService (unsupported claims, Persian cleanup, filler removal)
  ↓
TelegramRenderer (HTML escaping, character limits, semantic shortening, branding)
  ↓
FinalPublicationPayload & Idempotent Publisher (photo + caption, text fallback)
```

## 1. Newsworthiness vs. Publish Priority

- **`NewsworthinessScore`**: Measures the inherent value and significance of an event:
  - `importance`: 25%
  - `impact`: 20%
  - `utility`: 15%
  - `novelty`: 15%
  - `urgency`: 10%
  - `credibility`: 15%
- **`PublishPriorityScore`**: Determines whether right now is the optimal moment to publish:
  $$\text{Priority} = 0.35 \cdot \text{News} + 0.20 \cdot \text{Mom} + 0.10 \cdot \text{Acc} + 0.15 \cdot \text{Audience} + 0.05 \cdot \text{Fresh} + 0.10 \cdot \text{Indep} + 0.05 \cdot \text{Novelty} - \text{Penalties}$$
  - **Penalties**: Topic saturation ($\le 0.30$), recent lexical repetition ($\le 0.25$), credibility risk ($\le 0.30$).
  - **Breaking override**: High urgency ($\ge 0.85$) or breaking trend mitigates saturation and cooldown constraints.

## 2. Channel Novelty & Information Units

To prevent republishing the same facts with different wording:
- `NEW_STORY`: First time this real-world event is published in our channel.
- `MATERIAL_UPDATE`: Verified new facts added via `MaterialUpdateDecision` and `FactDiffService`.
- `CORRECTION`: Factual correction of a previous report (boosted +0.10).
- `MINOR_UPDATE` / `REPEAT`: Paraphrased content or updates lacking new verifiable facts (skipped).

## 3. Editorial Actions

1. **`PUBLISH_NOW`**: Cleared priority, passed credibility gate, novel content.
2. **`WATCH`**: High news value or rapid acceleration, but premature independent confirmation. Sets `recommended_wait_minutes = 5..10`.
3. **`SCHEDULE`**: Worthy of publication, queued for the next safe delivery slot subject to `MIN_SPACING_MINUTES = 20`.
4. **`UPDATE_EXISTING_STORY`**: Material update or correction linked to prior publication.
5. **`SKIP`**: Score below threshold, topic saturated, or repeat.
6. **`REJECT`**: Failed credibility gate, low-trust rumor, or unresolved factual conflict.

All decisions log counterfactual candidate sets in `PublicationSelectionRun` for offline evaluation.

## 4. Drafting, Persian Quality & Critic Gate

- **3 Headline Candidates**:
  - `DIRECT`: Factual, concise, direct.
  - `BREAKING`: Immediate, impact-first.
  - `CONTEXTUAL`: Analytical, includes background.
- **HeadlineEvaluator**: Penalizes clickbait terms (`باورنکردنی`, `شوکه‌کننده`, `عجیب ولی واقعی`), aligns headline style with target format.
- **DraftCriticService**: Validates that all claims are grounded in evidence, removes AI filler clichés (`لازم به ذکر است`, `شایان ذکر است`), and standardizes Persian spacing and ZWNJ.

## 5. Telegram-Native Formatting

Templates:
- `BREAKING`: 🚨 icon, immediate core facts, developing indicator, source block, signature.
- `STANDARD`: 📰 icon, lead, bullet points (`•`), "چرا مهم است؟" block, source attribution, signature.
- `QUICK_UPDATE`: 🔄 icon, update line, source attribution, signature.
- `OFFICIAL_STATEMENT`, `DEVELOPING`, `ANALYSIS`, `DATA`, `FOLLOW_UP`.

Limits:
- Caption: max 1024 characters.
- Text message: max 4096 characters.
- Semantic shortening: never truncates mid-word or mid-sentence.

## 6. Media Pipeline & Failure Handling

1. **Ingestion**: Harvests image candidates from RSS (`enclosure`, `media:content`), HTML (`og:image`, `article images`), and Telegram (`photo`).
2. **Validation**: `MediaValidationService` runs SSRF guards, checks MIME type and image size outside open DB transactions.
3. **Selection**: `MediaSelectionService` scores resolution, 16:9 aspect ratio, source reliability, and penalizes thumbnails (< 250px).
4. **Delivery**: `sendPhoto` with caption. If payload exceeds caption limits, sends photo with short caption followed by full message.
5. **Text Fallback**: If photo upload fails, automatically falls back to text-only send so valid news is never blocked by a broken image.
