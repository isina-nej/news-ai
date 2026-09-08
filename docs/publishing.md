# Telegram Publishing & Policy (Phase 7)

## 1. Engine Architecture (WHAT / WHEN / HOW)

The Publishing Engine divides publication decisions into three independent components:
- **WHAT**: Decides whether an active story should be published, scheduled for the next slot, held by the credibility gate, or skipped.
- **WHEN**: Decides delivery timing: immediately (`now`) if urgency >= 0.85 (breaking override), or queued (`schedule`) subject to `MIN_SPACING_MINUTES = 20` and `MAX_POSTS_PER_HOUR = 3`.
- **HOW**: Style candidates (`headline_style`, `tone`, `emoji_level`, `technical_depth`, `template_version="tg-v1"`).

## 2. Safety & Auto-Publish Control

- Default production setting: `ENABLE_AUTO_PUBLISH = false`. Live messages are never dispatched automatically unless this flag is explicitly turned on via database `FeatureFlag` or environment.
- **Dry-run mode**: `publish_story(story_id, dry_run=True)` generates the complete post, validates evidence and Telegram limits, checks idempotency, but does not send to Telegram.

## 3. Idempotency & State Machine

- Idempotency key pattern: `story-{story_id}-{content_fingerprint}`.
- Re-running or retrying publication of the same story payload reuses the existing `Publication` row and prevents duplicate sends.
- State transitions follow a strict finite-state machine: `DRAFT` -> `READY` -> `APPROVED` -> `PUBLISHING` -> `PUBLISHED` (or `FAILED` / `CANCELLED`).
- External network requests to the Telegram Bot API execute outside open database transactions, ensuring failure states are cleanly recorded upon errors.

## 4. Telegram Templates & Content Safety

- `render_post(headline, body, source_urls)` strictly escapes all HTML special characters (`<`, `>`, `&`).
- Maximum caption and text limits (4096 characters) are enforced.
- Only URLs present in the story's verified evidence are permitted in source attribution links; unsupported hallucinated links are discarded.

## 5. Own-Channel Engagement & Normalized Reward

- Post-publication snapshots are recorded in `PublicationEngagementSnapshot`.
- `normalized_reward(snapshot, baselines)` computes a 0.0000..1.0000 reward:
  - 50% relative forwards
  - 25% relative views
  - 15% relative reactions
  - 10% relative replies
  - Missing metrics automatically redistribute weight across available signals.
