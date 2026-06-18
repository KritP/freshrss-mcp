# Plan: `subscribe` and `unsubscribe` tools for freshrss-mcp

**Status:** draft
**Target repo:** `/home/ubuntu/repo/rss/freshrss-mcp/` (KritP/freshrss-mcp fork)
**Upstream:** `ChrisLAS/freshrss-mcp` v0.2.0
**Working version:** 0.3.0 (semver-minor: new user-facing tools, no breaking changes)
**Daily-ingest:** explicitly out of scope — separate plan later.

---

## 1. Goals

Add two MCP tools to the existing 10-tool surface, behind a clean GReader-API client:

| Tool | Inputs | Behavior |
|------|--------|----------|
| `subscribe_feed` | `url: str`, `title: str \| None = None`, `category: str \| None = None` | Add a feed by URL. Idempotent (returns existing feed if already subscribed). Optional title override and category placement. |
| `unsubscribe_feed` | `feed_id: int \| None = None`, `url: str \| None = None` | Remove a feed by numeric id or URL. At least one of the two is required. Errors clearly if neither matches an existing subscription. |

After this lands, the 12-tool surface is **read** (10) + **write-back (2)**. No `add_category`, `rename_feed`, `move_feed` — those are nice-to-haves but not in the user's stated goals; `subscription/edit` already supports them if we want them later.

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

*This plan is the work artifact. Mark items done as you go. After all steps are complete, the daily-cron task can proceed.*
