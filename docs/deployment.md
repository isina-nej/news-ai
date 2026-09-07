# Deployment

Prod settings: `config.settings.prod`, MySQL 8.4 utf8mb4, Redis 7. Secrets via env/secret manager only. `SESSION_ENCRYPTION_KEY` required for Telegram sessions. Beat uses DatabaseScheduler. Web behind TLS proxy (`X-Forwarded-Proto`).
