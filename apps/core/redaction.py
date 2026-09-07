"""Central secret and URL redaction utilities.

Ensures sensitive parameters (keys, tokens, passwords, sessions) are never
persisted in databases, logs, error messages, or exception traces.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Query parameter names that must always be masked
SENSITIVE_PARAM_NAMES = frozenset(
    {
        "key",
        "access_key",
        "access_token",
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "passwd",
        "auth",
        "authorization",
        "session",
        "session_id",
        "sig",
        "signature",
        "code",
    }
)

# Text patterns: key=value, Bearer token, password=...
RE_TEXT_SECRETS = [
    re.compile(
        r"(?i)(api[_-]?key|access[_-]?key|bot[_-]?token|token|password|passwd|secret|session)[\s:=]+['\"]?([A-Za-z0-9_\-\.]{4,})['\"]?"
    ),
    re.compile(r"(?i)(bearer)\s+[A-Za-z0-9_\-\.]{8,}"),
    re.compile(r"(?i)(https?://[^\s\?]+\?)([^\s\)]+)"),
]


def redact_url(url: str) -> str:
    """Redact sensitive query parameters from a URL.

    Example:
        https://rsshub:1200/feed?key=secret123&limit=10
        -> https://rsshub:1200/feed?key=[REDACTED]&limit=10
    """
    if not url or not isinstance(url, str):
        return ""

    try:
        parsed = urlparse(url)
    except Exception:
        return "[REDACTED_URL]"

    if not parsed.query:
        return url

    query_tuples = parse_qsl(parsed.query, keep_blank_values=True)
    redacted_query = []
    for k, v in query_tuples:
        is_sensitive = k.lower() in SENSITIVE_PARAM_NAMES or any(
            s in k.lower() for s in ("token", "secret", "key", "password")
        )
        if is_sensitive:
            redacted_query.append((k, "[REDACTED]"))
        else:
            redacted_query.append((k, v))

    new_query = urlencode(redacted_query)
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment)
    )


def redact_text(text: str) -> str:
    """Mask credentials, tokens, and sensitive URL query params in arbitrary text/error strings."""
    if not text or not isinstance(text, str):
        return ""

    result = text

    # Redact full URLs inside the text
    def _replace_url_match(match: re.Match) -> str:
        prefix = match.group(1)
        raw_query = match.group(2)
        full_url = prefix + raw_query
        return redact_url(full_url)

    result = RE_TEXT_SECRETS[2].sub(_replace_url_match, result)

    # Redact key=value patterns
    def _replace_key_val(match: re.Match) -> str:
        key_name = match.group(1)
        return f"{key_name}=[REDACTED]"

    result = RE_TEXT_SECRETS[0].sub(_replace_key_val, result)

    # Redact Bearer tokens
    result = RE_TEXT_SECRETS[1].sub("Bearer [REDACTED]", result)

    return result


def sanitize_error_message(msg: str) -> str:
    """Clean an exception or error string before persisting to DB or logs."""
    return redact_text(str(msg))
