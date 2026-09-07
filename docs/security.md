# Security

Secrets env-only, never git/logs. Sessions encrypted (Fernet, key env), masked in admin, session logic isolated. RedactFilter on logs. Expired session → distinct error state, no silent credential swap. Publication unique(story, channel) → idempotent.
