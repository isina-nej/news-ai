# Phase 5 AI provider decision

Use a minimal OpenAI-compatible httpx client instead of LiteLLM. The project
needs only JSON Chat Completions, bounded retries, timeouts, and token counts.
A full multi-provider abstraction would add dependency and operational surface
without changing the domain contract. `AIProvider` keeps that choice behind the
application service, so the provider can change without touching domain logic.
