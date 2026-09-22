"""Shared enums. Single source of truth, no migrations when reused across apps."""

from django.db import models


class Platform(models.TextChoices):
    TELEGRAM = "telegram", "Telegram"
    TWITTER_X = "twitter_x", "Twitter/X"
    RSS = "rss", "RSS/Atom"
    RSSHUB = "rsshub", "RSSHub"
    WEBSITE_HTML = "website_html", "Website (static HTML)"
    WEBSITE_DYNAMIC = "website_dynamic", "Website (JS-rendered)"
    OTHER = "other", "Other"


class ContentType(models.TextChoices):
    ARTICLE = "article", "Article"
    POST = "post", "Post"
    VIDEO = "video", "Video"
    IMAGE = "image", "Image"
    AUDIO = "audio", "Audio"
    POLL = "poll", "Poll"
    OTHER = "other", "Other"


class LifecycleState(models.TextChoices):
    DISCOVERED = "discovered", "Discovered"
    WATCHING = "watching", "Watching"
    RISING = "rising", "Rising"
    BREAKING = "breaking", "Breaking"
    PEAKING = "peaking", "Peaking"
    COOLING = "cooling", "Cooling"
    STALE = "stale", "Stale"
    ARCHIVED = "archived", "Archived"


class TrendState(models.TextChoices):
    NORMAL = "normal", "Normal"
    EARLY_SIGNAL = "early_signal", "Early Signal"
    RISING = "rising", "Rising"
    SURGING = "surging", "Surging"
    BREAKING = "breaking", "Breaking"
    SATURATED = "saturated", "Saturated"
    COOLING = "cooling", "Cooling"


class EditorialAction(models.TextChoices):
    PUBLISH_NOW = "publish_now", "Publish Now"
    WATCH = "watch", "Watch"
    SCHEDULE = "schedule", "Schedule"
    SKIP = "skip", "Skip"
    UPDATE_EXISTING_STORY = "update_existing_story", "Update Existing Story"
    REJECT = "reject", "Reject"
