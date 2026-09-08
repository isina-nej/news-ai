# Phase 7 Telegram Publishing Decision

## Context
Publishing to a public channel requires defense against duplicate posts, ungrounded claims, and runaway automated spam. Auto-publishing must remain completely disabled by default until explicit operator enablement.

## Decision
1. **Publisher Interface**: Implemented `PublisherPort` with `TelegramBotPublisher` (official Bot API via python-telegram-bot / asyncio) and `FakePublisher` for testing.
2. **WHAT/WHEN/HOW Separation**: Editorial decisions are modular and decoupled from delivery timing and visual formatting.
3. **Strict HTML Escaping & Length Limits**: All headlines and bodies pass through `html.escape` and are clamped to Telegram's 4096-character limit.
4. **Idempotent Dispatch Outside Transactions**: Network requests to Telegram take place after database publication creation and outside open database transactions, ensuring `FAILED` states and errors are committed without rollback.
5. **Auto-Publish Safety**: `ENABLE_AUTO_PUBLISH` defaults to `False`. Dry-run mode evaluates the full pipeline without transmitting messages.
6. **Normalized Reward**: Post engagement metrics are evaluated against own-channel historical percentiles, with missing metrics redistributing weights.
