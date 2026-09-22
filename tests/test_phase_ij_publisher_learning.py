"""Tests for Phase I (Telegram Publisher) and Phase J (Audience Learning & Rewards)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.core.choices import Platform
from apps.news.models import MediaAsset, MediaValidationStatus, SourceItem
from apps.publishing.models import Publication, UpdateType
from apps.publishing.rewards import normalized_reward
from apps.publishing.services import publish_material_update, publish_story
from apps.publishing.telegram import FakePublisher
from apps.sources.models import Source
from apps.stories.models import Story, StoryMembership

pytestmark = pytest.mark.django_db


def _create_story_with_media():
    source = Source.objects.create(
        name="Tech Source", platform=Platform.TELEGRAM, identifier="@tech_src"
    )
    story = Story.objects.create(canonical_title="Quantum Processor Released")
    item = SourceItem.objects.create(
        source=source,
        story=story,
        title="Quantum Processor Released",
        canonical_url="https://example.com/item",
        external_id="ext-q1",
    )
    StoryMembership.objects.create(story=story, source_item=item, is_current=True, is_primary=True)
    story.primary_item = item
    story.save()

    media = MediaAsset.objects.create(
        source_item=item,
        original_url="https://example.com/quantum.jpg",
        width=1280,
        height=720,
        validation_status=MediaValidationStatus.VALID,
    )
    return story, item, media


def test_publish_story_with_photo():
    story, item, media = _create_story_with_media()
    publisher = FakePublisher()

    res = publish_story(story.pk, force=True, publisher=publisher)
    assert res["status"] == "published"
    assert len(publisher.sent) == 1
    sent = publisher.sent[0]
    assert sent["photo"] == "https://example.com/quantum.jpg"
    assert "Quantum Processor Released" in sent["payload"] or "هوش مصنوعی" in sent["payload"]

    # File ID cached on MediaAsset
    media.refresh_from_db()
    assert media.telegram_file_id.startswith("fake-file-")

    # Publication has selected media and telegram ids
    pub = Publication.objects.get(pk=res["publication_id"])
    assert pub.selected_media == media
    assert pub.telegram_message_id == res["message_id"]


def test_publish_photo_failure_text_fallback():
    story, item, media = _create_story_with_media()

    class FailPhotoPublisher(FakePublisher):
        def send(
            self,
            *,
            chat_id,
            payload,
            caption=None,
            photo=None,
            photos=None,
            reply_to_message_id=None,
            parse_mode="HTML",
        ):
            if photo or photos:
                # Simulate Telegram photo failure and automatic fallback
                self.sent.append({"chat_id": chat_id, "payload": payload, "fallback": True})
                return {"message_id": "fallback-text-99", "photo_fallback": True}
            return super().send(chat_id=chat_id, payload=payload)

    pub_sim = FailPhotoPublisher()
    res = publish_story(story.pk, force=True, publisher=pub_sim)
    assert res["status"] == "published"
    assert res["message_id"] == "fallback-text-99"

    pub = Publication.objects.get(pk=res["publication_id"])
    assert pub.delivery_metadata.get("photo_fallback") is True


def test_publish_material_update_follow_up():
    story, item, media = _create_story_with_media()
    publisher = FakePublisher()

    # Initial publication
    res1 = publish_story(story.pk, force=True, publisher=publisher)
    assert res1["status"] == "published"

    # Material update follow-up
    res2 = publish_material_update(story.pk, update_type=UpdateType.MATERIAL_UPDATE)
    assert res2["status"] == "published"
    assert res2["publication_version"] == 2


def test_audience_learning_reward_decomposition_and_guardrail():
    # 1. Normal performance with high credibility
    snapshot = SimpleNamespace(
        views=5000,
        forwards=200,
        reactions=80,
        replies=30,
    )
    baselines = {
        "views": {"p25": 1000, "p50": 3000, "p75": 6000, "p90": 10000},
        "forwards": {"p25": 20, "p50": 100, "p75": 300, "p90": 600},
        "reactions": {"p25": 10, "p50": 50, "p75": 100, "p90": 200},
        "replies": {"p25": 5, "p50": 20, "p75": 50, "p90": 100},
    }

    reward_res = normalized_reward(snapshot, baselines=baselines, credibility_score=0.90)
    assert reward_res["reward"] is not None
    assert 0.0 <= reward_res["reward"] <= 1.0
    assert reward_res["view_reward"] is not None
    assert reward_res["forward_reward"] is not None
    assert reward_res["quality_guardrail"] == 1.0

    # 2. Clickbait guardrail: high views but low credibility caps reward
    clickbait_snap = SimpleNamespace(views=100000, forwards=5000, reactions=2000, replies=500)
    capped_res = normalized_reward(clickbait_snap, baselines=baselines, credibility_score=0.20)
    assert capped_res["quality_guardrail"] < 1.0
    # Capped reward should not exceed 0.40
    assert capped_res["reward"] <= 0.40


def test_publish_story_with_media_group_album():
    story, item, media1 = _create_story_with_media()
    # Add second and third high-res valid media assets
    media2 = MediaAsset.objects.create(
        source_item=item,
        original_url="https://example.com/quantum2.jpg",
        width=1280,
        height=720,
        validation_status=MediaValidationStatus.VALID,
    )
    media3 = MediaAsset.objects.create(
        source_item=item,
        original_url="https://example.com/quantum3.jpg",
        width=1280,
        height=720,
        validation_status=MediaValidationStatus.VALID,
    )

    publisher = FakePublisher()
    res = publish_story(story.pk, force=True, publisher=publisher)
    assert res["status"] == "published"
    assert len(publisher.sent) == 1
    sent = publisher.sent[0]
    assert sent["is_media_group"] is True
    assert len(sent["photos"]) >= 2

    # Multiple file_ids cached across media assets
    media1.refresh_from_db()
    media2.refresh_from_db()
    media3.refresh_from_db()
    assert media1.telegram_file_id
    assert media2.telegram_file_id
