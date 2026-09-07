"""Safe, pooled HTTP client with SSRF protection, size guards, and structured errors."""

from __future__ import annotations

import time

import httpx

from apps.sources.adapters.base import (
    AuthenticationError,
    NetworkError,
    PayloadTooLargeError,
    PermanentSourceError,
    RateLimitError,
    TimeoutError,
)
from apps.sources.adapters.ssrf import validate_url_for_ssrf

DEFAULT_USER_AGENT = "NewsAI-Bot/1.0 (+https://github.com/isina-nej/news-ai)"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5MB
DEFAULT_TIMEOUT = 30.0
MAX_REDIRECTS = 5


class SafeHttpClient:
    """Thread-safe client wrapping httpx.AsyncClient with SSRF checks and size guards."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout: float = DEFAULT_TIMEOUT,
        allow_hosts: list[str] | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.allow_hosts = allow_hosts or []
        self._client: httpx.AsyncClient | None = None

    async def get_client(self) -> httpx.AsyncClient:
        """Get or initialize the shared async client."""
        if self._client is None or self._client.is_closed:
            limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
            self._client = httpx.AsyncClient(
                limits=limits,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                follow_redirects=False,  # Redirects handled manually to re-verify SSRF
                headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate, br"},
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def fetch(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        timeout_seconds: float | None = None,
        max_bytes: int | None = None,
    ) -> tuple[int, dict[str, str], bytes, int]:
        """Perform safe GET request following redirects while checking SSRF at every hop.

        Returns: (status_code, headers_dict, body_bytes, duration_ms)
        """
        effective_max_bytes = max_bytes or self.max_bytes
        req_headers: dict[str, str] = dict(headers or {})
        if etag:
            req_headers["If-None-Match"] = etag
        if last_modified:
            req_headers["If-Modified-Since"] = last_modified

        client = await self.get_client()
        current_url = url
        redirects_followed = 0

        start_time = time.monotonic()

        while True:
            # 1. SSRF check before opening connection to this URL
            validate_url_for_ssrf(current_url, allow_hosts=self.allow_hosts)

            # 2. Execute GET request
            req_timeout = timeout_seconds or self.timeout
            try:
                response = await client.get(
                    current_url,
                    headers=req_headers,
                    timeout=req_timeout,
                )
            except httpx.TimeoutException as err:
                raise TimeoutError(f"HTTP request timed out for {current_url}: {err}") from err
            except httpx.NetworkError as err:
                raise NetworkError(f"Network error accessing {current_url}: {err}") from err
            except Exception as err:
                raise NetworkError(f"Unexpected error fetching {current_url}: {err}") from err

            # 3. Handle redirects manually to re-verify SSRF
            if response.is_redirect:
                redirects_followed += 1
                if redirects_followed > MAX_REDIRECTS:
                    raise PermanentSourceError(
                        f"Too many redirects (exceeded {MAX_REDIRECTS}) for {url}"
                    )
                location = response.headers.get("location")
                if not location:
                    raise PermanentSourceError(
                        f"Redirect response missing Location header from {current_url}"
                    )
                # Resolve relative redirects against current URL
                current_url = str(response.url.join(location))
                continue

            # 4. Check Content-Length if present
            content_length = response.headers.get("content-length")
            if content_length and content_length.isdigit():
                if int(content_length) > effective_max_bytes:
                    raise PayloadTooLargeError(
                        f"Content-length {content_length} exceeds limit {effective_max_bytes}"
                    )

            # 5. Read body with byte-count limit
            body = response.content
            if len(body) > effective_max_bytes:
                raise PayloadTooLargeError(
                    f"Response body {len(body)} bytes exceeds limit of {effective_max_bytes} bytes"
                )

            duration_ms = int((time.monotonic() - start_time) * 1000)
            res_headers = {k.lower(): v for k, v in response.headers.items()}
            status = response.status_code

            # 6. Map status codes to typed exceptions
            if status == 304:
                return (304, res_headers, b"", duration_ms)

            if status == 429:
                retry_after_header = res_headers.get("retry-after")
                retry_after = (
                    int(retry_after_header)
                    if retry_after_header and retry_after_header.isdigit()
                    else None
                )
                raise RateLimitError(
                    f"Rate limit exceeded (HTTP 429) for {url}",
                    retry_after=retry_after,
                    details={"status": 429, "retry_after": retry_after},
                )

            if status in (401, 403):
                raise AuthenticationError(
                    f"Authentication failed (HTTP {status}) for {url}",
                    details={"status": status},
                )

            if status in (404, 410):
                raise PermanentSourceError(
                    f"Permanent error (HTTP {status}) for {url}",
                    details={"status": status},
                )

            if status >= 500:
                raise NetworkError(
                    f"Server error (HTTP {status}) from {url}",
                    details={"status": status},
                )

            if status >= 400:
                raise PermanentSourceError(
                    f"Client error (HTTP {status}) for {url}",
                    details={"status": status},
                )

            return (status, res_headers, body, duration_ms)


# Global default client
default_http_client = SafeHttpClient()
