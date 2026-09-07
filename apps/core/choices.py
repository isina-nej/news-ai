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
