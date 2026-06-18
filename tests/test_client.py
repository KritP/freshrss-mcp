"""Tests for client.py — FreshRSS API client with mocked HTTP."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from freshrss_mcp.client import AuthenticationError, FreshRSSClient
from freshrss_mcp.config import Config


@pytest.fixture
def config():
    return Config(
        FRESHRSS_URL="https://test.freshrss.com",
        FRESHRSS_USERNAME="testuser",
        FRESHRSS_PASSWORD="testpass",
    )


@pytest.fixture
def client(config):
    return FreshRSSClient(config)


# --- Authentication ---


@pytest.mark.asyncio
async def test_authenticate_success(client):
    mock_response = MagicMock()
    mock_response.text = "SID=abc123\nLSID=def456\nAuth=ghi789"
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response):
        token = await client.authenticate()

    assert token == "abc123"
    assert client._auth_token == "abc123"


@pytest.mark.asyncio
async def test_authenticate_no_sid(client):
    """Response without SID raises AuthenticationError."""
    mock_response = MagicMock()
    mock_response.text = "Auth=ghi789\nLSID=def456"
    mock_response.raise_for_status = MagicMock()

    with (
        patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response),
        pytest.raises(AuthenticationError, match="No SID found"),
    ):
        await client.authenticate()


@pytest.mark.asyncio
async def test_authenticate_http_error(client):
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("Forbidden", request=MagicMock(), response=mock_response)
    )

    with (
        patch.object(client._client, "post", new_callable=AsyncMock, return_value=mock_response),
        pytest.raises(AuthenticationError, match="403"),
    ):
        await client.authenticate()


@pytest.mark.asyncio
async def test_get_auth_headers_unauthenticated(client):
    """Calling _get_auth_headers before authenticate raises."""
    with pytest.raises(AuthenticationError, match="Not authenticated"):
        client._get_auth_headers()


# --- Feed Operations ---


@pytest.mark.asyncio
async def test_list_feeds(client):
    client._auth_token = "tok"
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "subscriptions": [
            {"id": "feed/123", "title": "Feed A", "url": "https://a.com/rss"},
            {"id": "feed/456", "title": "Feed B", "url": "https://b.com/rss"},
        ]
    }
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._client, "get", new_callable=AsyncMock, return_value=mock_response):
        feeds = await client.list_feeds()

    assert len(feeds) == 2
    assert feeds[0].name == "Feed A"
    assert feeds[0].id == 123
    assert feeds[1].url == "https://b.com/rss"


@pytest.mark.asyncio
async def test_list_feeds_empty(client):
    client._auth_token = "tok"
    mock_response = MagicMock()
    mock_response.json.return_value = {"subscriptions": []}
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._client, "get", new_callable=AsyncMock, return_value=mock_response):
        feeds = await client.list_feeds()

    assert feeds == []


@pytest.mark.asyncio
async def test_get_unread_counts(client):
    client._auth_token = "tok"
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "unreadcounts": [
            {"id": "feed/123", "count": 5},
            {"id": "feed/456", "count": 3},
        ]
    }
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._client, "get", new_callable=AsyncMock, return_value=mock_response):
        counts = await client.get_unread_counts()

    assert counts[123] == 5
    assert counts[456] == 3


# --- Article Operations ---

SAMPLE_ITEM = {
    "id": "tag:google.com,2005:reader/item/1234567890",
    "title": "Test Article",
    "published": 1700000000,
    "alternate": [{"href": "https://example.com/article"}],
    "summary": {"content": "Article summary text"},
    "origin": {"title": "Source Feed"},
    "categories": ["user/-/state/com.google/read"],
}


@pytest.mark.asyncio
async def test_get_articles(client):
    client._auth_token = "tok"
    mock_response = MagicMock()
    mock_response.json.return_value = {"items": [SAMPLE_ITEM]}
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._client, "get", new_callable=AsyncMock, return_value=mock_response):
        articles = await client.get_articles(limit=10)

    assert len(articles) == 1
    assert articles[0].title == "Test Article"
    assert articles[0].is_read is True
    assert articles[0].is_starred is False
    assert articles[0].id == 1234567890


@pytest.mark.asyncio
async def test_get_articles_with_feed_filter(client):
    client._auth_token = "tok"
    mock_response = MagicMock()
    mock_response.json.return_value = {"items": [SAMPLE_ITEM]}
    mock_response.raise_for_status = MagicMock()

    mock_get = AsyncMock(return_value=mock_response)
    with patch.object(client._client, "get", mock_get):
        await client.get_articles(feed_id=42, limit=5)

    call_url = mock_get.call_args[0][0]
    assert "feed/42" in call_url


@pytest.mark.asyncio
async def test_get_articles_empty_response(client):
    client._auth_token = "tok"
    mock_response = MagicMock()
    mock_response.json.return_value = {"items": []}
    mock_response.raise_for_status = MagicMock()

    with patch.object(client._client, "get", new_callable=AsyncMock, return_value=mock_response):
        articles = await client.get_articles()

    assert articles == []


# --- Tag Operations ---


@pytest.mark.asyncio
async def test_mark_as_read(client):
    client._auth_token = "tok"
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    mock_post = AsyncMock(return_value=mock_response)
    with patch.object(client._client, "post", mock_post):
        result = await client.mark_as_read([100, 200])

    assert result is True
    call_data = mock_post.call_args[1]["data"]
    assert "user/-/state/com.google/read" in call_data["a"]


@pytest.mark.asyncio
async def test_mark_as_unread(client):
    client._auth_token = "tok"
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    mock_post = AsyncMock(return_value=mock_response)
    with patch.object(client._client, "post", mock_post):
        result = await client.mark_as_unread([100])

    assert result is True
    call_data = mock_post.call_args[1]["data"]
    assert "user/-/state/com.google/read" in call_data["r"]


@pytest.mark.asyncio
async def test_star_article(client):
    client._auth_token = "tok"
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    mock_post = AsyncMock(return_value=mock_response)
    with patch.object(client._client, "post", mock_post):
        result = await client.star_article(999)

    assert result is True


@pytest.mark.asyncio
async def test_unstar_article(client):
    client._auth_token = "tok"
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()

    mock_post = AsyncMock(return_value=mock_response)
    with patch.object(client._client, "post", mock_post):
        result = await client.unstar_article(999)

    assert result is True


# --- ID Extraction ---


class TestExtractFeedId:
    def test_numeric_with_prefix(self):
        assert FreshRSSClient._extract_feed_id("feed/123") == 123

    def test_numeric_without_prefix(self):
        assert FreshRSSClient._extract_feed_id("456") == 456

    def test_url_string_falls_back_to_hash(self):
        result = FreshRSSClient._extract_feed_id("feed/https://example.com/rss")
        assert isinstance(result, int)
        assert 0 <= result < 1_000_000

    def test_empty_string(self):
        result = FreshRSSClient._extract_feed_id("")
        assert isinstance(result, int)


class TestExtractArticleId:
    def test_decimal_id(self):
        assert (
            FreshRSSClient._extract_article_id("tag:google.com,2005:reader/item/1234567890")
            == 1234567890
        )

    def test_hex_id(self):
        result = FreshRSSClient._extract_article_id(
            "tag:google.com,2005:reader/item/00000186a7b3c4d5"
        )
        assert result == 0x00000186A7B3C4D5

    def test_non_numeric_falls_back_to_hash(self):
        result = FreshRSSClient._extract_article_id("some-random-string")
        assert isinstance(result, int)

    def test_empty_string(self):
        result = FreshRSSClient._extract_article_id("")
        assert isinstance(result, int)


# --- Parse Article Edge Cases ---


class TestParseArticle:
    def test_missing_summary(self, client):
        item = {
            "id": "tag:google.com,2005:reader/item/1",
            "title": "No Summary",
            "published": 0,
            "alternate": [{"href": "https://x.com"}],
            "origin": {"title": "Feed"},
            "categories": [],
        }
        article = client._parse_article(item)
        assert article is not None
        assert article.summary == ""

    def test_missing_alternate(self, client):
        item = {
            "id": "tag:google.com,2005:reader/item/1",
            "title": "No URL",
            "published": 0,
            "alternate": [],
            "summary": {"content": "text"},
            "origin": {"title": "Feed"},
            "categories": [],
        }
        article = client._parse_article(item)
        assert article is not None
        assert article.url == ""

    def test_starred_article(self, client):
        item = {
            "id": "tag:google.com,2005:reader/item/1",
            "title": "Starred",
            "published": 0,
            "alternate": [{"href": ""}],
            "summary": {"content": ""},
            "origin": {"title": "Feed"},
            "categories": ["user/-/state/com.google/starred"],
        }
        article = client._parse_article(item)
        assert article is not None
        assert article.is_starred is True
        assert article.is_read is False

    def test_malformed_item_still_parses(self, client):
        """Minimal/garbage data produces an Article with safe defaults."""
        article = client._parse_article({"garbage": True})
        assert article is not None
        assert article.title == "Untitled"
        assert article.summary == ""
        assert article.url == ""


# --- Lifecycle ---


@pytest.mark.asyncio
async def test_aclose(client):
    await client.aclose()
    assert client._client.is_closed


# --- Subscribe / Unsubscribe ---


def _auth_response():
    """Mock the ClientLogin response (used by _ensure_authenticated)."""
    r = MagicMock()
    r.text = "SID=abc123\nLSID=def456\nAuth=ghi789"
    r.raise_for_status = MagicMock()
    return r


def _ok_response():
    r = MagicMock()
    r.text = "OK"
    r.raise_for_status = MagicMock()
    return r


def _json_response(payload: dict):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status = MagicMock()
    return r


@pytest.fixture
def authed_client(client):
    """A FreshRSSClient with auth pre-set so tests don't need to mock ClientLogin."""
    client._auth_token = "abc123"
    return client


@pytest.mark.asyncio
async def test_subscribe_new_feed(authed_client):
    """Fresh quickadd: no existing feed, quickadd returns success."""
    # list_feeds returns empty (no existing subscription)
    list_feeds_resp = _json_response({"subscriptions": []})
    # quickadd returns success
    quickadd_resp = _json_response({
        "numResults": 1,
        "query": "https://example.com/feed.xml",
        "streamId": "feed/42",
        "streamName": "Example Feed",
    })

    with patch.object(
        authed_client._client, "get", new_callable=AsyncMock,
        side_effect=[list_feeds_resp, quickadd_resp],
    ):
        result = await authed_client.subscribe(url="https://example.com/feed.xml")

    assert result.feed_id == 42
    assert result.feed_url == "https://example.com/feed.xml"
    assert result.title == "Example Feed"
    assert result.category is None
    assert result.already_subscribed is False


@pytest.mark.asyncio
async def test_subscribe_idempotent_existing_url(authed_client):
    """If URL is already in list_feeds, return already_subscribed=True
    and do NOT call quickadd."""
    list_feeds_resp = _json_response({
        "subscriptions": [
            {"id": "feed/5", "title": "Existing", "url": "https://example.com/feed.xml"},
        ],
    })

    with patch.object(
        authed_client._client, "get", new_callable=AsyncMock,
        return_value=list_feeds_resp,
    ) as mock_get:
        result = await authed_client.subscribe(url="https://example.com/feed.xml")

    assert result.feed_id == 5
    assert result.already_subscribed is True
    # Only one GET (list_feeds); quickadd was not called.
    assert mock_get.call_count == 1


@pytest.mark.asyncio
async def test_subscribe_force_skips_idempotency(authed_client):
    """With force=True, the idempotency check is skipped and quickadd is called."""
    quickadd_resp = _json_response({
        "numResults": 1,
        "query": "https://example.com/feed.xml",
        "streamId": "feed/42",
        "streamName": "Re-added",
    })

    with patch.object(
        authed_client._client, "get", new_callable=AsyncMock,
        return_value=quickadd_resp,
    ) as mock_get:
        result = await authed_client.subscribe(
            url="https://example.com/feed.xml", force=True,
        )

    assert result.feed_id == 42
    assert result.already_subscribed is False
    # Only one GET (the quickadd call) — no list_feeds lookup.
    assert mock_get.call_count == 1


@pytest.mark.asyncio
async def test_subscribe_idempotent_with_title_update(authed_client):
    """If already subscribed and title is given, apply via subscription/edit
    and return updated title."""
    list_feeds_resp = _json_response({
        "subscriptions": [
            {"id": "feed/5", "title": "Old Title", "url": "https://example.com/feed.xml"},
        ],
    })
    edit_resp = _ok_response()

    with patch.object(
        authed_client._client, "get", new_callable=AsyncMock,
        return_value=list_feeds_resp,
    ), patch.object(
        authed_client._client, "post", new_callable=AsyncMock,
        return_value=edit_resp,
    ) as mock_post:
        result = await authed_client.subscribe(
            url="https://example.com/feed.xml", title="New Title",
        )

    assert result.feed_id == 5
    assert result.title == "New Title"
    assert result.already_subscribed is True
    assert mock_post.call_count == 1
    # Verify the edit endpoint was called with ac=edit
    call_args = mock_post.call_args
    assert call_args.kwargs["params"]["ac"] == "edit"
    assert call_args.kwargs["params"]["t"] == "New Title"


@pytest.mark.asyncio
async def test_subscribe_with_title_and_category_on_new_feed(authed_client):
    """Fresh quickadd followed by subscription/edit to apply title and category."""
    list_feeds_resp = _json_response({"subscriptions": []})
    quickadd_resp = _json_response({
        "numResults": 1,
        "query": "https://example.com/feed.xml",
        "streamId": "feed/42",
        "streamName": "Default Title",
    })
    edit_resp = _ok_response()

    with patch.object(
        authed_client._client, "get", new_callable=AsyncMock,
        side_effect=[list_feeds_resp, quickadd_resp],
    ), patch.object(
        authed_client._client, "post", new_callable=AsyncMock,
        return_value=edit_resp,
    ) as mock_post:
        result = await authed_client.subscribe(
            url="https://example.com/feed.xml",
            title="Custom",
            category="Tech",
        )

    assert result.title == "Custom"
    assert result.category == "Tech"
    # The edit call's `a` param should encode the category
    call_args = mock_post.call_args
    assert "Tech" in call_args.kwargs["params"]["a"]


@pytest.mark.asyncio
async def test_subscribe_quickadd_failure(authed_client):
    """quickadd returns numResults:0 → raise RuntimeError with the error message."""
    list_feeds_resp = _json_response({"subscriptions": []})
    quickadd_resp = _json_response({
        "numResults": 0,
        "error": "Feed not found",
    })

    with patch.object(
        authed_client._client, "get", new_callable=AsyncMock,
        side_effect=[list_feeds_resp, quickadd_resp],
    ):
        with pytest.raises(RuntimeError, match="Feed not found"):
            await authed_client.subscribe(url="https://invalid.example.com/feed.xml")


@pytest.mark.asyncio
async def test_subscribe_category_with_spaces_is_url_encoded(authed_client):
    """Category names with spaces / special chars get URL-encoded in `a=`."""
    list_feeds_resp = _json_response({"subscriptions": []})
    quickadd_resp = _json_response({
        "numResults": 1,
        "query": "https://example.com/feed.xml",
        "streamId": "feed/42",
        "streamName": "x",
    })
    edit_resp = _ok_response()

    with patch.object(
        authed_client._client, "get", new_callable=AsyncMock,
        side_effect=[list_feeds_resp, quickadd_resp],
    ), patch.object(
        authed_client._client, "post", new_callable=AsyncMock,
        return_value=edit_resp,
    ) as mock_post:
        await authed_client.subscribe(
            url="https://example.com/feed.xml",
            category="AI & ML",
        )

    call_args = mock_post.call_args
    # The 'a' param should contain the URL-encoded form
    assert "AI" in call_args.kwargs["params"]["a"]
    # & should be %26, space should be %20
    assert "%26" in call_args.kwargs["params"]["a"] or "%20" in call_args.kwargs["params"]["a"]


@pytest.mark.asyncio
async def test_unsubscribe_by_id(authed_client):
    with patch.object(
        authed_client._client, "get", new_callable=AsyncMock,
    ), patch.object(
        authed_client._client, "post", new_callable=AsyncMock,
        return_value=_ok_response(),
    ) as mock_post:
        ok = await authed_client.unsubscribe(feed_id=7)

    assert ok is True
    call_args = mock_post.call_args
    assert call_args.kwargs["params"]["ac"] == "unsubscribe"
    assert call_args.kwargs["params"]["s"] == "feed/7"


@pytest.mark.asyncio
async def test_unsubscribe_by_url(authed_client):
    list_feeds_resp = _json_response({
        "subscriptions": [
            {"id": "feed/9", "title": "X", "url": "https://example.com/feed.xml"},
        ],
    })
    with patch.object(
        authed_client._client, "get", new_callable=AsyncMock,
        return_value=list_feeds_resp,
    ), patch.object(
        authed_client._client, "post", new_callable=AsyncMock,
        return_value=_ok_response(),
    ) as mock_post:
        ok = await authed_client.unsubscribe(url="https://example.com/feed.xml")

    assert ok is True
    assert mock_post.call_args.kwargs["params"]["s"] == "feed/9"


@pytest.mark.asyncio
async def test_unsubscribe_url_not_found(authed_client):
    from freshrss_mcp.client import SubscriptionNotFound

    list_feeds_resp = _json_response({
        "subscriptions": [
            {"id": "feed/9", "title": "X", "url": "https://example.com/feed.xml"},
        ],
    })
    with patch.object(
        authed_client._client, "get", new_callable=AsyncMock,
        return_value=list_feeds_resp,
    ):
        with pytest.raises(SubscriptionNotFound):
            await authed_client.unsubscribe(url="https://other.com/feed.xml")


@pytest.mark.asyncio
async def test_unsubscribe_neither_id_nor_url(authed_client):
    with pytest.raises(ValueError, match="Either feed_id or url is required"):
        await authed_client.unsubscribe()


@pytest.mark.asyncio
async def test_unsubscribe_id_wins_over_url(authed_client):
    """If both are given, feed_id is used directly without list_feeds lookup."""
    with patch.object(
        authed_client._client, "get", new_callable=AsyncMock,
    ) as mock_get, patch.object(
        authed_client._client, "post", new_callable=AsyncMock,
        return_value=_ok_response(),
    ) as mock_post:
        ok = await authed_client.unsubscribe(
            feed_id=3, url="https://should-be-ignored.com/feed.xml",
        )

    assert ok is True
    # No GET call (list_feeds not consulted)
    assert mock_get.call_count == 0
    assert mock_post.call_args.kwargs["params"]["s"] == "feed/3"

