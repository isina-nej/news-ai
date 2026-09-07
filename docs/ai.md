# AI layer

Provider-agnostic, OpenAI-compatible via httpx. Prompt registry in `apps/ai/prompts/`, Pydantic v2 schemas, validation before DB/publish. Cheap model → classification, strong → final analysis. Calls logged (model, latency, tokens, error). Cache by content hash.
