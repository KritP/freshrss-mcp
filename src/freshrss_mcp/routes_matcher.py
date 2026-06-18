"""Match URLs against the RSSHub route catalog (radar field) and do
name/description search. Pure functions over an in-memory catalog loaded
once per process.

Catalog format (verified against diygod/rsshub:latest /assets/build/routes.json):

    {
      "<namespace>": {
        "name": "...",
        "routes": {
          "<in-namespace-path-template>": {
            "path": "<same>",
            "name": "...",
            "example": "...",
            "parameters": {...},
            "categories": [...],
            "features": {"requirePuppeteer": bool, "antiCrawler": bool, ...},
            "radar": [{"source": ["url-template", ...], "target": "..."}],
            ...
          },
          ...
        },
        "apiRoutes": {...}    # not used here
      },
      ...
    }

Path-template syntax (the part that bites):
    "github.com"             literal
    ":user"                  required param, captures [^/]+
    ":user?"                 optional param, may be absent
    "{.pattern}"             required, captures using pattern (no name)
    ":user{.pattern}"        required param with custom regex
    ":user{.pattern}?"       optional param with custom regex

The template-to-regex conversion is conservative: only the patterns we
actually see in the live catalog are supported. Exotic constructs (nested
braces, multi-pattern segments) fall through to literal matching rather
than crashing.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Matches a single path-template segment. Groups: (1) param name, (2) constraint, (3) optional marker.
_SEGMENT_RE = re.compile(r"^(:?[\w]+)?(?:\{([^}]*)\})?(\?)?$")


@dataclass
class RouteCandidate:
    """A single matched route from the catalog. Built into a feed URL via
    build_url() with the captured params filled in.
    """

    namespace: str
    path: str                       # In-namespace path, e.g. "/issue/:user/:repo"
    name: str
    example: str | None
    categories: list[str]
    features: dict[str, Any]
    captured_params: dict[str, str] = field(default_factory=dict)

    def build_url(self, base_url: str) -> str:
        """Build the full RSSHub URL for this route, filling in captured params.

        Strips {constraint} markers from the path before concatenating.
        """
        url_path = self.path
        for key, value in self.captured_params.items():
            url_path = url_path.replace(f":{key}", value)
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


_CATALOG_CACHE: dict[str, Any] = {}


def load_catalog(path: str) -> dict:
    """Load the routes catalog from disk. Cached for process lifetime.

    The MCP server's config path is read once at startup and never changes;
    the cache is a simple module-global. If the user changes the path via
    env var across container restarts, the process restarts and the cache
    is naturally re-populated.
    """
    global _CATALOG_CACHE
    if not _CATALOG_CACHE:
        with open(path) as f:
            _CATALOG_CACHE = json.load(f)
    return _CATALOG_CACHE


def is_feed_url(url: str) -> bool:
    """Heuristic: does this URL already look like a feed?"""
    return bool(
        re.search(
            r"\.(xml|atom|rss)(\?.*)?$|/(feed|rss)/?(/|$|\?)",
            url,
            re.IGNORECASE,
        )
    )


def _template_to_regex(template: str) -> tuple[re.Pattern[str], list[str]]:
    """Convert a URL-template string into an anchored regex + param names.

    Example:
        "github.com/:user/:repo"  ->  ^github\\.com/([^/]+)/([^/]+)$
        params: ['user', 'repo']

        "github.com/:user"        ->  ^github\\.com/([^/]+)$
        params: ['user']

        "example.com/:slug?"      ->  ^example\\.com/(?:/([^/]+))?$
        params: ['slug']

    The pattern is anchored (^...$) so a full string match is required.
    """
    parts: list[str] = []
    param_names: list[str] = []
    for segment in template.split("/"):
        if not segment:
            continue
        m = _SEGMENT_RE.match(segment)
        if not m:
            # Unknown shape — fall back to literal. Don't crash.
            parts.append("/" + re.escape(segment))
            continue
        name, constraint, optional = m.group(1), m.group(2), m.group(3)
        # Default regex: anything up to next slash. Constraint overrides.
        regex = constraint or "[^/]+"
        if optional and name:
            # Optional named param: slash is required when the param is
            # present, but the whole thing can be absent.
            # pattern: (?:/(<regex>))?   group captures the param value only
            parts.append(f"(?:/({regex}))?")
            param_names.append(name.lstrip(":"))
        elif optional:
            # Optional unnamed constraint segment (e.g. {:foo}?).
            parts.append(f"(?:/({regex}))?")
        else:
            parts.append(f"/({regex})")
            if name:
                param_names.append(name.lstrip(":"))
    pattern_str = "^" + "".join(parts).lstrip("/") + "$"
    return re.compile(pattern_str), param_names


def find_routes_by_url(url: str, catalog: dict) -> list[RouteCandidate]:
    """Find all RSSHub routes whose radar patterns match the URL.

    Normalizes the URL by stripping protocol, query, fragment, and trailing
    slash. The normalized form is matched against each radar source pattern.
    """
    normalized = re.sub(r"^https?://", "", url)
    normalized = re.sub(r"[?#].*$", "", normalized).rstrip("/")

    candidates: list[RouteCandidate] = []
    for namespace, ns_data in catalog.items():
        for path_key, route in ns_data.get("routes", {}).items():
            for radar in route.get("radar", []):
                for source in radar.get("source", []):
                    try:
                        pattern, param_names = _template_to_regex(source)
                    except Exception as e:  # noqa: BLE001
                        logger.debug(
                            "Skipping unparseable radar pattern %r: %s",
                            source, e,
                        )
                        continue
                    m = pattern.match(normalized)
                    if not m:
                        continue
                    captured: dict[str, str] = {}
                    for i, name in enumerate(param_names):
                        value = m.group(i + 1)
                        if value is not None:
                            captured[name] = value
                    target = radar.get("target", path_key)
                    candidates.append(
                        RouteCandidate(
                            namespace=namespace,
                            path=target,
                            name=route.get("name", ""),
                            example=route.get("example"),
                            categories=route.get("categories", []),
                            features=route.get("features", {}),
                            captured_params=captured,
                        )
                    )
    return candidates


def find_routes_by_name(
    query: str,
    catalog: dict,
    *,
    namespace: str | None = None,
    limit: int = 20,
) -> list[RouteCandidate]:
    """Substring search on name + description. Case-insensitive."""
    query_lower = query.lower()
    results: list[RouteCandidate] = []
    for ns, ns_data in catalog.items():
        if namespace and ns != namespace:
            continue
        for path_key, route in ns_data.get("routes", {}).items():
            name = (route.get("name") or "").lower()
            desc = (route.get("description") or "").lower()
            if query_lower in name or query_lower in desc:
                results.append(
                    RouteCandidate(
                        namespace=ns,
                        path=path_key,
                        name=route.get("name", ""),
                        example=route.get("example"),
                        categories=route.get("categories", []),
                        features=route.get("features", {}),
                    )
                )
                if len(results) >= limit:
                    return results
    return results
