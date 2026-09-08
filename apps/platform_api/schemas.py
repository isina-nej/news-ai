"""Response schemas. Raw payloads excluded by default; secrets never exposed."""

from __future__ import annotations

from ninja import Field, Schema


class SourceOut(Schema):
    id: int
    platform: str
    name: str
    identifier: str
    enabled: bool
    language: str
    consecutive_failures: int


class SourceItemOut(Schema):
    id: int
    source_id: int
    title: str
    language: str
    status: str
    published_at: str | None = None
    collected_at: str | None = None
    story_id: int | None = None


class StoryMemberOut(Schema):
    source_item_id: int
    similarity_score: float | None = None
    match_method: str
    independence: str
    is_primary: bool
    is_current: bool


class ScoreOut(Schema):
    algorithm_version: str
    news_value: float
    audience_fit: float
    momentum: float
    final_score: float


class StoryOut(Schema):
    id: int
    canonical_title: str
    status: str
    language: str
    observed_source_count: int
    independent_source_count: int
    primary_item_id: int | None = None


class StoryDetailOut(StoryOut):
    members: list[StoryMemberOut] = Field(default_factory=list)
    scores: list[ScoreOut] = Field(default_factory=list)
    publications: list[dict] = Field(default_factory=list)


class PublicationOut(Schema):
    id: int
    story_id: int
    channel: str
    status: str
    headline: str = ""


class FetchRunOut(Schema):
    id: int
    source_id: int
    status: str
    fetched_count: int = 0
    created_count: int = 0
    duplicate_count: int = 0


class TopicOut(Schema):
    id: int
    slug: str
    name: str
    enabled: bool


class FlagOut(Schema):
    key: str
    enabled: bool
