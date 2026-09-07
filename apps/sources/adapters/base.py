"""Source adapter interface and data transfer objects.

Pure Python DTOs, strictly independent of Django ORM models.
Adapters produce FetchedItems; NormalizationService and
IngestionPersistenceService handle persistence and business logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from apps.core.exceptions import DomainError

# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class AdapterError(DomainError):
    """Base error for adapter operations."""

    is_transient: bool = False

    def __init__(
        self,
        message: str,
        *,
        is_transient: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if is_transient is not None:
            self.is_transient = is_transient
        self.details = details or {}


class NetworkError(AdapterError):
    """Transient connectivity failure (DNS failure, connection refused, reset)."""

    is_transient = True


class TimeoutError(AdapterError):
    """Transient request timeout."""

    is_transient = True


class RateLimitError(AdapterError):
    """HTTP 429 or provider rate limit reached."""

    is_transient = True

    def __init__(
        self, message: str, *, retry_after: int | None = None, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message, is_transient=True, details=details)
        self.retry_after = retry_after


class AuthenticationError(AdapterError):
    """HTTP 401/403 or invalid credentials/session."""

    is_transient = False


class ParseError(AdapterError):
    """Failed to parse feed, XML, or response structure."""

    is_transient = False


class ExtractionError(AdapterError):
    """Failed to extract article content from valid HTML."""

    is_transient = False


class InvalidPayloadError(AdapterError):
    """Payload structure violates required invariants."""

    is_transient = False


class PermanentSourceError(AdapterError):
    """HTTP 404, 410, or unrecoverable endpoint failure."""

    is_transient = False


class SSRFError(PermanentSourceError):
    """Target IP address resolves to private, loopback, or link-local range."""

    is_transient = False


class PayloadTooLargeError(PermanentSourceError):
    """Response body exceeds configured size limit."""

    is_transient = False


# ---------------------------------------------------------------------------
# Data Transfer Objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FetchContext:
    """Input context passed to a source adapter."""

    source_id: int
    url: str
    platform: str
    adapter_type: str = ""
    configuration: dict[str, Any] = field(default_factory=dict)
    etag: str | None = None
    last_modified: str | None = None
    timeout_seconds: float = 30.0
    max_bytes: int = 5 * 1024 * 1024  # 5 MB
    correlation_id: str = ""


@dataclass
class FetchedItem:
    """Single item extracted from a source before normalization/persistence."""

    url: str
    title: str = ""
    raw_text: str = ""
    external_id: str | None = None
    canonical_url: str = ""
    published_at: datetime | None = None
    author: str = ""
    language: str = "und"
    media: dict[str, Any] = field(default_factory=dict)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    source_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchResult:
    """Result of a source fetch execution."""

    items: list[FetchedItem] = field(default_factory=list)
    status_code: int = 200
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    duration_ms: int = 0
    raw_preview: str = ""


# ---------------------------------------------------------------------------
# Adapter Protocol
# ---------------------------------------------------------------------------


class SourceAdapter(Protocol):
    """Protocol implemented by every platform ingestion adapter."""

    async def fetch(self, context: FetchContext) -> FetchResult:
        """Fetch items from the source specified by context."""
        ...
