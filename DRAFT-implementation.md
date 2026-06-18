# DRAFT — Implementation for `subscribe`/`unsubscribe` + RSSHub pipeline

**Status:** unverified draft. NOT YET TESTED against live FreshRSS/RSSHub.
**Read alongside:** `PLAN-subscribe-unsubscribe-rsshub.md` for the design rationale.
**Critique:** see bottom of this file.

This draft covers two release tracks:
- **v0.3.0:** `subscribe_feed`, `unsubscribe_feed` (GReader `quickadd` + `subscription/edit`)
- **v0.4.0:** `ingest_url`, `ingest_rsshub_path`, `list_routes` (RSSHub pipeline, depends on v0.3 internals)

---

## File 1: `src/freshrss_mcp/models.py` (additions)

Append to existing file:

```python
@dataclass
class SubscriptionResult:
    """Result of subscribe or unsubscribe. Used as the return type of
    client.subscribe(); tools.py stringifies via to_dict().
    """
    feed_id: int
    feed_url: str
    title: str
    category: str | None        # None for default/Uncategorized or when unknown
    already_subscribed: bool    # True for idempotent no-op on existing feed

    def to_dict(self) -> dict:
        return {
            "feed_id": self.feed_id,
            "feed_url": self.feed_url,
            "title": self.title,
            "category": self.category,
            "already_subscribed": self.already_subscribed,
        }
```

No changes to `Feed` or `Article`.

---

## File 2: `src/freshrss_mcp/config.py` (additions)

```python
class Config(BaseSettings):
    # ... existing fields ...

    # RSSHub pipeline (v0.4.0)
    rsshub_base_url: str = Field(
        default="http://100.91.202.122:8087",
        alias="RSSHUB_BASE_URL",
    )
    rsshub_routes_path: str = Field(
        default="/app/data/routes.json",
        alias="RSSHUB_ROUTES_PATH",   # (renamed from plan: was RSSHUB_RSSHUB_ROUTES_PATH — typo)
    )
```

---

## File 3: `src/freshrss_mcp/client.py` (additions)

Append to `FreshRSSClient`:

```python
class SubscriptionNotFound(Exception):
    """Raised when unsubscribe is called with a URL or feed_id that does not
    match any existing subscription."""


async def _quickadd(self, url: str) -> dict:
    """POST quickadd → returns parsed JSON.
    FreshRSS source: greader.php -> quickadd() returns
      {numResults, query, streamId='feed/<id>', streamName}  on success
      {numResults: 0, error: "..."}                             on failure
    Both are HTTP 200. Failure is signalled by numResults==0.
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
    action: str,                       # 'subscribe'|'unsubscribe'|'edit'
    feed_id: int,
    *,
    title: str | None = None,
    category: str | None = None,
) -> None:
    """POST subscription/edit. Returns silently on 200 + 'OK' body.
    FreshRSS source: greader.php -> subscriptionEdit() — on success exits 'OK',
    on bad request exits 400 (no body). It does not validate that the feed
    exists for unsubscribe; removeFeed is idempotent.
    """
    await self._ensure_authenticated()
    params: dict[str, str] = {
        "ac": action,
        "s": f"feed/{feed_id}",
    }
    if title is not None:
        params["t"] = title
    if category is not None:
        from urllib.parse import quote
        params["a"] = f"user/-/label/{quote(category, safe='')}"
    response = await self._client.post(
        f"{self.api_url}/reader/api/0/subscription/edit",
        headers=self._get_auth_headers(),
        params=params,
    )
    response.raise_for_status()
    # Body is 'OK\n' but we don't need to parse it.


async def subscribe(
    self,
    url: str,
    title: str | None = None,
    category: str | None = None,
) -> SubscriptionResult:
    """Subscribe to a feed by URL. Idempotent.

    1. Check list_feeds() for existing subscription with matching URL.
       If found, return SubscriptionResult(already_subscribed=True).
    2. POST quickadd. Parse {streamId, query, streamName}.
    3. If title or category given, follow up with subscription/edit?ac=edit
       to apply them (FreshRSS does not accept these in quickadd).
    4. Return SubscriptionResult(already_subscribed=False).
    """
    # Idempotency check
    feeds = await self.list_feeds()
    for feed in feeds:
        if feed.url == url:
            return SubscriptionResult(
                feed_id=feed.id,
                feed_url=feed.url,
                title=feed.name,
                category=None,            # list_feeds doesn't return category
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

    If both are given, feed_id wins. If url is given and feed_id is None,
    we look up the id via list_feeds().
    """
    if feed_id is None and url is None:
        raise ValueError("Either feed_id or url is required")

    if feed_id is None:
        # Resolve url → id
        feeds = await self.list_feeds()
        feed_id = next((f.id for f in feeds if f.url == url), None)
        if feed_id is None:
            raise SubscriptionNotFound(f"No subscription matches url: {url}")

    await self._subscription_edit(action="unsubscribe", feed_id=feed_id)
    return True
```

Add a `config` property to expose config to tools:

```python
@property
def config(self) -> Config:
    return self._config
```

---

## File 4: `src/freshrss_mcp/routes_matcher.py` (new)

```python
"""Match URLs against the RSSHub route catalog (radar field) and do
name/description search. Pure functions, no I/O besides the initial
catalog load (cached for process lifetime).
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RouteCandidate:
    namespace: str
    path: str                       # In-namespace path, e.g. "/issue/:user/:repo"
    name: str
    example: str | None
    categories: list[str]
    features: dict
    captured_params: dict[str, str] = field(default_factory=dict)

    def build_url(self, base_url: str) -> str:
        """Build the full RSSHub URL for this route, filling in captured params."""
        url_path = self.path
        for key, value in self.captured_params.items():
            url_path = url_path.replace(f":{key}", value)
        # Strip {regex-constraint} markers (e.g. '{.+}') from the path
        url_path = re.sub(r"\{[^}]*\}", "", url_path)
        return f"{base_url.rstrip('/')}/{self.namespace}{url_path}"

    def to_dict(self) -> dict:
        return {
            "namespace": self.namespace,
            "path": self.path,
            "name": self.name,
            "example": self.example,
            "categories": self.categories,
            "captured_params": self.captured_params,
        }


_CATALOG_CACHE: dict | None = None
_CATALOG_PATH: str | None = None


def load_catalog(path: str) -> dict:
    """Load the routes catalog from disk, cached for the lifetime of the process.
    Re-loads if path changes (e.g. after a container restart that swapped
    routes.json).
    """
    global _CATALOG_CACHE, _CATALOG_PATH
    if _CATALOG_CACHE is None or _CATALOG_PATH != path:
        with open(path) as f:
            _CATALOG_CACHE = json.load(f)
        _CATALOG_PATH = path
    return _CATALOG_CACHE


def is_feed_url(url: str) -> bool:
    """Heuristic: does this URL already look like a feed?"""
    return bool(re.search(
        r"\.(xml|atom|rss)(\?.*)?$|/(feed|rss)/?(/|$|\?)",
        url,
        re.IGNORECASE,
    ))


# Path-template parsing. Path segments look like:
#   "github.com"        — literal
#   ":user"             — required param, captures [^/]+
#   ":user?"            — optional param, may be absent
#   "{.pattern}"        — required, captures using pattern (no name)
#   ":user{.pattern}"   — required param with custom regex
#   ":user{.pattern}?"  — optional param with custom regex
#
# We return (compiled_regex, [param_names]) for matching. The regex is anchored.

def _template_to_regex(template: str) -> tuple[re.Pattern, list[str]]:
    parts: list[str] = []
    param_names: list[str] = []
    for segment in template.split("/"):
        if not segment:
            continue
        # Match: optional name, optional constraint, optional trailing '?'
        m = re.match(r"^(:?[\w]+)?(?:\{([^}]*)\})?(\?)?$", segment)
        if not m:
            # Fallback: treat as literal
            parts.append("/" + re.escape(segment))
            continue
        name, constraint, optional = m.group(1), m.group(2), m.group(3)
        regex = constraint or "[^/]+"
        if optional:
            parts.append(f"(?:/({regex}))?")
        else:
            parts.append(f"/({regex})")
        if name:
            param_names.append(name.lstrip(":"))
    pattern_str = "^" + "".join(parts).lstrip("/") + "$"
    return re.compile(pattern_str), param_names


def find_routes_by_url(url: str, catalog: dict) -> list[RouteCandidate]:
    """Find all RSSHub routes whose radar patterns match the URL."""
    # Normalize: strip protocol, query, fragment, trailing slash
    normalized = re.sub(r"^https?://", "", url)
    normalized = re.sub(r"[?#].*$", "", normalized).rstrip("/")

    candidates: list[RouteCandidate] = []
    for namespace, ns_data in catalog.items():
        for path_key, route in ns_data.get("routes", {}).items():
            for radar in route.get("radar", []):
                for source in radar.get("source", []):
                    try:
                        pattern, param_names = _template_to_regex(source)
                    except Exception:
                        continue
                    m = pattern.match(normalized)
                    if not m:
                        continue
                    captured = {}
                    for i, name in enumerate(param_names):
                        if m.group(i + 1):
                            captured[name] = m.group(i + 1)
                    target = radar.get("target", path_key)
                    candidates.append(RouteCandidate(
                        namespace=namespace,
                        path=target,
                        name=route.get("name", ""),
                        example=route.get("example"),
                        categories=route.get("categories", []),
                        features=route.get("features", {}),
                        captured_params=captured,
                    ))
    return candidates


def find_routes_by_name(
    query: str,
    catalog: dict,
    namespace: str | None = None,
    limit: int = 20,
) -> list[RouteCandidate]:
    """Substring search on name + description. Cheap, no regex."""
    query_lower = query.lower()
    results: list[RouteCandidate] = []
    for ns, ns_data in catalog.items():
        if namespace and ns != namespace:
            continue
        for path_key, route in ns_data.get("routes", {}).items():
            name = (route.get("name") or "").lower()
            desc = (route.get("description") or "").lower()
            if query_lower in name or query_lower in desc:
                results.append(RouteCandidate(
                    namespace=ns,
                    path=path_key,
                    name=route.get("name", ""),
                    example=route.get("example"),
                    categories=route.get("categories", []),
                    features=route.get("features", {}),
                ))
                if len(results) >= limit:
                    return results
    return results
```

---

## File 5: `src/freshrss_mcp/tools.py` (additions)

Append to `register_tools()`. Imports needed at top of file:

```python
from .routes_matcher import (
    RouteCandidate,
    find_routes_by_name,
    find_routes_by_url,
    is_feed_url,
    load_catalog,
)
```

Tools:

```python
@mcp.tool()
async def subscribe_feed(
    url: str,
    title: str | None = None,
    category: str | None = None,
) -> str:
    """Subscribe to a feed by URL. Idempotent.

    Args:
        url: Feed URL (RSS/Atom) or website URL (FreshRSS will autodiscover).
        title: Optional display title override.
        category: Optional category/folder name. Auto-created if it doesn't exist.

    Returns a dict with feed_id, feed_url, title, category, already_subscribed.
    """
    try:
        result = await client.subscribe(url=url, title=title, category=category)
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

    Returns "OK" on success, an error string if not found or on auth failure.
    """
    try:
        ok = await client.unsubscribe(feed_id=feed_id, url=url)
        return "OK" if ok else "Error: unsubscribe returned false"
    except SubscriptionNotFound as e:
        return f"Error: {e}"
    except Exception as e:
        logger.error("unsubscribe_feed failed: %s", e, exc_info=True)
        return f"Error: {e}"


@mcp.tool()
async def ingest_url(
    url: str,
    prefer: list[str] | None = None,
) -> str:
    """Subscribe to a non-RSS-native URL by routing it through the local
    RSSHub instance. Looks up the matching route, builds the feed URL,
    subscribes FreshRSS.

    Args:
        url: Site URL (YouTube channel, GitHub repo, etc.) or feed URL.
        prefer: Optional list of route path substrings to bias toward when
                multiple routes match (e.g. ['/issue', '/releases']).

    Returns a dict with input_url, matched_route, feed_url, subscription,
    warnings. Errors with 'no RSSHub route matches' or 'multiple routes match'
    if the lookup is ambiguous.
    """
    try:
        # Short-circuit: already looks like a feed
        if is_feed_url(url):
            sub = await client.subscribe(url=url)
            return str({
                "input_url": url,
                "matched_route": None,
                "feed_url": sub.feed_url,
                "subscription": sub.to_dict(),
                "warnings": [],
            })

        catalog = load_catalog(client.config.rsshub_routes_path)
        candidates = find_routes_by_url(url, catalog)

        if prefer:
            # Boost candidates whose path contains any prefer substring
            candidates.sort(
                key=lambda c: sum(1 for p in prefer if p in c.path),
                reverse=True,
            )

        if not candidates:
            return (
                f"Error: no RSSHub route matches {url}. "
                f"Try ingest_rsshub_path('/some/path') to use a known path, "
                f"or list_routes() to see what's available."
            )

        if len(candidates) > 1 and not prefer:
            paths = [f"{c.namespace}{c.path}" for c in candidates[:5]]
            return (
                f"Error: multiple routes match {url}: {paths}. "
                f"Pass prefer=['/some/substring'] to disambiguate, or use "
                f"ingest_rsshub_path() with the chosen path."
            )

        chosen = candidates[0]
        feed_url = chosen.build_url(client.config.rsshub_base_url)
        sub = await client.subscribe(url=feed_url)

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
) -> str:
    """Subscribe to a known RSSHub path directly, bypassing URL→route lookup.

    Args:
        path: RSSHub path with optional :param placeholders, e.g.
              '/github/issue/:user/:repo'.
        params: Dict mapping param names to values, e.g. {'user': 'DIYgod'}.

    Returns a dict with path, params, feed_url, subscription.
    """
    try:
        from urllib.parse import quote
        params = params or {}
        url_path = path
        for key, value in params.items():
            url_path = url_path.replace(f":{key}", quote(value, safe=""))
        # Strip any remaining {constraint} markers
        url_path = re.sub(r"\{[^}]*\}", "", url_path)
        feed_url = f"{client.config.rsshub_base_url.rstrip('/')}{url_path}"
        sub = await client.subscribe(url=feed_url)
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
        query: Substring to search for in route name and description.
        namespace: Optional filter to one namespace (e.g. 'github').
        limit: Max results to return (default 20).

    Returns a list of route dicts with namespace, path, name, example, categories.
    """
    try:
        catalog = load_catalog(client.config.rsshub_routes_path)
        results = find_routes_by_name(
            query, catalog, namespace=namespace, limit=limit
        )
        return str([r.to_dict() for r in results])
    except Exception as e:
        logger.error("list_routes failed: %s", exc_info=True)
        return f"Error: {e}"
```

---

## File 6: `Dockerfile` (additions)

Add a second build stage before the runtime stage, and `COPY` the catalog:

```dockerfile
# ── Stage 0: source RSSHub's routes catalog ────────────────────────────────
FROM diygod/rsshub:latest AS rsshub

# ── Stage 1: build (existing) ────────────────────────────────────────────
FROM python:3.13-slim AS build
# ... existing COPY and uv sync ...

# ── Stage 2: runtime (existing) ──────────────────────────────────────────
FROM python:3.13-slim AS runtime
# ... existing COPY --from=build ...

# Bring in the routes catalog
COPY --from=rsshub /app/assets/build/routes.json /app/data/routes.json
```

---

## End of draft. Critique below.

---

# Critique

## A. What I'm confident about

- The GReader API surface mapping (`quickadd` for subscribe, `subscription/edit` for unsubscribe + rename/move) is correct — verified against the FreshRSS PHP source in `/var/www/FreshRSS/p/api/greader.php`.
- `SubscriptionResult` is a clean dataclass that fits the existing model style.
- The overall control flow: idempotency check → quickadd → optional edit is right.
- The `RSSHUB_BASE_URL` default `http://100.91.202.122:8087` is correct for our Tailscale setup.
- FreshRSS's `subscription/edit` with `ac=edit` accepts title and category changes.

## B. Specific bugs I'm spotting in the draft

1. **`load_catalog` cache invalidation is too eager.** It reloads on every path change, but the path is read from `client.config.rsshub_routes_path` which is set at startup and never changes for the lifetime of the process. The cache will only ever hit the first call. Acceptable, but the logic is wrong (or at least misleading). Simpler: cache forever for the process lifetime.

2. **`_template_to_regex` regex is wrong for the empty-optional case.** I generate `(?:/(regex))?` which means the slash is also optional. For the path template `github.com/:user` with URL `github.com/DIYgod`, I want the slash to be there. Currently the `?` makes both the slash AND the param optional, so `pattern.match("github.com")` would also succeed (returning group(1)=None). Need to make the slash required and only the param optional. Fix: use `(?:/(regex))?` but anchor carefully, or use a different pattern structure.

3. **Path-template parsing doesn't handle `{:filepath{.+}}` (nested braces).** The regex `^(:?[\w]+)?(?:\{([^}]*)\})?(\?)?$` would fail on `:filepath{.+}` because `[^}]*` greedily matches `.+` but then the closing `}` is unaccounted for. Need a more careful parser. (Or just document that this format isn't supported and fall back to literal.)

4. **`ingest_url` always loads the full 5MB catalog.** Even when we just need to look up one URL. With caching, the first call is slow, but subsequent calls are fast. The plan doesn't have a benchmark; if performance becomes an issue, we'd want to pre-index by host. Not blocking v0.4.0.

5. **`subscribe()` calls `list_feeds()` on every call for idempotency check.** This is one extra HTTP round-trip per subscribe. For a daily cron ingesting a few feeds, fine. For a loop, expensive. Could add `check_existing: bool = True` parameter. Not blocking.

6. **`client.config` property:** I added a property to expose `_config`. But `Config` has a `SecretStr` field for the password — exposing the whole config leaks it. Better: pass just the needed fields to `register_tools` as separate args. The plan said "config additions to `config.py`" but doesn't address how tools get the config. This is a real gap.

7. **`unsubscribe()` doesn't validate that the `feed_id` is positive or actually exists.** If the user passes `feed_id=-1` or `feed_id=999999`, we call `subscription/edit?s=feed/-1` or `s=feed/999999`. FreshRSS's `removeFeed` is idempotent and returns OK silently, so we'd return True even though nothing happened. Not a correctness issue, but a UX issue — the user thinks they unsubscribed but maybe didn't.

8. **The path template `_template_to_regex` builds a regex with a single anchored match.** It doesn't handle the case where the template has wildcards (e.g. `{.+}` for "any path"). I saw `/file/:user/:repo/:branch/:filepath{.+}` in the catalog — the `{.+}` is "match anything including slashes". My regex uses `[^/]+` for unnamed constraints if I don't specify a constraint, but if a constraint IS specified, I use it as-is. So `{:.+}` would correctly match across slashes. But the parsing of `{:filepath{.+}}` is broken (point 3 above).

9. **`ingest_url`'s "multiple routes match" branch returns the first 5 paths as a string.** If there are 50 matches (unlikely but possible), the user only sees 5. Better: return all candidates with their `namespace/path` so the user (or the agent) can pick. Tradeoff: bigger response. The plan's spec said "return Error with paths", so this is a docs-vs-implementation gap.

10. **The `unsubscribe` tool's `SubscriptionNotFound` exception is handled separately but the other exceptions fall through to the generic `except Exception`.** The `client.unsubscribe` may also raise `ValueError` (when neither id nor url is given). That should probably also be handled as a clear error, not a generic exception.

## C. What I haven't verified (and need live testing to confirm)

1. **Exact JSON shape of `quickadd` response.** I'm assuming `{numResults, query, streamId, streamName}` from the PHP source. The actual response could differ in field order, case, or extra fields. The draft's `result.get("streamId", "")` is defensive, but if the field is named differently, we'd silently get 0 feed_id.

2. **Whether `subscription/edit` accepts `params` as a query string (GET-style) vs form body (POST-style).** I use `params=` in httpx, which builds a query string even on a POST. The PHP code reads from `$_REQUEST` which covers both GET and POST. Should work, but worth a live test.

3. **Whether `t=<title>` with URL-encoded special characters round-trips correctly.** E.g. `t=foo%20bar` — does FreshRSS store it as "foo bar" or "foo%20bar"?

4. **Whether `a=user/-/label/<category>` actually creates the category if it doesn't exist.** PHP source says yes (`addCategory` is called if `searchByName` returns null). But there could be permission or validation issues.

5. **The radar field's actual coverage.** The plan says 2316/3303 routes have radar. I haven't checked whether the routes we'd commonly use (YouTube, GitHub, Twitter, Reddit) all have radar or if some only have `name`. If only `name`, my URL→route matching won't find them, and the user has to use `ingest_rsshub_path`.

6. **The exact format of the `target` field in radar.** It might be an absolute path (`/issue/:user/:repo`) or relative (`:user/:repo`). I've assumed absolute, but a relative target would mean we don't prepend the namespace. Need to verify against a few real entries.

7. **How `routes.json` version drift between the running RSSHub and the bundled catalog affects results.** If we bundle v2024-12 routes and RSSHub runs v2025-03, the agent might suggest routes that no longer exist. Acceptable, but worth a build-time check.

## D. What I'm missing (gaps I'd want to fill before implementation)

1. **The `Feed` model doesn't have a `category` field.** So `SubscriptionResult(already_subscribed=True).category` is always `None`. To populate it, we'd need to either (a) extend `Feed` with category (requires understanding how `subscription/list` returns it — it does, as a `categories` array of category IDs, which need a separate `category/list` lookup), or (b) make a second API call. Both are "later" items, not blocking v0.3.

2. **A test for the case where `quickadd` returns a `streamId` we can't parse.** The `_extract_feed_id` falls back to `hash() % 1_000_000_000` which is a meaningless positive integer. We'd then call `subscription/edit?s=feed/<hash>`, which FreshRSS would reject. The current code would raise an httpx error. Acceptable behavior but worth a test.

3. **A way to "force subscribe" (bypass idempotency).** If a user wants to re-add a feed they previously deleted, the URL would no longer be in `list_feeds()`, so they'd just call `subscribe_feed(url)` again and it'd quickadd. Works without a `force` flag. So the flag isn't needed. But documenting this in the docstring would help.

4. **Path encoding for `ingest_rsshub_path`.** I added `urllib.parse.quote(value, safe="")` for the params. But if the user passes `path="/some/path with spaces"`, we don't encode the path. We probably should — but this is an edge case.

5. **The `list_routes` tool searches by name and description only.** It doesn't search by example URL or by captured param. If the user wants "all GitHub routes that take a `user` param", they have to filter client-side. Could add a `param` filter later.

## E. Questions for you (Krit)

1. **Are you OK with `RSSHUB_BASE_URL` defaulting to `http://100.91.202.122:8087`?** It's Tailscale-specific. If we want this to be portable to other machines, we should make it required (no default) or move it to a separate config file. The current default is fine for our setup.

2. **Split the work into two PRs (v0.3.0 subscribe/unsubscribe, v0.4.0 RSSHub pipeline) or land as one?** I lean toward one for atomicity, but if you want to ship subscribe/unsubscribe first and test it, two PRs is fine.

3. **Should `ingest_url` errors be plain strings (current pattern) or use FastMCP's structured error responses?** Existing tools use strings. I'll follow the existing pattern unless you want to change it.

4. **Pin `diygod/rsshub:latest` in the Dockerfile to a specific tag for reproducibility?** Default to latest, document rebuild-to-update. Or pin to a date tag (e.g. `diygod/rsshub:2024-12-01`)?

5. **What's the right behavior when `subscribe_feed` is called with a feed URL that's already subscribed but with a different `title=` or `category=`?** Current draft returns `already_subscribed=True` and ignores the title/category. Should we update the existing feed's title/category instead? (Easy add: a follow-up `subscription/edit?ac=edit` call.)

6. **`unsubscribe` should silently succeed or error if the feed isn't subscribed?** FreshRSS's own behavior is silent success (idempotent). My draft follows that. But a more honest API would error so the user knows nothing happened. Which do you prefer?

7. **Should `ingest_url` be a destructive operation by default?** I.e. if the URL matches a route, should it auto-subscribe, or should it return a "preview" that the user has to confirm? Current draft: auto-subscribe. Could add a `dry_run: bool = False` parameter.

## F. Out-of-scope items I noticed during the draft

- **Concurrent subscribe operations** — two parallel `ingest_url` calls for the same URL would both pass the idempotency check, both call quickadd, and the second would fail. No locking. Probably fine for our use case (single user, sequential cron).
- **Catalog versioning** — if RSSHub ships a new `routes.json` format, our parser breaks silently. No version field is read from the catalog.
- **The `apiRoutes` field** — some namespaces have an `apiRoutes` sub-dict (alternative URL patterns for REST-style APIs). I don't handle these. They're a different concept from `routes`.

## G. Estimated effort (refined after drafting)

- v0.3.0 (subscribe/unsubscribe only): 1.5 hours
  - 30 min: models + client.subscribe/unsubscribe
  - 30 min: tools.py additions
  - 30 min: unit tests
- v0.4.0 (RSSHub pipeline): 3 hours
  - 1 hour: routes_matcher.py + tests
  - 30 min: Dockerfile change
  - 30 min: tools.py additions (ingest_url, ingest_rsshub_path, list_routes)
  - 30 min: unit tests
  - 30 min: live verification
- Both: 15 min for README, version bump, commit, push, rebuild.

**Total: ~4.5–5 hours.** Slightly higher than the plan's estimate because the regex parser is more complex than I expected.

## H. What I'd change before implementing

1. **Don't expose `client.config` as a property.** Instead, pass `rsshub_base_url` and `rsshub_routes_path` as separate args to `register_tools(mcp, client, rsshub_base_url, rsshub_routes_path)`. Avoids leaking the SecretStr.

2. **Fix the `_template_to_regex` slash-optionality bug** before any test runs. Anchor the optional slash to require the param if the slash is present.

3. **Use a more conservative regex parser** that handles only the common patterns I've seen. For exotic patterns, log a warning and skip. Better to have 90% coverage that works than 100% coverage that crashes on edge cases.

4. **Cache the catalog in module scope, not in a function-local closure.** Easier to invalidate for tests, easier to reason about.

5. **Add a `force` parameter to `subscribe_feed`** to skip the idempotency check. Cheap, might be useful, doesn't add complexity.

6. **Add a `dry_run` parameter to `ingest_url`** so the agent (or the user) can preview what would be subscribed without actually doing it.
