"""Qdrant auxiliary vector index. MySQL is the source of truth, always.

- Point ids are deterministic: f\"item-{source_item_id}\".
- Qdrant holds vectors only; metadata mirrors MySQL ItemEmbedding rows.
- MySQL insert success never depends on Qdrant; indexing is eventual with
  PENDING -> INDEXED / FAILED / STALE states and a retry task.
- Full rebuild from MySQL (+ recompute) is always possible.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings


def point_id_for_item(source_item_id: int) -> str:
    return f"item-{int(source_item_id)}"


def _client():
    from qdrant_client import QdrantClient

    return QdrantClient(url=str(getattr(settings, "QDRANT_URL", "http://qdrant:6333")))


def collection_name() -> str:
    return str(getattr(settings, "QDRANT_COLLECTION", "newsai_items"))


def ensure_collection(dimension: int, *, distance: str = "Cosine") -> None:
    import logging

    from qdrant_client.http import models as qmodels

    client = _client()
    name = collection_name()
    try:
        client.get_collection(name)
        return
    except Exception as exc:  # noqa: S110 — missing collection is the expected path
        logging.getLogger(__name__).debug("qdrant collection missing, creating: %s", exc)
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
) -> None:
    from qdrant_client.http import models as qmodels

    client = _client()
    client.upsert(
        collection_name=collection_name(),
        points=[qmodels.PointStruct(id=point_id, vector=vector, payload=payload)],
    )


def search_points(
    *, vector: list[float], limit: int = 30, score_threshold: float | None = None
) -> list[dict[str, Any]]:
    client = _client()
    hits = client.search(
        collection_name=collection_name(),
        query_vector=vector,
        limit=max(1, min(int(limit), 100)),
        score_threshold=score_threshold,
        with_payload=True,
    )
    out: list[dict[str, Any]] = []
    for hit in hits:
        out.append(
            {
                "point_id": str(getattr(hit, "id", "")),
                "score": round(float(getattr(hit, "score", 0.0)), 4),
                "payload": dict(getattr(hit, "payload", {}) or {}),
            }
        )
    return out


def delete_points(point_ids: list[str]) -> None:
    if not point_ids:
        return
    from qdrant_client.http import models as qmodels

    _client().delete(
        collection_name=collection_name(),
        points_selector=qmodels.PointIdsList(points=point_ids),
    )
