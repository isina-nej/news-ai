"""Provider abstraction behind Application Service. Domain never sees httpx."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from django.conf import settings

from apps.core.exceptions import AIProviderError
from apps.core.redaction import sanitize_error_message


@dataclass(frozen=True)
class AIResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


class AIProvider:
    name = "base"

    def generate(self, *, task: str, prompt: str, model: str, timeout: float) -> AIResponse:
        raise NotImplementedError

    def structured(self, *, task: str, prompt: str, model: str, timeout: float) -> AIResponse:
        return self.generate(task=task, prompt=prompt, model=model, timeout=timeout)


class FakeAIProvider(AIProvider):
    name = "fake"

    def __init__(self, *, mode: str = "valid", latency_ms: int = 0) -> None:
        self.mode = mode
        self.latency_ms = latency_ms
        self.calls = 0

    def generate(self, *, task: str, prompt: str, model: str, timeout: float) -> AIResponse:
        self.calls += 1
        if self.mode == "timeout":
            raise AIProviderError("fake timeout")
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000)
        digest = hashlib.sha256(f"{task}|{prompt}|{model}".encode()).hexdigest()
        seed = int(digest[:8], 16)
        if task == "topic_classification":
            payload = {
                "topic_slug": None,
                "subtopic_slug": None,
                "candidate_topic": "unclassified",
                "confidence": round(0.2 + (seed % 30) / 100, 4),
                "reason_codes": ["fake-low-signal"],
            }
        elif task == "news_value":
            base = 0.4 + (seed % 40) / 100
            payload = {
                key: {
                    "score": round(min(0.95, base + ((seed >> i) % 10) / 100), 4),
                    "confidence": 0.5,
                    "reason_codes": ["fake"],
                }
                for i, key in enumerate(
                    ["importance", "utility", "impact", "novelty", "urgency", "credibility"]
                )
            }
        elif task == "conflict_detection":
            payload = {
                "has_conflict": False,
                "summary": "",
                "confidence": 0.6,
                "reason_codes": ["fake-no-conflict"],
            }
        elif task == "clustering_judge":
            payload = {
                "label": "UNRELATED",
                "confidence": 0.4,
                "reason_codes": ["fake-conservative"],
            }
        elif task == "material_update_detection":
            payload = {
                "label": "NO_NEW_INFORMATION",
                "confidence": 0.55,
                "reason_codes": ["fake"],
            }
        elif task == "post_draft":
            payload = {
                "headline": "Evidence draft headline",
                "body": "Evidence draft body.",
                "used_sources": [],
            }
        else:
            payload = {"ok": True}
        if self.mode == "malformed":
            return AIResponse(text="{not-json", input_tokens=8, output_tokens=4)
        return AIResponse(text=json.dumps(payload), input_tokens=42, output_tokens=38)


class OpenAICompatibleProvider(AIProvider):
    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float,
        max_retries: int,
    ) -> None:
        if not base_url or not api_key:
            raise AIProviderError("AI base URL/API key are not configured")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max(0, max_retries)

    def generate(self, *, task: str, prompt: str, model: str, timeout: float) -> AIResponse:
        import httpx

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        attempt = 0
        while True:
            try:
                with httpx.Client(timeout=timeout) as client:
                    response = client.post(
                        f"{self.base_url}/chat/completions", json=payload, headers=headers
                    )
                response.raise_for_status()
                data = response.json()
                text = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                return AIResponse(
                    text=text,
                    input_tokens=int(usage.get("prompt_tokens", 0) or 0),
                    output_tokens=int(usage.get("completion_tokens", 0) or 0),
                )
            except Exception as exc:
                if attempt >= self.max_retries:
                    raise AIProviderError(sanitize_error_message(str(exc))) from exc
                attempt += 1


def get_provider(name: str | None = None) -> AIProvider:
    provider = (name or str(getattr(settings, "AI_PROVIDER", "fake"))).lower()
    if provider == "openai-compatible":
        return OpenAICompatibleProvider(
            base_url=str(getattr(settings, "AI_BASE_URL", "")),
            api_key=str(getattr(settings, "AI_API_KEY", "")),
            timeout=float(getattr(settings, "AI_TIMEOUT", 30.0)),
            max_retries=int(getattr(settings, "AI_MAX_RETRIES", 2)),
        )
    return FakeAIProvider()


def route_model(task: str) -> str:
    cheap = {
        "topic_classification",
        "content_type",
        "language_refinement",
        "basic_extraction",
    }
    if task in cheap:
        return str(getattr(settings, "AI_MODEL_CHEAP", getattr(settings, "AI_MODEL", "")))
    return str(getattr(settings, "AI_MODEL_STRONG", getattr(settings, "AI_MODEL", "")))
