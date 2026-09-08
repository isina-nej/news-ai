"""Application Service. Owns prompt rendering, validation, cache, audit."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from pydantic import ValidationError as PydanticValidationError

from apps.ai import schemas
from apps.ai.models import AI_VERSION, AICallLog, AIResultCache
from apps.ai.prompts import get_prompt
from apps.ai.providers import AIProvider, get_provider, route_model
from apps.core.exceptions import AIProviderError
from apps.core.redaction import sanitize_error_message

logger = logging.getLogger(__name__)

TASKS = {
    "topic_classification": ("topics", "v1", schemas.TopicClassificationResult),
    "news_value": ("news_value", "v1", schemas.NewsValueResult),
    "conflict_detection": ("conflict", "v1", schemas.ConflictResult),
    "clustering_judge": ("clustering_judge", "v1", schemas.ClusteringJudgeResult),
    "material_update_detection": (
        "material_update",
        "v1",
        schemas.MaterialUpdateResult,
    ),
    "post_draft": ("post_draft", "v1", schemas.PostDraftResult),
}

SCHEMA_BY_TASK = {task: schema for task, (_, _, schema) in TASKS.items()}  # noqa: F841


def feature_enabled() -> bool:
    try:
        from apps.ops.models import FeatureFlag

        row = FeatureFlag.objects.filter(key="ENABLE_AI").first()
        if row is not None:
            return bool(row.enabled)
    except Exception:  # noqa: S110 — DB lookup fallback to settings
        pass
    return bool(getattr(settings, "FEATURE_FLAGS", {}).get("ENABLE_AI", True))


def input_hash(task: str, prompt_version: str, model: str, evidence: dict) -> str:
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{task}|{prompt_version}|{model}|{canonical}".encode()).hexdigest()


def render_prompt(name: str, version: str, replacements: dict[str, str]) -> str:
    template = get_prompt(name, version).text()
    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace(key, value)
    return rendered


def _parse_json(text: str) -> dict:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        parsed = json.loads(cleaned)
    except Exception as exc:
        raise AIProviderError("invalid_json") from exc
    if not isinstance(parsed, dict):
        raise AIProviderError("invalid_json_shape")
    return parsed


def _repair_json(text: str) -> str:
    cleaned = (text or "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        return cleaned[start : end + 1]
    return cleaned


def run_structured_task(
    *,
    task: str,
    evidence: dict,
    prompt_replacements: dict[str, str],
    model: str | None = None,
    provider: AIProvider | None = None,
    timeout: float | None = None,
) -> dict:
    if task not in TASKS:
        raise AIProviderError(f"unknown task {task}")
    prompt_name, prompt_version, schema_cls = TASKS[task]
    active = provider or get_provider()
    chosen_model = model or route_model(task)
    digest = input_hash(task, prompt_version, chosen_model, evidence)

    cached = (
        AIResultCache.objects.filter(
            task=task, prompt_version=prompt_version, model=chosen_model, input_hash=digest
        )
        .order_by("-created_at")
        .first()
    )
    if cached is not None and (cached.expires_at is None or cached.expires_at > timezone.now()):
        AICallLog.objects.create(
            provider=active.name,
            model=chosen_model,
            task=task,
            prompt_version=prompt_version,
            input_hash=digest,
            cached=True,
            status="cached",
        )
        return {
            "status": "cached",
            "payload": dict(cached.payload),
            "input_hash": digest,
            "model": chosen_model,
            "provider": active.name,
            "prompt_version": prompt_version,
        }

    prompt = render_prompt(prompt_name, prompt_version, prompt_replacements)
    started = time.monotonic()
    response = None
    error: Exception | None = None
    attempts = 1 + int(getattr(settings, "AI_MAX_RETRIES", 2) or 0)
    for attempt in range(max(1, attempts)):
        try:
            response = active.structured(
                task=task,
                prompt=prompt,
                model=chosen_model,
                timeout=timeout or float(getattr(settings, "AI_TIMEOUT", 30.0)),
            )
            error = None
            break
        except AIProviderError as exc:
            error = exc
            logger.warning(
                "AI %s attempt %s failed: %s",
                task,
                attempt + 1,
                sanitize_error_message(str(exc)),
            )
    latency_ms = int((time.monotonic() - started) * 1000)
    if error is not None or response is None:
        AICallLog.objects.create(
            provider=active.name,
            model=chosen_model,
            task=task,
            prompt_version=prompt_version,
            input_hash=digest,
            latency_ms=latency_ms,
            status="timeout" if "timeout" in str(error).lower() else "failure",
            error_type=sanitize_error_message(str(error))[:64],
        )
        raise error or AIProviderError("ai_failed")

    parsed: dict | None = None
    validation_error: Exception | None = None
    candidates = [response.text, _repair_json(response.text)]
    for candidate in candidates:
        try:
            parsed = schema_cls.model_validate(_parse_json(candidate)).model_dump()
            validation_error = None
            break
        except (AIProviderError, PydanticValidationError) as exc:
            validation_error = exc
    if parsed is None:
        AICallLog.objects.create(
            provider=active.name,
            model=chosen_model,
            task=task,
            prompt_version=prompt_version,
            input_hash=digest,
            latency_ms=latency_ms,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            status="invalid_response",
            error_type=sanitize_error_message(str(validation_error))[:64],
        )
        raise AIProviderError("invalid_response")

    ttl = int(getattr(settings, "AI_CACHE_TTL_SECONDS", 86400) or 86400)
    AIResultCache.objects.update_or_create(
        task=task,
        prompt_version=prompt_version,
        model=chosen_model,
        input_hash=digest,
        defaults={
            "payload": parsed,
            "algorithm_version": AI_VERSION,
            "expires_at": timezone.now() + timedelta(seconds=ttl),
        },
    )
    AICallLog.objects.create(
        provider=active.name,
        model=chosen_model,
        task=task,
        prompt_version=prompt_version,
        input_hash=digest,
        latency_ms=latency_ms,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        status="success",
    )
    return {
        "status": "success",
        "payload": parsed,
        "input_hash": digest,
        "model": chosen_model,
        "provider": active.name,
        "prompt_version": prompt_version,
    }


def to_decimal(value: float) -> Decimal:
    return Decimal(str(max(0.0, min(1.0, float(value))))).quantize(Decimal("0.0001"))
