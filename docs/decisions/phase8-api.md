# Phase 8 API & Operations Decision

## Context
The internal operations API provides administrative, review, and automation interfaces without exposing secrets, raw unvalidated JSON, or dangerous unbounded queries.

## Decision
1. **Django Ninja Versioned API**: Served at `/api/v1/` with automatic OpenAPI documentation generated at `/api/v1/docs`.
2. **Internal Bearer Token Authentication**: Controlled via `PLATFORM_API_TOKEN` in settings. Disabled when token is not configured (allowing local development ease) and strictly enforced when present.
3. **Ordering Allowlist & Safe Pagination**: Ordering parameters are matched against `ORDER_ALLOWLIST` per resource (`sources`, `items`, `stories`, `publications`) to prevent arbitrary SQL sorting. All list endpoints are paginated via `PageNumberPagination`.
4. **Leak-Free Schemas**: Raw source text, raw payloads, credentials, and tokens are omitted from default schemas (`SourceOut`, `SourceItemOut`, `StoryOut`).
5. **Audited Manual Actions**: Every operational command (`story.rescore`, `story.recluster`, `story.merge`, `item.reassign`, `publication.approve`, `publication.reject`, `publication.dry_run`, `source.trigger_fetch`) logs an immutable entry to `AuditLog`.
6. **Feature Flags**: Operational knobs (`ENABLE_AI`, `ENABLE_AUTO_PUBLISH`, `ENABLE_TWITTER_SOURCE`, `ENABLE_QDRANT`, `ENABLE_AUDIENCE_LEARNING`, `ENABLE_EXPLORATION`) are exposed via `/api/v1/feature-flags` and backed by `ops.models.FeatureFlag`.
