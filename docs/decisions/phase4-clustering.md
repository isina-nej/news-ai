# ADR: Phase 4 Story Detection / Clustering

Date: 2026-09-08. Status: accepted.

## Research (verified 2026-09-08 via PyPI + GitHub releases)

- RapidFuzz 3.14.6 (MIT, requires-python >=3.11, wheels incl. cp312, CI green).
  C++ Levenshtein core; `fuzz.WRatio/token_set_ratio/partial_ratio` used.
- datasketch 2.0.0 (MIT, requires-python >=3.9, released 2026-07-05).
  BREAKING: default MinHash permutation scheme changed to `affine32` (fixes
  similarity over-estimation bias, halves memory, ~4x faster updates; hash
  values differ from 1.x). 64-bit `affine64` exists; `scheme="legacy"`
  interoperates with old data. Consequence: we pin and persist
  `num_perm=128, scheme=affine32` on every sketch row and reject silent mixing.
- FastEmbed 0.8.0 (Apache-2.0, requires-python >=3.10, 2026-03-23). Local ONNX
  inference, no server needed; supports both shortlisted multilingual models
  (`paraphrase-multilingual-MiniLM-L12-v2`, `multilingual-e5-large`).
  Default production model: MiniLM-L12-v2 (384d, fast); e5-large is a config
  switch when quality demands it.
- qdrant-client 1.19.0 (Apache-2.0, 2026-08-04) against server v1.19.1
  (2026-09-04). Compose pins `qdrant:v1.19.1`; CI runs the same image.
- lingua-language-detector 2.2.0 (requires-python >=3.12, 2026-03-09):
  FST-backed models, 8 languages loaded, short-text/low-confidence safe.

Sources: [RapidFuzz](https://pypi.org/project/rapidfuzz/) · [datasketch releases](https://github.com/ekzhu/datasketch/releases) · [FastEmbed releases](https://github.com/qdrant/fastembed/releases) · [Qdrant releases](https://github.com/qdrant/qdrant/releases) · [lingua-py releases](https://github.com/pemistahl/lingua-py/releases)

## Decisions

1. **No from-scratch fuzzy/embeddings.** RapidFuzz + datasketch + FastEmbed
   cover lexical, near-dup and semantic layers; lingua covers language.
2. **Qdrant is auxiliary.** MySQL owns truth (`ItemEmbedding` metadata rows);
   vectors live only in Qdrant with deterministic `item-{pk}` ids. Indexing is
   eventual (pending/indexed/failed/stale + retry task); assignment never
   fails on Qdrant outage.
3. **MinHash persistence is versioned ints, never pickle.** `num_perm` +
   `scheme` stored per row; mismatched rows are recomputed, not compared.
4. **Conservative matching wins.** Three-band thresholds (match/ambiguous/new)
   with ambiguous -> new story + decision row for the Phase 5 AI judge. False
   merge is costlier than false split (merge is recoverable via merge service,
   but a bad merge can poison ranking/publication first).
5. **Centroid = capped mean (<=8).** Cheap, stable, deterministic; full
   re-embed per write is O(n) with no gain at current scale. Documented in
   `docs/clustering.md`.
6. **Copy-network v1 is signal-based, UNKNOWN-first.** Forward origin or
   near-identical+signal -> `likely_copy`; single signal -> `unknown` (never
   forced independent); paraphrase -> `independent`. Counts derive from
   labels with `indep-v1` recorded.
7. **Benchmark is offline and fake-provider based.** 30 labeled pairs ship in
   `tests/fixtures/clustering_benchmark.json`; cross-language pairs keep
   shared latin anchors so the fake hash-bucket provider (no multilingual
   understanding) can exercise the pipeline. Real-model quality is validated
   by the opt-in `embedding_live` marker, never in CI.
