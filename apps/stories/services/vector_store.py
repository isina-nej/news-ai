"""Qdrant auxiliary vector index. MySQL is the source of truth, always.

- Point ids are deterministic: f\"item-{source_item_id}\".
- Qdrant holds vectors only; metadata mirrors MySQL ItemEmbedding rows.
- MySQL insert success never depends on Qdrant; indexing is eventual with
  PENDING -> INDEXED / FAILED / STALE states and a retry task.
- Full rebuild from MySQL (+ recompute) is always possible.
"""

from __future__ import annotations

import hashlib
from typing import Any

from django.conf import settings


def point_id_for_item(source_item_id: int) -> str:
    return f"item-{int(source_item_id)}"


def _client():
    from qdrant_client import QdrantClient

    return QdrantClient(url=str(getattr(settings, "QDRANT_URL", "http://qdrant:6333")))


def collection_name(
    *,
    dimension: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    model_version: str | None = None,
) -> str:
    """Return a collection isolated by the complete embedding identity.

    New reads/writes require identity. Calling without identity returns only the
    configured base name for diagnostics; production vector operations reject it.
    """
    base = str(getattr(settings, "QDRANT_COLLECTION", "newsai_items"))
    if dimension is None or not model:
        return base
    identity = "|".join((provider or "unknown", model, model_version or "unknown", str(dimension)))
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(model).split("/")[-1])
    slug = "-".join(filter(None, slug.split("-")))[:48] or "model"
    return f"{base}__{slug}__d{int(dimension)}__{digest}"


def _require_identity(provider: str | None, model: str | None, model_version: str | None) -> None:
    if not provider or not model or not model_version:
        raise ValueError("provider, model and model_version are required for Qdrant isolation")


def ensure_collection(
    dimension: int,
    *,
    provider: str,
    model: str,
    model_version: str,
    distance: str = "Cosine",
) -> None:
    import logging

    from qdrant_client.http import models as qmodels

    _require_identity(provider, model, model_version)
    client = _client()
    name = collection_name(
        dimension=dimension,
        provider=provider,
        model=model,
        model_version=model_version,
    )
    try:
        info = client.get_collection(name)
    except Exception as exc:  # noqa: S110 — missing collection is the expected path
        logging.getLogger(__name__).debug("qdrant collection missing, creating: %s", exc)
    else:
        configured = getattr(getattr(info, "config", None), "params", None)
        vectors = getattr(configured, "vectors", None)
        size = getattr(vectors, "size", dimension)
        if int(size) != int(dimension):
            raise ValueError(f"Qdrant collection {name} has dimension {size}, expected {dimension}")
        return
    client.create_collection(
        collection_name=name,
        vectors_config=qmodels.VectorParams(
            size=dimension, distance=getattr(qmodels.Distance, distance.upper(), "COSINE")
        ),
    )


def upsert_point(
    *,
    point_id: str,
    vector: list[float],
    payload: dict[str, Any],
    provider: str,
    model: str,
    model_version: str,
) -> None:
    from qdrant_client.http import models as qmodels

    _require_identity(provider, model, model_version)
    name = collection_name(
        dimension=len(vector),
        provider=provider,
        model=model,
        model_version=model_version,
    )
    identity_payload = {
        **payload,
        "embedding_provider": provider,
        "embedding_model": model,
        "embedding_model_version": model_version,
        "embedding_dimension": len(vector),
    }
    _client().upsert(
        collection_name=name,
        points=[qmodels.PointStruct(id=point_id, vector=vector, payload=identity_payload)],
    )


def search_points(
    *,
    vector: list[float],
    provider: str,
    model: str,
    model_version: str,
    limit: int = 30,
    score_threshold: float | None = None,
) -> list[dict[str, Any]]:
    _require_identity(provider, model, model_version)
    name = collection_name(
        dimension=len(vector),
        provider=provider,
        model=model,
        model_version=model_version,
    )
    hits = _client().search(
        collection_name=name,
        query_vector=vector,
        limit=max(1, min(int(limit), 100)),
        score_threshold=score_threshold,
        with_payload=True,
    )
    out: list[dict[str, Any]] = []
    for hit in hits:
        payload = dict(getattr(hit, "payload", {}) or {})
        if (
            payload.get("embedding_provider") != provider
            or payload.get("embedding_model") != model
            or payload.get("embedding_model_version") != model_version
            or payload.get("embedding_dimension") != len(vector)
        ):
            continue
        out.append(
            {
                "point_id": str(getattr(hit, "id", "")),
                "score": round(float(getattr(hit, "score", 0.0)), 4),
                "payload": payload,
            }
        )
    return out


def delete_points(
    point_ids: list[str],
    *,
    dimension: int,
    provider: str,
    model: str,
    model_version: str,
) -> None:
    if not point_ids:
        return
    from qdrant_client.http import models as qmodels

    _require_identity(provider, model, model_version)
    _client().delete(
        collection_name=collection_name(
            dimension=dimension,
            provider=provider,
            model=model,
            model_version=model_version,
        ),
        points_selector=qmodels.PointIdsList(points=point_ids),
    )
