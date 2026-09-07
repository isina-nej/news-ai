"""Source ingestion adapters and registry."""

from apps.core.choices import Platform
from apps.sources.adapters.base import (
    AdapterError,
    AuthenticationError,
    ExtractionError,
    FetchContext,
    FetchedItem,
    FetchResult,
    InvalidPayloadError,
    NetworkError,
    ParseError,
    PayloadTooLargeError,
    PermanentSourceError,
    RateLimitError,
    SourceAdapter,
    SSRFError,
    TimeoutError,
)
from apps.sources.adapters.html import HTMLSourceAdapter
from apps.sources.adapters.http_client import SafeHttpClient, default_http_client
from apps.sources.adapters.registry import AdapterNotFoundError, AdapterRegistry, adapter_registry
from apps.sources.adapters.rss import RSSSourceAdapter
from apps.sources.adapters.rsshub import RSSHubAdapter

# Register standard adapters for Phase 2
adapter_registry.register(Platform.RSS, "rss", factory=RSSSourceAdapter)
adapter_registry.register(Platform.RSS, "", factory=RSSSourceAdapter)
adapter_registry.register(Platform.RSSHUB, "rsshub", factory=RSSHubAdapter)
adapter_registry.register(Platform.RSSHUB, "", factory=RSSHubAdapter)
adapter_registry.register(Platform.WEBSITE_HTML, "html", factory=HTMLSourceAdapter)
adapter_registry.register(Platform.WEBSITE_HTML, "", factory=HTMLSourceAdapter)

__all__ = [
    "AdapterError",
    "AdapterNotFoundError",
    "AdapterRegistry",
    "AuthenticationError",
    "ExtractionError",
    "FetchContext",
    "FetchResult",
    "FetchedItem",
    "HTMLSourceAdapter",
    "InvalidPayloadError",
    "NetworkError",
    "ParseError",
    "PayloadTooLargeError",
    "PermanentSourceError",
    "RSSHubAdapter",
    "RSSSourceAdapter",
    "RateLimitError",
    "SSRFError",
    "SafeHttpClient",
    "SourceAdapter",
    "TimeoutError",
    "adapter_registry",
    "default_http_client",
]
