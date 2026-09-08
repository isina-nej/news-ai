"""Clustering quality benchmark runner.

Supports:
- Default FakeEmbeddingProvider (offline regression / CI)
- Live embedding: multilingual MiniLM-L12-v2 or another FastEmbed model
- Sliced metrics: Overall, Persian/Persian, English/English, Persian/English
- Latency and memory reporting
- Strict prioritization: 1) False Merge 2) Precision 3) Recall
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
import django  # noqa: E402

django.setup()

from apps.stories.services.evaluation import evaluate_pairs, load_fixture  # noqa: E402


def run_benchmark(
    fixture_path: str = "tests/fixtures/clustering_benchmark.json",
    *,
    live_model: str | None = None,
    high: float | None = None,
    low: float | None = None,
) -> dict:
    pairs = load_fixture(fixture_path)
    total_pairs = len(pairs)
    print(f"Loaded {total_pairs} pairs from {fixture_path}")

    semantics = None
    model_name = "FakeEmbeddingProvider (hash-deterministic)"
    model_dim = 128
    latency_ms_per_pair = 0.0
    load_time = 0.0
    rss_before_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    if live_model:
        from fastembed import TextEmbedding

        print(f"Loading live embedding model: {live_model}...")
        start_load = time.perf_counter()
        cache_dir = os.environ.get("FASTEMBED_CACHE_PATH")
        embedder = TextEmbedding(model_name=live_model, cache_dir=cache_dir)
        load_time = time.perf_counter() - start_load
        print(f"Loaded model in {load_time:.2f}s")
        model_name = live_model

        from apps.stories.services.embeddings import cosine
        from apps.stories.services.representation import build_clustering_text

        a_texts = [build_clustering_text(p.get("a_title", ""), p.get("a_text", "")) for p in pairs]
        b_texts = [build_clustering_text(p.get("b_title", ""), p.get("b_text", "")) for p in pairs]

        t0 = time.perf_counter()
        a_vecs = list(embedder.embed(a_texts))
        b_vecs = list(embedder.embed(b_texts))
        duration = time.perf_counter() - t0

        model_dim = len(a_vecs[0]) if a_vecs else 384
        semantics = [cosine(list(a), list(b)) for a, b in zip(a_vecs, b_vecs, strict=True)]
        latency_ms_per_pair = (duration / (len(pairs) * 2)) * 1000.0

    overall = evaluate_pairs(pairs, high=high, low=low, semantics=semantics)

    # Sub-slice evaluations
    slices = {
        "English/English": [
            p
            for p in pairs
            if p.get("language") == "en"
            or (p.get("language") == "" and not any(ord(c) > 127 for c in p.get("a_title", "")))
        ],
        "Persian/Persian": [p for p in pairs if p.get("language") == "fa"],
        "Persian/English (cross)": [
            p
            for p in pairs
            if p.get("language") == ""
            or (p.get("language") == "cross")
            or (
                any(ord(c) > 127 for c in p.get("a_title", ""))
                != any(ord(c) > 127 for c in p.get("b_title", ""))
            )
        ],
    }

    slice_results = {}
    for name, slice_pairs in slices.items():
        if slice_pairs:
            # Map corresponding semantics
            sub_semantics = None
            if semantics is not None:
                indices = [pairs.index(p) for p in slice_pairs]
                sub_semantics = [semantics[i] for i in indices]
            slice_results[name] = evaluate_pairs(
                slice_pairs, high=high, low=low, semantics=sub_semantics
            )

    rss_after_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    report = {
        "model": model_name,
        "dimension": model_dim,
        "model_load_seconds": round(load_time, 3),
        "latency_ms_per_item": round(latency_ms_per_pair, 2),
        "peak_rss_mib": round(rss_after_kib / 1024, 2),
        "rss_growth_mib": round(max(0, rss_after_kib - rss_before_kib) / 1024, 2),
        "overall": overall,
        "slices": slice_results,
    }

    # Print summary
    print("\n" + "=" * 60)
    print(f"CLUSTERING BENCHMARK REPORT: {model_name}")
    print("=" * 60)
    print(f"Total Pairs: {overall['pairs']}")
    print(f"Precision:         {overall['precision']:.4f}")
    print(f"Recall:            {overall['recall']:.4f}")
    print(f"F1 Score:          {overall['f1']:.4f}")
    print(f"False Merge Rate:  {overall['false_merge_rate']:.4f} (Goal: 0.0)")
    print(f"False Split Rate:  {overall['false_split_rate']:.4f}")
    print(f"Decision Thresholds: high={overall['high']}, low={overall['low']}")

    print("\nSlice Performance:")
    for s_name, s_res in slice_results.items():
        print(
            f"  - {s_name:24} (N={s_res['pairs']:2}): "
            f"P={s_res['precision']:.2f}, R={s_res['recall']:.2f}, F1={s_res['f1']:.2f}, "
            f"F-Merge={s_res['false_merge_rate']:.2f}, F-Split={s_res['false_split_rate']:.2f}"
        )
    print("=" * 60 + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run clustering quality benchmark")
    parser.add_argument("--fixture", default="tests/fixtures/clustering_benchmark.json")
    parser.add_argument("--live-model", default=None, help="Hugging Face / FastEmbed model name")
    parser.add_argument("--high", type=float, default=None)
    parser.add_argument("--low", type=float, default=None)
    parser.add_argument("--output", type=Path, default=None, help="Write JSON report")
    args = parser.parse_args()

    result = run_benchmark(
        fixture_path=args.fixture,
        live_model=args.live_model,
        high=args.high,
        low=args.low,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
