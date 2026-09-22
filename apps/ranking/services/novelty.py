"""Channel novelty service: semantic and fact-based comparison against prior publications."""

from __future__ import annotations

from typing import Any

from apps.ai.models import MaterialUpdateDecision
from apps.publishing.models import Publication, PublicationStatus
from apps.stories.models import Story


class ChannelNoveltyType:
    NEW_STORY = "NEW_STORY"
    MATERIAL_UPDATE = "MATERIAL_UPDATE"
    CORRECTION = "CORRECTION"
    MINOR_UPDATE = "MINOR_UPDATE"
    REPEAT = "REPEAT"


class ChannelNoveltyService:
    """Evaluates whether the channel audience has already seen this information."""

    @classmethod
    def evaluate_novelty(cls, story: Story) -> tuple[str, dict[str, Any]]:
        """Return (novelty_type, detail_dict)."""
        existing_pubs = Publication.objects.filter(
            story=story,
            status__in=[
                PublicationStatus.PUBLISHED,
                PublicationStatus.PUBLISHING,
                PublicationStatus.SCHEDULED,
            ],
        ).order_by("-publication_version")

        if not existing_pubs.exists():
            return ChannelNoveltyType.NEW_STORY, {
                "reason": "first_time_story",
                "prior_publications": 0,
            }

        latest_pub = existing_pubs.first()
        # Prior publication exists — check for verified material updates
        latest_decision = (
            MaterialUpdateDecision.objects.filter(story=story).order_by("-created_at").first()
        )
        if latest_decision is None:
            return ChannelNoveltyType.REPEAT, {
                "reason": "already_published_no_material_decision",
                "latest_publication_id": latest_pub.pk,
            }

        label = (latest_decision.label or "").upper()
        diff = latest_decision.information_unit_diff or {}
        new_facts = diff.get("new_facts", [])

        if label == "CORRECTION":
            return ChannelNoveltyType.CORRECTION, {
                "reason": "correction_declared",
                "latest_publication_id": latest_pub.pk,
                "confidence": float(latest_decision.confidence),
            }

        if label in ("MATERIAL_UPDATE", "MAJOR_BREAKING_UPDATE") or len(new_facts) > 0:
            return ChannelNoveltyType.MATERIAL_UPDATE, {
                "reason": f"material_update_{label.lower()}",
                "latest_publication_id": latest_pub.pk,
                "new_facts_count": len(new_facts),
                "confidence": float(latest_decision.confidence),
            }

        if label == "MINOR_UPDATE":
            return ChannelNoveltyType.MINOR_UPDATE, {
                "reason": "minor_update_without_material_facts",
                "latest_publication_id": latest_pub.pk,
            }

        return ChannelNoveltyType.REPEAT, {
            "reason": "duplicate_content_no_new_information",
            "latest_publication_id": latest_pub.pk,
        }
