"""Deterministic canonical URL normalization and hashing service."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Conservative tracking parameters: only well-known analytics/ad identifiers.
# Generic parameters like 'source' and 'ref' are intentionally NOT in this global list
# because they often carry genuine routing/article state on news and content platforms.
DEFAULT_TRACKING_PARAMS = frozenset(
    {
        # Google Analytics / Urchin
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_source_platform",
        # Facebook / Meta
        "fbclid",
        "fb_action_ids",
        "fb_action_types",
        "fb_source",
        # Google Ads / DoubleClick
        "gclid",
        "gclsrc",
        "dclid",
        "wbraid",
        "gbraid",
        # Microsoft Ads
        "msclkid",
        # Yandex
        "yclid",
        # Mailchimp
        "mc_cid",
        "mc_eid",
        # Google / General analytics
        "_ga",
        "_gl",
        # Instagram
        "igshid",
    }
)


class CanonicalURLService:
    """Service to normalize web URLs into deterministic canonical representations.

    Adheres strictly to RFC 3986:
    - Path reserved characters like %2F, %3F, %26 are NOT unquoted, preserving route semantics.
    - Scheme and host are lowercased; default ports (:80, :443) are stripped.
    - Fragments (#...) are removed.
    - Global tracking params stripped; meaningful params preserved and sorted.
    - Per-domain tracking overrides supported.
    """

    def __init__(
        self,
        *,
        tracking_params: frozenset[str] = DEFAULT_TRACKING_PARAMS,
        domain_tracking_params: dict[str, set[str]] | None = None,
    ) -> None:
        self.tracking_params = tracking_params
        self.domain_tracking_params = domain_tracking_params or {}

    def normalize(self, url: str) -> str:
        """Produce a canonical normalized URL."""
        if not url or not isinstance(url, str):
            return ""

        url = url.strip()
        parsed = urlparse(url)

        scheme = parsed.scheme.lower()
        if not scheme:
            scheme = "https"

        netloc = parsed.netloc.lower()
        # Strip default ports
        if scheme == "http" and netloc.endswith(":80"):
            netloc = netloc[:-3]
        elif scheme == "https" and netloc.endswith(":443"):
            netloc = netloc[:-4]

        # Normalize path without unquoting reserved characters (%2F, %3F, %26, etc.)
        path = parsed.path or "/"
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")

        # Combine global tracking params with domain-specific tracking params
        domain_extras = self.domain_tracking_params.get(netloc, set())
        active_tracking = self.tracking_params | domain_extras

        # Filter and sort query parameters
        filtered_query: list[tuple[str, str]] = []
        if parsed.query:
            for k, v in parse_qsl(parsed.query, keep_blank_values=True):
                if k.lower() not in active_tracking:
                    filtered_query.append((k, v))
            filtered_query.sort()

        query_str = urlencode(filtered_query) if filtered_query else ""

        # Fragment is always removed
        canonical = urlunparse((scheme, netloc, path, "", query_str, ""))
        return canonical

    @staticmethod
    def compute_hash(canonical_url: str) -> str:
        """Compute SHA-256 hex digest of canonical URL."""
        if not canonical_url:
            return ""
        return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()

    def canonicalize_and_hash(self, url: str) -> tuple[str, str]:
        """Convenience method returning (canonical_url, url_hash)."""
        canonical = self.normalize(url)
        return canonical, self.compute_hash(canonical)


canonical_url_service = CanonicalURLService()
