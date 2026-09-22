"""Newsworthiness calculation: intrinsic event significance independent of publishing timing."""

from __future__ import annotations

from apps.stories.models import Story

NEWSWORTHINESS_ALGORITHM_VERSION = "news-v1"

DEFAULT_NEWS_WEIGHTS = {
    "importance": 0.25,
    "impact": 0.20,
    "utility": 0.15,
    "novelty": 0.15,
    "urgency": 0.10,
    "credibility": 0.15,
}


class NewsworthinessService:
    """Evaluates the intrinsic news value of a Story (0..1)."""

    @classmethod
    def evaluate(cls, story: Story) -> tuple[float, dict[str, float]]:
        """Compute composite newsworthiness and 0..1 component breakdown."""
        nv_obj = getattr(story, "news_value", None)
        if nv_obj:
            comp = {
                "importance": float(nv_obj.importance),
                "impact": float(nv_obj.impact),
                "utility": float(nv_obj.utility),
                "novelty": float(nv_obj.novelty),
                "urgency": float(nv_obj.urgency),
                "credibility": float(nv_obj.credibility),
            }
        else:
            # Deterministic fallback when AI has not analyzed the story yet
            base_cred = min(1.0, 0.40 + 0.20 * min(3, story.independent_source_count))
            comp = {
                "importance": round(min(1.0, 0.40 + 0.15 * min(4, story.observed_source_count)), 4),
                "impact": 0.50,
                "utility": 0.50,
                "novelty": 0.70,
                "urgency": 0.50,
                "credibility": round(base_cred, 4),
            }

        score = sum(comp[k] * DEFAULT_NEWS_WEIGHTS[k] for k in DEFAULT_NEWS_WEIGHTS)
        return round(max(0.0, min(1.0, score)), 4), comp
