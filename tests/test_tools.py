"""Tests for tools.py — tool registration and error boundaries.

These tests verify that:
1. Tools catch all exceptions and return 'Error: ...' strings
2. Tools produce correct output for happy paths
3. The _truncate_summary helper works at boundaries
"""

import json
from typing import Callable
from unittest.mock import AsyncMock

import pytest

from freshrss_mcp.client import FreshRSSClient
from freshrss_mcp.config import Config
from freshrss_mcp.models import Article, Feed
from freshrss_mcp.tools import _truncate_summary, register_tools

# We don't need a real FastMCP server — we just need to capture the
# registered tool functions so we can call them directly.


class FakeMCP:
    """Minimal stand-in that captures tool registrations."""

    def __init__(self):
        self.tools: dict[str, Callable] = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


@pytest.fixture
def config():
    return Config(
        FRESHRSS_URL="https://test.freshrss.com",
        FRESHRSS_USERNAME="testuser",
        FRESHRSS_PASSWORD="testpass",
    )


@pytest.fixture
def mock_client(config):
    client = FreshRSSClient(config)
    client._auth_token = "test-token"
    return client


@pytest.fixture
def tools(mock_client):
    """Register tools on a fake MCP and return them as a dict."""
    fake_mcp = FakeMCP()
    register_tools(
        fake_mcp, mock_client,
        rsshub_base_url="http://localhost:8087",
        rsshub_routes_path="/nonexistent/routes.json",
    )
    return fake_mcp.tools


# --- _truncate_summary ---


class TestTruncateSummary:
    def test_short_text_unchanged(self):
        assert _truncate_summary("hello", 100) == "hello"

    def test_exact_length_unchanged(self):
        text = "a" * 50
        assert _truncate_summary(text, 50) == text

    def test_truncates_at_word_boundary(self):
        text = "hello world this is a test"
        result = _truncate_summary(text, 15)
        assert result.endswith("...")
        assert len(result) <= 18  # 15 + "..."

    def test_empty_string(self):
        assert _truncate_summary("", 100) == ""

    def test_zero_max_length(self):
        result = _truncate_summary("hello world", 0)
        assert result.endswith("...")


# --- Tool Error Boundaries ---


SAMPLE_ARTICLES = [
    Article(
        id=1,
        title="Art 1",
        summary="Summary one",
        url="https://a.com",
        published=1000,
        feed_name="Feed A",
        is_read=False,
        is_starred=False,
    ),
    Article(
        id=2,
        title="Art 2",
        summary="Summary two",
        url="https://b.com",
        published=2000,
        feed_name="Feed B",
        is_read=True,
        is_starred=True,
    ),
]

SAMPLE_FEEDS = [
    Feed(id=10, name="Feed A", url="https://a.com/rss"),
    Feed(id=20, name="Feed B", url="https://b.com/rss"),
]


@pytest.mark.asyncio
async def test_get_unread_articles_happy_path(tools, mock_client):
    mock_client.get_articles = AsyncMock(return_value=SAMPLE_ARTICLES)

    result = await tools["get_unread_articles"]()
    assert "Art 1" in result
    assert "Art 2" in result


@pytest.mark.asyncio
async def test_get_unread_articles_error_returns_string(tools, mock_client):
    mock_client.get_articles = AsyncMock(side_effect=RuntimeError("connection lost"))

    result = await tools["get_unread_articles"]()
    assert result.startswith("Error:")
    assert "connection lost" in result


@pytest.mark.asyncio
async def test_list_feeds_happy_path(tools, mock_client):
    mock_client.list_feeds = AsyncMock(return_value=SAMPLE_FEEDS)
    mock_client.get_unread_counts = AsyncMock(return_value={10: 5, 20: 0})

    result = await tools["list_feeds"]()
    assert "Feed A" in result
    assert "Feed B" in result


@pytest.mark.asyncio
async def test_list_feeds_error_returns_string(tools, mock_client):
    mock_client.list_feeds = AsyncMock(side_effect=RuntimeError("timeout"))

    result = await tools["list_feeds"]()
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_get_feed_info_found(tools, mock_client):
    mock_client.list_feeds = AsyncMock(return_value=SAMPLE_FEEDS)
    mock_client.get_unread_counts = AsyncMock(return_value={10: 3})

    result = await tools["get_feed_info"](feed_id=10)
    assert "Feed A" in result


@pytest.mark.asyncio
async def test_get_feed_info_not_found(tools, mock_client):
    mock_client.list_feeds = AsyncMock(return_value=SAMPLE_FEEDS)
    mock_client.get_unread_counts = AsyncMock(return_value={})

    result = await tools["get_feed_info"](feed_id=999)
    assert "Error:" in result
    assert "999" in result


@pytest.mark.asyncio
async def test_search_articles_matches(tools, mock_client):
    mock_client.get_articles = AsyncMock(return_value=SAMPLE_ARTICLES)

    result = await tools["search_articles"](query="Art 1")
    assert "Art 1" in result
    assert "Art 2" not in result


@pytest.mark.asyncio
async def test_search_articles_no_match(tools, mock_client):
    mock_client.get_articles = AsyncMock(return_value=SAMPLE_ARTICLES)

    result = await tools["search_articles"](query="nonexistent")
    assert result == "[]"


@pytest.mark.asyncio
async def test_search_articles_error(tools, mock_client):
    mock_client.get_articles = AsyncMock(side_effect=RuntimeError("fail"))

    result = await tools["search_articles"](query="test")
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_mark_as_read_empty_list(tools, mock_client):
    result = await tools["mark_as_read"](article_ids=[])
    assert result == "OK"


@pytest.mark.asyncio
async def test_mark_as_read_success(tools, mock_client):
    mock_client.mark_as_read = AsyncMock(return_value=True)

    result = await tools["mark_as_read"](article_ids=[1, 2, 3])
    assert result == "OK"


@pytest.mark.asyncio
async def test_mark_as_read_error(tools, mock_client):
    mock_client.mark_as_read = AsyncMock(side_effect=RuntimeError("server error"))

    result = await tools["mark_as_read"](article_ids=[1])
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_mark_as_unread_success(tools, mock_client):
    mock_client.mark_as_unread = AsyncMock(return_value=True)

    result = await tools["mark_as_unread"](article_ids=[1])
    assert result == "OK"


@pytest.mark.asyncio
async def test_star_article_success(tools, mock_client):
    mock_client.star_article = AsyncMock(return_value=True)

    result = await tools["star_article"](article_id=42)
    assert result == "OK"


@pytest.mark.asyncio
async def test_star_article_error(tools, mock_client):
    mock_client.star_article = AsyncMock(side_effect=RuntimeError("denied"))

    result = await tools["star_article"](article_id=42)
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_unstar_article_success(tools, mock_client):
    mock_client.unstar_article = AsyncMock(return_value=True)

    result = await tools["unstar_article"](article_id=42)
    assert result == "OK"


@pytest.mark.asyncio
async def test_get_feed_stats_happy(tools, mock_client):
    mock_client.list_feeds = AsyncMock(return_value=SAMPLE_FEEDS)
    mock_client.get_unread_counts = AsyncMock(return_value={10: 7, 20: 2})

    result = await tools["get_feed_stats"]()
    assert "Feed A" in result
    assert "7" in result


@pytest.mark.asyncio
async def test_get_articles_by_feed_success(tools, mock_client):
    mock_client.get_articles = AsyncMock(return_value=[SAMPLE_ARTICLES[0]])

    result = await tools["get_articles_by_feed"](feed_id=10)
    assert "Art 1" in result


@pytest.mark.asyncio
async def test_get_articles_by_feed_error(tools, mock_client):
    mock_client.get_articles = AsyncMock(side_effect=RuntimeError("boom"))

    result = await tools["get_articles_by_feed"](feed_id=10)
    assert result.startswith("Error:")


# --- subscribe_feed / unsubscribe_feed / ingest_url / ingest_rsshub_path / list_routes ---


from freshrss_mcp.client import SubscriptionNotFound
from freshrss_mcp.models import SubscriptionResult


@pytest.mark.asyncio
async def test_subscribe_feed_tool_success(tools, mock_client):
    mock_client.subscribe = AsyncMock(
        return_value=SubscriptionResult(
            feed_id=42, feed_url="https://x.com/feed",
            title="X", category=None, already_subscribed=False,
        )
    )
    result = await tools["subscribe_feed"](url="https://x.com/feed")
    assert "feed_id" in result
    assert "42" in result
    assert "already_subscribed" in result


@pytest.mark.asyncio
async def test_subscribe_feed_tool_error_boundary(tools, mock_client):
    mock_client.subscribe = AsyncMock(side_effect=RuntimeError("quickadd failed"))
    result = await tools["subscribe_feed"](url="https://x.com/feed")
    assert result == "Error: quickadd failed"


@pytest.mark.asyncio
async def test_unsubscribe_feed_tool_success_by_id(tools, mock_client):
    mock_client.unsubscribe = AsyncMock(return_value=True)
    result = await tools["unsubscribe_feed"](feed_id=7)
    assert result == "OK"


@pytest.mark.asyncio
async def test_unsubscribe_feed_tool_subscription_not_found(tools, mock_client):
    mock_client.unsubscribe = AsyncMock(
        side_effect=SubscriptionNotFound("no match"),
    )
    result = await tools["unsubscribe_feed"](url="https://x.com/feed")
    assert result.startswith("Error:")
    assert "no match" in result


@pytest.mark.asyncio
async def test_unsubscribe_feed_tool_value_error(tools, mock_client):
    mock_client.unsubscribe = AsyncMock(
        side_effect=ValueError("Either feed_id or url is required"),
    )
    result = await tools["unsubscribe_feed"]()
    assert result.startswith("Error:")


@pytest.mark.asyncio
async def test_ingest_url_short_circuits_feed_url(tools, mock_client, tmp_path, monkeypatch):
    """If the URL looks like a feed, skip RSSHub resolution."""
    import freshrss_mcp.routes_matcher as rm
    monkeypatch.setattr(rm, "_CATALOG_CACHE", {})

    # Write a real catalog to a temp file so load_catalog works.
    catalog = {"github": {"routes": {}}}
    catalog_file = tmp_path / "routes.json"
    catalog_file.write_text(json.dumps(catalog))

    # Re-register tools with the real catalog path.
    from freshrss_mcp.tools import register_tools
    from tests.test_tools import FakeMCP
    fake_mcp = FakeMCP()
    register_tools(
        fake_mcp, mock_client,
        rsshub_base_url="http://localhost:8087",
        rsshub_routes_path=str(catalog_file),
    )
    new_tools = fake_mcp.tools

    mock_client.subscribe = AsyncMock(
        return_value=SubscriptionResult(
            feed_id=1, feed_url="https://example.com/feed.xml",
            title="x", category=None, already_subscribed=False,
        )
    )
    result = await new_tools["ingest_url"](url="https://example.com/feed.xml")
    assert "matched_route" in result
    assert "None" in result  # matched_route is None (short-circuit)
    assert "feed.xml" in result


@pytest.mark.asyncio
async def test_ingest_url_radar_match_single(tools, mock_client, tmp_path, monkeypatch):
    """Single radar match → auto-pick, no error."""
    import freshrss_mcp.routes_matcher as rm
    monkeypatch.setattr(rm, "_CATALOG_CACHE", {})

    catalog = {
        "github": {
            "routes": {
                "/github/issue/:user/:repo": {
                    "name": "Repository Issues",
                    "example": "/github/issue/foo/bar",
                    "features": {},
                    "radar": [{"source": ["github.com/:user/:repo"],
                               "target": "/github/issue/:user/:repo"}],
                }
            }
        }
    }
    catalog_file = tmp_path / "routes.json"
    catalog_file.write_text(json.dumps(catalog))

    from freshrss_mcp.tools import register_tools
    from tests.test_tools import FakeMCP
    fake_mcp = FakeMCP()
    register_tools(
        fake_mcp, mock_client,
        rsshub_base_url="http://localhost:8087",
        rsshub_routes_path=str(catalog_file),
    )
    new_tools = fake_mcp.tools

    mock_client.subscribe = AsyncMock(
        return_value=SubscriptionResult(
            feed_id=99, feed_url="http://localhost:8087/github/issue/foo/bar",
            title="Repository Issues", category=None, already_subscribed=False,
        )
    )
    result = await new_tools["ingest_url"](
        url="https://github.com/foo/bar"
    )
    assert "Repository Issues" in result
    assert "feed_url" in result
    assert "matched_route" in result


@pytest.mark.asyncio
async def test_ingest_url_radar_ambiguous_returns_error(tools, mock_client, tmp_path, monkeypatch):
    """Multiple radar matches with no prefer → error with all paths."""
    import freshrss_mcp.routes_matcher as rm
    monkeypatch.setattr(rm, "_CATALOG_CACHE", {})

    catalog = {
        "github": {
            "routes": {
                "/github/issue/:user/:repo": {
                    "name": "Repository Issues", "features": {},
                    "radar": [{"source": ["github.com/:user/:repo"],
                               "target": "/github/issue/:user/:repo"}],
                },
                "/github/release/:user/:repo": {
                    "name": "Repository Releases", "features": {},
                    "radar": [{"source": ["github.com/:user/:repo"],
                               "target": "/github/release/:user/:repo"}],
                },
            }
        }
    }
    catalog_file = tmp_path / "routes.json"
    catalog_file.write_text(json.dumps(catalog))

    from freshrss_mcp.tools import register_tools
    from tests.test_tools import FakeMCP
    fake_mcp = FakeMCP()
    register_tools(
        fake_mcp, mock_client,
        rsshub_base_url="http://localhost:8087",
        rsshub_routes_path=str(catalog_file),
    )
    new_tools = fake_mcp.tools

    result = await new_tools["ingest_url"](
        url="https://github.com/foo/bar"
    )
    assert result.startswith("Error: multiple routes match")


@pytest.mark.asyncio
async def test_ingest_url_radar_ambiguous_with_prefer(tools, mock_client, tmp_path, monkeypatch):
    """Multiple matches with prefer → bias toward prefer substring."""
    import freshrss_mcp.routes_matcher as rm
    monkeypatch.setattr(rm, "_CATALOG_CACHE", {})

    catalog = {
        "github": {
            "routes": {
                "/github/issue/:user/:repo": {
                    "name": "Repository Issues", "features": {},
                    "radar": [{"source": ["github.com/:user/:repo"],
                               "target": "/github/issue/:user/:repo"}],
                },
                "/github/release/:user/:repo": {
                    "name": "Repository Releases", "features": {},
                    "radar": [{"source": ["github.com/:user/:repo"],
                               "target": "/github/release/:user/:repo"}],
                },
            }
        }
    }
    catalog_file = tmp_path / "routes.json"
    catalog_file.write_text(json.dumps(catalog))

    from freshrss_mcp.tools import register_tools
    from tests.test_tools import FakeMCP
    fake_mcp = FakeMCP()
    register_tools(
        fake_mcp, mock_client,
        rsshub_base_url="http://localhost:8087",
        rsshub_routes_path=str(catalog_file),
    )
    new_tools = fake_mcp.tools

    mock_client.subscribe = AsyncMock(
        return_value=SubscriptionResult(
            feed_id=1, feed_url="http://localhost:8087/github/release/foo/bar",
            title="Repository Releases", category=None, already_subscribed=False,
        )
    )
    result = await new_tools["ingest_url"](
        url="https://github.com/foo/bar", prefer=["release"],
    )
    assert "Repository Releases" in result


@pytest.mark.asyncio
async def test_ingest_url_no_route_match(tools, mock_client, tmp_path, monkeypatch):
    """No radar hit → error message suggesting list_routes / ingest_rsshub_path."""
    import freshrss_mcp.routes_matcher as rm
    monkeypatch.setattr(rm, "_CATALOG_CACHE", {})

    catalog = {"github": {"routes": {}}}
    catalog_file = tmp_path / "routes.json"
    catalog_file.write_text(json.dumps(catalog))

    from freshrss_mcp.tools import register_tools
    from tests.test_tools import FakeMCP
    fake_mcp = FakeMCP()
    register_tools(
        fake_mcp, mock_client,
        rsshub_base_url="http://localhost:8087",
        rsshub_routes_path=str(catalog_file),
    )
    new_tools = fake_mcp.tools

    result = await new_tools["ingest_url"](url="https://example.com/whatever")
    assert result.startswith("Error: no RSSHub route matches")


@pytest.mark.asyncio
async def test_ingest_url_puppeteer_warning(tools, mock_client, tmp_path, monkeypatch):
    """Routes with requirePuppeteer should produce a warning."""
    import freshrss_mcp.routes_matcher as rm
    monkeypatch.setattr(rm, "_CATALOG_CACHE", {})

    catalog = {
        "youtube": {
            "routes": {
                "/youtube/user/:username": {
                    "name": "YouTube User", "features": {"requirePuppeteer": True},
                    "radar": [{"source": ["www.youtube.com/:username"],
                               "target": "/youtube/user/:username"}],
                }
            }
        }
    }
    catalog_file = tmp_path / "routes.json"
    catalog_file.write_text(json.dumps(catalog))

    from freshrss_mcp.tools import register_tools
    from tests.test_tools import FakeMCP
    fake_mcp = FakeMCP()
    register_tools(
        fake_mcp, mock_client,
        rsshub_base_url="http://localhost:8087",
        rsshub_routes_path=str(catalog_file),
    )
    new_tools = fake_mcp.tools

    mock_client.subscribe = AsyncMock(
        return_value=SubscriptionResult(
            feed_id=1, feed_url="http://localhost:8087/youtube/user/foo",
            title="x", category=None, already_subscribed=False,
        )
    )
    result = await new_tools["ingest_url"](url="https://www.youtube.com/foo")
    assert "puppeteer" in result


@pytest.mark.asyncio
async def test_ingest_rsshub_path_basic(tools, mock_client):
    mock_client.subscribe = AsyncMock(
        return_value=SubscriptionResult(
            feed_id=1, feed_url="http://localhost:8087/github/issue/DIYgod/RSSHub",
            title="Repository Issues", category=None, already_subscribed=False,
        )
    )
    result = await tools["ingest_rsshub_path"](
        path="/github/issue/:user/:repo",
        params={"user": "DIYgod", "repo": "RSSHub"},
    )
    assert "github/issue/DIYgod/RSSHub" in result
    assert "feed_url" in result


@pytest.mark.asyncio
async def test_ingest_rsshub_path_url_encodes_params(tools, mock_client):
    mock_client.subscribe = AsyncMock(
        return_value=SubscriptionResult(
            feed_id=1, feed_url="http://localhost:8087/x/foo%20bar",
            title="x", category=None, already_subscribed=False,
        )
    )
    result = await tools["ingest_rsshub_path"](
        path="/x/:slug", params={"slug": "foo bar"},
    )
    assert "foo%20bar" in result


@pytest.mark.asyncio
async def test_list_routes_search(tools, tmp_path, monkeypatch):
    import freshrss_mcp.routes_matcher as rm
    monkeypatch.setattr(rm, "_CATALOG_CACHE", {})

    catalog = {
        "github": {
            "routes": {
                "/github/issue/:user/:repo": {
                    "name": "Repository Issues",
                    "description": "Issues for any GitHub repo",
                    "example": "/github/issue/foo/bar",
                    "features": {},
                },
                "/github/release/:user/:repo": {
                    "name": "Repository Releases",
                    "description": "Releases for any GitHub repo",
                    "example": "/github/release/foo/bar",
                    "features": {},
                },
            }
        }
    }
    catalog_file = tmp_path / "routes.json"
    catalog_file.write_text(json.dumps(catalog))

    from freshrss_mcp.tools import register_tools
    from tests.test_tools import FakeMCP
    fake_mcp = FakeMCP()
    register_tools(
        fake_mcp, mock_client,
        rsshub_base_url="http://localhost:8087",
        rsshub_routes_path=str(catalog_file),
    )
    new_tools = fake_mcp.tools

    result = await new_tools["list_routes"](query="issues", limit=5)
    assert "Repository Issues" in result


@pytest.mark.asyncio
async def test_list_routes_namespace_filter(tools, mock_client, tmp_path, monkeypatch):
    import freshrss_mcp.routes_matcher as rm
    monkeypatch.setattr(rm, "_CATALOG_CACHE", {})

    catalog = {
        "github": {"routes": {"/x": {"name": "GH x", "description": "x", "features": {}}}},
        "youtube": {"routes": {"/y": {"name": "YT y", "description": "y", "features": {}}}},
    }
    catalog_file = tmp_path / "routes.json"
    catalog_file.write_text(json.dumps(catalog))

    from freshrss_mcp.tools import register_tools
    from tests.test_tools import FakeMCP
    fake_mcp = FakeMCP()
    register_tools(
        fake_mcp, mock_client,
        rsshub_base_url="http://localhost:8087",
        rsshub_routes_path=str(catalog_file),
    )
    new_tools = fake_mcp.tools

    result = await new_tools["list_routes"](query="x", namespace="github")
    assert "GH x" in result
    assert "YT y" not in result
