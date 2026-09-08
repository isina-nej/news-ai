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
from apps.sources.adapters.telegram import TelegramSourceAdapter
from apps.sources.adapters.twitter import TwitterSessionClient, TwitterSourceAdapter

# Register standard adapters for Phase 2
adapter_registry.register(Platform.RSS, "rss", factory=RSSSourceAdapter)
adapter_registry.register(Platform.RSS, "", factory=RSSSourceAdapter)
adapter_registry.register(Platform.RSSHUB, "rsshub", factory=RSSHubAdapter)
adapter_registry.register(Platform.RSSHUB, "", factory=RSSHubAdapter)
adapter_registry.register(Platform.WEBSITE_HTML, "html", factory=HTMLSourceAdapter)
adapter_registry.register(Platform.WEBSITE_HTML, "", factory=HTMLSourceAdapter)
# Telegram user-session ingestion (Kurigram). adapter_type "telegram" explicit + platform fallback.
adapter_registry.register(Platform.TELEGRAM, "telegram", factory=TelegramSourceAdapter)
adapter_registry.register(Platform.TELEGRAM, "", factory=TelegramSourceAdapter)
# Twitter/X session-based read-only ingestion. Explicit opt-in via ENABLE_TWITTER_SOURCE.
adapter_registry.register(Platform.TWITTER_X, "twitter", factory=TwitterSourceAdapter)
adapter_registry.register(Platform.TWITTER_X, "", factory=TwitterSourceAdapter)

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
    "TelegramSourceAdapter",
    "TimeoutError",
    "TwitterSessionClient",
    "TwitterSourceAdapter",
    "adapter_registry",
    "default_http_client",
]
