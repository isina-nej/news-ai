"""StoryClusteringService: find -> features -> score -> decide -> assign.

Pure matchers stay side-effect free; this service owns ORM writes inside
bounded transactions. Conservative by design: false merge > false split.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.news.models import SourceItem
from apps.stories.models import ClusteringDecision, MatchMethod, Story, StoryMembership
from apps.stories.services import candidates as cand
from apps.stories.services import lexical as lex
from apps.stories.services import scoring
from apps.stories.services.embeddings import (
    centroid,
    cosine,
    embedding_identity,
    get_embedding_provider,
)
from apps.stories.services.features import extract_pair_features, item_meta_for
from apps.stories.services.independence import aggregate_counts, classify_membership
from apps.stories.services.language import detect_language
from apps.stories.services.representation import (
    build_clustering_text,
    clustering_input_hash,
    extract_metadata,
)

logger = logging.getLogger(__name__)

MATCH_METHOD_MAP = {
    "url": MatchMethod.SHARED_URL,
    "lexical": MatchMethod.TITLE_SIM,
    "semantic": MatchMethod.SEMANTIC,
}


class StoryClusteringService:
    def __init__(self) -> None:
        self._provider = None

    @property
    def provider(self):
        if self._provider is None:
            self._provider = get_embedding_provider()
        return self._provider

    # -- story representation -------------------------------------------

    def _story_rep(self, story: Story) -> dict:
        meta = dict(story.metadata or {})
        cluster_meta = dict(meta.get("cluster_meta", {}))
        primary = story.primary_item
        members = list(
            story.memberships.filter(is_current=True)
            .select_related("source_item")
            .order_by("added_at")[:8]
        )
        texts = [m.source_item.normalized_text for m in members if m.source_item.normalized_text]
        rep_text = ""
        if primary and primary.normalized_text:
            rep_text = primary.normalized_text[:2000]
        elif texts:
            rep_text = texts[0][:2000]
        return {
            "title": story.canonical_title or "",
            "text": rep_text,
            "published_at": story.first_published_at,
            "language": story.language or "",
            "meta": cluster_meta,
            "member_texts": texts[:8],
        }

    def _story_vector(self, story: Story) -> list[float] | None:
        meta = dict(story.metadata or {})
        vec = meta.get("centroid")
        if isinstance(vec, list) and vec:
            return [float(v) for v in vec]
        return None

    def _refresh_story_rep(self, story: Story, *, representative_texts: list[str]) -> None:
        meta = dict(story.metadata or {})
        cluster_rep = dict(meta.get("cluster_rep", {}))
        shingles: set[str] = set()
        for text in representative_texts[:8]:
            shingles |= lex.shingles(text or "")
        cluster_rep["shingles"] = sorted(shingles)[:2000]
        meta["cluster_rep"] = cluster_rep
        cluster_meta = dict(meta.get("cluster_meta", {}))
        urls: list[str] = []
        domains: list[str] = []
        for text in representative_texts[:4]:
            md = extract_metadata("", text or "")
            for url in md.get("urls", []):
                if url not in urls:
                    urls.append(url)
            for domain in md.get("domains", []):
                if domain not in domains:
                    domains.append(domain)
        cluster_meta["urls"] = urls[:20]
        cluster_meta["domains"] = domains[:20]
        meta["cluster_meta"] = cluster_meta
        story.metadata = meta

    def _refresh_centroid(self, story: Story, vectors: list[list[float]]) -> None:
        if not vectors:
            return
        center = centroid(vectors, cap=8)
        if center is None:
            return
        meta = dict(story.metadata or {})
        meta["centroid"] = center
        meta["centroid_updated_at"] = timezone.now().isoformat()
        story.metadata = meta

    # -- main entry ------------------------------------------------------

    def cluster_item(self, source_item_id: int, *, correlation_id: str = "") -> dict[str, Any]:
        started = time.monotonic()
        try:
            item = SourceItem.objects.select_related("source").get(pk=source_item_id)
        except SourceItem.DoesNotExist:
            return {"status": "skipped", "reason": "item_not_found"}
        if item.memberships.filter(is_current=True).exists():
            return {"status": "skipped", "reason": "already_clustered"}

        clustering_text = build_clustering_text(item.title, item.normalized_text)
        item_meta = item_meta_for(item.title, item.normalized_text)
        detected = detect_language(clustering_text)
        if (item.language or "und") == "und" and detected.get("reliable"):
            item_language = detected["language"]
        else:
            item_language = item.language or "und"

        # MinHash + embedding (best-effort; lexical path always runs).
        minhash_sim_by_story: dict[int, float] = {}
        item_vector: list[float] | None = None
        ident = embedding_identity()
        try:
            from apps.stories.services.lexical import minhash_for_text

            item_mh = minhash_for_text(
                clustering_text,
                num_perm=int(getattr(settings, "MINHASH_NUM_PERM", 128)),
                scheme=str(getattr(settings, "MINHASH_SCHEME", "affine32")),
            )
        except Exception:
            item_mh = None
        try:
            item_vector = self.provider.embed([clustering_text])[0]
        except Exception as exc:
            logger.warning("embedding failed for item %s: %s", item.pk, exc)
            item_vector = None

        # Candidates (bounded union). Exclude the item's own story (already
        # clustered reruns must stay skipped, not self-matched).
        stories = [
            story
            for story in cand.generate_candidates(
                item_text=clustering_text, item_meta=item_meta, item_vector=item_vector
            )
            if story.pk != getattr(item.story, "pk", None)
        ]
        if item_mh is not None:
            for story in stories:
                try:
                    rep_texts = [
                        m.source_item.normalized_text
                        for m in story.memberships.filter(is_current=True).select_related(
                            "source_item"
                        )[:4]
                    ]
                    best = 0.0
                    from apps.stories.services.lexical import minhash_for_text as _mft
                    from apps.stories.services.lexical import minhash_jaccard

                    for rep_text in rep_texts:
                        other = _mft(
                            rep_text or "",
                            num_perm=int(getattr(settings, "MINHASH_NUM_PERM", 128)),
                            scheme=str(getattr(settings, "MINHASH_SCHEME", "affine32")),
                        )
                        best = max(best, minhash_jaccard(item_mh, other))
                    minhash_sim_by_story[story.pk] = best
                except Exception as exc:  # noqa: S112 — one bad story must not abort matching
                    logger.debug("minhash prefilter failed for story %s: %s", story.pk, exc)
                    continue

        high, low, threshold_version = scoring.thresholds()
        algorithm_version = str(getattr(settings, "CLUSTER_ALGORITHM_VERSION", "cluster-v1"))

        best: dict[str, Any] | None = None
        scored_all: list[dict[str, Any]] = []
        member_vectors: dict[int, list[float]] = {}
        if item_vector is not None:
            try:
                for story in stories:
                    vecs: list[list[float]] = []
                    for membership in story.memberships.filter(is_current=True).select_related(
                        "source_item"
                    )[:4]:
                        member_item = membership.source_item
                        member_text = build_clustering_text(
                            member_item.title, member_item.normalized_text
                        )
                        try:
                            vecs.append(self.provider.embed([member_text])[0])
                        except Exception as exc:  # noqa: S112 — skip unembeddable member
                            logger.debug("member embed failed: %s", exc)
                            continue
                    if vecs:
                        member_vectors[story.pk] = centroid(vecs, cap=4) or vecs[0]
            except Exception as exc:
                logger.debug("member vector prefetch failed: %s", exc)
                member_vectors = {}
        for story in stories:
            rep = self._story_rep(story)
            story_vector = self._story_vector(story) or member_vectors.get(story.pk)
            semantic = None
            if item_vector and story_vector:
                try:
                    semantic = cosine(item_vector, story_vector)
                except Exception:
                    semantic = None
            features = extract_pair_features(
                item_title=item.title,
                item_text=clustering_text,
                item_published=item.published_at,
                item_meta=item_meta,
                story_title=rep["title"],
                story_text=rep["text"],
                story_published=rep["published_at"],
                story_meta=rep["meta"],
                story_language=rep["language"],
                item_language=item_language,
                minhash_sim=minhash_sim_by_story.get(story.pk),
                semantic_sim=semantic,
            )
            veto = scoring.evidence_veto(
                features,
                item_meta=item_meta,
                story_meta=rep["meta"],
                item_title=item.title,
                item_text=clustering_text,
                story_title=rep["title"],
                story_text=rep["text"],
            )
            score, components = scoring.match_score(features)
            if veto is not None:
                score = min(score, low - 0.01)
            entry = {
                "story": story,
                "score": score,
                "components": components,
                "features": features,
                "veto": veto,
            }
            scored_all.append(entry)
            if best is None or score > best["score"]:
                best = entry

        if best is None:
            return self._create_story(
                item,
                clustering_text=clustering_text,
                item_meta=item_meta,
                item_vector=item_vector,
                item_mh=item_mh,
                ident=ident,
                algorithm_version=algorithm_version,
                threshold_version=threshold_version,
                duration_ms=int((time.monotonic() - started) * 1000),
                correlation_id=correlation_id,
            )

        decision = scoring.decide(best["score"], high=high, low=low)
        if decision == "match":
            return self._assign(
                item,
                best["story"],
                best,
                clustering_text=clustering_text,
                item_meta=item_meta,
                item_vector=item_vector,
                item_mh=item_mh,
                ident=ident,
                algorithm_version=algorithm_version,
                threshold_version=threshold_version,
                duration_ms=int((time.monotonic() - started) * 1000),
                correlation_id=correlation_id,
            )
        if decision == "ambiguous":
            self._log_decision(
                item,
                best["story"],
                "ambiguous",
                best,
                algorithm_version,
                threshold_version,
                ident,
            )
            return self._create_story(
                item,
                clustering_text=clustering_text,
                item_meta=item_meta,
                item_vector=item_vector,
                item_mh=item_mh,
                ident=ident,
                algorithm_version=algorithm_version,
                threshold_version=threshold_version,
                duration_ms=int((time.monotonic() - started) * 1000),
                correlation_id=correlation_id,
                ambiguous_from=best,
            )
        self._log_decision(
            item, best["story"], "new_story", best, algorithm_version, threshold_version, ident
        )
        return self._create_story(
            item,
            clustering_text=clustering_text,
            item_meta=item_meta,
            item_vector=item_vector,
            item_mh=item_mh,
            ident=ident,
            algorithm_version=algorithm_version,
            threshold_version=threshold_version,
            duration_ms=int((time.monotonic() - started) * 1000),
            correlation_id=correlation_id,
        )

    # -- assignment helpers ----------------------------------------------

    def _log_decision(
        self, item, story, decision, best, algorithm_version, threshold_version, ident
    ) -> None:

        ClusteringDecision.objects.create(
            source_item=item,
            candidate_story=story,
            decision=decision,
            match_score=Decimal(str(best["score"])),
            feature_snapshot={
                "features": best["features"],
                "components": best["components"],
                "veto": best.get("veto"),
            },
            algorithm_version=algorithm_version,
            threshold_version=threshold_version,
            embedding_model=str(ident.get("model", "")),
            embedding_version=str(ident.get("model_version", "")),
            method="fused",
        )

    def _pick_method(self, best: dict) -> str:
        features = best["features"]
        components = best["components"]
        if (features.get("url_overlap") or 0) > 0 or (features.get("domain_overlap") or 0) > 0:
            return str(MatchMethod.SHARED_URL)
        order = sorted(
            (
                ("semantic", components.get("semantic", 0)),
                ("lexical", components.get("lexical", 0)),
            ),
            key=lambda t: t[1],
            reverse=True,
        )
        top = order[0][0] if order else "lexical"
        if top == "semantic":
            return str(MatchMethod.SEMANTIC)
        return str(MatchMethod.TITLE_SIM)

    def _assign(
        self,
        item,
        story,
        best,
        *,
        clustering_text,
        item_meta,
        item_vector,
        item_mh,
        ident,
        algorithm_version,
        threshold_version,
        duration_ms,
        correlation_id,
    ) -> dict[str, Any]:
        from apps.stories.services import vector_store

        with transaction.atomic():
            locked_story = Story.objects.select_for_update().get(pk=story.pk)
            if item.memberships.filter(is_current=True).exists():
                return {"status": "skipped", "reason": "already_clustered"}
            method = self._pick_method(best)
            # Independence classification vs current members.
            members = list(
                locked_story.memberships.filter(is_current=True).select_related("source_item")
            )
            story_texts = [m.source_item.normalized_text for m in members]
            story_urls: list[str] = []
            for m in members:
                md = extract_metadata(m.source_item.title, m.source_item.normalized_text)
                story_urls.extend(md.get("urls", []))
            fwd = (
                (item.raw_payload or {}).get("forward_origin")
                if isinstance(item.raw_payload, dict)
                else None
            )
            label, score, evidence = classify_membership(
                item_text=item.normalized_text or "",
                item_meta=item_meta,
                forward_origin=fwd if isinstance(fwd, dict) else None,
                story_texts=story_texts,
                story_urls=story_urls,
                time_gap_hours=None,
            )
            membership = StoryMembership.objects.create(
                story=locked_story,
                source_item=item,
                similarity_score=Decimal(str(best["score"])),
                match_method=method,
                independence=label,
                independence_score=Decimal(str(score)),
                is_current=True,
                detail={
                    "features": best["features"],
                    "components": best["components"],
                    "veto": best.get("veto"),
                    "evidence": evidence,
                    "algorithm_version": algorithm_version,
                },
            )
            item.story = locked_story
            item.save(update_fields=["story", "updated_at"])
            self._log_decision(
                item, locked_story, "match", best, algorithm_version, threshold_version, ident
            )
            ClusteringDecision.objects.filter(
                source_item=item, candidate_story=locked_story, decision="ambiguous"
            ).delete()
            self._after_membership_change(
                locked_story, item_vector=item_vector, item_mh=item_mh, ident=ident, item=item
            )
            # Persist/refresh auxiliary indexes (best-effort, never fail assignment).
            try:
                self._persist_aux(item, clustering_text, item_vector, item_mh, ident)
            except Exception as exc:
                logger.warning("aux persist failed for item %s: %s", item.pk, exc)
            # Refresh counts + timestamps.
            self._refresh_story_stats(locked_story)
        try:
            if item_vector:
                vector_store.upsert_point(
                    point_id=vector_store.point_id_for_item(item.pk),
                    vector=item_vector,
                    payload={"story_id": locked_story.pk, "source_item_id": item.pk},
                )
                from apps.stories.models import ItemEmbedding as ItemEmbeddingModel

                ItemEmbeddingModel.objects.filter(source_item=item).update(status="indexed")
        except Exception as exc:
            logger.warning("qdrant upsert failed for item %s: %s", item.pk, exc)
            try:
                from apps.stories.models import ItemEmbedding as ItemEmbeddingModel

                ItemEmbeddingModel.objects.filter(source_item=item).update(status="failed")
            except Exception as mark_exc:  # noqa: S110 — status flag is best-effort
                logger.debug("embedding status mark failed: %s", mark_exc)
        return {
            "status": "matched",
            "story_id": locked_story.pk,
            "membership_id": membership.pk,
            "score": best["score"],
            "duration_ms": duration_ms,
            "correlation_id": correlation_id,
        }

    def _create_story(
        self,
        item,
        *,
        clustering_text,
        item_meta,
        item_vector,
        item_mh,
        ident,
        algorithm_version,
        threshold_version,
        duration_ms,
        correlation_id,
        ambiguous_from: dict | None = None,
    ) -> dict[str, Any]:
        from apps.stories.services import vector_store

        with transaction.atomic():
            if item.memberships.filter(is_current=True).exists():
                return {"status": "skipped", "reason": "already_clustered"}
            story = Story.objects.create(
                canonical_title=(item.title or clustering_text[:200] or "Untitled"),
                language=item.language or "und",
                first_published_at=item.published_at,
                latest_source_update_at=item.published_at or timezone.now(),
                primary_item=item,
                metadata={
                    "cluster_meta": {
                        "urls": item_meta.get("urls", []),
                        "domains": item_meta.get("domains", []),
                    },
                    "cluster_rep": {"shingles": sorted(lex_shingles(clustering_text))[:2000]},
                },
            )
            membership = StoryMembership.objects.create(
                story=story,
                source_item=item,
                similarity_score=Decimal("1.0"),
                match_method=MatchMethod.MANUAL,
                independence="independent",
                independence_score=Decimal("1.0"),
                is_primary=True,
                is_current=True,
                detail={"reason": "new_story_seed", "algorithm_version": algorithm_version},
            )
            item.story = story
            item.save(update_fields=["story", "updated_at"])
            ClusteringDecision.objects.create(
                source_item=item,
                candidate_story=(ambiguous_from["story"] if ambiguous_from else None),
                decision="ambiguous" if ambiguous_from else "new_story",
                match_score=Decimal(str((ambiguous_from or {}).get("score", 0.0))),
                feature_snapshot={
                    "features": (ambiguous_from or {}).get("features", {}),
                    "components": (ambiguous_from or {}).get("components", {}),
                },
                algorithm_version=algorithm_version,
                threshold_version=threshold_version,
                embedding_model=str(ident.get("model", "")),
                embedding_version=str(ident.get("model_version", "")),
                method="seed",
            )
            try:
                self._persist_aux(item, clustering_text, item_vector, item_mh, ident)
            except Exception as exc:
                logger.warning("aux persist failed for item %s: %s", item.pk, exc)
            self._refresh_story_stats(story)
            if item_vector:
                self._refresh_centroid(story, [item_vector])
                story.save(update_fields=["metadata", "updated_at"])
        try:
            if item_vector:
                vector_store.upsert_point(
                    point_id=vector_store.point_id_for_item(item.pk),
                    vector=item_vector,
                    payload={"story_id": story.pk, "source_item_id": item.pk},
                )
                from apps.stories.models import ItemEmbedding as ItemEmbeddingModel

                ItemEmbeddingModel.objects.filter(source_item=item).update(status="indexed")
        except Exception as exc:
            logger.warning("qdrant upsert failed for item %s: %s", item.pk, exc)
        return {
            "status": "new_story",
            "story_id": story.pk,
            "membership_id": membership.pk,
            "duration_ms": duration_ms,
            "correlation_id": correlation_id,
        }

    def _persist_aux(self, item, clustering_text, item_vector, item_mh, ident) -> None:
        from apps.stories.models import ItemEmbedding, ItemMinHash

        input_hash = clustering_input_hash(item.title, item.normalized_text)
        if item_vector:
            ItemEmbedding.objects.update_or_create(
                source_item=item,
                defaults={
                    "content_hash": input_hash,
                    "provider": str(ident.get("provider", "")),
                    "model": str(ident.get("model", "")),
                    "model_version": str(ident.get("model_version", "")),
                    "dimension": len(item_vector),
                    "status": "pending",
                    "point_id": f"item-{int(item.pk)}",
                },
            )
        if item_mh is not None:
            from apps.stories.services.lexical import digest_to_list

            ItemMinHash.objects.update_or_create(
                source_item=item,
                defaults={
                    "num_perm": int(getattr(settings, "MINHASH_NUM_PERM", 128)),
                    "scheme": str(getattr(settings, "MINHASH_SCHEME", "affine32")),
                    "digest": digest_to_list(item_mh),
                },
            )

    def _after_membership_change(self, story, *, item_vector, item_mh, ident, item) -> None:
        members = list(story.memberships.filter(is_current=True).select_related("source_item")[:8])
        texts = [m.source_item.normalized_text for m in members if m.source_item.normalized_text]
        self._refresh_story_rep(story, representative_texts=texts)
        vectors: list[list[float]] = []
        if item_vector:
            vectors.append(item_vector)
        try:
            center = centroid(vectors, cap=8) if vectors else None
            if center:
                meta = dict(story.metadata or {})
                meta["centroid"] = center
                story.metadata = meta
        except Exception as exc:  # noqa: S110 — centroid is auxiliary
            logger.debug("centroid refresh failed: %s", exc)
        story.save(update_fields=["metadata", "updated_at"])

    def _refresh_story_stats(self, story: Story) -> None:
        members = list(story.memberships.filter(is_current=True).select_related("source_item"))
        sources = {m.source_item.source_id for m in members}
        story.observed_source_count = len(sources)
        labels = [m.independence for m in members]
        _, independent, version = aggregate_counts(labels)
        story.independent_source_count = independent
        meta = dict(story.metadata or {})
        meta["independence_version"] = version
        story.metadata = meta

        # Primary: earliest reliable publication, then content completeness.
        def _rank(m):
            item = m.source_item
            published = item.published_at
            has_time = 0 if published else 1
            length = -(len(item.normalized_text or ""))
            return (has_time, published or timezone.now(), length)

        if members:
            primary_membership = sorted(members, key=_rank)[0]
            story.primary_item = primary_membership.source_item
            for m in members:
                want_primary = m.pk == primary_membership.pk
                if m.is_primary != want_primary:
                    m.is_primary = want_primary
                    m.save(update_fields=["is_primary", "updated_at"])
        pubs = [m.source_item.published_at for m in members if m.source_item.published_at]
        if pubs:
            story.first_published_at = min(pubs)
            story.latest_source_update_at = max(pubs)
        story.save(
            update_fields=[
                "observed_source_count",
                "independent_source_count",
                "metadata",
                "primary_item",
                "first_published_at",
                "latest_source_update_at",
                "updated_at",
            ]
        )


def lex_shingles(text: str) -> set[str]:
    from apps.stories.services import lexical as _lex

    return _lex.shingles(text or "")


story_clustering_service = StoryClusteringService()
