"""Deterministic canonical URL normalization and hashing service."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

# Query parameters used solely for tracking / analytics
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
        # Social / Ref
        "igshid",
        "ref",
        "ref_src",
        "ref_url",
        "source",
    }
)


class CanonicalURLService:
    """Service to normalize web URLs into deterministic canonical representations."""

    def __init__(self, *, tracking_params: frozenset[str] = DEFAULT_TRACKING_PARAMS) -> None:
        self.tracking_params = tracking_params

    def normalize(self, url: str) -> str:
        """Produce a canonical normalized URL.

        Rules:
        - Scheme and host lowercased.
        - Default ports (:80, :443) stripped.
        - Fragments (#...) removed.
        - Tracking parameters stripped; meaningful parameters kept and sorted.
        - Trailing slash standardized (stripped unless path is root '/').
        """
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

        # Normalize path
        path = unquote(parsed.path) or "/"
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")

        # Filter query parameters
        filtered_query: list[tuple[str, str]] = []
        if parsed.query:
            for k, v in parse_qsl(parsed.query, keep_blank_values=True):
                if k.lower() not in self.tracking_params:
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
