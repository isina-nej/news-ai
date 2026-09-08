"""Story merge + reassign services. Audit-safe, idempotent, history-preserving."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.ops.models import AuditLog
from apps.stories.models import ClusteringDecision, Story, StoryMembership, StoryStatus


def merge_stories(
    *, source_story_id: int, target_story_id: int, actor: str = "system", reason: str = ""
) -> dict:
    """Merge source story into target story. Idempotent, never hard-deletes."""
    if source_story_id == target_story_id:
        return {"status": "skipped", "reason": "same_story"}
    with transaction.atomic():
        source = Story.objects.select_for_update().get(pk=source_story_id)
        target = Story.objects.select_for_update().get(pk=target_story_id)
        if source.status == StoryStatus.MERGED and source.merged_into_id == target.pk:
            return {"status": "skipped", "reason": "already_merged"}
        if source.status == StoryStatus.MERGED:
            return {"status": "skipped", "reason": "source_already_merged_elsewhere"}
        # Move current memberships; skip pairs that already exist on target.
        moved = 0
        for membership in StoryMembership.objects.filter(story=source, is_current=True):
            item = membership.source_item
            if StoryMembership.objects.filter(story=target, source_item=item).exists():
                membership.is_current = False
                membership.save(update_fields=["is_current", "current_slot", "updated_at"])
                continue
            # Flip current assignment to target (single-current invariant holds
            # because the source-side row is retired first).
            membership.is_current = False
            membership.save(update_fields=["is_current", "current_slot", "updated_at"])
            StoryMembership.objects.create(
                story=target,
                source_item=item,
                similarity_score=membership.similarity_score,
                match_method=membership.match_method,
                independence=membership.independence,
                independence_score=membership.independence_score,
                is_current=True,
                detail={**(membership.detail or {}), "merged_from": source.pk},
            )
            item.story = target
            item.save(update_fields=["story", "updated_at"])
            moved += 1
        source.status = StoryStatus.MERGED
        source.merged_into = target
        source.save(update_fields=["status", "merged_into", "updated_at"])
        ClusteringDecision.objects.create(
            source_item=target.primary_item or membership.source_item,
            candidate_story=target,
            decision="merge",
            match_score=None,
            feature_snapshot={"from_story": source.pk, "moved": moved, "reason": reason},
            algorithm_version="cluster-v1",
            threshold_version="",
            method="merge",
        )
        AuditLog.objects.create(
            actor=actor,
            action="story.merge",
            entity_type="story",
            entity_id=str(source.pk),
            detail={"from": source.pk, "to": target.pk, "moved": moved, "reason": reason},
        )
        # Refresh target stats via clustering service helper (no import cycle at runtime).
        from apps.stories.services.clustering import story_clustering_service

        story_clustering_service._refresh_story_stats(target)
        target.latest_source_update_at = timezone.now()
        target.save(update_fields=["latest_source_update_at", "updated_at"])
        return {"status": "merged", "from": source.pk, "to": target.pk, "moved": moved}


def reassign_item(
    *, source_item_id: int, target_story_id: int, actor: str = "system", reason: str = ""
) -> dict:
    """Move one item's current assignment to another story. Audit-safe."""
    from apps.news.models import SourceItem

    with transaction.atomic():
        item = SourceItem.objects.select_for_update().get(pk=source_item_id)
        target = Story.objects.select_for_update().get(pk=target_story_id)
        current = item.memberships.filter(is_current=True).first()
        if current is not None and current.story_id == target.pk:
            return {"status": "skipped", "reason": "already_assigned"}
        old_story_id = current.story_id if current else None
        if current is not None:
            current.is_current = False
            current.save(update_fields=["is_current", "current_slot", "updated_at"])
        StoryMembership.objects.create(
            story=target,
            source_item=item,
            match_method="manual",
            is_current=True,
            detail={"reason": reason or "reassign", "from_story": old_story_id},
        )
        old_story = None
        if old_story_id:
            old_story = Story.objects.select_for_update().get(pk=old_story_id)
        item.story = target
        item.save(update_fields=["story", "updated_at"])
        ClusteringDecision.objects.create(
            source_item=item,
            candidate_story=target,
            decision="reassign",
            match_score=None,
            feature_snapshot={"from_story": old_story_id, "reason": reason},
            algorithm_version="cluster-v1",
            threshold_version="",
            method="reassign",
        )
        AuditLog.objects.create(
            actor=actor,
            action="story.reassign",
            entity_type="source_item",
            entity_id=str(item.pk),
            detail={"from_story": old_story_id, "to_story": target.pk, "reason": reason},
        )
        from apps.stories.services.clustering import story_clustering_service

        story_clustering_service._refresh_story_stats(target)
        if old_story is not None:
            story_clustering_service._refresh_story_stats(old_story)
        return {"status": "reassigned", "item": item.pk, "to": target.pk}
