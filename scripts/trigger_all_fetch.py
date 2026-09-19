"""Trigger fetch for all 8 Telegram channels."""
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')
django.setup()
from apps.sources.tasks import fetch_source_task
for sid in [12, 13, 14, 15, 16, 17, 18]:
    fetch_source_task.delay(sid)
    print(f"Queued: source {sid}")
print("Done: 7 fetches queued")
