"""Prompt registry. Single source of truth, no scattered prompts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class PromptSpec:
    name: str
    version: str
    task: str
    schema: str
    filename: str

    def text(self) -> str:
        return (PROMPT_DIR / self.filename).read_text(encoding="utf-8")


REGISTRY: dict[tuple[str, str], PromptSpec] = {}


def _register(spec: PromptSpec) -> None:
    REGISTRY[(spec.name, spec.version)] = spec


_register(
    PromptSpec("topics", "v1", "topic_classification", "TopicClassificationResult", "topics_v1.txt")
)
_register(PromptSpec("news_value", "v1", "news_value", "NewsValueResult", "news_value_v1.txt"))
_register(PromptSpec("conflict", "v1", "conflict_detection", "ConflictResult", "conflict_v1.txt"))
_register(
    PromptSpec(
        "clustering_judge", "v1", "clustering_judge", "ClusteringJudgeResult", "judge_v1.txt"
    )
)
_register(
    PromptSpec(
        "material_update",
        "v1",
        "material_update_detection",
        "MaterialUpdateResult",
        "material_update_v1.txt",
    )
)
_register(PromptSpec("post_draft", "v1", "post_draft", "PostDraftResult", "post_draft_v1.txt"))


def get_prompt(name: str, version: str) -> PromptSpec:
    return REGISTRY[(name, version)]


def latest(name: str) -> PromptSpec:
    cands = [spec for (n, _), spec in REGISTRY.items() if n == name]
    cands.sort(key=lambda s: s.version)
    return cands[-1]
