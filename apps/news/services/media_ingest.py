"""Media ingestion service: harvests MediaAssets from SourceItems with provenance."""

from __future__ import annotations

import hashlib

from apps.news.models import MediaAsset, MediaValidationStatus, SourceItem


class MediaIngestionService:
    """Extracts and persists MediaAssets from SourceItem media payloads."""

    @classmethod
    def harvest_item_media(cls, source_item: SourceItem) -> list[MediaAsset]:
        """Inspect source_item.media and raw_payload to persist valid MediaAsset rows."""
        media_payload = source_item.media or {}
        created_assets: list[MediaAsset] = []

        # 1. Inspect direct media dict (e.g. from Telegram, RSS enclosure)
        url = media_payload.get("url") or media_payload.get("image_url") or ""
        file_id = media_payload.get("file_id") or ""
        kind = media_payload.get("kind") or "image"
        width = media_payload.get("width")
        height = media_payload.get("height")
        size = media_payload.get("file_size")
        mime = media_payload.get("mime_type") or (
            "image/jpeg" if kind in ("image", "photo") else ""
        )

        if url or file_id:
            # Deterministic hash for duplicate detection
            hash_input = f"{source_item.pk}:{url}:{file_id}"
            c_hash = hashlib.sha256(hash_input.encode()).hexdigest()

            asset, _ = MediaAsset.objects.get_or_create(
                source_item=source_item,
                content_hash=c_hash,
                defaults={
                    "original_url": url[:2048],
                    "platform_media_id": str(file_id)[:256],
                    "media_type": kind[:32],
                    "mime_type": mime[:64],
                    "width": width,
                    "height": height,
                    "file_size": size,
                    "provenance": {
                        "platform": source_item.source.platform,
                        "source_id": source_item.source_id,
                    },
                    "validation_status": MediaValidationStatus.PENDING,
                },
            )
            created_assets.append(asset)

        return created_assets
