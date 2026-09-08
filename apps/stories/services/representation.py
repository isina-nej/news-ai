"""Deterministic clustering text representation + cheap metadata extraction.

No LLM, no heavy NLP. All outputs are pure functions of SourceItem fields so
results are replayable and auditable.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

CLUSTERING_TEXT_VERSION = "ct-v1"
CLUSTERING_TITLE_CHARS = 500
CLUSTERING_BODY_CHARS = 2000

RE_URL = re.compile(r"https?://[^\s<>\"]+|www\.[^\s<>\"]+", re.IGNORECASE)
RE_HASHTAG = re.compile(r"#([\w؀-ۿ]+)", re.UNICODE)
RE_MENTION = re.compile(r"@([\w؀-ۿ]{2,64})", re.UNICODE)
RE_NUMBER = re.compile(r"\d+(?:[.,/]\d+)*")
RE_CURRENCY = re.compile(
    r"(?:[$€£¥₹₽₺]|USD|EUR|GBP|IRR|IRT|تومان|ریال|دلار|یورو)\s?\d[\d.,/]*"
    r"|\d[\d.,/]*\s?(?:USD|EUR|GBP|IRR|IRT|تومان|ریال|دلار|یورو)",
    re.IGNORECASE,
)


def build_clustering_text(title: str, normalized_text: str) -> str:
    """Deterministic representation for embedding + MinHash.

    Title (bounded) + head of body. Telegram posts send full text up to the
    safe limit; long web articles send the important first portion.
    """
    head_title = (title or "").strip()[:CLUSTERING_TITLE_CHARS]
    head_body = (normalized_text or "").strip()[:CLUSTERING_BODY_CHARS]
    if head_title and head_body:
        return f"{head_title}\n\n{head_body}"
    return head_title or head_body


def clustering_input_hash(title: str, normalized_text: str) -> str:
    import hashlib

    rep = build_clustering_text(title, normalized_text)
    return hashlib.sha256(rep.encode("utf-8")).hexdigest()


def _domains(urls: list[str]) -> list[str]:
    out: list[str] = []
    for url in urls:
        try:
            host = (urlparse(url).hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            if host and host not in out:
                out.append(host)
        except Exception as exc:  # noqa: S112 — skip unparseable URL
            import logging

            logging.getLogger(__name__).debug("domain parse failed: %s", exc)
            continue
    return out


def extract_metadata(title: str, normalized_text: str) -> dict:
    """Cheap deterministic metadata: urls, domains, hashtags, mentions, numbers."""
    blob = f"{title or ''}\n{normalized_text or ''}"
    urls = RE_URL.findall(blob)
    hashtags = [tag.lower() for tag in RE_HASHTAG.findall(blob)][:20]
    mentions = [m.lower() for m in RE_MENTION.findall(blob)][:20]
    numbers = RE_NUMBER.findall(blob)[:40]
    currencies = RE_CURRENCY.findall(blob)[:20]
    media_present = bool(urls)
    return {
        "urls": urls[:20],
        "domains": _domains(urls),
        "hashtags": sorted(set(hashtags)),
        "mentions": sorted(set(mentions)),
        "numbers": numbers,
        "currencies": currencies,
        "content_length": len(blob),
        "media_present": media_present,
        "clustering_text_version": CLUSTERING_TEXT_VERSION,
    }
