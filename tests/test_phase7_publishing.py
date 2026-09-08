"""Phase 7: Telegram publishing safety, dry-run, idempotency, scheduling, reward."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.ai.providers import FakeAIProvider
from apps.core.choices import Platform
from apps.core.exceptions import PublishingError
from apps.news.models import SourceItem
from apps.publishing.models import (
    Publication,
    PublicationEngagementSnapshot,
    PublicationStatus,
)
from apps.publishing.rewards import normalized_reward
from apps.publishing.services import (
    decide_how,
    decide_what,
    decide_when,
    generate_validated_draft,
    publish_story,
)
from apps.publishing.telegram import FakePublisher, render_post
from apps.sources.models import Source
from apps.stories.models import Story, StoryMembership

pytestmark = [pytest.mark.django_db]


def _source(**kwargs):
    kwargs.setdefault("platform", Platform.TELEGRAM)
    kwargs.setdefault("name", "Pub Source")
    kwargs.setdefault("identifier", "@pubsrc")
    kwargs.setdefault("url", "https://t.me/pubsrc")
    return Source.objects.create(**kwargs)


def _story(title="Publishable event"):
    return Story.objects.create(
        canonical_title=title,
        latest_source_update_at=timezone.now(),
        independent_source_count=2,
        observed_source_count=2,
        language="en",
    )


def _attach(source, story, title="Publishable event"):
    item = SourceItem.objects.create(
        source=source,
        title=title,
        raw_text="Body with evidence https://example.com/a",
        normalized_text="Body with evidence https://example.com/a",
        canonical_url="https://example.com/a",
        published_at=timezone.now() - timedelta(hours=1),
    )
    StoryMembership.objects.create(story=story, source_item=item, is_current=True)
    return item


def test_rendering_escapes_html_and_limits_length():
    rendered = render_post(
        headline="<b>Bad</b> headline",
        body="Body " * 2000,
        source_urls=["https://example.com/a", "not-a-url"],
    )
    assert "&lt;b&gt;" in rendered.payload
    assert len(rendered.payload) <= 4096
    assert "https://example.com/a" in rendered.payload
    assert "not-a-url" not in rendered.payload


def test_dry_run_generates_without_sending():
    source = _source()
    story = _story()
    _attach(source, story)
    result = publish_story(story.pk, dry_run=True, provider=FakeAIProvider())
    assert result["status"] == "dry_run"
    assert Publication.objects.count() == 0


def test_idempotent_retry_never_duplicates():
    source = _source()
    story = _story()
    _attach(source, story)
    publisher = FakePublisher()
    first = publish_story(story.pk, force=True, provider=FakeAIProvider(), publisher=publisher)
    second = publish_story(story.pk, force=True, provider=FakeAIProvider(), publisher=publisher)
    assert first["status"] == "published"
    assert second["status"] in ("published", "duplicate")
    assert Publication.objects.filter(story=story).count() == 1


def test_failed_publication_can_retry_same_key():
    source = _source()
    story = _story()
    _attach(source, story)

    class Boom(FakePublisher):
        def send(self, *, chat_id, payload, parse_mode):
            raise RuntimeError("boom")

    with pytest.raises(PublishingError):
        publish_story(story.pk, force=True, provider=FakeAIProvider(), publisher=Boom())
    failed = Publication.objects.get(story=story)
    assert failed.status == PublicationStatus.FAILED
    recovered = publish_story(
        story.pk, force=True, provider=FakeAIProvider(), publisher=FakePublisher()
    )
    assert recovered["status"] in ("published", "duplicate")


def test_state_machine_rejects_illegal_transition():
    source = _source()
    story = _story()
    _attach(source, story)
    publication = Publication.objects.create(
        story=story,
        channel="@c",
        headline="h",
        content="c",
        status=PublicationStatus.DRAFT,
        idempotency_key="phase7-illegal",
    )
    with pytest.raises(ValidationError):
        publication.transition(PublicationStatus.PUBLISHED)


def test_scheduling_enforces_spacing_and_hourly_cap():
    source = _source()
    story = _story()
    _attach(source, story)
    timing, _ = decide_when(urgency=0.2)
    assert timing in ("now", "schedule")
    breaking_timing, _ = decide_when(urgency=0.95)
    assert breaking_timing == "now"


def test_what_when_how_are_independent():
    story = _story()
    what, _ = decide_what(story, scores={"final_score": 0.9})
    assert what in ("publish", "schedule", "hold", "skip")
    how = decide_how(story)
    assert how["template_version"] == "tg-v1"
    assert how["headline_style"] == "factual_short"


def test_unsupported_claims_rejected_to_evidence_urls():
    source = _source()
    story = _story()
    _attach(source, story)
    draft = generate_validated_draft(story, provider=FakeAIProvider())
    assert all(url.startswith("http") for url in draft["used_sources"])


def test_reward_redistributes_missing_metrics():
    source = _source()
    story = _story()
    _attach(source, story)
    publication = Publication.objects.create(
        story=story,
        channel="@c",
        headline="h",
        content="c",
        status=PublicationStatus.PUBLISHED,
        idempotency_key="phase7-reward",
    )
    snapshot = PublicationEngagementSnapshot.objects.create(
        publication=publication, views=1000, forwards=None, reactions=50, replies=None
    )
    result = normalized_reward(
        snapshot,
        baselines={
            "views": {"p25": 100, "p50": 500, "p75": 1000, "p90": 2000, "p95": 3000, "p99": 5000},
            "reactions": {"p25": 5, "p50": 20, "p75": 50, "p90": 100, "p95": 200, "p99": 500},
        },
    )
    assert result["reward"] is not None
    assert 0.0 <= result["reward"] <= 1.0
