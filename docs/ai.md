# AI layer

Provider-agnostic. `apps/ai/providers.py` exposes `AIProvider.generate` and
`AIProvider.structured`; application code never imports httpx directly. The
default is `FakeAIProvider`. `openai-compatible` uses a minimal Chat Completions
client and is selected with `AI_PROVIDER=openai-compatible`, `AI_BASE_URL`,
`AI_API_KEY`, cheap/strong model names, timeout, and retries. `AI_MODEL` is a
fallback for both cheap and strong routes.

Prompts live only in `apps/ai/prompts/` and are addressed by `(name, version)`.
Machine-consumed outputs are Pydantic-validated; malformed JSON gets one
bounded repair attempt, then an `invalid_response` audit row and `AITask`
`failed` state. Every call writes `AICallLog` with provider, model, task,
prompt version, input hash, latency, token counts, cache status, and
error type. Secrets and raw source text are never stored there.

Cache key is `(task, prompt_version, model, input_hash)`; content revision
changes the hash and invalidates the cache. Cheap model handles topic/content
classification; strong model handles judge, conflict, novelty, and drafts.

Topic output is constrained to the DB taxonomy; unknown maps to
`candidate_topic`. News value has six 0..1 components with confidence and
reason codes. Credibility combines source trust, independent spread, copy
network, conflict penalty, and AI as one signal only. Only `ambiguous`
clustering decisions go to the judge; low confidence stays split. Material
updates use a closed five-label schema. Drafts use evidence URLs only and
preserve uncertainty and attribution. AI outages leave ingestion intact via
`pending/processing/done/failed` ledger plus async retry.
