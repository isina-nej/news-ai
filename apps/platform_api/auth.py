"""Internal API authentication. Header token, never secrets in query params."""

from __future__ import annotations

from django.conf import settings
from ninja.security import HttpBearer


class InternalTokenAuth(HttpBearer):
    def authenticate(self, request, token: str | None):
        expected = str(getattr(settings, "PLATFORM_API_TOKEN", "") or "")
        if not expected or not token:
            return None
        if token != expected:
            return None
        return {"token": "internal"}


def auth_enabled() -> bool:
    return bool(str(getattr(settings, "PLATFORM_API_TOKEN", "") or ""))
