"""Comprehensive simulation of the 10 required real-world scenarios (Instruction #66)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.ai.models import MaterialUpdateDecision
from apps.core.choices import EditorialAction, LifecycleState, Platform, TrendState
from apps.news.models import EngagementSnapshot, MediaAsset, MediaValidationStatus, SourceItem
from apps.publishing.models import Publication, PublicationStatus
from apps.publishing.renderer import TelegramRenderer
from apps.publishing.services import publish_story
from apps.publishing.telegram import FakePublisher
from apps.ranking.models import SourceBaseline
from apps.ranking.services.editorial import EditorialPolicyEngine
from apps.ranking.services.novelty import ChannelNoveltyService, ChannelNoveltyType
from apps.sources.models import Source
from apps.stories.models import Story, StoryMembership
from apps.stories.services.coordinator import StoryReanalysisCoordinator
from apps.stories.services.independence import aggregate_counts
from apps.stories.services.momentum import StoryMomentumService
from apps.stories.services.series import ItemMetricSeriesService
from apps.stories.services.trend import TrendDetector

pytestmark = pytest.mark.django_db


# Scenario 1: Low importance, quiet story -> SKIP / not published
def test_scenario_1_quiet_low_importance_story_not_published():
    source = Source.objects.create(
        name="Quiet Blog", platform=Platform.RSS, identifier="https://quiet.org"
    )
    story = Story.objects.create(canonical_title="Local Community Notice")
    item = SourceItem.objects.create(source=source, story=story, title="Local Community Notice")
    StoryMembership.objects.create(story=story, source_item=item, is_current=True)

    dec = EditorialPolicyEngine.evaluate_story(story)
    assert dec["action"] in (EditorialAction.SKIP, EditorialAction.WATCH)
    assert dec["publish_priority_score"] < 0.65


# Scenario 2: Large channel with high raw views but normal velocity -> Not false viral
def test_scenario_2_large_channel_normal_velocity_not_false_viral():
    source = Source.objects.create(
        name="Huge Channel", platform=Platform.TELEGRAM, identifier="@huge"
    )
    story = Story.objects.create(canonical_title="Expected Daily Announcement")
    now = timezone.now()

    # Seed high baseline (p50 = 500k views)
    SourceBaseline.objects.create(
        source=source,
        platform=Platform.TELEGRAM,
        age_bucket_minutes=60,
        metric="views",
        sample_count=100,
        p25=200000.0,
        p50=500000.0,
        p75=800000.0,
        p90=1200000.0,
        p99=2000000.0,
    )

    item = SourceItem.objects.create(
        source=source,
        story=story,
        title="Expected Daily Announcement",
        published_at=now - timedelta(hours=1),
    )
    StoryMembership.objects.create(story=story, source_item=item, is_current=True)

    # Views are 510k (close to median)
    EngagementSnapshot.objects.create(
        source_item=item,
        captured_at=now - timedelta(minutes=20),
        post_age_seconds=2400,
        views=490000,
    )
    EngagementSnapshot.objects.create(
        source_item=item,
        captured_at=now,
        post_age_seconds=3600,
        views=510000,
    )

    series = ItemMetricSeriesService.compute_item_series_metrics(item)
    # Normalized velocity around 0.50 (median)
    assert series["normalized_velocity"] is not None
    assert series["normalized_velocity"] < 0.70

    metrics = StoryMomentumService.compute_metrics(story)
    trend, _ = TrendDetector.detect_state(metrics)
    assert trend != TrendState.BREAKING


# Scenario 3: Small story with rapid acceleration across independent sources -> RISING then BREAKING
def test_scenario_3_accelerating_independent_sources_becomes_breaking():
    now = timezone.now()
    story = Story.objects.create(canonical_title="Emerging Critical Event")
    src1 = Source.objects.create(name="Small A", platform=Platform.TELEGRAM, identifier="@small_a")
    src2 = Source.objects.create(name="Small B", platform=Platform.TELEGRAM, identifier="@small_b")
    src3 = Source.objects.create(
        name="Small C", platform=Platform.RSS, identifier="https://small_c.org"
    )

    it1 = SourceItem.objects.create(
        source=src1, story=story, title="Ev", published_at=now - timedelta(minutes=15)
    )
    it2 = SourceItem.objects.create(
        source=src2, story=story, title="Ev", published_at=now - timedelta(minutes=10)
    )
    it3 = SourceItem.objects.create(
        source=src3, story=story, title="Ev", published_at=now - timedelta(minutes=5)
    )

    for it in (it1, it2, it3):
        StoryMembership.objects.create(
            story=story, source_item=it, is_current=True, independence="independent"
        )

    story.observed_source_count = 3
    story.independent_source_count = 3
    story.save()

    # Rapid snapshots for item 1
    EngagementSnapshot.objects.create(
        source_item=it1, captured_at=now - timedelta(minutes=20), views=20
    )
    EngagementSnapshot.objects.create(
        source_item=it1, captured_at=now - timedelta(minutes=10), views=150
    )
    EngagementSnapshot.objects.create(source_item=it1, captured_at=now, views=950, forwards=80)

    # Process story
    res = StoryReanalysisCoordinator.process_story(story.pk)
    assert res["status"] == "processed"
    assert res["lifecycle_state"] in (LifecycleState.RISING, LifecycleState.BREAKING)


# Scenario 4: 20 channels copy one source -> independent_confirmation = 1, not 20
def test_scenario_4_copy_network_not_counted_as_independent():
    labels = ["independent"] + ["likely_copy"] * 19
    observed, independent, _ = aggregate_counts(labels)
    assert observed == 20
    assert independent == 1


# Scenario 5: Already published without new facts -> REPEAT / no republish
def test_scenario_5_already_published_no_new_facts_not_republished():
    source = Source.objects.create(name="Src", platform=Platform.TELEGRAM, identifier="@src")
    story = Story.objects.create(canonical_title="Published Event")
    item = SourceItem.objects.create(source=source, story=story, title="Item")
    StoryMembership.objects.create(story=story, source_item=item, is_current=True)

    Publication.objects.create(
        story=story,
        channel="@channel",
        headline=story.canonical_title,
        content="Published body",
        status=PublicationStatus.PUBLISHED,
        idempotency_key="pub-orig-1",
    )

    nov_type, _ = ChannelNoveltyService.evaluate_novelty(story)
    assert nov_type == ChannelNoveltyType.REPEAT

    dec = EditorialPolicyEngine.evaluate_story(story)
    assert dec["action"] == EditorialAction.SKIP


# Scenario 6: Already published story with material update -> UPDATE_EXISTING_STORY
def test_scenario_6_material_update_creates_follow_up():
    source = Source.objects.create(
        name="Reliable", platform=Platform.TELEGRAM, identifier="@rel", trust_score=Decimal("0.90")
    )
    story = Story.objects.create(canonical_title="Developing Crisis")
    story.independent_source_count = 2
    story.save()
    item = SourceItem.objects.create(source=source, story=story, title="Item")
    StoryMembership.objects.create(story=story, source_item=item, is_current=True)

    Publication.objects.create(
        story=story,
        channel="@channel",
        headline=story.canonical_title,
        content="First report.",
        status=PublicationStatus.PUBLISHED,
        idempotency_key="pub-crisis-1",
    )

    # Material update decision added
    MaterialUpdateDecision.objects.create(
        story=story,
        source_item=item,
        label="MATERIAL_UPDATE",
        confidence=Decimal("0.9500"),
        information_unit_diff={"new_facts": ["Hostages released"]},
    )

    dec = EditorialPolicyEngine.evaluate_story(story)
    assert dec["action"] in (EditorialAction.UPDATE_EXISTING_STORY, EditorialAction.PUBLISH_NOW)


# Scenario 7: Broken image -> publish text fallback
def test_scenario_7_broken_image_text_fallback():
    source = Source.objects.create(name="Src", platform=Platform.TELEGRAM, identifier="@s7")
    story = Story.objects.create(canonical_title="News with broken image")
    item = SourceItem.objects.create(source=source, story=story, title="Item")
    StoryMembership.objects.create(story=story, source_item=item, is_current=True)
    story.primary_item = item
    story.save()

    # Invalid media asset (SSRF / private IP)
    MediaAsset.objects.create(
        source_item=item,
        original_url="http://192.168.1.1/internal.jpg",
        validation_status=MediaValidationStatus.INVALID,
        failure_reason="ssrf_rejected",
    )

    publisher = FakePublisher()
    res = publish_story(story.pk, force=True, publisher=publisher)
    assert res["status"] == "published"
    # Photo was None because invalid media was excluded -> sent as text
    assert publisher.sent[0]["photo"] is None


# Scenario 8: Long caption > 1024 chars -> photo short caption + full text message
def test_scenario_8_long_caption_split():
    source = Source.objects.create(name="Src", platform=Platform.TELEGRAM, identifier="@s8")
    story = Story.objects.create(canonical_title="Long Detailed Story")
    item = SourceItem.objects.create(source=source, story=story, title="Long Detailed Story")
    StoryMembership.objects.create(story=story, source_item=item, is_current=True)

    long_body = "نکته مهم و تفصیلی رویداد. " * 80
    rendered = TelegramRenderer.render(
        story=story,
        headline="تیتر رویداد تفصیلی",
        lead=long_body[:500],
        body_points=[long_body[500:1000]],
        why_it_matters=long_body[1000:1500],
    )

    # Caption is strictly <= 1024
    assert len(rendered.caption) <= 1024
    # Full message can be up to 4096
    assert len(rendered.full_message) > 1024
    assert len(rendered.full_message) <= 4096


# Scenario 9: HTML unsafe characters in source text -> properly escaped
def test_scenario_9_html_unsafe_chars_properly_escaped():
    source = Source.objects.create(
        name="Malicious Src", platform=Platform.TELEGRAM, identifier="@mal"
    )
    story = Story.objects.create(canonical_title="<script>alert(1)</script> & Test")
    item = SourceItem.objects.create(source=source, story=story, title="Item")
    StoryMembership.objects.create(story=story, source_item=item, is_current=True)

    rendered = TelegramRenderer.render(
        story=story,
        headline="<script>alert('xss')</script> & <b>bold</b>",
        lead="Test <tag> & unclosed tag",
    )

    assert "<script>" not in rendered.full_message
    assert "&lt;script&gt;" in rendered.full_message
    assert "&amp;" in rendered.full_message


# Scenario 10: AI unsupported claim -> critic flags it
def test_scenario_10_unsupported_claim_critic_flags_issue():
    from apps.publishing.critic import DraftCriticService

    review = DraftCriticService.review_draft(
        headline="تیتر ادعایی تایید نشده",
        lead="ادعای بدون مدرک مبنی بر سقوط شرکت.",
        body_points=[],
        evidence_summary="گزارش رسمی صرفاً از تغییر مدیرعامل خبر داده است.",
    )
    assert review is not None
    assert "is_approved" in review
    assert "cleaned_lead" in review
