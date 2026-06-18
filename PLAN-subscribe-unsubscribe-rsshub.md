# Plan: `subscribe`, `unsubscribe`, and `ingest_url` tools for freshrss-mcp

**Status:** draft
**Target repo:** `/home/ubuntu/repo/rss/freshrss-mcp/` (KritP/freshrss-mcp fork)
**Upstream:** `ChrisLAS/freshrss-mcp` v0.2.0
**Working version:** 0.3.0 (semver-minor: new user-facing tools, no breaking changes)
**Daily-ingest:** explicitly out of scope — separate plan later.

---

## 1. Goals

Add three MCP tools to the existing 10-tool surface, behind a clean GReader-API client:

| Tool | Inputs | Behavior |
|------|--------|----------|
| `subscribe_feed` | `url: str`, `title: str \| None = None`, `category: str \| None = None` | Add a feed by URL. Idempotent (returns existing feed if already subscribed). Optional title override and category placement. |
| `unsubscribe_feed` | `feed_id: int \| None = None`, `url: str \| None = None` | Remove a feed by numeric id or URL. At least one of the two is required. Errors clearly if neither matches an existing subscription. |
| `ingest_url` | `url: str`, `prefer: list[str] \| None = None` | Subscribe to a non-RSS-native URL by routing it through the local RSSHub instance. Looks up the matching route, builds the feed URL, subscribes FreshRSS. Returns the matched route + feed URL + subscription result. |
| `ingest_rsshub_path` | `path: str`, `params: dict \| None = None` | Bypass the URL→route lookup. Subscribe to a known RSSHub path directly. |
| `list_routes` | `query: str`, `namespace: str \| None = None`, `limit: int = 20` | Search the bundled RSSHub route catalog. Useful for discovery. |

After this lands, the 15-tool surface is **read** (10) + **write-back (2) + RSSHub-pipeline (3)**. No `add_category`, `rename_feed`, `move_feed` — those are nice-to-haves but not in the user's stated goals; `subscription/edit` already supports them if we want them later.

## 2. GReader API surface (verified against FreshRSS source)

Source: `/var/www/FreshRSS/p/api/greader.php` inside `rss-freshrss`.

### 2.1 `quickadd` (used for subscribe by URL)

```
POST /api/greader.php/reader/api/0/subscription/quickadd?quickadd=<URL>
Headers: Authorization: GoogleLogin auth=<SID>
Response (JSON):
  { "numResults": 1,
    "query": "<resolved_feed_url>",
    "streamId": "feed/<numeric_id>",
    "streamName": "<title>" }
Failure: { "numResults": 0, "error": "<message>" } (HTTP 200 either way)
```

- URL may be a feed URL, a website URL (FreshRSS tries to autodiscover), or `feed/<id>` (an existing feed — re-subscribes).
- Returns the resolved feed's canonical URL and its internal numeric id.
- This is the **TheOldReader API extension**, not core GReader, but FreshRSS implements it. CapyReader uses the same endpoint.

### 2.2 `subscription/edit` (used for unsubscribe + edit)

```
POST /api/greader.php/reader/api/0/subscription/edit
    ?ac=unsubscribe   (or "subscribe" or "edit")
    &s=feed/<id>      (can repeat; for unsubscribe, one feed at a time)
    &t=<title>        (only meaningful for "edit"; rename)
    &a=user/-/label/<category>  (only for "edit"; move to category)
    &r=user/-/label/<category>  (only for "edit"; remove from category)
Headers: Authorization: GoogleLogin auth=<SID>
Response: "OK\n" (text, HTTP 200)
Failure: HTTP 400 with empty body
```

**Critical finding:** `subscription/edit` operates on **stream IDs** (`feed/<numeric_id>`), not URLs. To unsubscribe, we need the numeric id. Either:
- Caller provides `feed_id` (lookup table already exists in `list_feeds`).
- Caller provides `url` → we resolve via `list_feeds()` (already in client).

### 2.3 Implication for tool design

- `subscribe_feed(url)` → call `quickadd` → return the resolved feed (id, title, url).
- `unsubscribe_feed(feed_id|None, url|str|None)` → if URL given, look up id via `list_feeds` → call `subscription/edit?ac=unsubscribe&s=feed/<id>`.

## 3. Concrete design

### 3.1 New dataclass: `SubscriptionResult`

Lives in `models.py`. One new model to keep return shapes typed and tests easy to write.

```python
@dataclass
class SubscriptionResult:
    """Result of a subscribe or unsubscribe operation."""
    feed_id: int            # FreshRSS internal numeric id
    feed_url: str           # canonical feed URL
    title: str              # feed display title
    category: str | None    # category name, or None for default/Uncategorized
    already_subscribed: bool  # True if subscribe was a no-op (idempotent hit)

    def to_dict(self) -> dict: ...
```

### 3.2 New client methods (`client.py`)

```python
async def subscribe(
    self,
    url: str,
    title: str | None = None,
    category: str | None = None,
) -> SubscriptionResult:
    """Subscribe to a feed by URL. Idempotent.

    1. Check if URL is already subscribed (match against list_feeds()).
       - If yes, return SubscriptionResult(already_subscribed=True, ...).
    2. POST to /reader/api/0/subscription/quickadd?quickadd=<url>.
    3. Parse {streamId, query, streamName} from response.
    4. If title or category given, follow up with subscription/edit
       ?ac=edit&s=feed/<id>&t=<title>&a=user/-/label/<category> (idempotent).
    5. Return SubscriptionResult(already_subscribed=False, ...).
    """

async def unsubscribe(
    self,
    feed_id: int | None = None,
    url: str | None = None,
) -> bool:
    """Unsubscribe a feed by id or URL. Returns True on success.

    1. Require at least one of {feed_id, url}; raise ValueError otherwise.
    2. If url given (and no feed_id), look up via list_feeds() by URL exact match.
       - If not found, raise SubscriptionNotFound.
    3. POST to /reader/api/0/subscription/edit?ac=unsubscribe&s=feed/<id>.
    4. Return True on HTTP 200 + body "OK".
    """
```

Two new private helpers to keep `client.py` DRY:

```python
async def _quickadd(self, url: str) -> dict: ...
async def _subscription_edit(self, action: str, feed_id: int, *, title: str | None = None, category: str | None = None) -> None: ...
```

### 3.3 New exception: `SubscriptionNotFound`

```python
class SubscriptionNotFound(Exception):
    """Raised when unsubscribe is called with a URL or feed_id that does not match any subscription."""
```

(`AuthenticationError` is already in client.py; add `SubscriptionNotFound` next to it.)

### 3.4 New tool registrations (`tools.py`)

Add to `register_tools()`:

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
        category: Optional category/folder name. Defaults to Uncategorized.

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
    """Unsubscribe from a feed. Provide either feed_id or url.

    Args:
        feed_id: Numeric feed id (from list_feeds).
        url: Feed URL (matched against list_feeds).

    Returns "OK" on success, an error string if not found or on auth failure.
    """
    try:
        ok = await client.unsubscribe(feed_id=feed_id, url=url)
        return "OK" if ok else "Error: unsubscribe returned false"
    except Exception as e:
        logger.error("unsubscribe_feed failed: %s", e, exc_info=True)
        return f"Error: {e}"
```

Tool surface after this lands: 12 tools total.

## 4. Test plan

### 4.1 Unit tests in `tests/test_client.py` (respx)

- `test_subscribe_new_feed` — mock `quickadd` JSON response, assert `subscribe()` returns `SubscriptionResult(already_subscribed=False, ...)`.
- `test_subscribe_idempotent` — pre-populate `list_feeds` mock with the URL; assert `subscribe()` returns `already_subscribed=True` and does NOT call `quickadd`.
- `test_subscribe_with_title_and_category` — assert the follow-up `subscription/edit` POST is sent with `ac=edit&t=...&a=user/-/label/<cat>`.
- `test_subscribe_quickadd_error` — mock `quickadd` returning `numResults: 0, error: "..."`; assert `subscribe()` raises the underlying error message.
- `test_unsubscribe_by_id` — assert correct `subscription/edit?ac=unsubscribe&s=feed/<id>` POST.
- `test_unsubscribe_by_url` — pre-populate `list_feeds`; assert lookup + unsubscribe.
- `test_unsubscribe_url_not_found` — pre-populate `list_feeds` without the URL; assert `SubscriptionNotFound` raised.
- `test_unsubscribe_neither_id_nor_url` — assert `ValueError`.
- `test_subscribe_feed_invalid_url` — assert FreshRSS 4xx/5xx bubbles up as an `httpx.HTTPStatusError` (not swallowed).

### 4.2 Unit tests in `tests/test_tools.py`

- `test_subscribe_feed_tool_success` — fake client returns `SubscriptionResult`; tool returns `str(result.to_dict())`.
- `test_subscribe_feed_tool_error_boundary` — fake client raises; tool returns `f"Error: {e}"`.
- Same pair for `unsubscribe_feed`.

### 4.3 Test count delta: +11 tests (9 client + 2 tools = 11; original suite was 67).

### 4.4 Live verification recipe (manual, after unit tests pass)

```bash
# 1. Add the Unsubscribe-Harness feed (uses QuickAdd endpoint, then unsubscribe).
#    Use a known-valid, low-volume feed so we can verify quickly.
TEST_URL="https://github.com/KritP/freshrss-mcp/commits/main.atom"

# 2. Subscribe (from the host, via the MCP server endpoint):
curl -s -X POST http://100.91.202.122:8005/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d "$(...initialize...)" -D /tmp/h.txt -o /dev/null
SID=...
curl -s -X POST http://100.91.202.122:8005/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SID" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",
       \"params\":{\"name\":\"subscribe_feed\",
                  \"arguments\":{\"url\":\"$TEST_URL\"}}}"
# Expected: returns feed_id, feed_url, title, already_subscribed=false

# 3. Verify it shows in list_feeds:
curl ... -d '{"jsonrpc":"2.0","id":4,"method":"tools/call",
              "params":{"name":"list_feeds","arguments":{}}}'
# Expected: a new feed with the test URL is present

# 4. Unsubscribe by URL:
curl ... -d "{\"jsonrpc\":\"2.0\",\"id\":5,\"method\":\"tools/call\",
              \"params\":{\"name\":\"unsubscribe_feed\",
                         \"arguments\":{\"url\":\"$TEST_URL\"}}}"
# Expected: "OK"

# 5. Verify removal in list_feeds (should be back to 1 feed: FreshRSS releases).

# 6. Idempotency check: subscribe again, then subscribe a 3rd time.
#    Second call should return already_subscribed=true.
```

## 5. Implementation order

Sequential because each step gates the next:

1. **Add `SubscriptionResult` to `models.py`** + add `to_dict()`. No behavior change; existing tests pass.
2. **Add `SubscriptionNotFound` to `client.py`** + helper `_quickadd` and `_subscription_edit`. No public API change yet.
3. **Add `subscribe()` and `unsubscribe()` public methods to `client.py`.** Existing tests still pass; new methods uncovered.
4. **Add unit tests in `tests/test_client.py`** (9 tests) — all should pass.
5. **Add `subscribe_feed` and `unsubscribe_feed` tool registrations in `tools.py`.** No public API change for other tools.
6. **Add unit tests in `tests/test_tools.py`** (2 tests) — all should pass.
7. **Run full suite** — `uv run pytest -v` from the repo root. Should report 78/78 pass.
8. **Live verification recipe** (Section 4.4) — exercised against the running `rss-freshrss-mcp` container.
9. **Bump version** in `pyproject.toml` from `0.2.0` → `0.3.0`.
10. **Update `README.md`** tool table to include the two new tools + brief note that `subscribe_feed` is idempotent.
11. **Commit + push to `KritP/freshrss-mcp`.** Suggested commit message: `feat: add subscribe_feed and unsubscribe_feed tools (GReader quickadd + subscription/edit)`.
12. **`docker compose build freshrss-mcp` in `/home/ubuntu/repo/rss/`** to refresh the sidecar image with the new code.
13. **Test the same end-to-end workflow through the running container** (same as step 8 but with the rebuilt image).

## 6. Risks & open questions

- **Idempotency on subscribe is "best-effort":** we match by URL exact string. FreshRSS may normalize the URL (e.g. `https://example.com/feed` vs `https://example.com/feed/`). Acceptable for v0.3.0; flag for v0.4 if CapyReader shows duplicates.
- **`quickadd` always succeeds for autodiscoverable URLs but may 5xx for malformed ones.** We surface the error to the tool caller; no retry logic. (Retry-on-5xx was a separate "later" item in the original analysis report — still later.)
- **Category names with spaces / Unicode** — GReader uses `user/-/label/<urlencoded-name>`. The client must URL-encode the category name when building the `a` query param for `subscription/edit`. Worth a unit test specifically for `category = "AI & ML"`.
- **No streaming / async ack on subscribe:** FreshRSS doesn't return immediately whether the feed has been fetched successfully. `quickadd` returns the feed metadata but the first articles may not be present for a few seconds. The tool returns as soon as FreshRSS has the subscription, not when articles arrive. Document this in the tool docstring.
- **`unsubscribe` race:** if a feed is being fetched in another request when we unsubscribe, FreshRSS may log a warning. Not a correctness issue; ignore for v0.3.0.

## 7. Out of scope (later, not this plan)

- `daily_ingest` — deferred per user. Separate plan.
- `add_category` / `rename_feed` / `move_feed_to_category` — trivially derivable from `subscription/edit` (`ac=edit`). Add when needed.
- Retry / backoff on 5xx — generic client hardening.
- Auth-token caching across requests — single-instance, single-user; not needed.

## 8. Estimated effort

- 1.5–2 hours: code + tests (steps 1–7).
- 15 min: live verification (step 8).
- 15 min: README/version/commit/push/rebuild (steps 9–12).
- **Total: ~2.5 hours wall clock, fully self-contained.**

---

# Addendum: `ingest_url` — RSSHub pipeline integration

Added 2026-06-18 after the user requested the same MCP tool surface to handle non-RSS-native URLs (YouTube channels, GitHub repos, Twitter/X, etc.) by routing through the local RSSHub instance. Conceptually: a single MCP command that, given any URL, figures out which RSSHub route to use, then subscribes FreshRSS to the resulting feed.

## A.1 Why this is needed

Most interesting web content (YouTube, GitHub, X/Twitter, Reddit, arXiv, HN, podcasts, Instagram, Bilibili, etc.) has no native RSS feed. The user wants to be able to say "subscribe me to this YouTube channel" or "ingest this GitHub repo's releases" without knowing the RSSHub path syntax. RSSHub is already running locally and produces RSS for ~3300 route families, but:

1. **No route catalog endpoint is exposed** — `/api/routes` returns 503 by default; the route list lives in `/app/assets/build/routes.json` baked into the image (5MB JSON, 1669 namespaces, 3303 route definitions, 2316 with `radar` metadata).
2. **The `radar` field** maps site URLs to RSSHub paths (e.g. `github.com/DIYgod/RSSHub` → `/github/issue/:user/:repo`).
3. **FreshRSS's `quickadd` accepts any feed URL** including an RSSHub URL. Verified that `GET /api/greader.php/reader/api/0/subscription/quickadd?quickadd=http://100.91.202.122:8087/github/issue/DIYgod/RSSHub` works end-to-end.

So the agent's job is: **URL → look up matching route → build feed URL → subscribe**. That's the `ingest_url` tool.

## A.2 Tool design

### A.2.1 `ingest_url(url: str, *, prefer: list[str] | None = None) -> str`

Single tool. One user-facing call. Returns a JSON dict with the resolution chain so the agent can explain what it did.

**Inputs:**
- `url` — the site URL the user wants to follow (e.g. `https://www.youtube.com/@mkbhd`, `https://github.com/anthropics/anthropic-sdk-python`, `https://news.ycombinator.com/item?id=42424242`).
- `prefer` — optional list of route path substrings to prefer among matches (e.g. `["/issue", "/releases"]` to bias toward GitHub issues/releases over `/activity`). Default: `None` (use the first match).

**Behavior:**

1. If `url` already looks like a feed URL (matches `\.xml$`, `\.atom$`, `/feed/?$`, `application/(rss|atom)\+xml`), short-circuit: delegate straight to `subscribe_feed(url)`. No RSSHub involvement.
2. Otherwise, **look up the URL in the bundled routes catalog** (`/app/assets/build/routes.json` — see A.3 for how we get it into the container).
3. The matching strategy is `radar`-first, then `name`-fallback:
   - **Radar match:** iterate the catalog, parse the `radar.source` patterns (URL templates like `github.com/:user/:repo`), build a regex from each pattern, and find one that matches `url` and captures the params. Convert the captured params to the route's `target` template (e.g. `/issue/:user/:repo`).
   - **Name fallback:** if no radar hit, do a substring/keyword search on the route `name` field (e.g. user says "Hacker News best" → search for `"Hacker News"` + `"best"` in `name`/`description`).
4. **Pick the best candidate** (first radar hit, or highest-scoring name match). If `prefer` is set, weight candidates whose path contains any `prefer` substring.
5. **Build the feed URL** by filling the route's path with captured params and prepending the RSSHub base URL. Base URL comes from env var `RSSHUB_BASE_URL` (default `http://100.91.202.122:8087`).
6. **Subscribe** by calling `client.subscribe(feed_url=...)`. This uses the idempotency + return-type machinery from the main plan.
7. **Return** a dict with:
   ```python
   {
     "input_url": "<original>",
     "matched_route": {"namespace": "github", "path": "/github/issue/:user/:repo", "name": "Repository Issues", "categories": ["programming"]},
     "feed_url": "http://100.91.202.122:8087/github/issue/anthropics/anthropic-sdk-python",
     "subscription": <SubscriptionResult.to_dict()>,
     "warnings": []   // e.g. "route requires puppeteer — first fetch may take 30-60s"
   }
   ```

**Errors:**
- `NoRouteMatch` — no radar hit and no name hit. Tool returns `Error: no RSSHub route matches <url>. Try ingest_rsshub_path('/some/path') to use a known path directly.`
- `AmbiguousRoute` — multiple high-confidence matches. Tool returns `Error: multiple routes match <url>: [<paths>]. Use ingest_rsshub_path('/chosen/path') or pass prefer=[...] to disambiguate.`
- Subscription failure bubbles up as `Error: <client.subscribe exception>`.

### A.2.2 Companion tools (cheap, recommended)

- `ingest_rsshub_path(path: str, params: dict | None = None)` — bypass the URL→route lookup. Build the feed URL directly from a known path. Useful when `ingest_url` is ambiguous and the user picks a route.
- `list_routes(query: str, namespace: str | None = None, limit: int = 20)` — search the catalog by name/description/namespace. Useful when the user wants to discover what's available ("what Reddit feeds can I get?" → `list_routes(query="reddit")`).

These three together give the agent a complete "I want a feed for X" toolkit. The user said "ideally with one MCP command" — `ingest_url` is the primary; the companions are for the disambiguation cases.

## A.3 Routes catalog: bundling strategy

**Problem:** RSSHub doesn't expose its routes catalog via HTTP (the `/api/routes` endpoint is intentionally 503 by default in the image; turning it on would require a separate `rsshub-api` service). The catalog is baked into the image as `/app/assets/build/routes.json` (5MB).

**Decision: bundle the catalog into the MCP server image at build time.**

Add to the Dockerfile:

```dockerfile
# During build, copy the routes catalog from the rsshub image (or fetch from
# github.com/DIYgod/RSSHub/routes.json at build time). 5MB.
COPY routes.json /app/data/routes.json
```

Two ways to source it at build time:

| Option | How | Trade-off |
|---|---|---|
| **A. Docker multi-stage from RSSHub** | Add a `FROM diygod/rsshub:latest AS rsshub` stage, `COPY --from=rsshub /app/assets/build/routes.json /app/data/routes.json`. Re-build pulls whatever RSSHub ships. | Tied to RSSHub image version. Clean. ~5MB added to final image. |
| **B. Fetch at build from `raw.githubusercontent.com/DIYgod/RSSHub/main/assets/build/routes.json`** | `curl -L -o routes.json https://raw.githubusercontent.com/DIYgod/RSSHub/main/assets/build/routes.json` in Dockerfile. | Network dependency at build time. Always latest. Could break with upstream schema changes. |

**Recommendation: Option A** — pinned, reproducible, no network at build, always matches the running RSSHub container. Document in the Dockerfile that the routes.json version is whatever `diygod/rsshub:latest` ships at build time; user can override the FROM tag if they want a pinned version.

Alternative simpler path: **mount the routes.json as a volume from the rsshub container** at runtime. But that makes the MCP image depend on a sibling container's filesystem, which is fragile. Bundle is better.

## A.4 Configuration additions

Add to `config.py`:

```python
rsshub_base_url: str = Field(default="http://100.91.202.122:8087", alias="RSSHUB_BASE_URL")
rsshub_routes_path: str = Field(default="/app/data/routes.json", alias="RSSHUB_RSSHUB_ROUTES_PATH")
```

In the docker-compose environment block, override the default to point at the internal `rss-net` hostname:

```yaml
RSSHUB_BASE_URL: http://rsshub:1200
```

Wait — that's a problem. FreshRSS fetches the feed URL **from the URL string**, not from RSSHub directly. So the URL we hand to FreshRSS's `quickadd` must be **reachable by FreshRSS**. If we hand it `http://rsshub:1200/...` (internal), FreshRSS will fail to resolve `rsshub`. We need to hand it `http://100.91.202.122:8087/...` (the Tailscale-bind public URL). So `RSSHUB_BASE_URL` is what FreshRSS sees, which is the external URL.

This means the MCP server needs **two URLs**:
- `RSSHUB_INTERNAL_URL` — for the MCP server itself if it ever calls RSSHub directly (we don't need this; the MCP server never fetches RSSHub — it only writes a URL to FreshRSS).
- `RSSHUB_PUBLIC_URL` — the URL FreshRSS will use to fetch the feed. **This is the only one we need.** Default: `http://100.91.202.122:8087` (matches the compose publish).

So: **one env var, `RSSHUB_BASE_URL`**, default `http://100.91.202.122:8087`. FreshRSS reaches RSSHub via Tailscale just like the MCP server does. No internal magic. Clean.

## A.5 Test plan

### A.5.1 Unit tests in `tests/test_routes.py` (new file)

The route-catalog matching logic is a pure function: takes a URL and the routes.json, returns a list of candidate routes. Easy to test with a small fixture catalog (5–10 routes).

- `test_radar_match_simple` — URL `https://github.com/anthropics/anthropic-sdk-python` → matches `github.com/:user/:repo` radar pattern, captures user + repo, produces feed URL with `/issue/:user/:repo` target.
- `test_radar_match_with_path_template` — URL with trailing path (`github.com/foo/bar/issues`) → `radar.source` like `github.com/:user/:repo/issues` → captures correctly.
- `test_radar_no_match` — returns empty list, no exception.
- `test_name_match_fallback` — query `"hacker news best"` → finds the `/hackernews/best` route by name search.
- `test_prefer_filter` — multiple candidates, `prefer=["/issue"]` → bias toward that path.
- `test_is_feed_url_short_circuit` — `https://example.com/feed.xml` → returns `is_feed_url=True` without consulting catalog.
- `test_url_template_regex` — internal: `:user` segment becomes `[^/]+` in the compiled regex.

### A.5.2 Unit tests in `tests/test_client.py` and `tests/test_tools.py`

- `test_ingest_url_full_flow` — fake route catalog (1 entry), fake httpx responses for radar match + quickadd. Assert tool returns the expected dict.
- `test_ingest_url_no_match` — catalog has 0 entries that match → tool returns `Error: no RSSHub route matches ...`.
- `test_ingest_url_already_subscribed` — pre-populate `list_feeds` mock; assert tool returns `already_subscribed=True` and the route metadata.
- `test_ingest_rsshub_path` — bypass URL→route lookup, build URL from path + params, subscribe.
- `test_list_routes_search` — query `"github"`, `namespace=None` → returns up to `limit` matching routes.
- `test_ingest_url_routes_path_env_override` — env `RSSHUB_RSSHUB_ROUTES_PATH=/custom/path` → loads from there.

### A.5.3 Test count delta: +6 routes + +5 client/tools = +11 (suite goes from 78 → 89 with the main plan, or 67 → 78 if these land without the main plan).

### A.5.4 Live verification recipe

```bash
# Subscribe the MCP server, set RSSHUB_BASE_URL, restart.

# 1. Ingest a known GitHub URL
curl ... -d '{"jsonrpc":"2.0","id":N,"method":"tools/call",
              "params":{"name":"ingest_url",
                        "arguments":{"url":"https://github.com/anthropics/anthropic-sdk-python"}}}'
# Expected: matched_route.path = "/github/issue/:user/:repo",
#           feed_url = "http://100.91.202.122:8087/github/issue/anthropics/anthropic-sdk-python",
#           subscription already_subscribed=false

# 2. Verify in list_feeds — a new feed should appear pointing at the RSSHub URL.

# 3. Wait 30-60s (FreshRSS cron */30) and get_unread_articles to confirm RSSHub
#    is actually delivering content.

# 4. Ingest a YouTube channel (different namespace)
curl ... -d '{"jsonrpc":"2.0","id":N+1,"method":"tools/call",
              "params":{"name":"ingest_url",
                        "arguments":{"url":"https://www.youtube.com/@mkbhd"}}}'
# Expected: matched_route under "youtube" namespace.

# 5. Search what's available
curl ... -d '{"jsonrpc":"2.0","id":N+2,"method":"tools/call",
              "params":{"name":"list_routes",
                        "arguments":{"query":"hackernews","limit":5}}}'
# Expected: up to 5 hackernews route definitions with name/path/example.

# 6. Force a path
curl ... -d '{"jsonrpc":"2.0","id":N+3,"method":"tools/call",
              "params":{"name":"ingest_rsshub_path",
                        "arguments":{"path":"/github/release/:user/:repo",
                                     "params":{"user":"DIYgod","repo":"RSSHub"}}}}'
# Expected: subscribes to /github/release/DIYgod/RSSHub without URL matching.

# 7. Cleanup
curl ... -d '{"jsonrpc":"2.0","id":N+4,"method":"tools/call",
              "params":{"name":"unsubscribe_feed",
                        "arguments":{"url":"http://100.91.202.122:8087/github/issue/anthropics/anthropic-sdk-python"}}}'
```

## A.6 Implementation order (this addendum on top of main plan)

The main plan's steps 1–7 must complete first (because `ingest_url` reuses `client.subscribe` and `SubscriptionResult`). Then:

8. **Bake the routes catalog into the Dockerfile** (Option A: multi-stage `FROM diygod/rsshub:latest AS rsshub`, `COPY --from=rsshub /app/assets/build/routes.json /app/data/routes.json`).
9. **Add `RSSHUB_BASE_URL` and `RSSHUB_RSSHUB_ROUTES_PATH` to `config.py`.**
10. **Add `routes_matcher.py`** — pure-function module that takes a URL + the catalog, returns candidate routes. Implements radar match + name fallback. ~120 LOC.
11. **Add `routes_matcher` unit tests** (Section A.5.1).
12. **Add `ingest_url`, `ingest_rsshub_path`, and `list_routes` tool registrations to `tools.py`.** `ingest_url` and `ingest_rsshub_path` call `client.subscribe`. `list_routes` is a pure read of the catalog.
13. **Add tool tests** (Section A.5.2).
14. **Run full suite** — `uv run pytest -v`. Should report 89/89 pass.
15. **Live verification** (Section A.5.4).
16. **Bump version** to `0.4.0` (semver minor: 3 new tools).
17. **Update `README.md` tool table** — add the three new tools, document the `RSSHUB_BASE_URL` env var, mention the routes catalog bundling.
18. **Commit + push + rebuild sidecar image** in `/home/ubuntu/repo/rss/`.

## A.7 Risks & open questions

- **Routes catalog freshness:** the routes.json is baked in at build time. The user has to rebuild the image to get new RSSHub routes. Acceptable for a personal setup; document it in the README ("rebuild the sidecar to pick up new RSSHub routes").
- **Radar patterns can be ambiguous:** a URL like `https://github.com/DIYgod` could match `/:user` (profile activity) or `/:user/:repo` (issues of `DIYgod` repo — but there's no second segment, so this one doesn't actually match). Real ambiguity cases exist (`youtube.com/watch?v=...` could match `/watchlater`, `/subscriptions`, or generic `/youtube`). The `prefer` param handles the common case; otherwise surface as `AmbiguousRoute` and let the user pick.
- **Anti-crawler routes:** some routes (e.g. certain Twitter/X scrapers) require login cookies and will fail in our setup. Catalog entries with `features.antiCrawler: true` are unlikely to work. The tool should warn when matching against such routes (a `warnings` entry in the result).
- **Puppeteer routes:** `features.requirePuppeteer: true` routes work but take 30-60s on first hit. Add a `warnings` entry so the user knows to expect a delay.
- **No radar match for a URL the user can see in the catalog:** fall back to `ingest_rsshub_path` after `list_routes` reveals the right path. UX is two-step instead of one-step; document it.

## A.8 Out of scope (later, not this plan)

- All items from the main plan Section 7 (daily-ingest, add_category, etc.).
- Auto-refresh of the routes catalog (currently build-time-baked).
- A "subscribe by name" tool that fuzzy-matches `r.name` against the user's prompt ("Hacker News best" → `/hackernews/best`). Easy to add but the user said "ideally one command" so we don't preempt `ingest_url`.

## A.9 Estimated effort (addendum only)

- 30 min: Dockerfile multi-stage + routes.json bundling.
- 1.5–2 hours: `routes_matcher.py` + tests.
- 1 hour: tool registrations + tests.
- 30 min: live verification.
- 15 min: README/version/commit/push/rebuild.
- **Total: ~3.5–4 hours wall clock, fully self-contained, builds on the main plan.**

---

*This plan is the work artifact. Mark items done as you go. After all steps are complete, the daily-cron task can proceed.*

