# Final Hardening Phase Decision

## Context
Preparation for production readiness requires verifying that the entire pipeline operates smoothly without external credentials, providing local demonstration and health probes, reviewing security bounds, and documenting deployment and retention procedures.

## Decision
1. **End-to-End Test Suite**: Implemented `tests/test_final_hardening_e2e.py` covering ingestion, clustering, AI analysis, ranking, selection, publication dry-run, feedback, and audience learning.
2. **Operational CLI Commands**:
   - `seed_demo_news`: creates sample realistic news items, topics, and metrics.
   - `system_health`: validates MySQL, Redis, Qdrant, AI, Telegram, and Twitter readiness without printing secrets.
   - `run_news_pipeline`: executes end-to-end pipeline in `--dry-run` or live mode.
   - `ranking_backtest`: historical replay without live sends.
3. **Failure Injection Verified**: Database persists source items and maintains status even when Qdrant or AI encounters outages.
4. **Production Compose Review**: Added `restart: unless-stopped` across all application services, kept MySQL, Redis, and Qdrant strictly internal, mounted the ONNX model cache volume, and fixed `docker/Dockerfile` with Debian build tools (`pkg-config`, `default-libmysqlclient-dev`).
5. **Security & Prompt Injection Defenses**: Verified SSRF protection, strict HTML escaping on Telegram posts, evidence URL allowlist validation, exclusion of raw payloads and secrets from Admin/API, and explicit prompt demarcation declaring news content as untrusted DATA.
