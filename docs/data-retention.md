# Data Retention & Operational Cleanup Policy

## 1. Principles

1. **Auditability First**: Every editorial, clustering, and ranking decision must remain replayable.
2. **Never Auto-Delete Core Intelligence**: `SourceItem`, `Story`, `StoryMembership`, `ClusteringDecision`, and `ScoreRecord` rows are preserved indefinitely. Merges and reassignments retire records (`status="merged"`, `is_current=False`) rather than deleting them.
3. **No Destructive Operations Without Explicit Configuration**: Automated cleanup jobs must never perform un-audited bulk purges.

## 2. Retention Schedules

| Dataset | Storage Location | Retention Period | Archival / Pruning Strategy |
|---|---|---|---|
| **AI Call Logs** (`AICallLog`) | MySQL `apps.ai.AICallLog` | 90 Days | Rotate older rows to cold storage / compressed dumps. Never store raw prompt texts or secrets. |
| **AI Cache** (`AIResultCache`) | MySQL `apps.ai.AIResultCache` | 30 Days (TTL configurable) | Rows expire via `expires_at = now + AI_CACHE_TTL_SECONDS` (default 86400s / 24h). Expired rows vacuumed periodically. |
| **Raw Payloads** (`SourceItem.raw_payload`) | MySQL `apps.news.SourceItem` | 180 Days | Strip internal debugging metadata after 180 days while retaining platform ID and canonical URL. |
| **Engagement Snapshots** (`EngagementSnapshot`) | MySQL `apps.news.EngagementSnapshot` | 365 Days | Milestones (10m, 30m, 1h, 3h, 6h, 12h, 24h) preserved for baseline recomputation. |
| **Publication Snapshots** (`PublicationEngagementSnapshot`) | MySQL `apps.publishing` | Indefinite | Required for ongoing audience learning and baseline models. |
| **Operational Audit Logs** (`AuditLog`) | MySQL `apps.ops.AuditLog` | Indefinite | Read-only audit trail. Immutable, no delete permission in Django Admin. |
| **Decision Logs** (`DecisionLog`) | MySQL `apps.ranking.DecisionLog` | 365 Days | Feature snapshots and rewards used for offline contextual bandit training. |
| **Fetch Runs** (`FetchRun`) | MySQL `apps.sources.FetchRun` | 30 Days | High-volume operational run logs pruned after 30 days. |

## 3. Pruning Implementation Guidelines

- Dry-run mode required before any purge command.
- Soft-deletion or tombstoning preferred over physical deletes where relationships exist.
- Bounded batches with explicit timeouts to prevent locking MySQL tables under production load.
