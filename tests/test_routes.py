"""Tests for routes_matcher.py — radar pattern matching + name search."""

import json
import tempfile
from pathlib import Path

import pytest

from freshrss_mcp.routes_matcher import (
    RouteCandidate,
    _template_to_regex,
    find_routes_by_name,
    find_routes_by_url,
    is_feed_url,
    load_catalog,
)


# --- is_feed_url -----------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("https://example.com/feed.xml", True),
    ("https://example.com/feed.atom", True),
    ("https://example.com/feed.rss", True),
    ("https://example.com/feed", True),
    ("https://example.com/feed/", True),
    ("https://example.com/rss", True),
    ("https://example.com/rss/", True),
    ("https://example.com/index.xml?foo=bar", True),
    ("https://example.com/rss.xml", True),
    ("https://github.com/anthropics/anthropic-sdk-python", False),
    ("https://www.youtube.com/@mkbhd", False),
    ("https://news.ycombinator.com/item?id=42", False),
    ("https://example.com/articles/2024", False),
])
def test_is_feed_url(url, expected):
    assert is_feed_url(url) is expected


# --- _template_to_regex ----------------------------------------------------


def test_template_to_regex_required_params():
    pattern, params = _template_to_regex("github.com/:user/:repo")
    assert params == ["user", "repo"]
    assert pattern.match("github.com/DIYgod/RSSHub") is not None
    assert pattern.match("github.com/DIYgod/RSSHub/issues") is None
    # Anchored — no leading protocol match
    assert pattern.match("https://github.com/DIYgod/RSSHub") is None


def test_template_to_regex_optional_param():
    pattern, params = _template_to_regex("example.com/:slug?")
    assert params == ["slug"]
    # With param
    m = pattern.match("example.com/foobar")
    assert m is not None
    assert m.group(1) == "foobar"
    # Without param — slash is required if param is present; absent = no slash either
    assert pattern.match("example.com") is not None
    # Trailing slash without a value is rejected (not "example.com/" matching with empty slug)


def test_template_to_regex_constraint_in_braces():
    pattern, params = _template_to_regex("example.com/:path{.+}")
    assert params == ["path"]
    # Custom regex .+ matches across slashes
    m = pattern.match("example.com/some/deep/path")
    assert m is not None
    assert m.group(1) == "some/deep/path"


def test_template_to_regex_literal():
    pattern, params = _template_to_regex("just.a.domain")
    assert params == []
    assert pattern.match("just.a.domain") is not None
    assert pattern.match("notjust.a.domain") is None


# --- find_routes_by_url (radar match) --------------------------------------


@pytest.fixture
def small_catalog():
    """A tiny catalog with realistic routes for testing."""
    return {
        "github": {
            "name": "GitHub",
            "routes": {
                "/github/issue/:user/:repo": {
                    "name": "Repository Issues",
                    "example": "/github/issue/DIYgod/RSSHub",
                    "categories": ["programming"],
                    "features": {
                        "requirePuppeteer": False,
                        "antiCrawler": False,
                    },
                    "radar": [
                        {
                            "source": ["github.com/:user/:repo"],
                            "target": "/github/issue/:user/:repo",
                        }
                    ],
                },
                "/github/release/:user/:repo": {
                    "name": "Repository Releases",
                    "example": "/github/release/DIYgod/RSSHub",
                    "categories": ["programming"],
                    "features": {
                        "requirePuppeteer": False,
                        "antiCrawler": False,
                    },
                    "radar": [
                        {
                            "source": ["github.com/:user/:repo"],
                            "target": "/github/release/:user/:repo",
                        }
                    ],
                },
            },
        },
        "youtube": {
            "name": "YouTube",
            "routes": {
                "/youtube/user/:username": {
                    "name": "YouTube User",
                    "example": "/youtube/user/@mkbhd",
                    "categories": ["social-media"],
                    "features": {
                        "requirePuppeteer": True,
                        "antiCrawler": False,
                    },
                    "radar": [
                        {
                            "source": ["www.youtube.com/:username"],
                            "target": "/youtube/user/:username",
                        }
                    ],
                },
            },
        },
        "no_radar": {
            "name": "Site without radar",
            "routes": {
                "/no_radar/foo": {
                    "name": "No Radar Route",
                    "example": "/no_radar/foo",
                    "features": {},
                    # No radar field
                },
            },
        },
    }


def test_find_routes_by_url_github_issue(small_catalog):
    candidates = find_routes_by_url(
        "https://github.com/anthropics/anthropic-sdk-python", small_catalog
    )
    # Two routes match: issue + release
    assert len(candidates) == 2
    namespaces_paths = {(c.namespace, c.path) for c in candidates}
    assert ("github", "/github/issue/:user/:repo") in namespaces_paths
    assert ("github", "/github/release/:user/:repo") in namespaces_paths
    # Captured params propagated
    for c in candidates:
        assert c.captured_params == {"user": "anthropics", "repo": "anthropic-sdk-python"}


def test_find_routes_by_url_youtube_with_protocol(small_catalog):
    candidates = find_routes_by_url("https://www.youtube.com/@mkbhd", small_catalog)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.namespace == "youtube"
    assert c.captured_params == {"username": "@mkbhd"}


def test_find_routes_by_url_no_match(small_catalog):
    candidates = find_routes_by_url("https://example.com/whatever", small_catalog)
    assert candidates == []


def test_find_routes_by_url_skips_routes_without_radar(small_catalog):
    """Routes with no radar field should not produce false positives."""
    candidates = find_routes_by_url("https://no_radar/foo", small_catalog)
    assert candidates == []


def test_find_routes_by_url_strips_trailing_slash(small_catalog):
    candidates = find_routes_by_url(
        "https://github.com/anthropics/anthropic-sdk-python/", small_catalog
    )
    assert len(candidates) == 2


def test_find_routes_by_url_strips_query_and_fragment(small_catalog):
    candidates = find_routes_by_url(
        "https://github.com/anthropics/anthropic-sdk-python?tab=readme#install", small_catalog
    )
    assert len(candidates) == 2


# --- find_routes_by_name ---------------------------------------------------


def test_find_routes_by_name_searches_name(small_catalog):
    results = find_routes_by_name("issues", small_catalog)
    assert len(results) >= 1
    assert any("Issues" in r.name for r in results)


def test_find_routes_by_name_case_insensitive(small_catalog):
    results = find_routes_by_name("ISSUES", small_catalog)
    assert len(results) >= 1


def test_find_routes_by_name_no_match(small_catalog):
    results = find_routes_by_name("nonexistent xyz", small_catalog)
    assert results == []


def test_find_routes_by_name_namespace_filter(small_catalog):
    results = find_routes_by_name("Repository", small_catalog, namespace="github")
    assert all(r.namespace == "github" for r in results)
    # None in 'youtube'
    results_y = find_routes_by_name("Repository", small_catalog, namespace="youtube")
    assert results_y == []


def test_find_routes_by_name_limit(small_catalog):
    results = find_routes_by_name("Repository", small_catalog, limit=1)
    assert len(results) == 1


# --- RouteCandidate.build_url ----------------------------------------------


def test_build_url_simple():
    c = RouteCandidate(
        namespace="github",
        path="/issue/:user/:repo",
        name="Issues",
        example=None,
        categories=[],
        features={},
        captured_params={"user": "DIYgod", "repo": "RSSHub"},
    )
    assert c.build_url("http://localhost:8087") == "http://localhost:8087/github/issue/DIYgod/RSSHub"


def test_build_url_strips_constraints():
    c = RouteCandidate(
        namespace="github",
        path="/file/:user/:repo/:branch/:filepath{.+}",
        name="File",
        example=None,
        categories=[],
        features={},
        captured_params={
            "user": "x", "repo": "y", "branch": "main",
            "filepath": "src/index.ts",
        },
    )
    # Base URL without trailing slash — build_url normalizes to single slash.
    assert c.build_url("http://h") == "http://h/github/file/x/y/main/src/index.ts"
    # Base URL with trailing slash — also single slash.
    assert c.build_url("http://h/") == "http://h/github/file/x/y/main/src/index.ts"


def test_build_url_strips_trailing_slash_in_base():
    c = RouteCandidate(
        namespace="github", path="/issue", name="", example=None,
        categories=[], features={}, captured_params={},
    )
    assert c.build_url("http://h:8087/") == "http://h:8087/github/issue"


# --- load_catalog (caching) -----------------------------------------------


def test_load_catalog_caches_per_path(tmp_path, monkeypatch):
    import freshrss_mcp.routes_matcher as rm
    monkeypatch.setattr(rm, "_CATALOG_CACHE", {})

    p = tmp_path / "routes.json"
    p.write_text(json.dumps({"ns": {"routes": {}}}))
    # First load reads from disk
    result = load_catalog(str(p))
    assert result == {"ns": {"routes": {}}}
    # Mutate on disk; second load should still return cached content
    p.write_text('{"different": true}')
    result2 = load_catalog(str(p))
    assert result2 == {"ns": {"routes": {}}}  # Cached, not the new disk content
