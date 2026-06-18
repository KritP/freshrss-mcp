"""Data models for FreshRSS MCP Server."""

from dataclasses import dataclass


@dataclass
class Article:
    """Represents a FreshRSS article with minimal fields for token efficiency."""

    id: int
    title: str
    summary: str
    url: str
    published: int
    feed_name: str
    is_read: bool
    is_starred: bool

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "url": self.url,
            "published": self.published,
            "feed_name": self.feed_name,
            "is_read": self.is_read,
            "is_starred": self.is_starred,
        }


@dataclass
class Feed:
    """Represents a FreshRSS feed."""

    id: int
    name: str
    url: str
    unread_count: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "unread_count": self.unread_count,
        }


@dataclass
class SubscriptionResult:
    """Result of a subscribe operation. Returned by client.subscribe() and
    consumed by the subscribe_feed / ingest_url / ingest_rsshub_path tools.

    Fields:
        feed_id: FreshRSS internal numeric id.
        feed_url: Canonical feed URL (the URL FreshRSS will poll).
        title: Display title of the feed.
        category: Category name, or None for default/Uncategorized. None is
            also returned for idempotent no-ops (since list_feeds doesn't
            expose category, we don't know what bucket an existing feed sits in
            without an extra API call we don't currently make).
        already_subscribed: True for idempotent no-op when the URL was
            already in list_feeds(). False for fresh quickadd.
    """

    feed_id: int
    feed_url: str
    title: str
    category: str | None
    already_subscribed: bool

    def to_dict(self) -> dict:
        return {
            "feed_id": self.feed_id,
            "feed_url": self.feed_url,
            "title": self.title,
            "category": self.category,
            "already_subscribed": self.already_subscribed,
        }

