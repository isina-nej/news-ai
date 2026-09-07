"""Safe, pooled HTTP client with loop-aware lifecycle, streaming size guard,
and secret redaction.
"""

from __future__ import annotations

import asyncio
import email.utils
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from apps.core.redaction import redact_url
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
MAX_RETRY_AFTER_SECONDS = 3600  # Cap backoff to 1 hour to prevent worker starvation


def parse_retry_after(header_val: str | None) -> int | None:
    """Parse HTTP Retry-After header supporting both delta-seconds and HTTP-date formats.

    Bounds result to a safe upper limit (MAX_RETRY_AFTER_SECONDS).
    """
    if not header_val or not isinstance(header_val, str):
        return None

    header_val = header_val.strip()

    # 1. Delta-seconds (e.g. '120')
    if header_val.isdigit():
        secs = int(header_val)
        return min(max(0, secs), MAX_RETRY_AFTER_SECONDS)

    # 2. HTTP-date (RFC 7231 / RFC 2822 format, e.g. 'Wed, 21 Oct 2026 07:28:00 GMT')
    try:
        dt = email.utils.parsedate_to_datetime(header_val)
        if dt:
            now = datetime.now(UTC)
            delta = int((dt - now).total_seconds())
            return min(max(0, delta), MAX_RETRY_AFTER_SECONDS)
    except (ValueError, TypeError, OverflowError):
        return None

    return None


class SafeHttpClient:
    """Loop-aware HTTP client with chunked streaming size guards and SSRF checks.

    Lifecycle:
    - Binds httpx.AsyncClient strictly to the running event loop.
    - If called from a new event loop (e.g. across Celery tasks using asyncio.run()),
      it detects loop mismatch and instantiates a clean client for that loop,
      avoiding cross-loop socket reuse and 'Event loop is closed' errors.
    - Uses response.aiter_bytes() streaming so oversized responses (even chunked
      transfers without Content-Length) are aborted before consuming memory.
    - Sanitizes all URLs in error messages to prevent secret leakage.
    """

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
        self._loop: asyncio.AbstractEventLoop | None = None

    async def get_client(self) -> httpx.AsyncClient:
        """Get or initialize the async client, ensuring it is attached to the current loop."""
        current_loop = asyncio.get_running_loop()

        # If previous client is bound to a different or closed event loop, discard it
        if self._loop is not None and (self._loop != current_loop or self._loop.is_closed()):
            self._client = None
            self._loop = None

        if self._client is None or self._client.is_closed:
            limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
            self._client = httpx.AsyncClient(
                limits=limits,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                follow_redirects=False,  # Redirects followed manually to re-verify SSRF
                headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate, br"},
            )
            self._loop = current_loop

        return self._client

    async def close(self) -> None:
        """Explicitly shut down the underlying AsyncClient and connection pool."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
        self._loop = None

    async def __aenter__(self) -> SafeHttpClient:
        await self.get_client()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

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
        """Perform safe GET following redirects with SSRF check at each hop.

        Guarded by chunk-by-chunk streaming size limit.
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
            safe_display_url = redact_url(current_url)

            # 1. SSRF validation before opening connection
            validate_url_for_ssrf(current_url, allow_hosts=self.allow_hosts)

            # 2. Execute streaming GET request
            req_timeout = timeout_seconds or self.timeout
            try:
                request = client.build_request(
                    "GET", current_url, headers=req_headers, timeout=req_timeout
                )
                response = await client.send(request, stream=True)
            except httpx.TimeoutException as err:
                raise TimeoutError(f"HTTP request timed out for {safe_display_url}: {err}") from err
            except httpx.NetworkError as err:
                raise NetworkError(f"Network error accessing {safe_display_url}: {err}") from err
            except Exception as err:
                raise NetworkError(f"Unexpected error fetching {safe_display_url}: {err}") from err

            res_headers = {k.lower(): v for k, v in response.headers.items()}
            status = response.status_code

            # 3. Handle manual redirects with SSRF re-check
            if response.is_redirect:
                await response.aclose()
                redirects_followed += 1
                if redirects_followed > MAX_REDIRECTS:
                    raise PermanentSourceError(
                        f"Too many redirects (exceeded {MAX_REDIRECTS}) for {safe_display_url}"
                    )
                location = res_headers.get("location")
                if not location:
                    raise PermanentSourceError(
                        f"Redirect missing Location header from {safe_display_url}"
                    )
                current_url = str(response.url.join(location))
                continue

            # 4. Handle 304 Not Modified immediately
            if status == 304:
                await response.aclose()
                duration_ms = int((time.monotonic() - start_time) * 1000)
                return (304, res_headers, b"", duration_ms)

            # 5. Handle HTTP status errors before streaming body
            if status == 429:
                await response.aclose()
                retry_after = parse_retry_after(res_headers.get("retry-after"))
                raise RateLimitError(
                    f"Rate limit exceeded (HTTP 429) for {safe_display_url}",
                    retry_after=retry_after,
                    details={"status": 429, "retry_after": retry_after},
                )

            if status in (401, 403):
                await response.aclose()
                raise AuthenticationError(
                    f"Authentication failed (HTTP {status}) for {safe_display_url}",
                    details={"status": status},
                )

            if status in (404, 410):
                await response.aclose()
                raise PermanentSourceError(
                    f"Permanent error (HTTP {status}) for {safe_display_url}",
                    details={"status": status},
                )

            if status >= 500:
                await response.aclose()
                raise NetworkError(
                    f"Server error (HTTP {status}) from {safe_display_url}",
                    details={"status": status},
                )

            if status >= 400:
                await response.aclose()
                raise PermanentSourceError(
                    f"Client error (HTTP {status}) for {safe_display_url}",
                    details={"status": status},
                )

            # 6. Early check on Content-Length optimization
            content_length = res_headers.get("content-length")
            is_valid_len = content_length and content_length.isdigit()
            if is_valid_len and int(content_length) > effective_max_bytes:
                await response.aclose()
                raise PayloadTooLargeError(
                    f"Content-length {content_length} exceeds limit {effective_max_bytes}"
                )

            # 7. Chunk-by-chunk streaming read with strict size enforcement
            chunks: list[bytes] = []
            bytes_read = 0

            try:
                async for chunk in response.aiter_bytes():
                    bytes_read += len(chunk)
                    if bytes_read > effective_max_bytes:
                        err_msg = (
                            f"Response body exceeded limit of {effective_max_bytes} "
                            f"bytes for {safe_display_url}"
                        )
                        raise PayloadTooLargeError(err_msg)
                    chunks.append(chunk)
            finally:
                await response.aclose()

            body = b"".join(chunks)
            duration_ms = int((time.monotonic() - start_time) * 1000)
            return (status, res_headers, body, duration_ms)


# Global default client (loop-aware, safely resets across event loops)
default_http_client = SafeHttpClient()
