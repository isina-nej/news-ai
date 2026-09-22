"""Tests for Phase G (Drafting & Telegram Rendering) and Phase H (Media Pipeline)."""

from __future__ import annotations

import pytest

from apps.core.choices import Platform
from apps.news.models import MediaAsset, MediaValidationStatus, SourceItem
from apps.news.services.media_ingest import MediaIngestionService
from apps.news.services.media_validation import MediaValidationService
from apps.publishing.attribution import SourceAttributionService
from apps.publishing.critic import clean_persian_text
from apps.publishing.headlines import HeadlineEvaluator
from apps.publishing.media_selection import MediaSelectionService
from apps.publishing.payload import FinalPublicationPayload, validate_publication_payload
from apps.publishing.renderer import TelegramRenderer
from apps.sources.models import Source
from apps.stories.models import Story, StoryMembership

pytestmark = pytest.mark.django_db


def test_headline_evaluator_format_and_clickbait():
    direct = "شرکت پیشرو مدل محاسباتی جدیدی عرضه کرد"
    breaking = "فوری: رونمایی از مدل محاسباتی جدید"
    clickbait = "باورنکردنی و شوکه‌کننده: این مدل همه چیز را عوض کرد"

    # BREAKING format selects breaking headline
    sel_breaking, audit_b = HeadlineEvaluator.evaluate_candidates(
        direct=direct, breaking=breaking, contextual="زمینه رویداد", target_format="BREAKING"
    )
    assert sel_breaking == breaking
    assert audit_b["selected_style"] == "BREAKING"

    # Clickbait penalized
    _, audit_c = HeadlineEvaluator.evaluate_candidates(
        direct=clickbait, breaking=breaking, contextual=direct, target_format="STANDARD"
    )
    assert any("clickbait_detected" in f for f in audit_c["flags"]["DIRECT"])


def test_clean_persian_text_filler_removal():
    raw = "لازم به ذکر است که تیم توسعه نسخه نهایی را منتشر کرد."
    cleaned = clean_persian_text(raw)
    assert "لازم به ذکر است" not in cleaned
    assert "تیم توسعه نسخه نهایی را منتشر کرد." in cleaned


def test_source_attribution_rendering():
    source = Source.objects.create(
        name="Reuters",
        platform=Platform.RSS,
        url="https://reuters.com",
        identifier="https://reuters.com",
    )
    story = Story.objects.create(canonical_title="Global Summit")
    item = SourceItem.objects.create(
        source=source, story=story, title="Summit", canonical_url="https://reuters.com/1"
    )
    StoryMembership.objects.create(story=story, source_item=item, is_current=True, is_primary=True)
    story.primary_item = item
    story.independent_source_count = 3
    story.save()

    block = SourceAttributionService.render_attribution_block(story)
    assert "Reuters" in block
    assert "https://reuters.com/1" in block
    assert "تأیید مستقل" in block
    assert "3 منبع" in block


def test_telegram_renderer_formats():
    source = Source.objects.create(
        name="TechSource", platform=Platform.TELEGRAM, identifier="@tech"
    )
    story = Story.objects.create(canonical_title="AI Breakthrough Announced")
    item = SourceItem.objects.create(source=source, story=story, title="Post")
    StoryMembership.objects.create(story=story, source_item=item, is_current=True)

    # 1. Standard format
    rendered_std = TelegramRenderer.render(
        story=story,
        headline="تیتر استاندارد خبر",
        lead="لید کوتاه و دقیق رویداد.",
        body_points=["نکته اول", "نکته دوم"],
        why_it_matters="پیامد مهم در صنعت",
        format_type="STANDARD",
    )
    assert "📰 <b>تیتر استاندارد خبر</b>" in rendered_std.full_message
    assert "• نکته اول" in rendered_std.full_message
    assert "<b>چرا مهم است؟</b>" in rendered_std.full_message
    assert len(rendered_std.caption) <= 1024
    assert len(rendered_std.full_message) <= 4096

    # 2. Breaking format
    rendered_brk = TelegramRenderer.render(
        story=story,
        headline="تیتر فوری",
        lead="لید فوری و ضربتی.",
        format_type="BREAKING",
    )
    assert "🚨 <b>تیتر فوری</b>" in rendered_brk.full_message
    assert len(rendered_brk.caption) <= 1024


def test_final_publication_payload_validation():
    # Valid payload
    payload = FinalPublicationPayload(
        story_id=1,
        format="STANDARD",
        headline="تیتر معتبر",
        caption="<b>کپشن معتبر</b>",
        message="<b>پیام کامل معتبر</b>",
    )
    is_valid, reason = validate_publication_payload(payload)
    assert is_valid is True
    assert reason == "valid"

    # Unbalanced HTML
    unbalanced = FinalPublicationPayload(
        story_id=1,
        format="STANDARD",
        headline="تیتر",
        caption="<b>نامعتبر",
        message="<b>تگ بسته نشده",
    )
    is_valid_u, reason_u = validate_publication_payload(unbalanced)
    assert is_valid_u is False
    assert "unbalanced_html_tag" in reason_u


def test_media_ingestion_and_validation():
    source = Source.objects.create(
        name="Photo Channel", platform=Platform.TELEGRAM, identifier="@photo"
    )
    item = SourceItem.objects.create(
        source=source,
        title="Photo Post",
        media={
            "kind": "photo",
            "url": "https://example.com/valid-image.png",
            "width": 1280,
            "height": 720,
            "mime_type": "image/png",
        },
    )

    assets = MediaIngestionService.harvest_item_media(item)
    assert len(assets) == 1
    asset = assets[0]
    assert asset.source_item == item
    assert asset.width == 1280

    # Validation
    is_valid = MediaValidationService.validate_asset(asset)
    assert is_valid is True
    assert asset.validation_status == MediaValidationStatus.VALID


def test_media_selection_scores_high_resolution():
    source = Source.objects.create(
        name="Photo Src", platform=Platform.RSS, identifier="https://ex.com"
    )
    story = Story.objects.create(canonical_title="Visual Event")
    item = SourceItem.objects.create(source=source, story=story, title="Item")
    StoryMembership.objects.create(story=story, source_item=item, is_current=True)
    story.primary_item = item
    story.save()

    # Asset 1: Thumbnail (poor)
    MediaAsset.objects.create(
        source_item=item,
        original_url="https://example.com/thumb.jpg",
        width=150,
        height=150,
        validation_status=MediaValidationStatus.VALID,
    )

    # Asset 2: 16:9 High Res (optimal)
    optimal = MediaAsset.objects.create(
        source_item=item,
        original_url="https://example.com/highres.jpg",
        width=1280,
        height=720,
        aspect_ratio=1.7778,
        validation_status=MediaValidationStatus.VALID,
    )

    selected, audit = MediaSelectionService.select_media_for_story(story)
    assert selected is not None
    assert selected.pk == optimal.pk
    assert audit["score"] >= 0.70
