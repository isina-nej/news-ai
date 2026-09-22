"""WHAT/WHEN/HOW publishing engine behind the Publisher port."""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.ai.evidence import story_evidence
from apps.ai.service import run_structured_task
from apps.core.exceptions import PublishingError
from apps.core.redaction import sanitize_error_message
from apps.publishing.models import (
    Publication,
    PublicationStatus,
    UpdateType,
)
from apps.publishing.telegram import FakePublisher, PublisherPort, TelegramBotPublisher, render_post

POLICY_ALGORITHM_VERSION = "publish-v1"
MIN_SPACING_MINUTES = 20
MAX_POSTS_PER_HOUR = 3


def _channel() -> str:
    return str(getattr(settings, "TELEGRAM_CHANNEL_ID", "") or "")


def _publisher() -> PublisherPort:
    token = str(getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "")
    if not token:
        return FakePublisher()
    return TelegramBotPublisher(bot_token=token)


def _auto_publish_enabled() -> bool:
    try:
        from apps.ops.models import FeatureFlag

        row = FeatureFlag.objects.filter(key="ENABLE_AUTO_PUBLISH").first()
        if row is not None:
            return bool(row.enabled)
    except Exception:  # noqa: S110 — flag lookup fallback to settings
        pass
    flags = getattr(settings, "FEATURE_FLAGS", {})
    return bool(flags.get("ENABLE_AUTO_PUBLISH", False))


def decide_what(story, *, scores: dict | None = None) -> tuple[str, str]:
    """Return (action, reason): publish, schedule, hold, or skip."""
    from apps.ranking.services.scoring import CredibilityGate

    credibility = 0.5
    try:
        credibility = float(story.news_value.credibility)
    except Exception:
        credibility = 0.5
    passed, reason = CredibilityGate.evaluate(story, credibility)
    if not passed:
        return "hold", f"credibility_gate:{reason}"
    if story.independent_source_count < 1:
        return "hold", "insufficient_confirmation"
    score = float(scores.get("final_score", 0.5)) if scores else 0.5
    if score < 0.45:
        return "skip", "score_below_threshold"
    if score >= 0.70:
        return "publish", "high_score_now"
    return "schedule", "moderate_score_next_slot"


def decide_when(*, urgency: float = 0.5) -> tuple[str, Any]:
    """Return (timing, scheduled_at): now or next safe slot."""
    now = timezone.now()
    if urgency >= 0.85:
        return "now", now
    recent = Publication.objects.filter(
        status__in=[
            PublicationStatus.PUBLISHED,
            PublicationStatus.PUBLISHING,
            PublicationStatus.SCHEDULED,
        ],
        created_at__gte=now - timedelta(hours=1),
    ).count()
    if recent >= MAX_POSTS_PER_HOUR:
        return "schedule", now + timedelta(minutes=MIN_SPACING_MINUTES * 2)
    latest = (
        Publication.objects.filter(
            status__in=[
                PublicationStatus.PUBLISHED,
                PublicationStatus.PUBLISHING,
                PublicationStatus.SCHEDULED,
            ]
        )
        .order_by("-created_at")
        .first()
    )
    if latest and (now - latest.created_at).total_seconds() < MIN_SPACING_MINUTES * 60:
        return "schedule", latest.created_at + timedelta(minutes=MIN_SPACING_MINUTES)
    return "now", now


def decide_how(story) -> dict[str, Any]:
    """Return HOW style fields. One variant in V1; variants remain extensible."""
    depth = 2
    text_len = len((story.canonical_title or "") + (story.summary or ""))
    if text_len > 1500:
        depth = 3
    return {
        "headline_style": "factual_short",
        "tone": "neutral",
        "emoji_level": 0,
        "technical_depth": depth,
        "template_version": "tg-v1",
    }


def generate_validated_draft(story, *, provider=None, format_type: str = "STANDARD") -> dict:
    """Generate an evidence-grounded structured draft, critic review, media selection, and rendering."""
    import json

    evidence = story_evidence(story)
    evidence_json = json.dumps(evidence, sort_keys=True, default=str)[:12000]

    try:
        from apps.publishing.critic import DraftCriticService
        from apps.publishing.headlines import HeadlineEvaluator
        from apps.publishing.media_selection import MediaSelectionService
        from apps.publishing.renderer import TelegramRenderer

        result = run_structured_task(
            task="draft_generation",
            evidence=evidence,
            prompt_replacements={"__EVIDENCE__": evidence_json},
            provider=provider,
        )
        payload = result["payload"]
        direct = payload.get("headline_direct", "")
        breaking = payload.get("headline_breaking", "")
        contextual = payload.get("headline_contextual", "")

        best_headline, headline_audit = HeadlineEvaluator.evaluate_candidates(
            direct=direct, breaking=breaking, contextual=contextual, target_format=format_type
        )
        lead = payload.get("lead", "")
        body_points = payload.get("body_points", [])

        critic_res = DraftCriticService.review_draft(
            headline=best_headline,
            lead=lead,
            body_points=body_points,
            evidence_summary=evidence_json[:2000],
            provider=provider,
        )
        cleaned_lead = critic_res.get("cleaned_lead", lead)
        cleaned_points = critic_res.get("cleaned_body_points", body_points)

        media_list, media_audit = MediaSelectionService.select_media_group_for_story(
            story, max_items=5
        )
        selected_media = media_list[0] if media_list else None

        rendered = TelegramRenderer.render(
            story=story,
            headline=best_headline,
            lead=cleaned_lead,
            body_points=cleaned_points,
            why_it_matters=payload.get("why_it_matters"),
            context=payload.get("context"),
            update_line=payload.get("update_line"),
            format_type=format_type,
        )

        return {
            "headline": rendered.headline,
            "body": rendered.full_message,
            "payload": rendered.full_message,
            "caption": rendered.caption,
            "selected_media": selected_media,
            "selected_media_list": media_list,
            "editorial_metadata": {
                "headline_audit": headline_audit,
                "critic_review": critic_res,
                "media_audit": media_audit,
                "format_type": format_type,
            },
            "used_sources": list(evidence.get("evidence_urls", []))[:5],
            "model": result.get("model", ""),
            "provider": result.get("provider", ""),
            "prompt_version": result.get("prompt_version", "v2"),
        }
    except Exception:
        result = run_structured_task(
            task="post_draft",
            evidence=evidence,
            prompt_replacements={"__EVIDENCE__": evidence_json},
            provider=provider,
        )
        payload = result["payload"]
        allowed = set(evidence.get("evidence_urls", []))
        used = [u for u in payload.get("used_sources", []) if u in allowed]
        rendered = render_post(
            headline=payload.get("headline", ""),
            body=payload.get("body", ""),
            source_urls=used,
        )
        return {
            "headline": rendered.headline,
            "body": rendered.body,
            "payload": rendered.payload,
            "caption": rendered.payload[:1000] + "…",
            "selected_media": None,
            "editorial_metadata": {"fallback": True},
            "used_sources": used,
            "model": result.get("model", ""),
            "provider": result.get("provider", ""),
            "prompt_version": result.get("prompt_version", "v1"),
        }


def publish_story(
    story_id: int,
    *,
    dry_run: bool = False,
    force: bool = False,
    provider=None,
    publisher: PublisherPort | None = None,
    publication_version: int = 1,
    update_type: str = UpdateType.INITIAL,
    parent_publication_id: int | None = None,
) -> dict[str, Any]:
    """Publish one story once. Retries reuse the idempotency key row."""
    from apps.ranking.services.selection import SelectionService
    from apps.stories.models import Story

    story = Story.objects.get(pk=story_id)
    eval_res = SelectionService.evaluate_story(story)
    what = eval_res.get("action", "skip")
    what_reason = eval_res.get("reason", "")
    if what in ("skip", "hold") and not force and not dry_run:
        return {"status": what, "reason": what_reason, "story_id": story.pk}

    urgency = 0.5
    try:
        urgency = float(story.news_value.urgency)
    except Exception:
        urgency = 0.5
    timing, scheduled_at = decide_when(urgency=urgency)
    style = decide_how(story)
    format_type = "BREAKING" if urgency >= 0.85 else "STANDARD"
    draft = generate_validated_draft(story, provider=provider, format_type=format_type)

    channel = _channel() or "@dry-run-channel"
    content_fingerprint = hashlib.sha256(
        f"{draft['headline']}\n{draft['payload']}".encode()
    ).hexdigest()
    version_str = f"-v{publication_version}" if publication_version > 1 else ""
    key = f"story-{story.pk}{version_str}-{content_fingerprint[:16]}"
    if dry_run:
        return {
            "status": "dry_run",
            "story_id": story.pk,
            "what": what,
            "when": timing,
            "how": style,
            "headline": draft["headline"],
            "payload_chars": len(draft["payload"]),
            "idempotency_key": key,
            "publication_version": publication_version,
            "update_type": update_type,
        }

    existing = Publication.objects.filter(idempotency_key=key).first()
    if existing and existing.status == PublicationStatus.PUBLISHED:
        return {"status": "duplicate", "publication_id": existing.pk, "story_id": story.pk}

    selected_media = draft.get("selected_media")

    with transaction.atomic():
        publication, created = Publication.objects.get_or_create(
            idempotency_key=key,
            defaults={
                "story": story,
                "channel": channel,
                "status": PublicationStatus.DRAFT,
                "publication_version": publication_version,
                "update_type": update_type,
                "parent_publication_id": parent_publication_id,
                "update_sequence": max(0, publication_version - 1),
                "headline": draft["headline"][:1024],
                "content": draft["payload"],
                "template_version": style["template_version"],
                "headline_style": style["headline_style"],
                "tone": style["tone"],
                "emoji_level": style["emoji_level"],
                "technical_depth": style["technical_depth"],
                "scheduled_at": scheduled_at,
                "selected_media": selected_media,
                "telegram_chat_id": channel,
                "editorial_metadata": draft.get("editorial_metadata", {}),
            },
        )
        if not created and publication.status == PublicationStatus.PUBLISHED:
            return {
                "status": "duplicate",
                "publication_id": publication.pk,
                "story_id": story.pk,
            }
        publication.headline = draft["headline"][:1024]
        publication.content = draft["payload"]
        publication.template_version = style["template_version"]
        publication.headline_style = style["headline_style"]
        publication.tone = style["tone"]
        publication.emoji_level = style["emoji_level"]
        publication.technical_depth = style["technical_depth"]
        publication.scheduled_at = scheduled_at
        publication.selected_media = selected_media
        publication.telegram_chat_id = channel
        publication.editorial_metadata = draft.get("editorial_metadata", {})
        publication.save()

        for target in ("ready", "approved"):
            try:
                publication.transition(
                    PublicationStatus.READY if target == "ready" else PublicationStatus.APPROVED
                )
            except Exception:
                pass

    if not _auto_publish_enabled() and not force:
        return {
            "status": "held",
            "reason": "auto_publish_disabled",
            "publication_id": publication.pk,
            "story_id": story.pk,
        }

    try:
        publication.transition(PublicationStatus.PUBLISHING)
    except Exception:
        pass

    active_publisher = publisher or _publisher()
    selected_media_list = draft.get("selected_media_list") or (
        [selected_media] if selected_media else []
    )
    photo_targets = [
        (m.telegram_file_id or m.original_url)
        for m in selected_media_list
        if (m.telegram_file_id or m.original_url)
    ]

    try:
        send_result = active_publisher.send(
            chat_id=channel,
            payload=draft["payload"],
            caption=draft.get("caption"),
            photo=photo_targets[0] if len(photo_targets) == 1 else None,
            photos=photo_targets if len(photo_targets) >= 2 else None,
            parse_mode="HTML",
        )
    except TimeoutError as exc:
        publication.last_error = sanitize_error_message(str(exc))[:500]
        publication.attempt_count += 1
        publication.save(update_fields=["last_error", "attempt_count", "updated_at"])
        return {
            "status": "retry",
            "publication_id": publication.pk,
            "reason": "telegram_timeout",
        }
    except Exception as exc:
        publication.last_error = sanitize_error_message(str(exc))[:500]
        publication.attempt_count += 1
        publication.save(update_fields=["last_error", "attempt_count", "updated_at"])
        try:
            publication.transition(PublicationStatus.FAILED)
        except Exception:
            pass
        raise PublishingError(publication.last_error) from exc

    if send_result.get("error") == "retry_after":
        publication.last_error = f"retry_after:{send_result.get('retry_after')}"
        publication.attempt_count += 1
        publication.save(update_fields=["last_error", "attempt_count", "updated_at"])
        return {
            "status": "retry",
            "publication_id": publication.pk,
            "reason": "telegram_retry_after",
        }

    # If new file_ids were returned from upload, cache them on respective MediaAssets
    new_file_ids = send_result.get("file_ids") or (
        [send_result.get("file_id")] if send_result.get("file_id") else []
    )
    for idx, fid in enumerate(new_file_ids):
        if idx < len(selected_media_list) and fid:
            asset = selected_media_list[idx]
            if not asset.telegram_file_id:
                asset.telegram_file_id = str(fid)[:256]
                asset.save(update_fields=["telegram_file_id", "updated_at"])

    publication.external_message_id = str(send_result.get("message_id", ""))[:128]
    publication.telegram_message_id = str(send_result.get("message_id", ""))[:128]
    publication.delivery_metadata = send_result
    publication.published_at = timezone.now()
    publication.save(
        update_fields=[
            "external_message_id",
            "telegram_message_id",
            "delivery_metadata",
            "published_at",
            "updated_at",
        ]
    )

    try:
        publication.transition(PublicationStatus.PUBLISHED)
    except IntegrityError:
        duplicate = Publication.objects.filter(idempotency_key=key).first()
        return {
            "status": "duplicate",
            "publication_id": duplicate.pk if duplicate else publication.pk,
            "story_id": story.pk,
        }
    return {
        "status": "published",
        "publication_id": publication.pk,
        "message_id": publication.external_message_id,
        "story_id": story.pk,
        "publication_version": publication.publication_version,
    }


def publish_material_update(
    story_id: int,
    *,
    update_type: str = UpdateType.MATERIAL_UPDATE,
    dry_run: bool = False,
    publisher: PublisherPort | None = None,
) -> dict[str, Any]:
    """Publish a new versioned row for a material update or correction."""
    from apps.stories.models import Story

    story = Story.objects.get(pk=story_id)
    latest_pub = Publication.objects.filter(story=story).order_by("-publication_version").first()
    next_version = (latest_pub.publication_version + 1) if latest_pub else 2
    if next_version < 2:
        next_version = 2
    parent_id = latest_pub.pk if latest_pub else None

    return publish_story(
        story_id,
        dry_run=dry_run,
        force=True,
        provider=None,
        publisher=publisher,
        publication_version=next_version,
        update_type=update_type,
        parent_publication_id=parent_id,
    )


def make_dry_run_idempotency_key() -> str:
    return f"dry-run-{uuid.uuid4().hex[:16]}"
