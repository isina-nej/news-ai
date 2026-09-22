"""Media validation service: SSRF-safe URL validation and media integrity checks."""

from __future__ import annotations

from apps.news.models import MediaAsset, MediaValidationStatus
from apps.sources.adapters.ssrf import validate_url_for_ssrf

ALLOWED_MIME_PREFIXES = ("image/jpeg", "image/png", "image/webp", "image/gif")


class MediaValidationService:
    """Validates MediaAssets against SSRF, size limits, and invalid formats."""

    @classmethod
    def validate_asset(cls, asset: MediaAsset) -> bool:
        """Validate a single MediaAsset. Runs network checks outside DB transactions."""
        # 1. Platform native media ID (Telegram file_id) is locally valid
        if asset.platform_media_id and not asset.original_url:
            asset.validation_status = MediaValidationStatus.VALID
            asset.failure_reason = ""
            asset.save(update_fields=["validation_status", "failure_reason", "updated_at"])
            return True

        # 2. Remote URL checks
        url = asset.original_url or ""
        if not url:
            asset.validation_status = MediaValidationStatus.INVALID
            asset.failure_reason = "missing_url"
            asset.save(update_fields=["validation_status", "failure_reason", "updated_at"])
            return False

        # SSRF validation
        try:
            validate_url_for_ssrf(url)
        except Exception as exc:
            asset.validation_status = MediaValidationStatus.INVALID
            asset.failure_reason = f"ssrf_rejected:{str(exc)[:64]}"
            asset.save(update_fields=["validation_status", "failure_reason", "updated_at"])
            return False

        # Basic format checks
        if asset.mime_type and not any(
            asset.mime_type.startswith(p) for p in ALLOWED_MIME_PREFIXES
        ):
            asset.validation_status = MediaValidationStatus.INVALID
            asset.failure_reason = f"unsupported_mime:{asset.mime_type}"
            asset.save(update_fields=["validation_status", "failure_reason", "updated_at"])
            return False

        # Size check if known (> 10MB rejected)
        if asset.file_size and asset.file_size > 10 * 1024 * 1024:
            asset.validation_status = MediaValidationStatus.INVALID
            asset.failure_reason = "file_too_large"
            asset.save(update_fields=["validation_status", "failure_reason", "updated_at"])
            return False

        asset.validation_status = MediaValidationStatus.VALID
        asset.failure_reason = ""
        asset.save(update_fields=["validation_status", "failure_reason", "updated_at"])
        return True
