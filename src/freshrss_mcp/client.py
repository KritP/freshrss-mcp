"""FreshRSS API client using Google Reader API."""

import logging
from urllib.parse import quote

import httpx

from .config import Config
from .models import Article, Feed, SubscriptionResult

logger = logging.getLogger(__name__)


class FreshRSSClient:
    """Async client for FreshRSS Google Reader API.

    Designed for single-instance lifecycle: create once at startup,
    authenticate, then reuse for all tool calls.
    """

    def __init__(self, config: Config):
        self._config = config
        base_url = config.freshrss_url.rstrip("/")
        api_path = config.freshrss_api_path.rstrip("/")
        self.api_url = f"{base_url}{api_path}"
        self._auth_token: str | None = None
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=30.0,
            follow_redirects=True,
        )

    async def authenticate(self) -> str:
        """Authenticate with FreshRSS and obtain auth token.

        Returns:
            Authentication token (SID)

        Raises:
            AuthenticationError: If authentication fails
        """
        auth_url = f"{self.api_url}/accounts/ClientLogin"
        logger.debug("Authenticating to %s", auth_url)

        try:
            response = await self._client.post(
                auth_url,
                data={
                    "Email": self._config.freshrss_username,
                    "Passwd": self._config.freshrss_password.get_secret_value(),
                },
            )
            response.raise_for_status()

            for line in response.text.split("\n"):
                if line.startswith("SID="):
                    self._auth_token = line[4:]
                    logger.info("Authentication successful")
                    return self._auth_token

            raise AuthenticationError("No SID found in authentication response")

        except httpx.HTTPStatusError as e:
            raise AuthenticationError(f"Authentication failed: {e.response.status_code}") from e
        except AuthenticationError:
            raise
        except Exception as e:
            raise AuthenticationError(f"Authentication error: {e}") from e

    async def _ensure_authenticated(self) -> None:
        """Authenticate lazily if no token is held yet."""
        if not self._auth_token:
            await self.authenticate()

    def _get_auth_headers(self) -> dict[str, str]:
        """Get headers with authentication token."""
        if not self._auth_token:
            raise AuthenticationError("Not authenticated. Call authenticate() first.")
        return {"Authorization": f"GoogleLogin auth={self._auth_token}"}

    async def list_feeds(self) -> list[Feed]:
        """List all subscribed feeds."""
        await self._ensure_authenticated()
        headers = self._get_auth_headers()
        url = f"{self.api_url}/reader/api/0/subscription/list"

        response = await self._client.get(url, headers=headers, params={"output": "json"})
        response.raise_for_status()

        data = response.json()
        feeds = []
        for sub in data.get("subscriptions", []):
            feed = Feed(
                id=self._extract_feed_id(sub.get("id", "")),
                name=sub.get("title", "Unknown"),
                url=sub.get("url", ""),
            )
            feeds.append(feed)

        logger.info("Retrieved %d feeds", len(feeds))
        return feeds

    async def get_unread_counts(self) -> dict[int, int]:
        """Get unread article counts per feed.

        Returns:
            Dictionary mapping feed_id to unread count
        """
        await self._ensure_authenticated()
        headers = self._get_auth_headers()
        url = f"{self.api_url}/reader/api/0/unread-count"

        response = await self._client.get(url, headers=headers, params={"output": "json"})
        response.raise_for_status()

        data = response.json()
        unread_counts: dict[int, int] = {}
        for item in data.get("unreadcounts", []):
            feed_id = self._extract_feed_id(item.get("id", ""))
            count = item.get("count", 0)
            if feed_id:
                unread_counts[feed_id] = count

        return unread_counts

    async def get_articles(
        self,
        feed_id: int | None = None,
        limit: int = 20,
        include_read: bool = False,
        since_timestamp: int | None = None,
    ) -> list[Article]:
        """Get articles from FreshRSS.

        Args:
            feed_id: Optional feed ID to filter by
            limit: Maximum number of articles to return
            include_read: Whether to include read articles
            since_timestamp: Only return articles published after this timestamp
        """
        await self._ensure_authenticated()
        headers = self._get_auth_headers()
        stream_id = f"feed/{feed_id}" if feed_id else "user/-/state/com.google/reading-list"
        url = f"{self.api_url}/reader/api/0/stream/contents/{stream_id}"

        params: dict[str, str | int] = {"output": "json", "n": limit}
        if not include_read:
            params["xt"] = "user/-/state/com.google/read"
        if since_timestamp:
            params["ot"] = since_timestamp

        response = await self._client.get(url, headers=headers, params=params)
        response.raise_for_status()

        data = response.json()
        articles = []
        for item in data.get("items", []):
            article = self._parse_article(item)
            if article:
                articles.append(article)

        logger.info("Retrieved %d articles", len(articles))
        return articles

    async def mark_as_read(self, article_ids: list[int]) -> bool:
        """Mark articles as read."""
        return await self._edit_tags(article_ids, add_tags=["user/-/state/com.google/read"])

    async def mark_as_unread(self, article_ids: list[int]) -> bool:
        """Mark articles as unread."""
        return await self._edit_tags(article_ids, remove_tags=["user/-/state/com.google/read"])

    async def star_article(self, article_id: int) -> bool:
        """Star an article."""
        return await self._edit_tags([article_id], add_tags=["user/-/state/com.google/starred"])

    async def unstar_article(self, article_id: int) -> bool:
        """Unstar an article."""
        return await self._edit_tags([article_id], remove_tags=["user/-/state/com.google/starred"])

    async def _edit_tags(
        self,
        article_ids: list[int],
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
    ) -> bool:
        """Edit tags on articles."""
        await self._ensure_authenticated()
        headers = self._get_auth_headers()
        url = f"{self.api_url}/reader/api/0/edit-tag"

        item_ids = [f"tag:google.com,2005:reader/item/{aid}" for aid in article_ids]
        data: dict[str, list[str]] = {"i": item_ids}
        if add_tags:
            data["a"] = add_tags
        if remove_tags:
            data["r"] = remove_tags

        response = await self._client.post(url, headers=headers, data=data)
        response.raise_for_status()

        logger.info("Updated tags for %d articles", len(article_ids))
        return True

    def _parse_article(self, item: dict) -> Article | None:
        """Parse a FreshRSS article item into an Article model."""
        try:
            article_id = self._extract_article_id(item.get("id", ""))
            origin = item.get("origin", {})
            feed_name = origin.get("title", "Unknown Feed")

            summary = ""
            content = item.get("summary", {})
            if content:
                summary = content.get("content", "")

            categories = item.get("categories", [])
            is_read = "user/-/state/com.google/read" in categories
            is_starred = "user/-/state/com.google/starred" in categories

            alternates = item.get("alternate", [])
            url = alternates[0].get("href", "") if alternates else ""

            return Article(
                id=article_id,
                title=item.get("title", "Untitled"),
                summary=summary,
                url=url,
                published=item.get("published", 0),
                feed_name=feed_name,
                is_read=is_read,
                is_starred=is_starred,
            )
        except Exception as e:
            logger.warning("Failed to parse article: %s", e)
            return None

    @staticmethod
    def _extract_feed_id(feed_id_str: str) -> int:
        """Extract numeric feed ID from Google Reader feed ID format.

        Handles formats like "feed/123" or plain "123".
        Falls back to hash for non-numeric string IDs.
        """
        if feed_id_str.startswith("feed/"):
            feed_id_str = feed_id_str[5:]
        try:
            return int(feed_id_str)
        except ValueError:
            return hash(feed_id_str) % 1_000_000

    @staticmethod
    def _extract_article_id(article_id_str: str) -> int:
        """Extract numeric article ID from Google Reader format.

        Handles "tag:google.com,2005:reader/item/<id>" where <id>
        may be decimal or hex.
        """
        if "reader/item/" in article_id_str:
            raw = article_id_str.split("/")[-1]
            try:
                return int(raw)
            except ValueError:
                try:
                    return int(raw, 16)
                except ValueError:
                    pass
        return hash(article_id_str) % 1_000_000_000

    async def aclose(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Subscribe / Unsubscribe (GReader quickadd + subscription/edit) ─────

    async def _quickadd(self, url: str) -> dict:
        """POST to /reader/api/0/subscription/quickadd.

        FreshRSS source: greader.php -> quickadd() returns
            {numResults: 1, query, streamId="feed/<id>", streamName}  on success
            {numResults: 0, error: "..."}                              on failure
        Both are HTTP 200. Failure is signalled by numResults == 0.
        """
        await self._ensure_authenticated()
        response = await self._client.get(
            f"{self.api_url}/reader/api/0/subscription/quickadd",
            headers=self._get_auth_headers(),
            params={"quickadd": url},
        )
        response.raise_for_status()
        return response.json()

    async def _subscription_edit(
        self,
        action: str,                       # 'subscribe' | 'unsubscribe' | 'edit'
        feed_id: int,
        *,
        title: str | None = None,
        category: str | None = None,
    ) -> None:
        """POST to /reader/api/0/subscription/edit.

        FreshRSS source: greader.php -> subscriptionEdit() — on success exits
        'OK' (HTTP 200), on bad request exits 400 (no body). removeFeed is
        idempotent: unsubscribing a non-existent feed returns OK.

        title and category are only meaningful for action='edit'. URL-encode
        category because GReader label streams use '/'.
        """
        await self._ensure_authenticated()
        params: dict[str, str] = {
            "ac": action,
            "s": f"feed/{feed_id}",
        }
        if title is not None:
            params["t"] = title
        if category is not None:
            params["a"] = f"user/-/label/{quote(category, safe='')}"
        response = await self._client.post(
            f"{self.api_url}/reader/api/0/subscription/edit",
            headers=self._get_auth_headers(),
            params=params,
        )
        response.raise_for_status()
        # Body is 'OK\n' on success. We don't need to parse it.

    async def subscribe(
        self,
        url: str,
        title: str | None = None,
        category: str | None = None,
        *,
        force: bool = False,
    ) -> SubscriptionResult:
        """Subscribe to a feed by URL. Idempotent unless force=True.

        Flow:
            1. (Skip if force) Look up url in list_feeds(); if present, return
               SubscriptionResult(already_subscribed=True) with the existing
               feed's id/title. If title or category were passed, apply them
               to the existing feed via subscription/edit?ac=edit before
               returning.
            2. POST quickadd. Parse {streamId, query, streamName}.
            3. If title or category given, follow up with subscription/edit
               to apply them (quickadd does not accept these).
            4. Return SubscriptionResult(already_subscribed=False).
        """
        # Idempotency check (skip if force)
        if not force:
            feeds = await self.list_feeds()
            for feed in feeds:
                if feed.url == url:
                    # Apply title/category updates to existing feed if asked.
                    if title is not None or category is not None:
                        await self._subscription_edit(
                            action="edit",
                            feed_id=feed.id,
                            title=title,
                            category=category,
                        )
                        applied_title = title if title is not None else feed.name
                    else:
                        applied_title = feed.name
                    return SubscriptionResult(
                        feed_id=feed.id,
                        feed_url=feed.url,
                        title=applied_title,
                        category=category,  # may be None; we don't know old
                        already_subscribed=True,
                    )

        result = await self._quickadd(url)
        if result.get("numResults", 0) == 0:
            raise RuntimeError(
                f"FreshRSS quickadd failed: {result.get('error', 'unknown error')}"
            )

        stream_id = result.get("streamId", "")
        feed_id = self._extract_feed_id(stream_id)
        feed_url = result.get("query", url)
        feed_title = result.get("streamName", "")

        if title is not None or category is not None:
            await self._subscription_edit(
                action="edit",
                feed_id=feed_id,
                title=title,
                category=category,
            )
            if title is not None:
                feed_title = title

        return SubscriptionResult(
            feed_id=feed_id,
            feed_url=feed_url,
            title=feed_title,
            category=category,
            already_subscribed=False,
        )

    async def unsubscribe(
        self,
        feed_id: int | None = None,
        url: str | None = None,
    ) -> bool:
        """Unsubscribe a feed by id or URL. At least one required.

        Behavior:
            - If feed_id is given, use it directly.
            - If only url is given, look up the id via list_feeds().
            - If url is not in list_feeds(), raise SubscriptionNotFound.
            - FreshRSS's removeFeed is idempotent: unsubscribe of a non-
              existent feed_id returns OK silently (we follow that).
        """
        if feed_id is None and url is None:
            raise ValueError("Either feed_id or url is required")

        if feed_id is None:
            feeds = await self.list_feeds()
            match = next((f for f in feeds if f.url == url), None)
            if match is None:
                raise SubscriptionNotFound(f"No subscription matches url: {url}")
            feed_id = match.id

        await self._subscription_edit(action="unsubscribe", feed_id=feed_id)
        return True


class AuthenticationError(Exception):
    """Raised when authentication fails."""


class SubscriptionNotFound(Exception):
    """Raised when unsubscribe is called with a URL or feed_id that does not
    match any existing subscription."""
