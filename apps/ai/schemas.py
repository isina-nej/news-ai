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
