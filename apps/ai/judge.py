"""Phase 5 judge application. Conservative, versioned, reversible."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from apps.ai.analysis import judge_ambiguous_candidate
from apps.ai.models import AI_VERSION, ClusteringJudgeDecision
from apps.ai.service import to_decimal
from apps.stories.models import ClusteringDecision, MatchMethod, StoryMembership

APPLY_CONFIDENCE = Decimal("0.80")


def apply_judge_to_decision(decision_id: int, *, provider=None) -> dict:
    decision = ClusteringDecision.objects.select_related("source_item", "candidate_story").get(
        pk=decision_id
    )
    if decision.decision != "ambiguous" or decision.candidate_story_id is None:
        return {"status": "skipped", "reason": "not_ambiguous"}
    result = judge_ambiguous_candidate(
        source_item=decision.source_item,
        candidate_story=decision.candidate_story,
        decision=decision,
        provider=provider,
    )
    payload = result["payload"]
    with transaction.atomic():
        judge_row = ClusteringJudgeDecision.objects.create(
            source_item=decision.source_item,
            candidate_story=decision.candidate_story,
            decision=decision,
            label=payload["label"],
            confidence=to_decimal(payload.get("confidence", 0)),
            applied=False,
            provider=result.get("provider", provider.name if provider else "fake"),
            model=result.get("model", ""),
            prompt_version=result.get("prompt_version", "v1"),
            algorithm_version=AI_VERSION,
            reason_codes=payload.get("reason_codes", []),
        )
        # Only high-confidence SAME_EVENT/MATERIAL_UPDATE merges are applied.
        # Everything else remains split and fully reversible via membership delete.
        if (
            payload["label"] in ("SAME_EVENT", "MATERIAL_UPDATE")
            and judge_row.confidence >= APPLY_CONFIDENCE
        ):
            item = decision.source_item
            story = decision.candidate_story
            if not item.memberships.filter(is_current=True).exists():
                membership = StoryMembership.objects.create(
                    story=story,
                    source_item=item,
                    similarity_score=judge_row.confidence,
                    match_method=MatchMethod.AI_VERIFIED,
                    independence="unknown",
                    independence_score=Decimal("0.5"),
                    is_current=True,
                    detail={"ai_judge": judge_row.pk, "algorithm_version": AI_VERSION},
                )
                item.story = story
                item.save(update_fields=["story", "updated_at"])
                judge_row.applied = True
                judge_row.save(update_fields=["applied", "updated_at"])
                return {
                    "status": "applied",
                    "judge_id": judge_row.pk,
                    "membership_id": membership.pk,
                }
    return {"status": "held", "judge_id": judge_row.pk}
