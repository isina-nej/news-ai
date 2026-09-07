"""RSSHub first-class source adapter with configuration-based route generation and env keys."""

from __future__ import annotations

from urllib.parse import urlencode

from django.conf import settings

from apps.sources.adapters.base import (
    FetchContext,
    FetchResult,
    SourceAdapter,
)
from apps.sources.adapters.http_client import SafeHttpClient, default_http_client
from apps.sources.adapters.rss import RSSSourceAdapter


class RSSHubAdapter(SourceAdapter):
    """Adapter for self-hosted or public RSSHub instances.

    Builds route URLs dynamically from source configuration, injects
    ACCESS_KEY from settings/env (never persisted in DB or logged),
    and delegates feed parsing to the RSS engine.
    """

    def __init__(self, http_client: SafeHttpClient | None = None) -> None:
        self.http_client = http_client or default_http_client
        self._rss_adapter = RSSSourceAdapter(http_client=self.http_client)

    def build_endpoint_url(self, context: FetchContext) -> str:
        """Construct target RSSHub feed URL with route, params, and access key."""
        base_url = (
            context.configuration.get("base_url")
            or getattr(settings, "RSSHUB_BASE_URL", "http://rsshub:1200")
        ).rstrip("/")

        # Route can come from configuration['route'] or context.url
        route = context.configuration.get("route", "")
        if not route and context.url:
            # If full URL was passed in context.url, check if it already points to route
            if context.url.startswith("/"):
                route = context.url
            elif "://" in context.url:
                from urllib.parse import urlparse

                route = urlparse(context.url).path

        route = route.lstrip("/")
        endpoint = f"{base_url}/{route}"

        # Combine parameters
        params = dict(context.configuration.get("params", {}))

        # Access key from settings/env only (never DB)
        access_key = getattr(settings, "RSSHUB_ACCESS_KEY", "")
        if access_key and "key" not in params:
            params["key"] = access_key

        if params:
            endpoint = f"{endpoint}?{urlencode(params)}"

        return endpoint

    async def fetch(self, context: FetchContext) -> FetchResult:
        endpoint_url = self.build_endpoint_url(context)

        # Only allow explicit trusted hosts configured in deployment settings.
        # Arbitrary source configurations cannot bypass SSRF protections.
        trusted_hosts = set(getattr(settings, "RSSHUB_TRUSTED_HOSTS", ["rsshub"]))
        default_base = getattr(settings, "RSSHUB_BASE_URL", "")
        if default_base:
            from urllib.parse import urlparse

            default_host = urlparse(default_base).hostname
            if default_host:
                trusted_hosts.add(default_host)

        allow_hosts = list(set(self.http_client.allow_hosts) | trusted_hosts)

        rsshub_http = SafeHttpClient(
            user_agent=self.http_client.user_agent,
            max_bytes=context.max_bytes,
            timeout=context.timeout_seconds,
            allow_hosts=allow_hosts,
        )

        sub_context = FetchContext(
            source_id=context.source_id,
            url=endpoint_url,
            platform=context.platform,
            adapter_type="rsshub",
            configuration=context.configuration,
            etag=context.etag,
            last_modified=context.last_modified,
            timeout_seconds=context.timeout_seconds,
            max_bytes=context.max_bytes,
            correlation_id=context.correlation_id,
        )

        try:
            adapter = RSSSourceAdapter(http_client=rsshub_http)
            result = await adapter.fetch(sub_context)
            return result
        finally:
            await rsshub_http.close()
