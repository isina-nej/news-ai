"""Offline pair-decision evaluation: precision/recall/F1 + false-merge/split rates.

Reads a labeled JSON fixture of {a_title, a_text, b_title, b_text, label}
where label in {same, different}. Runs the V1 fusion scorer and reports.

Semantic similarities for the benchmark come from the configured embedding
provider (fake provider by default in CI: deterministic hash-bucket vectors,
no download). Pass explicit `semantics=[...]` to override per-pair.
"""

from __future__ import annotations

import json

from apps.stories.services import lexical as lex
from apps.stories.services.embeddings import cosine, get_embedding_provider
from apps.stories.services.features import extract_pair_features
from apps.stories.services.representation import build_clustering_text, extract_metadata
from apps.stories.services.scoring import decide, evidence_veto, match_score, thresholds


def evaluate_pairs(
    pairs: list[dict],
    *,
    high: float | None = None,
    low: float | None = None,
    semantics: list[float | None] | None = None,
) -> dict:
    default_high, default_low, _ = thresholds()
    high = default_high if high is None else high
    low = default_low if low is None else low
    if semantics is None:
        try:
            provider = get_embedding_provider()
            a_vecs = provider.embed(
                [build_clustering_text(p.get("a_title", ""), p.get("a_text", "")) for p in pairs]
            )
            b_vecs = provider.embed(
                [build_clustering_text(p.get("b_title", ""), p.get("b_text", "")) for p in pairs]
            )
            semantics = [cosine(a, b) for a, b in zip(a_vecs, b_vecs, strict=True)]
        except Exception:
            semantics = [None] * len(pairs)
    tp = fp = tn = fn = 0
    false_merges: list[int] = []
    false_splits: list[int] = []
    for idx, pair in enumerate(pairs):
        a_meta = extract_metadata(pair.get("a_title", ""), pair.get("a_text", ""))
        b_meta = extract_metadata(pair.get("b_title", ""), pair.get("b_text", ""))
        semantic = (
            pair.get("semantic", semantics[idx] if idx < len(semantics) else None)
            if "semantic" not in pair
            else pair.get("semantic")
        )
        if "semantic" in pair and pair.get("semantic") is None and idx < len(semantics):
            semantic = semantics[idx]
        features = extract_pair_features(
            item_title=pair.get("a_title", ""),
            item_text=pair.get("a_text", ""),
            item_published=None,
            item_meta=a_meta,
            story_title=pair.get("b_title", ""),
            story_text=pair.get("b_text", ""),
            story_published=None,
            story_meta=b_meta,
            story_language=pair.get("language", ""),
            item_language=pair.get("language", ""),
            minhash_sim=None,
            semantic_sim=semantic,
        )
        veto = evidence_veto(
            features,
            item_meta=a_meta,
            story_meta=b_meta,
            item_title=pair.get("a_title", ""),
            item_text=pair.get("a_text", ""),
            story_title=pair.get("b_title", ""),
            story_text=pair.get("b_text", ""),
        )
        score, _ = match_score(features)
        if veto is not None:
            score = min(score, (low or 0.0) - 0.01)
        decision = decide(score, high=high, low=low)
        predicted_same = decision == "match"
        # Ambiguous counts as "not merged" (conservative): correct for
        # different-story pairs, a miss for same-story pairs.
        actual_same = pair.get("label") == "same"
        if predicted_same and actual_same:
            tp += 1
        elif predicted_same and not actual_same:
            fp += 1
            false_merges.append(idx)
        elif not predicted_same and not actual_same:
            tn += 1
        else:
            fn += 1
            false_splits.append(idx)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    total = tp + fp + tn + fn or 1
    return {
        "pairs": len(pairs),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "false_merge_rate": round(fp / total, 4),
        "false_split_rate": round(fn / total, 4),
        "false_merges": false_merges,
        "false_splits": false_splits,
        "high": high,
        "low": low,
    }


def load_fixture(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def lexical_gap(pairs: list[dict]) -> dict:
    """Sanity helper: RapidFuzz/MinHash similarity path smoke (unused by scorer directly)."""
    _ = lex.title_fuzzy("a", "b")
    return {"pairs": len(pairs)}
