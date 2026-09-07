# Decision: RSSHub as first website-ingestion option

DIYgod/RSSHub active (push 2026-09-06, 46k stars). Self-host pinned date tag `diygod/rsshub:2026-09-06` + Redis + ACCESS_KEY. Route map outlet→template, RSS/JSON parse, 429/empty handling. Best-effort; direct scraper fallback. Route breakage isolated behind adapter.
