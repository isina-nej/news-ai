"""MediaSelectionService: scores and selects the most relevant, high-resolution media for a story."""

from __future__ import annotations

from typing import Any

from apps.news.models import MediaAsset, MediaValidationStatus
from apps.news.services.media_validation import MediaValidationService
from apps.stories.models import Story

SELECTION_THRESHOLD = 0.50


class MediaSelectionService:
    """Selects the single best MediaAsset for publication, or None for text-only fallback."""

    @classmethod
    def select_media_for_story(cls, story: Story) -> tuple[MediaAsset | None, dict[str, Any]]:
        """Return (selected_asset, selection_audit_dict)."""
        candidate_assets = list(
            MediaAsset.objects.filter(
                source_item__memberships__story=story,
                source_item__memberships__is_current=True,
            ).select_related("source_item__source")
        )

        if not candidate_assets:
            return None, {"reason": "no_candidates", "candidate_count": 0}

        scored_candidates: list[tuple[MediaAsset, float, dict[str, Any]]] = []

        for asset in candidate_assets:
            # Validate if still pending
            if asset.validation_status == MediaValidationStatus.PENDING:
                MediaValidationService.validate_asset(asset)

            if asset.validation_status != MediaValidationStatus.VALID:
                continue

            score = 0.50
            reasons = []

            # 1. Resolution
            w = asset.width or 0
            if w >= 800:
                score += 0.20
                reasons.append("high_resolution")
            elif w >= 500:
                score += 0.10
                reasons.append("standard_resolution")
            elif 0 < w < 250:
                score -= 0.35
                reasons.append("thumbnail_penalty")

            # 2. Aspect Ratio (landscape 16:9 / 4:3 preferred)
            ar = asset.aspect_ratio or (w / (asset.height or 1) if w and asset.height else 1.0)
            if 1.30 <= ar <= 1.90:
                score += 0.20
                reasons.append("optimal_aspect_ratio")
            elif ar < 0.6 or ar > 2.5:
                score -= 0.30
                reasons.append("extreme_aspect_ratio_penalty")

            # 3. Primary source preference
            if story.primary_item_id == asset.source_item_id:
                score += 0.10
                reasons.append("primary_source_bonus")

            final_score = round(max(0.0, min(1.0, score)), 4)
            scored_candidates.append(
                (asset, final_score, {"score": final_score, "reasons": reasons})
            )

        if not scored_candidates:
            return None, {"reason": "no_valid_candidates", "candidate_count": len(candidate_assets)}

        # Filter candidates above threshold
        passed_candidates = [
            (asset, score, detail)
            for asset, score, detail in scored_candidates
            if score >= SELECTION_THRESHOLD
        ]
        if not passed_candidates:
            return None, {
                "reason": "best_score_below_threshold",
                "best_score": scored_candidates[0][1],
                "threshold": SELECTION_THRESHOLD,
            }

        best_asset, best_score, best_detail = passed_candidates[0]
        selected_assets = [asset for asset, _, _ in passed_candidates]

        audit = {
            "selected_asset_id": best_asset.pk,
            "selected_asset_ids": [a.pk for a in selected_assets],
            "score": best_score,
            "detail": best_detail,
            "candidate_count": len(scored_candidates),
            "valid_count": len(passed_candidates),
        }
        return best_asset, audit

    @classmethod
    def select_media_group_for_story(
        cls, story: Story, max_items: int = 5
    ) -> tuple[list[MediaAsset], dict[str, Any]]:
        """Return all valid, high-quality MediaAssets (up to max_items) for an album."""
        primary_asset, audit = cls.select_media_for_story(story)
        if not primary_asset:
            return [], audit

        selected_ids = audit.get("selected_asset_ids", [primary_asset.pk])
        assets = list(
            MediaAsset.objects.filter(pk__in=selected_ids[:max_items]).order_by(
                "-width", "-height", "pk"
            )
        )
        return assets, audit
