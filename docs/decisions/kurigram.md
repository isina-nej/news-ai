# Decision: Kurigram for Telegram user session

Upstream Telethon archived → dropped. Kurigram (kurigram-org/kurigram) active: v2.2.25 (2026-08-21), commit 2026-09-06, 811 stars. PyPI `kurigram`, StringSession via `export_session_string()`, fully async, Pyrogram-v2 drop-in. Pin `kurigram==2.2.*`.

Bound behind `TelegramSourceAdapter`; session logic isolated, swappable to Hydrogram if needed.
