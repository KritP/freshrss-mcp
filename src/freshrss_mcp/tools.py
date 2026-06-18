"""MCP tool definitions for FreshRSS.

Each tool does exactly one thing. All exceptions are caught at the
tool boundary and returned as "Error: ..." strings so the MCP protocol
never sees an uncaught exception.
"""

import logging
import re
from urllib.parse import quote

from fastmcp import FastMCP

from .client import FreshRSSClient, SubscriptionNotFound
from .routes_matcher import (
    find_routes_by_name,
    find_routes_by_url,
    is_feed_url,
    load_catalog,
)

logger = logging.getLogger(__name__)


def _truncate_summary(summary: str, max_length: int) -> str:
    """Truncate summary to max_length at a word boundary."""
    if len(summary) <= max_length:
        return summary
    return summary[:max_length].rsplit(" ", 1)[0] + "..."


def register_tools(
    mcp: FastMCP,
    client: FreshRSSClient,
    *,
    rsshub_base_url: str,
    rsshub_routes_path: str,
) -> None:
    """Register all FreshRSS tools on the given MCP server instance.

    Args:
        mcp: FastMCP instance.
        client: FreshRSS API client.
        rsshub_base_url: Public URL where RSSHub is reachable from FreshRSS
            (typically the Tailscale-bind URL). Used by ingest_url and
            ingest_rsshub_path to build the feed URL that FreshRSS will poll.
        rsshub_routes_path: Filesystem path to the bundled RSSHub routes
            catalog (routes.json) inside the container. Used by ingest_url
            and list_routes.
    """

    @mcp.tool()
    async def get_unread_articles(
        limit: int = 20,
        feed_ids: list[int] | None = None,
        since_timestamp: int | None = None,
        max_summary_length: int = 500,
    ) -> str:
        """Get unread articles from FreshRSS.

        Args:
            limit: Maximum number of articles to return (1-100, default 20).
            feed_ids: Optional list of feed IDs to filter by.
            since_timestamp: Only return articles published after this Unix timestamp.
            max_summary_length: Maximum characters for article summaries (default 500).

        Returns a JSON-formatted list of articles with id, title, summary, url,
        published timestamp, feed_name, is_read, and is_starred fields.
        """
        try:
            if feed_ids:
                all_articles = []
                for fid in feed_ids:
                    articles = await client.get_articles(
                        feed_id=fid,
                        limit=limit,
                        include_read=False,
                        since_timestamp=since_timestamp,
                    )
                    all_articles.extend(articles)
                all_articles.sort(key=lambda a: a.published, reverse=True)
                articles = all_articles[:limit]
            else:
                articles = await client.get_articles(
                    limit=limit,
                    include_read=False,
                    since_timestamp=since_timestamp,
                )

            result = []
            for article in articles:
                d = article.to_dict()
                d["summary"] = _truncate_summary(d["summary"], max_summary_length)
                result.append(d)

            return str(result)
        except Exception as e:
            logger.error("get_unread_articles failed: %s", e, exc_info=True)
            return f"Error: {e}"

    @mcp.tool()
    async def get_articles_by_feed(
        feed_id: int,
        limit: int = 20,
        include_read: bool = False,
    ) -> str:
        """Get articles from a specific feed.

        Args:
            feed_id: ID of the feed to fetch articles from.
            limit: Maximum number of articles to return (1-100, default 20).
            include_read: Whether to include already-read articles (default False).

        Returns a JSON-formatted list of article objects.
        """
        try:
            articles = await client.get_articles(
                feed_id=feed_id, limit=limit, include_read=include_read
            )
            return str([a.to_dict() for a in articles])
        except Exception as e:
            logger.error("get_articles_by_feed failed: %s", e, exc_info=True)
            return f"Error: {e}"

    @mcp.tool()
    async def search_articles(
        query: str,
        limit: int = 10,
        feed_ids: list[int] | None = None,
    ) -> str:
        """Search articles by keyword in title or summary.

        Performs client-side filtering since FreshRSS API lacks server-side search.

        Args:
            query: Search query string (case-insensitive).
            limit: Maximum number of matching articles to return (default 10).
            feed_ids: Optional list of feed IDs to search within.

        Returns a JSON-formatted list of matching article objects.
        """
        try:
            fetch_limit = limit * 3
            if feed_ids:
                all_articles = []
                for fid in feed_ids:
                    articles = await client.get_articles(
                        feed_id=fid, limit=fetch_limit, include_read=True
                    )
                    all_articles.extend(articles)
            else:
                all_articles = await client.get_articles(limit=fetch_limit, include_read=True)

            query_lower = query.lower()
            matching = [
                a
                for a in all_articles
                if query_lower in a.title.lower() or query_lower in a.summary.lower()
            ]
            return str([a.to_dict() for a in matching[:limit]])
        except Exception as e:
            logger.error("search_articles failed: %s", e, exc_info=True)
            return f"Error: {e}"

    @mcp.tool()
    async def list_feeds() -> str:
        """List all subscribed feeds with unread counts.

        Returns a JSON-formatted list of feed objects with id, name, url,
        and unread_count fields.
        """
        try:
            feeds = await client.list_feeds()
            unread_counts = await client.get_unread_counts()
            for feed in feeds:
                feed.unread_count = unread_counts.get(feed.id, 0)
            return str([f.to_dict() for f in feeds])
        except Exception as e:
            logger.error("list_feeds failed: %s", e, exc_info=True)
            return f"Error: {e}"

    @mcp.tool()
    async def get_feed_info(feed_id: int) -> str:
        """Get detailed information about a specific feed.

        Args:
            feed_id: ID of the feed.

        Returns a JSON-formatted feed object, or an error if the feed is not found.
        """
        try:
            feeds = await client.list_feeds()
            unread_counts = await client.get_unread_counts()
            for feed in feeds:
                if feed.id == feed_id:
                    feed.unread_count = unread_counts.get(feed.id, 0)
                    return str(feed.to_dict())
            return f"Error: Feed {feed_id} not found"
        except Exception as e:
            logger.error("get_feed_info failed: %s", e, exc_info=True)
            return f"Error: {e}"

    @mcp.tool()
    async def get_feed_stats() -> str:
        """Get statistics for all feeds.

        Returns a JSON-formatted list of objects with feed_id, feed_name,
        and unread_count fields.
        """
        try:
            feeds = await client.list_feeds()
            unread_counts = await client.get_unread_counts()
            result = []
            for feed in feeds:
                result.append(
                    {
                        "feed_id": feed.id,
                        "feed_name": feed.name,
                        "unread_count": unread_counts.get(feed.id, 0),
                    }
                )
            return str(result)
        except Exception as e:
            logger.error("get_feed_stats failed: %s", e, exc_info=True)
            return f"Error: {e}"

    @mcp.tool()
    async def mark_as_read(article_ids: list[int]) -> str:
        """Mark articles as read.

        Args:
            article_ids: List of article IDs to mark as read.

        Returns "OK" on success or an error message.
        """
        try:
            if not article_ids:
                return "OK"
            await client.mark_as_read(article_ids)
            return "OK"
        except Exception as e:
            logger.error("mark_as_read failed: %s", e, exc_info=True)
            return f"Error: {e}"

    @mcp.tool()
    async def mark_as_unread(article_ids: list[int]) -> str:
        """Mark articles as unread.

        Args:
            article_ids: List of article IDs to mark as unread.

        Returns "OK" on success or an error message.
        """
        try:
            if not article_ids:
                return "OK"
            await client.mark_as_unread(article_ids)
            return "OK"
        except Exception as e:
            logger.error("mark_as_unread failed: %s", e, exc_info=True)
            return f"Error: {e}"

    @mcp.tool()
    async def star_article(article_id: int) -> str:
        """Star/favorite an article.

        Args:
            article_id: ID of the article to star.

        Returns "OK" on success or an error message.
        """
        try:
            await client.star_article(article_id)
            return "OK"
        except Exception as e:
            logger.error("star_article failed: %s", e, exc_info=True)
            return f"Error: {e}"

    @mcp.tool()
    async def unstar_article(article_id: int) -> str:
        """Remove star from an article.

        Args:
            article_id: ID of the article to unstar.

        Returns "OK" on success or an error message.
        """
        try:
            await client.unstar_article(article_id)
            return "OK"
        except Exception as e:
            logger.error("unstar_article failed: %s", e, exc_info=True)
            return f"Error: {e}"

    # ── Subscribe / Unsubscribe / RSSHub pipeline ────────────────────────

    @mcp.tool()
    async def subscribe_feed(
        url: str,
        title: str | None = None,
        category: str | None = None,
        *,
        force: bool = False,
    ) -> str:
        """Subscribe to a feed by URL. Idempotent unless force=True.

        Args:
            url: Feed URL (RSS/Atom) or website URL (FreshRSS will
                autodiscover).
            title: Optional display title override. Applied to existing
                feed if already subscribed.
            category: Optional category/folder name. Auto-created if it
                doesn't exist.
            force: If True, skip the idempotency check and call quickadd
                unconditionally. Use this if you've previously unsubscribed
                and want to re-add, or if the URL string differs slightly
                from what's in list_feeds (e.g. trailing slash).

        Returns a dict with feed_id, feed_url, title, category,
        already_subscribed.
        """
        try:
            result = await client.subscribe(
                url=url, title=title, category=category, force=force,
            )
            return str(result.to_dict())
        except Exception as e:
            logger.error("subscribe_feed failed: %s", e, exc_info=True)
            return f"Error: {e}"

    @mcp.tool()
    async def unsubscribe_feed(
        feed_id: int | None = None,
        url: str | None = None,
    ) -> str:
        """Unsubscribe from a feed. Provide at least one of feed_id or url.

        Args:
            feed_id: Numeric feed id (from list_feeds).
            url: Feed URL (matched against list_feeds).

        Returns "OK" on success. If the feed is not found, returns an
        Error string with the unmatched url/id. (Unlike the underlying
        FreshRSS API, which is idempotent on missing feed_id, this
        surfaces missing URLs so the user knows nothing happened.)
        """
        try:
            ok = await client.unsubscribe(feed_id=feed_id, url=url)
            return "OK" if ok else "Error: unsubscribe returned false"
        except ValueError as e:
            return f"Error: {e}"
        except SubscriptionNotFound as e:
            return f"Error: {e}"
        except Exception as e:
            logger.error("unsubscribe_feed failed: %s", e, exc_info=True)
            return f"Error: {e}"

    @mcp.tool()
    async def ingest_url(
        url: str,
        prefer: list[str] | None = None,
        *,
        force: bool = False,
    ) -> str:
        """Subscribe to a non-RSS-native URL by routing it through RSSHub.

        Looks up the URL in the bundled RSSHub routes catalog (radar match
        first, name fallback if no radar hit), builds the feed URL, and
        subscribes FreshRSS to it.

        Args:
            url: Site URL (YouTube channel, GitHub repo, etc.) or a
                feed URL (skips RSSHub resolution in that case).
            prefer: Optional list of route path substrings to bias toward
                when multiple routes match (e.g. ['/issue', '/releases']).
                If set, the highest-scoring match is picked automatically.
                If unset and there are multiple matches, returns an error
                so the caller can disambiguate.
            force: Forwarded to subscribe_feed. Skips the idempotency
                check so you can re-subscribe a previously-deleted feed.

        Returns a dict with:
            input_url, matched_route (or None if short-circuited as a
            feed), feed_url, subscription, warnings.
        """
        try:
            if is_feed_url(url):
                sub = await client.subscribe(url=url, force=force)
                return str({
                    "input_url": url,
                    "matched_route": None,
                    "feed_url": sub.feed_url,
                    "subscription": sub.to_dict(),
                    "warnings": [],
                })

            catalog = load_catalog(rsshub_routes_path)
            candidates = find_routes_by_url(url, catalog)

            if prefer:
                # Boost candidates whose path contains any prefer substring.
                candidates.sort(
                    key=lambda c: sum(1 for p in prefer if p in c.path),
                    reverse=True,
                )

            if not candidates:
                return (
                    f"Error: no RSSHub route matches {url}. "
                    f"Try ingest_rsshub_path('/some/path') to use a known "
                    f"path, or list_routes() to see what's available."
                )

            # If multiple candidates and no prefer bias, refuse to guess.
            if len(candidates) > 1 and not prefer:
                paths = [f"{c.namespace}{c.path}" for c in candidates[:10]]
                return (
                    f"Error: multiple routes match {url}: {paths}. "
                    f"Pass prefer=['/some/substring'] to disambiguate, or "
                    f"use ingest_rsshub_path() with the chosen path."
                )

            chosen = candidates[0]
            feed_url = chosen.build_url(rsshub_base_url)
            sub = await client.subscribe(url=feed_url, force=force)

            warnings: list[str] = []
            if chosen.features.get("requirePuppeteer"):
                warnings.append(
                    "Route requires puppeteer — first fetch may take 30-60s."
                )
            if chosen.features.get("antiCrawler"):
                warnings.append(
                    "Route has anti-crawler protection — may fail without login."
                )

            return str({
                "input_url": url,
                "matched_route": chosen.to_dict(),
                "feed_url": feed_url,
                "subscription": sub.to_dict(),
                "warnings": warnings,
            })
        except Exception as e:
            logger.error("ingest_url failed: %s", e, exc_info=True)
            return f"Error: {e}"

    @mcp.tool()
    async def ingest_rsshub_path(
        path: str,
        params: dict[str, str] | None = None,
        *,
        force: bool = False,
    ) -> str:
        """Subscribe to a known RSSHub path directly, bypassing URL→route.

        Args:
            path: RSSHub path with optional :param placeholders, e.g.
                '/github/issue/:user/:repo'.
            params: Dict mapping param names to values, e.g.
                {'user': 'DIYgod', 'repo': 'RSSHub'}.
            force: Forwarded to subscribe_feed. Skips the idempotency check.

        Returns a dict with path, params, feed_url, subscription.
        """
        try:
            params = params or {}
            url_path = path
            for key, value in params.items():
                url_path = url_path.replace(f":{key}", quote(value, safe=""))
            # Strip any remaining {constraint} markers
            url_path = re.sub(r"\{[^}]*\}", "", url_path)
            feed_url = f"{rsshub_base_url.rstrip('/')}{url_path}"
            sub = await client.subscribe(url=feed_url, force=force)
            return str({
                "path": path,
                "params": params,
                "feed_url": feed_url,
                "subscription": sub.to_dict(),
            })
        except Exception as e:
            logger.error("ingest_rsshub_path failed: %s", e, exc_info=True)
            return f"Error: {e}"

    @mcp.tool()
    async def list_routes(
        query: str,
        namespace: str | None = None,
        limit: int = 20,
    ) -> str:
        """Search the RSSHub route catalog by name and description.

        Args:
            query: Substring to search for in route name and description
                (case-insensitive).
            namespace: Optional filter to one namespace (e.g. 'github').
            limit: Max results to return (default 20).

        Returns a list of route dicts with namespace, path, name, example,
        categories, captured_params (empty for name-search results).
        """
        try:
            catalog = load_catalog(rsshub_routes_path)
            results = find_routes_by_name(
                query, catalog, namespace=namespace, limit=limit,
            )
            return str([r.to_dict() for r in results])
        except Exception as e:
            logger.error("list_routes failed: %s", e, exc_info=True)
            return f"Error: {e}"
