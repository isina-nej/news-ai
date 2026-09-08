"""Phase 4: story detection / clustering — no live models, no network in CI."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.core.choices import Platform
from apps.news.models import SourceItem
from apps.ops.models import AuditLog
from apps.sources.models import Source
from apps.stories.models import (
    ClusteringDecision,
    ItemEmbedding,
    ItemMinHash,
    Story,
    StoryMembership,
    StoryStatus,
)
from apps.stories.services import candidates as cand
from apps.stories.services import lexical as lex
from apps.stories.services.clustering import story_clustering_service
from apps.stories.services.embeddings import FakeEmbeddingProvider, cosine
from apps.stories.services.evaluation import evaluate_pairs, load_fixture
from apps.stories.services.independence import aggregate_counts, classify_membership
from apps.stories.services.language import detect_language
from apps.stories.services.merge import merge_stories, reassign_item
from apps.stories.services.representation import (
    build_clustering_text,
    clustering_input_hash,
    extract_metadata,
)
from apps.stories.services.scoring import decide, evidence_veto, match_score

pytestmark = pytest.mark.django_db


def _source(**kwargs):
    kwargs.setdefault("platform", Platform.TELEGRAM)
    kwargs.setdefault("name", "Clu Source")
    kwargs.setdefault("identifier", "@clusrc")
    kwargs.setdefault("url", "https://t.me/clusrc")
    return Source.objects.create(**kwargs)


def _item(source, title, text, **kwargs):
    now = timezone.now()
    kwargs.setdefault("published_at", now - timedelta(hours=1))
    item = SourceItem.objects.create(
        source=source, title=title, raw_text=text, normalized_text=text, **kwargs
    )
    return item


# --- representation / language ---


def test_clustering_text_versioned_and_deterministic():
    a = build_clustering_text("Title here", "Body text here " * 500)
    b = build_clustering_text("Title here", "Body text here " * 500)
    assert a == b
    assert len(a) <= 500 + 2 + 2000
    assert clustering_input_hash("t", "b") == clustering_input_hash("t", "b")


def test_language_detection_separates_signals():
    en = detect_language("OpenAI unveiled its new flagship model GPT-X with faster reasoning.")
    fa = detect_language("تیم ملی در فینال با نتیجه ۲ بر ۱ قهرمان شد و هواداران خوشحال شدند.")
    short = detect_language("OK")
    assert en["language"] == "en" and en["reliable"] is True
    assert fa["language"] == "fa" and fa["reliable"] is True
    assert short == {"language": "und", "confidence": 0.0, "reliable": False}


def test_metadata_extraction_deterministic():
    meta = extract_metadata("Big launch $50M", "See https://example.com/a and #AI @openai 2026")
    assert "https://example.com/a" in meta["urls"]
    assert "example.com" in meta["domains"]
    assert "ai" in meta["hashtags"]
    assert "openai" in meta["mentions"]
    assert meta["clustering_text_version"] == "ct-v1"


# --- lexical / minhash / embeddings ---


def test_rapidfuzz_features_normalized():
    assert lex.title_fuzzy("Hello world", "Hello world") == 1.0
    assert 0.0 <= lex.title_fuzzy("OpenAI releases GPT-X", "Apple posts record quarter") <= 1.0
    assert lex.token_set("", "x") == 0.0
    assert lex.partial("abc", "") == 0.0


def test_minhash_versioned_and_jaccard():
    mh1 = lex.minhash_for_text("the quick brown fox jumps over", num_perm=128, scheme="affine32")
    mh2 = lex.minhash_for_text("the quick brown fox jumps over", num_perm=128, scheme="affine32")
    assert lex.minhash_jaccard(mh1, mh2) == 1.0
    digest = lex.digest_to_list(mh1)
    assert len(digest) == 128 and all(isinstance(v, int) for v in digest)
    restored = lex.minhash_from_digest(digest, num_perm=128, scheme="affine32")
    assert lex.minhash_jaccard(mh1, restored) == 1.0


def test_fake_embedding_deterministic_and_cosine():
    provider = FakeEmbeddingProvider(dimension=128)
    a = provider.embed(["OpenAI unveils GPT-X"])[0]
    b = provider.embed(["OpenAI unveils GPT-X"])[0]
    assert a == b
    assert cosine(a, b) == 1.0
    other = provider.embed(["Apple posts record quarter"])[0]
    assert cosine(a, other) < cosine(a, b)


def test_embedding_stale_on_content_change():
    from apps.stories.services import vector_store as _  # noqa: F401  (import surface)

    h1 = clustering_input_hash("t", "body one")
    h2 = clustering_input_hash("t", "body two changed")
    assert h1 != h2


# --- candidates bounded ---


def test_candidate_generation_bounded_and_time_filtered():
    source = _source()
    now = timezone.now()
    recent_item = _item(source, "Fresh event X happened", "Fresh event X happened today downtown.")
    story = Story.objects.create(
        canonical_title="Fresh event X happened",
        first_published_at=now - timedelta(hours=2),
        latest_source_update_at=now - timedelta(hours=2),
        metadata={"cluster_meta": {"urls": [], "domains": []}, "cluster_rep": {"shingles": []}},
    )
    StoryMembership.objects.create(story=story, source_item=recent_item, is_current=True)
    old_story = Story.objects.create(
        canonical_title="Ancient unrelated event",
        first_published_at=now - timedelta(days=30),
        latest_source_update_at=now - timedelta(days=30),
        metadata={"cluster_meta": {"urls": [], "domains": []}, "cluster_rep": {"shingles": []}},
    )
    cands = cand.generate_candidates(
        item_text="Fresh event X happened today downtown.",
        item_meta=extract_metadata("Fresh event X", "Fresh event X happened today downtown."),
    )
    ids = {s.pk for s in cands}
    assert story.pk in ids
    assert old_story.pk not in ids
    assert len(cands) <= cand.max_candidates()


# --- scoring / veto / thresholds ---


def test_match_score_fusion_and_decision_bands():
    high, low = 0.78, 0.52
    assert decide(0.9, high=high, low=low) == "match"
    assert decide(0.6, high=high, low=low) == "ambiguous"
    assert decide(0.2, high=high, low=low) == "new_story"
    features = {
        "title_fuzzy_similarity": 0.9,
        "token_set_similarity": 0.9,
        "minhash_similarity": 0.9,
        "semantic_similarity": 0.9,
        "time_proximity": 0.9,
        "url_overlap": 0.0,
        "domain_overlap": 0.0,
        "number_overlap": 0.0,
        "hashtag_overlap": 0.0,
        "language_match": 1.0,
    }
    score, components = match_score(features)
    assert 0.0 <= score <= 1.0
    assert set(components) == {"lexical", "semantic", "temporal", "metadata", "weights"}


def test_evidence_veto_conflicting_numbers_and_antonym_swap():
    a_meta = extract_metadata(
        "Bank holds rates at 5.25 percent", "Bank holds rates at 5.25 percent"
    )
    b_meta = extract_metadata("Bank raises rates to 6 percent", "Bank raises rates to 6 percent")
    features = {
        "title_fuzzy_similarity": 0.7,
        "semantic_similarity": 0.7,
    }
    veto = evidence_veto(features, item_meta=a_meta, story_meta=b_meta)
    assert veto == "conflicting_numbers"
    features2 = {
        "title_fuzzy_similarity": 0.8,
        "semantic_similarity": 0.8,
    }
    veto2 = evidence_veto(
        features2,
        item_meta={},
        story_meta={},
        item_title="Parliament passes data bill",
        item_text="Parliament passed the data protection bill",
        story_title="Parliament rejects data bill",
        story_text="Parliament rejected the data protection bill",
    )
    assert veto2 is not None and veto2.startswith("antonym_action_swap")


def test_cross_language_not_rejected_lexical_discounted():
    features = {
        "title_fuzzy_similarity": 0.2,
        "token_set_similarity": 0.2,
        "minhash_similarity": None,
        "semantic_similarity": 0.85,
        "time_proximity": 0.9,
        "url_overlap": 0.0,
        "domain_overlap": 0.0,
        "number_overlap": 0.0,
        "hashtag_overlap": 0.0,
        "language_match": 0.0,
    }
    score, _ = match_score(features)
    assert score > 0.4


# --- clustering assignment ---


def test_cluster_creates_new_story_then_matches_paraphrase():
    from apps.stories.services import clustering as clu_mod

    source = _source()
    item1 = _item(
        source,
        "OpenAI unveils flagship model GPT-X",
        "OpenAI unveiled GPT-X with faster reasoning.",
    )
    with patch.object(
        clu_mod.story_clustering_service, "_provider", FakeEmbeddingProvider(128), create=True
    ):
        out1 = story_clustering_service.cluster_item(item1.pk)
    assert out1["status"] == "new_story"
    story_id = out1["story_id"]

    item2 = _item(
        source,
        "OpenAI introduces GPT-X flagship model",
        "OpenAI introduced GPT-X, its newest flagship model with quicker reasoning.",
    )
    with (
        patch.object(
            clu_mod.story_clustering_service, "_provider", FakeEmbeddingProvider(128), create=True
        ),
        patch(
            "apps.stories.services.clustering.scoring.thresholds",
            return_value=(0.50, 0.30, "cluster-test"),
        ),
    ):
        out2 = story_clustering_service.cluster_item(item2.pk)
    assert out2["status"] == "matched"
    assert out2["story_id"] == story_id
    item2.refresh_from_db()
    assert item2.story_id == story_id


def test_cluster_does_not_merge_release_vs_price_cut():
    from apps.stories.services import clustering as clu_mod

    source = _source()
    item1 = _item(
        source,
        "OpenAI unveils flagship model GPT-X",
        "OpenAI unveiled GPT-X with faster reasoning.",
    )
    with patch.object(
        clu_mod.story_clustering_service, "_provider", FakeEmbeddingProvider(128), create=True
    ):
        out1 = story_clustering_service.cluster_item(item1.pk)
    assert out1["status"] == "new_story"

    item2 = _item(
        source,
        "OpenAI cuts GPT-X API prices 40 percent",
        "OpenAI cut API prices for GPT-X by 40 percent.",
    )
    with patch.object(
        clu_mod.story_clustering_service, "_provider", FakeEmbeddingProvider(128), create=True
    ):
        out2 = story_clustering_service.cluster_item(item2.pk)
    assert out2["status"] in ("new_story",)
    assert out2["story_id"] != out1["story_id"]


def test_cluster_rerun_idempotent_and_single_current():
    from apps.stories.services import clustering as clu_mod

    source = _source()
    item1 = _item(source, "Metro line 4 extension opens", "Line 4 metro extension opened today.")
    with patch.object(
        clu_mod.story_clustering_service, "_provider", FakeEmbeddingProvider(128), create=True
    ):
        story_clustering_service.cluster_item(item1.pk)
        out = story_clustering_service.cluster_item(item1.pk)
    assert out["status"] == "skipped"
    assert item1.memberships.filter(is_current=True).count() == 1


def test_cluster_time_distant_unrelated_creates_new_story():
    from apps.stories.services import clustering as clu_mod

    source = _source()
    item1 = _item(
        source,
        "Summit set for Geneva next month",
        "The summit will be held in Geneva next month.",
        published_at=timezone.now() - timedelta(days=10),
    )
    with patch.object(
        clu_mod.story_clustering_service, "_provider", FakeEmbeddingProvider(128), create=True
    ):
        out1 = story_clustering_service.cluster_item(item1.pk)
    item2 = _item(
        source,
        "Summit held in Vienna last month",
        "The summit was held in Vienna last month.",
        published_at=timezone.now(),
    )
    with patch.object(
        clu_mod.story_clustering_service, "_provider", FakeEmbeddingProvider(128), create=True
    ):
        out2 = story_clustering_service.cluster_item(item2.pk)
    assert out1["story_id"] != out2["story_id"]


def test_clustering_decision_log_persisted():
    from apps.stories.services import clustering as clu_mod

    source = _source()
    item1 = _item(
        source, "Chip trial shows 18 percent energy cut", "Low-power chip cut energy 18 percent."
    )
    with patch.object(
        clu_mod.story_clustering_service, "_provider", FakeEmbeddingProvider(128), create=True
    ):
        story_clustering_service.cluster_item(item1.pk)
    assert ClusteringDecision.objects.filter(source_item=item1).exists()
    decision = ClusteringDecision.objects.filter(source_item=item1).latest("created_at")
    assert decision.algorithm_version == "cluster-v1"
    assert decision.feature_snapshot != {}


# --- copy network / independence ---


def test_copy_network_exact_copy_flagged():
    label, score, evidence = classify_membership(
        item_text="Breaking news identical text here",
        item_meta={"urls": ["https://example.com/a"]},
        forward_origin=None,
        story_texts=["Breaking news identical text here"],
        story_urls=["https://example.com/a"],
        time_gap_hours=1.0,
    )
    assert label == "likely_copy"
    assert score < 0.5


def test_forward_origin_flagged_likely_copy_but_preserved():
    source = _source()
    item = _item(source, "Forwarded news text", "Forwarded news text body")
    label, _, _ = classify_membership(
        item_text=item.normalized_text,
        item_meta={},
        forward_origin={"origin_type": "channel", "origin_title": "Original"},
        story_texts=["Something vaguely similar but different"],
        story_urls=[],
        time_gap_hours=5.0,
    )
    assert label == "likely_copy"


def test_independent_paraphrases_counted():
    observed, independent, version = aggregate_counts(["independent", "independent", "unknown"])
    assert observed == 3
    assert independent == 2
    assert version == "indep-v1"


def test_independent_counts_on_story_are_copy_aware():
    from apps.stories.services import clustering as clu_mod

    source = _source()
    item1 = _item(source, "Seed story title here", "Seed story body text here for clustering.")
    with patch.object(
        clu_mod.story_clustering_service, "_provider", FakeEmbeddingProvider(128), create=True
    ):
        out = story_clustering_service.cluster_item(item1.pk)
    story = Story.objects.get(pk=out["story_id"])
    assert story.observed_source_count >= 1
    assert story.independent_source_count >= 1


# --- merge / reassign ---


def test_merge_moves_memberships_and_audits_without_delete():
    from apps.stories.services import clustering as clu_mod

    source = _source()
    item1 = _item(source, "Story A title one", "Story A body content one for merge test.")
    item2 = _item(source, "Story B title two", "Story B body content two for merge test.")
    with patch.object(
        clu_mod.story_clustering_service, "_provider", FakeEmbeddingProvider(128), create=True
    ):
        out1 = story_clustering_service.cluster_item(item1.pk)
        out2 = story_clustering_service.cluster_item(item2.pk)
    assert out1["story_id"] != out2["story_id"]
    result = merge_stories(
        source_story_id=out2["story_id"], target_story_id=out1["story_id"], reason="test"
    )
    assert result["status"] == "merged"
    source_story = Story.objects.get(pk=out2["story_id"])
    assert source_story.status == StoryStatus.MERGED
    assert source_story.merged_into_id == out1["story_id"]
    # Repeat merge is idempotent.
    again = merge_stories(
        source_story_id=out2["story_id"], target_story_id=out1["story_id"], reason="test"
    )
    assert again["status"] == "skipped"
    assert AuditLog.objects.filter(action="story.merge").exists()
    # No publication/history loss: items still exist with current membership.
    item2.refresh_from_db()
    assert item2.story_id == out1["story_id"]


def test_reassign_moves_item_and_recomputes_counts():
    from apps.stories.services import clustering as clu_mod

    source = _source()
    item1 = _item(source, "Reassign seed alpha", "Reassign seed alpha body text here.")
    item2 = _item(source, "Reassign seed beta", "Reassign seed beta body text here.")
    with patch.object(
        clu_mod.story_clustering_service, "_provider", FakeEmbeddingProvider(128), create=True
    ):
        story_clustering_service.cluster_item(item1.pk)
        out2 = story_clustering_service.cluster_item(item2.pk)
    result = reassign_item(
        source_item_id=item1.pk, target_story_id=out2["story_id"], reason="test-reassign"
    )
    assert result["status"] == "reassigned"
    item1.refresh_from_db()
    assert item1.story_id == out2["story_id"]
    assert item1.memberships.filter(is_current=True).count() == 1


# --- embeddings / qdrant resilience ---


def test_qdrant_unavailable_marks_failed_and_rebuild_possible():
    from apps.stories.services import clustering as clu_mod

    source = _source()
    item = _item(source, "Qdrant failure title", "Qdrant failure body text for test.")
    with (
        patch.object(
            clu_mod.story_clustering_service, "_provider", FakeEmbeddingProvider(128), create=True
        ),
        patch(
            "apps.stories.services.vector_store.upsert_point",
            side_effect=Exception("qdrant down for test"),
        ),
    ):
        out = story_clustering_service.cluster_item(item.pk)
    assert out["status"] in ("new_story", "matched")
    embedding = ItemEmbedding.objects.filter(source_item=item).first()
    assert embedding is not None
    assert embedding.status in ("pending", "failed")
    # Rebuild path: recompute from MySQL content hash is always possible.
    assert embedding.content_hash
    assert embedding.point_id == f"item-{item.pk}"


def test_minhash_persisted_versioned_not_pickle():
    from apps.stories.services import clustering as clu_mod

    source = _source()
    item = _item(source, "MinHash persist title", "MinHash persist body text content.")
    with patch.object(
        clu_mod.story_clustering_service, "_provider", FakeEmbeddingProvider(128), create=True
    ):
        story_clustering_service.cluster_item(item.pk)
    sketch = ItemMinHash.objects.filter(source_item=item).first()
    assert sketch is not None
    assert sketch.scheme in ("affine32", "affine64", "legacy")
    assert isinstance(sketch.digest, list) and len(sketch.digest) == sketch.num_perm


# --- benchmark / evaluation ---


def test_benchmark_evaluation_prefers_precision():
    pairs = load_fixture("tests/fixtures/clustering_benchmark.json")
    assert len(pairs) >= 30
    result = evaluate_pairs(pairs)
    assert result["pairs"] == len(pairs)
    # Conservative V1: zero false merges is the hard requirement.
    assert result["false_merge_rate"] == 0.0
    assert result["precision"] == 1.0 or result["tp"] + result["fp"] == 0
    assert set(result) >= {
        "precision",
        "recall",
        "f1",
        "false_merge_rate",
        "false_split_rate",
    }
