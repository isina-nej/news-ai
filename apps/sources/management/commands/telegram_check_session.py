"""Check the Telegram user-session health without printing any secret."""

from __future__ import annotations

import asyncio

from django.core.management.base import BaseCommand, CommandError

from apps.sources.adapters.base import AuthenticationError
from apps.sources.adapters.telegram_client import get_telegram_client_manager


class Command(BaseCommand):
    help = (
        "Verify Telegram user-session connectivity (prints account identity only, never secrets)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--account-key", default="default", help="Telegram account key (default: default)"
        )

    def handle(self, *args, **options):
        account_key = str(options.get("account_key") or "default")
        manager = get_telegram_client_manager(account_key)
        try:
            info = asyncio.run(manager.health_check())
        except AuthenticationError as exc:
            raise CommandError(f"Telegram session invalid: {exc}") from exc
        except Exception as exc:
            raise CommandError(f"Telegram session check failed: {type(exc).__name__}") from exc
        finally:
            try:
                asyncio.run(manager.disconnect())
            except Exception:  # noqa: S110 — best-effort cleanup, never fail the command
                pass
        self.stdout.write(
            self.style.SUCCESS(
                f"Telegram session OK: account_key={info.get('account_key')} "
                f"user_id={info.get('user_id')} username={info.get('username')}"
            )
        )
