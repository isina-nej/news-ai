"""Phase 5: strict Pydantic outputs for machine-consumed AI results."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

JUDGE_LABELS = ("SAME_EVENT", "MATERIAL_UPDATE", "RELATED_BUT_DISTINCT", "UNRELATED")
UPDATE_LABELS = (
    "NO_NEW_INFORMATION",
    "MINOR_UPDATE",
    "MATERIAL_UPDATE",
    "CORRECTION",
    "MAJOR_BREAKING_UPDATE",
)


def _clamp(value: float) -> float:
    if value != value:
        return 0.0
    return max(0.0, min(1.0, float(value)))


class ScoreWithConfidence(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("reason_codes")
    @classmethod
    def check_codes(cls, value: list[str]) -> list[str]:
        return [str(code)[:64] for code in (value or [])][:8]


class TopicClassificationResult(BaseModel):
    topic_slug: str | None = Field(default=None, max_length=64)
    subtopic_slug: str | None = Field(default=None, max_length=64)
    candidate_topic: str = Field(default="", max_length=128)
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list, max_length=8)


class NewsValueResult(BaseModel):
    importance: ScoreWithConfidence
    utility: ScoreWithConfidence
    impact: ScoreWithConfidence
    novelty: ScoreWithConfidence
    urgency: ScoreWithConfidence
    credibility: ScoreWithConfidence


class ConflictResult(BaseModel):
    has_conflict: bool
    summary: str = Field(default="", max_length=1000)
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list, max_length=8)


class ClusteringJudgeResult(BaseModel):
    label: Literal["SAME_EVENT", "MATERIAL_UPDATE", "RELATED_BUT_DISTINCT", "UNRELATED"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list, max_length=8)


class MaterialUpdateResult(BaseModel):
    label: Literal[
        "NO_NEW_INFORMATION",
        "MINOR_UPDATE",
        "MATERIAL_UPDATE",
        "CORRECTION",
        "MAJOR_BREAKING_UPDATE",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str] = Field(default_factory=list, max_length=8)


class PostDraftResult(BaseModel):
    headline: str = Field(max_length=250)
    body: str = Field(max_length=3800)
    used_sources: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("used_sources")
    @classmethod
    def check_sources(cls, value: list[str]) -> list[str]:
        return [str(url)[:2048] for url in (value or [])][:12]


class StoryIntelligenceResult(BaseModel):
    canonical_event: str = Field(default="", max_length=500)
    what_happened: str = Field(default="", max_length=2000)
    who: list[str] = Field(default_factory=list)
    where: list[str] = Field(default_factory=list)
    when: str = Field(default="", max_length=255)
    why_it_matters: str = Field(default="", max_length=1500)
    confirmed_facts: list[str] = Field(default_factory=list)
    uncertain_claims: list[str] = Field(default_factory=list)
    conflicting_claims: list[str] = Field(default_factory=list)
    new_information: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    importance: float = Field(ge=0.0, le=1.0, default=0.5)
    impact: float = Field(ge=0.0, le=1.0, default=0.5)
    utility: float = Field(ge=0.0, le=1.0, default=0.5)
    novelty: float = Field(ge=0.0, le=1.0, default=0.5)
    urgency: float = Field(ge=0.0, le=1.0, default=0.5)
    credibility: float = Field(ge=0.0, le=1.0, default=0.5)
    editorial_risk: float = Field(ge=0.0, le=1.0, default=0.0)
    recommended_depth: str = Field(default="standard", max_length=32)
    recommended_tone: str = Field(default="neutral", max_length=32)
    reasoning_summary: str = Field(default="", max_length=500)


class EditorialDecisionResult(BaseModel):
    recommended_action: str = Field(max_length=64)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    primary_reason: str = Field(default="", max_length=500)
    risks: list[str] = Field(default_factory=list)
    missing_confirmation: list[str] = Field(default_factory=list)
    recommended_wait_minutes: int | None = None
    recommended_format: str = Field(default="STANDARD", max_length=32)


class StructuredDraftResult(BaseModel):
    headline_direct: str = Field(max_length=250)
    headline_breaking: str = Field(max_length=250)
    headline_contextual: str = Field(max_length=250)
    subheadline: str | None = Field(default=None, max_length=250)
    lead: str = Field(max_length=2000)
    body_points: list[str] = Field(default_factory=list)
    why_it_matters: str | None = Field(default=None, max_length=1500)
    context: str | None = Field(default=None, max_length=1500)
    update_line: str | None = Field(default=None, max_length=500)
    tone: str = Field(default="neutral", max_length=32)
    urgency_label: str | None = Field(default=None, max_length=32)


class DraftCriticResult(BaseModel):
    is_approved: bool = True
    severity: Literal["pass", "minor", "major", "reject"] = "pass"
    issue_codes: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    headline_exaggeration: bool = False
    persian_quality_score: float = Field(ge=0.0, le=1.0, default=1.0)
    repair_instructions: str = Field(default="", max_length=1000)
