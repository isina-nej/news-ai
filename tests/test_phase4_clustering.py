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
    pytest.importorskip("lingua", reason="lingua wheel absent offline")
    en = detect_language("OpenAI unveiled its new flagship model GPT-X with faster reasoning.")
    fa = detect_language("تیم ملی در فینال با نتیجه ۲ بر ۱ قهرمان شد و هواداران خوشحال شدند.")
    short = detect_language("OK")
    assert en["language"] == "en" and en["reliable"] is True
    assert fa["language"] == "fa" and fa["reliable"] is True
    assert short == {"language": "und", "confidence": 0.0, "reliable": False}


def test_language_detection_degrades_gracefully_without_wheel():
    short = detect_language("OK")
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
    pytest.importorskip("datasketch", reason="datasketch/scipy wheel absent offline")
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
    result = cand.generate_candidates(
        item_text="Fresh event X happened today downtown.",
        item_meta=extract_metadata("Fresh event X", "Fresh event X happened today downtown."),
    )
    cands = result["candidates"]
    counts = result["counts"]
    ids = {s.pk for s in cands}
    assert story.pk in ids
    assert old_story.pk not in ids
    assert len(cands) <= cand.max_candidates()
    assert counts["eligible_candidate_count"] == len(cands)
    assert counts["recency_candidate_count"] >= 1


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
    assert independent == 2  # conservative default: UNKNOWN excluded
    assert version.startswith("indep-v1")
    observed_inc, independent_inc, _ = aggregate_counts(
        ["independent", "independent", "unknown"], unknown_policy="include"
    )
    assert (observed_inc, independent_inc) == (3, 3)


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
    pytest.importorskip("datasketch", reason="datasketch/scipy wheel absent offline")
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


# --- Phase 4.1 correctness ---


def test_candidate_eligibility_gate_rejects_qdrant_bypass():
    from apps.stories.services import candidates as cand_mod

    source = _source()
    now = timezone.now()
    recent_item = _item(source, "Eligible story seed", "Eligible story seed body here.")
    good = Story.objects.create(
        canonical_title="Eligible story seed",
        status=StoryStatus.ACTIVE,
        first_published_at=now - timedelta(hours=1),
        latest_source_update_at=now - timedelta(hours=1),
        metadata={"cluster_meta": {"urls": [], "domains": []}, "cluster_rep": {"shingles": []}},
    )
    StoryMembership.objects.create(story=good, source_item=recent_item, is_current=True)
    merged = Story.objects.create(
        canonical_title="Merged story",
        status=StoryStatus.MERGED,
        merged_into=good,
        first_published_at=now - timedelta(hours=1),
        latest_source_update_at=now - timedelta(hours=1),
        metadata={"cluster_meta": {"urls": [], "domains": []}, "cluster_rep": {"shingles": []}},
    )
    archived = Story.objects.create(
        canonical_title="Archived story",
        status=StoryStatus.ARCHIVED,
        first_published_at=now - timedelta(hours=1),
        latest_source_update_at=now - timedelta(hours=1),
        metadata={"cluster_meta": {"urls": [], "domains": []}, "cluster_rep": {"shingles": []}},
    )
    old = Story.objects.create(
        canonical_title="Old active story",
        status=StoryStatus.ACTIVE,
        first_published_at=now - timedelta(days=30),
        latest_source_update_at=now - timedelta(days=30),
        metadata={"cluster_meta": {"urls": [], "domains": []}, "cluster_rep": {"shingles": []}},
    )
    assert cand_mod.is_candidate_eligible(good, now=now) is True
    assert cand_mod.is_candidate_eligible(merged, now=now) is False
    assert cand_mod.is_candidate_eligible(archived, now=now) is False
    assert cand_mod.is_candidate_eligible(old, now=now) is False

    with patch.object(cand_mod, "vector_candidates", return_value=[old, merged, archived, good]):
        result = cand_mod.generate_candidates(
            item_text="vector-only candidate",
            item_meta={},
            item_vector=[1.0, 0.0],
            now=now,
        )
    assert [story.pk for story in result["candidates"]] == [good.pk]
    assert result["counts"]["vector_candidate_count"] == 4
    assert result["counts"]["eligible_candidate_count"] == 1


def test_centroid_real_members_with_metadata():
    from apps.stories.services import clustering as clu_mod
    from apps.stories.services.embeddings import FakeEmbeddingProvider, centroid
    from apps.stories.services.representation import clustering_input_hash

    provider = FakeEmbeddingProvider(4)
    source = _source()
    items = [
        _item(source, "Centroid alpha", "one alpha"),
        _item(source, "Centroid beta", "two beta"),
        _item(source, "Centroid gamma", "three gamma"),
    ]
    story = Story.objects.create(
        canonical_title="Centroid regression story",
        status=StoryStatus.ACTIVE,
        primary_item=items[0],
        latest_source_update_at=timezone.now(),
    )
    for index, item in enumerate(items):
        StoryMembership.objects.create(
            story=story,
            source_item=item,
            is_primary=index == 0,
            is_current=True,
        )
        item.story = story
        item.save(update_fields=["story", "updated_at"])
        ItemEmbedding.objects.create(
            source_item=item,
            content_hash=clustering_input_hash(item.title, item.normalized_text),
            provider="fake",
            model="fake-hash-bucket",
            model_version="fake-v1",
            dimension=4,
            status="indexed",
            point_id=f"item-{item.pk}",
        )

    vectors = [provider.embed([f"{item.title}\n\n{item.normalized_text}"])[0] for item in items]
    expected = centroid(vectors)
    assert expected is not None and expected != vectors[-1]
    ident = {
        "provider": "fake",
        "model": "fake-hash-bucket",
        "model_version": "fake-v1",
        "dimension": 4,
    }
    with (
        patch.object(clu_mod.story_clustering_service, "_provider", provider, create=True),
        patch("apps.stories.services.clustering.embedding_identity", return_value=ident),
    ):
        clu_mod.story_clustering_service._after_membership_change(
            story, item_vector=None, item_mh=None, ident=ident, item=items[-1]
        )

    story.refresh_from_db()
    meta = story.metadata or {}
    assert meta["centroid"] == expected
    assert meta["centroid"] != vectors[-1]
    assert meta["centroid_provider"] == "fake"
    assert meta["centroid_model"] == "fake-hash-bucket"
    assert meta["centroid_model_version"] == "fake-v1"
    assert meta["centroid_dimension"] == 4
    assert meta["centroid_member_count"] == 3
    assert set(meta["centroid_member_ids"]) == {item.pk for item in items}
    assert meta["centroid_updated_at"]


def test_centroid_rejects_stale_failed_model_and_dimension_mismatches():
    from apps.stories.services.clustering import _valid_member_vectors
    from apps.stories.services.embeddings import FakeEmbeddingProvider
    from apps.stories.services.representation import clustering_input_hash

    provider = FakeEmbeddingProvider(4)
    source = _source()
    story = Story.objects.create(
        canonical_title="Centroid validity",
        status=StoryStatus.ACTIVE,
        latest_source_update_at=timezone.now(),
    )
    items = [_item(source, f"Member {i}", f"body {i}") for i in range(5)]
    states = [
        ("indexed", "fake-hash-bucket", "fake-v1", 4, True),
        ("stale", "fake-hash-bucket", "fake-v1", 4, True),
        ("failed", "fake-hash-bucket", "fake-v1", 4, True),
        ("indexed", "other-model", "fake-v1", 4, True),
        ("indexed", "fake-hash-bucket", "fake-v1", 8, True),
    ]
    for item, (status, model, version, dimension, correct_hash) in zip(items, states, strict=True):
        StoryMembership.objects.create(story=story, source_item=item, is_current=True)
        ItemEmbedding.objects.create(
            source_item=item,
            content_hash=(
                clustering_input_hash(item.title, item.normalized_text) if correct_hash else "stale"
            ),
            provider="fake",
            model=model,
            model_version=version,
            dimension=dimension,
            status=status,
            point_id=f"item-{item.pk}",
        )
    ident = {
        "provider": "fake",
        "model": "fake-hash-bucket",
        "model_version": "fake-v1",
        "dimension": 4,
    }
    with patch("apps.stories.services.clustering.embedding_identity", return_value=ident):
        vectors, member_ids = _valid_member_vectors(story, provider)
    assert len(vectors) == 1
    assert member_ids == [items[0].pk]


def test_story_freshness_uses_max_published_and_edit_time():
    from apps.stories.services import clustering as clu_mod
    from apps.stories.services.embeddings import FakeEmbeddingProvider

    source = _source()
    now = timezone.now()
    item1 = _item(
        source,
        "Freshness seed",
        "Freshness seed body.",
        published_at=now - timedelta(hours=5),
    )
    with patch.object(
        clu_mod.story_clustering_service, "_provider", FakeEmbeddingProvider(128), create=True
    ):
        out = story_clustering_service.cluster_item(item1.pk)
    story = Story.objects.get(pk=out["story_id"])
    assert story.latest_source_update_at is not None
    # Simulate a Telegram edit arriving later: freshness must advance.
    item1.source_updated_at = now
    item1.save(update_fields=["source_updated_at", "updated_at"])
    clu_mod.story_clustering_service._refresh_story_stats(story)
    story.refresh_from_db()
    assert story.latest_source_update_at is not None
    assert abs((story.latest_source_update_at - now).total_seconds()) < 3600


def test_primary_policy_deterministic_and_versioned():
    from apps.stories.services import clustering as clu_mod
    from apps.stories.services.clustering import PRIMARY_POLICY_VERSION
    from apps.stories.services.embeddings import FakeEmbeddingProvider

    assert PRIMARY_POLICY_VERSION == "primary-v1"
    source = _source()
    high_trust = _source(name="High trust", identifier="@high")
    high_trust.trust_score = "0.95"
    high_trust.reliability_score = "0.95"
    high_trust.save()
    now = timezone.now()
    early = _item(
        source,
        "Primary race seed",
        "Primary race seed body.",
        published_at=now - timedelta(hours=3),
    )
    late = _item(
        high_trust,
        "Primary race seed",
        "Primary race seed body duplicate text.",
        published_at=now - timedelta(minutes=10),
    )
    # Force both into one story via merge to test primary selection directly.
    with patch.object(
        clu_mod.story_clustering_service, "_provider", FakeEmbeddingProvider(128), create=True
    ):
        out1 = story_clustering_service.cluster_item(early.pk)
        out2 = story_clustering_service.cluster_item(late.pk)
    from apps.stories.services.merge import merge_stories

    if out1["story_id"] != out2["story_id"]:
        merge_stories(
            source_story_id=out2["story_id"], target_story_id=out1["story_id"], reason="test"
        )
    story = Story.objects.get(pk=out1["story_id"])
    assert story.primary_item_id == early.pk  # earliest valid publication always wins
    assert (story.metadata or {}).get("primary_policy_version") == "primary-v1"


def test_copy_time_gap_signal_and_distinct_source_counts():
    from apps.stories.services.independence import classify_membership

    label_fast, _, ev_fast = classify_membership(
        item_text="Breaking identical text here now",
        item_meta={},
        forward_origin=None,
        story_texts=["Breaking identical text here now"],
        story_urls=[],
        time_gap_hours=0.1,
    )
    assert label_fast == "likely_copy"
    assert ev_fast["time_gap_hours"] == 0.1
    label_slow, _, _ = classify_membership(
        item_text="Completely rewritten independent report with other wording",
        item_meta={},
        forward_origin=None,
        story_texts=["Something else entirely different here"],
        story_urls=[],
        time_gap_hours=0.1,
    )
    assert label_slow == "independent"


def test_qdrant_collection_versioned_by_model():
    from apps.stories.services import vector_store

    base = vector_store.collection_name()
    identity = {
        "dimension": 384,
        "provider": "fastembed",
        "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "model_version": "phase4-default-v1",
    }
    versioned = vector_store.collection_name(**identity)
    changed_dimension = vector_store.collection_name(**{**identity, "dimension": 768})
    changed_version = vector_store.collection_name(**{**identity, "model_version": "v2"})
    changed_provider = vector_store.collection_name(**{**identity, "provider": "fake"})
    assert versioned != base
    assert len({versioned, changed_dimension, changed_version, changed_provider}) == 4
    with pytest.raises(ValueError):
        vector_store.search_points(vector=[0.1] * 384, provider="", model="", model_version="")
    # Qdrant is auxiliary only: MySQL ItemEmbedding rows identify a versioned collection.
    identity_row = ItemEmbedding(
        provider=identity["provider"],
        model=identity["model"],
        model_version=identity["model_version"],
        dimension=identity["dimension"],
    )
    collection = vector_store.collection_name(
        dimension=identity_row.dimension,
        provider=identity_row.provider,
        model=identity_row.model,
        model_version=identity_row.model_version,
    )
    assert collection == versioned


def test_relationship_field_ready_for_future():
    from apps.stories.models import ClusteringDecision, StoryRelationship

    assert StoryRelationship.SAME_EVENT == "same_event"
    assert StoryRelationship.MATERIAL_UPDATE == "material_update"
    assert StoryRelationship.RELATED_EVENT == "related_event"
    assert StoryRelationship.UNRELATED == "unrelated"
    field = ClusteringDecision._meta.get_field("relationship")
    assert field.blank is True
