"""Pluggable embedding backend: fake (deterministic, CI-safe) + FastEmbed (local ONNX).

Contract: embed(texts) -> list[list[float]], dim fixed per provider+model.
Versioning: every embedding carries provider/model/version/dimension/input_hash;
stale rows (content changed) are marked STALE, model change => new rows.
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol

from django.conf import settings


class EmbeddingResult(Protocol):
    vectors: list[list[float]]


class FakeEmbeddingProvider:
    """Deterministic hash-bucket embeddings. No network, no model download."""

    name = "fake"

    def __init__(self, dimension: int = 128) -> None:
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dimension
            for token in (text or "").lower().split():
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                idx = int.from_bytes(digest[:4], "little") % self.dimension
                sign = 1.0 if digest[4] % 2 == 0 else -1.0
                vec[idx] += sign
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


class FastEmbedProvider:
    """Local multilingual ONNX inference via FastEmbed. Lazy-loaded, batched."""

    name = "fastembed"

    def __init__(self, model: str, batch_size: int = 32) -> None:
        self.model_name = model
        self.batch_size = batch_size
        self._model = None
        self._dim: int | None = None

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self.model_name)
        return self._model

    @property
    def dimension(self) -> int:
        if self._dim is None:
            model = self._load()
            probe = list(model.embed(["probe"]))
            self._dim = len(probe[0])
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        out: list[list[float]] = []
        batch: list[str] = []
        for text in texts:
            batch.append(text or "")
            if len(batch) >= self.batch_size:
                out.extend([list(map(float, v)) for v in model.embed(batch)])
                batch = []
        if batch:
            out.extend([list(map(float, v)) for v in model.embed(batch)])
        return out


def get_embedding_provider():
    provider = str(getattr(settings, "EMBEDDING_PROVIDER", "fake") or "fake").lower()
    if provider == "fastembed":
        return FastEmbedProvider(
            model=str(getattr(settings, "EMBEDDING_MODEL", "")),
            batch_size=int(getattr(settings, "EMBEDDING_BATCH_SIZE", 32) or 32),
        )
    dimension = int(getattr(settings, "EMBEDDING_DIMENSION", 128) or 128)
    return FakeEmbeddingProvider(dimension=dimension)


def embedding_identity() -> dict:
    provider = str(getattr(settings, "EMBEDDING_PROVIDER", "fake") or "fake").lower()
    if provider == "fastembed":
        return {
            "provider": "fastembed",
            "model": str(getattr(settings, "EMBEDDING_MODEL", "")),
            "model_version": str(getattr(settings, "EMBEDDING_MODEL_VERSION", "")),
            "dimension": int(getattr(settings, "EMBEDDING_DIMENSION", 384) or 384),
        }
    dimension = int(getattr(settings, "EMBEDDING_DIMENSION", 128) or 128)
    return {
        "provider": "fake",
        "model": "fake-hash-bucket",
        "model_version": "fake-v1",
        "dimension": dimension,
    }


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return round(max(-1.0, min(1.0, dot / (na * nb))), 4)


def centroid(vectors: list[list[float]], *, cap: int = 8) -> list[float] | None:
    """Capped mean over representative vectors (primary + newest-first)."""
    if not vectors:
        return None
    use = vectors[: max(1, cap)]
    dim = len(use[0])
    if not dim or any(len(vector) != dim for vector in use):
        return None
    mean = [sum(v[i] for v in use) / len(use) for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in mean)) or 1.0
    return [v / norm for v in mean]
