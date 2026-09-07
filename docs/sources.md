# Sources

Adapter contract: `fetch() -> list[NormalizedItem]`. Priority: RSS → RSSHub route → HTML static → dynamic (playwright fallback). Telegram: Kurigram user session, per-source interval via Beat. Twitter: stub (phase 1). Only `same source + external_id` deduped; cross-source kept and clustered.
