"""
Unofficial BO3.gg REST API wrapper
Python 3.9 compatible. Multi-game build for CS2, Valorant, R6S, Dota2, LoL, and MLBB.

Vercel/GitHub layout:
  main.py
  app.py          -> from main import app
  requirements.txt
  vercel.json

Local run:
  python3 -m pip install -r requirements.txt
  python3 main.py

Examples:
  curl "http://127.0.0.1:3002/v2/match?game=cs2&q=finished"
  curl "http://127.0.0.1:3002/v2/match?game=valorant&q=current"
  curl "http://127.0.0.1:3002/v2/match/all?q=finished"
  curl "http://127.0.0.1:3002/v2/match/details?game=cs2&team1=Team%20Nemesis&team2=FOKUS"
  curl "http://127.0.0.1:3002/v2/match/details?game=cs2&search=Nemesis%20FOKUS"
  curl "http://127.0.0.1:3002/v2/match/details?game=cs2&q=finished&search=Nemesis%20FOKUS"
  curl "http://127.0.0.1:3002/v2/match/details?url=https://bo3.gg/matches/gentle-mates-cs-vs-team-nemesis-cs-31-05-2026"
"""

import asyncio
import html
import json
import os
import re
import time
from datetime import datetime, timedelta
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode, urljoin, urlparse

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

BASE_URL = "https://bo3.gg"
API_PORT = int(os.getenv("BO3API_PORT", "3002"))
DEFAULT_TIMEOUT = float(os.getenv("BO3API_TIMEOUT", "20"))
CACHE_TTL_SECONDS = int(os.getenv("BO3API_CACHE_TTL", "30"))
DEBUG_FETCH_CHARS = int(os.getenv("BO3API_DEBUG_FETCH_CHARS", "1500"))

# Cache policy:
#   BO3API_CACHE_TTL > 0  -> in-process memory cache for that many seconds.
#   BO3API_CACHE_TTL <= 0 -> fully bypass the app cache on every lookup.
# This is important for live verifier usage where /v2/match/details must always
# reflect the latest BO3.gg response instead of a stale Vercel warm-instance cache.
def cache_enabled(ttl: Optional[int] = None) -> bool:
    try:
        effective_ttl = CACHE_TTL_SECONDS if ttl is None else int(ttl)
    except Exception:
        effective_ttl = 0
    return effective_ttl > 0

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "CDN-Cache-Control": "no-store",
    "Vercel-CDN-Cache-Control": "no-store",
}

# When cache is disabled, prefer BO3's browser JSON API for match lists.
# The public HTML/SSR match-list cards can lag or contain prediction tails like
# "Live T1 0 - 0 Gen.G Esports 2 2 - 3" while BO3's browser-rendered page
# has already moved to the real live score.  Use the JSON API first only in
# no-cache mode so normal cached deployments keep the cheaper HTML path.
BO3API_PREFER_JSON_WHEN_NO_CACHE = os.getenv(
    "BO3API_PREFER_JSON_WHEN_NO_CACHE", "true"
).strip().lower() not in ("0", "false", "no", "off", "")

# BO3.gg separates games by URL namespace. Root /matches is CS2 only.
GAME_PREFIXES = {
    "cs2": "",
    "cs": "",
    "counterstrike": "",
    "counter-strike": "",
    "valorant": "/valorant",
    "val": "/valorant",
    "r6s": "/r6siege",
    "r6": "/r6siege",
    "rainbow6": "/r6siege",
    "rainbow-six": "/r6siege",
    "rainbowsix": "/r6siege",
    "r6siege": "/r6siege",
    "dota2": "/dota2",
    "dota": "/dota2",
    "lol": "/lol",
    "league": "/lol",
    "leagueoflegends": "/lol",
    "league-of-legends": "/lol",
    "mlbb": "/mlbb",
    "mobilelegends": "/mlbb",
    "mobile-legends": "/mlbb",
}

CANONICAL_GAMES = {
    "cs2": {"slug": "cs2", "name": "CS2", "prefix": ""},
    "valorant": {"slug": "valorant", "name": "Valorant", "prefix": "/valorant"},
    "r6s": {"slug": "r6s", "name": "Rainbow Six Siege", "prefix": "/r6siege"},
    "dota2": {"slug": "dota2", "name": "Dota 2", "prefix": "/dota2"},
    "lol": {"slug": "lol", "name": "League of Legends", "prefix": "/lol"},
    "mlbb": {"slug": "mlbb", "name": "Mobile Legends: Bang Bang", "prefix": "/mlbb"},
}

PREFIX_TO_GAME = {"": "cs2", "/valorant": "valorant", "/r6siege": "r6s", "/dota2": "dota2", "/lol": "lol", "/mlbb": "mlbb"}

# BO3's rendered match pages are sometimes missing the actual match cards,
# while the browser still loads them from BO3's JSON API.  The exact numeric
# discipline IDs have moved before, so the fallback tries the likely primary ID
# first and then a small safe range, filtering the returned URLs back to the
# requested game namespace.
DISCIPLINE_ID_CANDIDATES = {
    # CS2 is BO3's root namespace and uses discipline_id=1 in public scrape
    # examples. The other game IDs have changed before, so keep a bounded
    # fallback range for non-CS2 games instead of assuming one fixed value.
    "cs2": [1, 2, 3, 4, 5, 6, 7, 8],
    "valorant": [2, 3, 4, 5, 6, 7, 8, 9, 10, 1],
    "r6s": [3, 4, 5, 6, 7, 8, 9, 10, 1, 2],
    "dota2": [4, 3, 5, 6, 7, 8, 9, 10, 1, 2],
    "lol": [5, 4, 6, 3, 7, 8, 9, 10, 11, 12, 1, 2],
    "mlbb": [6, 5, 7, 4, 8, 9, 10, 11, 12, 3, 1, 2],
}

GAME_API_SLUGS = {
    "cs2": ["cs2", "csgo", "counter-strike", "counterstrike"],
    "valorant": ["valorant"],
    "r6s": ["r6siege", "r6s", "rainbow-six", "rainbowsix"],
    "dota2": ["dota2", "dota"],
    "lol": ["lol", "league-of-legends", "leagueoflegends", "league_of_legends"],
    "mlbb": ["mlbb", "mobile-legends", "mobilelegends"],
}

# Extra raw-text markers used to avoid accepting the wrong discipline when BO3
# changes numeric IDs.  CS2 is root and does not need markers.
GAME_API_RAW_MARKERS = {
    "valorant": ["valorant", "vct", "challengers"],
    "r6s": ["r6siege", "rainbow", "siege"],
    "dota2": ["dota2", "dota"],
    "lol": ["/lol/", "-lol", "league of legends", "league-of-legends", "lcs", "lec", "lck", "lpl", "cblol", "lol"],
    "mlbb": ["mlbb", "mobile legends", "mobile-legends"],
}

GAME_PREFIX_PATTERN = r"(?:valorant|r6siege|dota2|lol|mlbb)"

# Do not request br/zstd. Vercel's Python runtime + httpx can behave differently
# depending on optional decoder packages. gzip/deflate are safe everywhere.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

# BO3's browser app reads match rows from the API host.  The plain
# bo3.gg HTML can be an SEO/crawler page that has article text but not the
# hydrated match table, especially for /lol/matches/finished.
API_V1_BASE_URL = "https://api.bo3.gg/api/v1"
API_V1_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Origin": "https://bo3.gg",
    "Referer": "https://bo3.gg/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

# BO3.gg/Nuxt can return an empty client-side shell to normal server-side
# HTTP clients. Search crawlers often receive prerendered HTML. Try those UAs
# before giving up, otherwise Vercel gets 0 visible chars / 0 anchors.
HEADER_PROFILES = [
    ("desktop", HEADERS),
    (
        "googlebot",
        dict(
            HEADERS,
            **{
                "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
                "X-Forwarded-For": "66.249.66.1",
            },
        ),
    ),
    (
        "bingbot",
        dict(
            HEADERS,
            **{
                "User-Agent": "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)",
                "X-Forwarded-For": "40.77.167.1",
            },
        ),
    ),
    (
        "facebook",
        dict(
            HEADERS,
            **{
                "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
            },
        ),
    ),
]

MAP_NAMES = (
    # CS2
    "Dust II",
    "Dust 2",
    "Mirage",
    "Inferno",
    "Nuke",
    "Train",
    "Ancient",
    "Anubis",
    "Vertigo",
    "Overpass",
    "Cache",
    "Cobblestone",
    # Valorant
    "Abyss",
    "Ascent",
    "Bind",
    "Breeze",
    "Corrode",
    "Fracture",
    "Haven",
    "Icebox",
    "Lotus",
    "Pearl",
    "Split",
    "Sunset",
)

NOISE_LINES = {
    "0 comments",
    "comments",
    "full stats",
    "overview",
    "performance",
    "aim",
    "grenades",
    "devices",
    "economy",
    "full match winner",
    "scoreboard",
    "k",
    "d",
    "a",
    "+/-",
    "adr",
    "od",
    "mk",
    "maps score",
    "score",
    "form",
    "time",
    "match",
    "prediction",
    "tournament",
    "data",
    "pred.",
    "t",
}

STATUS_WORDS = {"live", "ended", "scheduled", "postponed", "cancelled"}


class AnchorExtractor(HTMLParser):
    """Dependency-free anchor extractor."""

    def __init__(self) -> None:
        HTMLParser.__init__(self, convert_charrefs=True)
        self._stack: List[Dict[str, Any]] = []
        self.anchors: List[Dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = dict(attrs)
        self._stack.append({"href": attrs_dict.get("href") or "", "text": []})

    def handle_data(self, data: str) -> None:
        if not self._stack:
            return
        for item in self._stack:
            item["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._stack:
            return
        item = self._stack.pop()
        href = (item.get("href") or "").strip()
        text = collapse_ws(" ".join(item.get("text") or []))
        if href:
            self.anchors.append({"href": href, "text": text})


class TextExtractor(HTMLParser):
    """Dependency-free visible text extractor."""

    def __init__(self) -> None:
        HTMLParser.__init__(self, convert_charrefs=True)
        self._skip_depth = 0
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        elif tag in {
            "br",
            "p",
            "div",
            "section",
            "article",
            "li",
            "tr",
            "td",
            "th",
            "h1",
            "h2",
            "h3",
            "h4",
            "header",
            "footer",
            "main",
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        data = clean_text(data)
        if data:
            self.parts.append(data)

    def text(self) -> str:
        lines = [collapse_ws(x) for x in "\n".join(self.parts).splitlines()]
        lines = [x for x in lines if x]
        return "\n".join(lines)


@dataclass
class CacheEntry:
    ts: float
    value: str


_cache: Dict[str, CacheEntry] = {}
_client: Optional[httpx.AsyncClient] = None


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("\xa0", " ")
    value = value.replace("\u00a0", " ")
    value = value.replace("–", "-")
    value = value.replace("—", "-")
    value = value.replace("−", "-")
    return value.strip()


def collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", clean_text(value)).strip()


def norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", collapse_ws(value).casefold())


def strip_tags(fragment: str) -> str:
    fragment = re.sub(r"<script\b[^>]*>.*?</script>", " ", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<style\b[^>]*>.*?</style>", " ", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<svg\b[^>]*>.*?</svg>", " ", fragment, flags=re.I | re.S)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return collapse_ws(fragment)


def normalize_game(game: str) -> str:
    key = re.sub(r"[^a-z0-9-]+", "", (game or "cs2").strip().lower())
    if not key:
        key = "cs2"
    if key not in GAME_PREFIXES:
        raise ValueError("unsupported game '%s'; use cs2, valorant, r6s, dota2, lol, or mlbb" % game)
    prefix = GAME_PREFIXES[key]
    return PREFIX_TO_GAME.get(prefix, "cs2")


def game_prefix(game: str) -> str:
    canonical = normalize_game(game)
    return CANONICAL_GAMES[canonical]["prefix"]


def game_from_path(path: str) -> str:
    path = path or ""
    m = re.match(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?(" + GAME_PREFIX_PATTERN + r")(?=/|$)", path, flags=re.I)
    if m:
        return PREFIX_TO_GAME.get("/" + m.group(1).lower(), "cs2")
    return "cs2"


def match_list_path(game: str, q_norm: str) -> Tuple[str, str, int]:
    prefix = game_prefix(game)
    # Use the global BO3API_CACHE_TTL for all list pages.  With
    # BO3API_CACHE_TTL=0 this endpoint now fetches current/finished lists fresh
    # every time instead of keeping the old hardcoded 20s/60s caches.
    ttl = CACHE_TTL_SECONDS
    if q_norm in {"current", "live", "schedule", "upcoming"}:
        return prefix + "/matches/current", "current", ttl
    if q_norm in {"finished", "results"}:
        return prefix + "/matches/finished", "finished", ttl
    raise ValueError("q must be one of current/live/schedule/upcoming/finished/results")


def normalize_url(url_or_path: str) -> str:
    if not url_or_path:
        raise ValueError("empty URL/path")
    full_url = urljoin(BASE_URL, url_or_path)
    parsed = urlparse(full_url)
    if parsed.netloc and parsed.netloc not in {"bo3.gg", "www.bo3.gg"}:
        raise ValueError("only bo3.gg URLs are allowed")
    return full_url


def abs_bo3_url(href: str) -> str:
    href = (href or "").strip()
    if not href:
        return href
    if href.startswith("/"):
        return urljoin(BASE_URL, href)
    return href


def scope_root_match_url_for_game(url_or_path: str, game: str) -> str:
    """Attach the game namespace when BO3 gives a root /matches URL.

    CS2 lives at /matches/... with no /cs2 prefix. Other games live under
    /lol/matches, /valorant/matches, etc. Some BO3 JSON/client payloads are
    game-scoped but still return root-looking /matches/<slug> paths; without
    this correction LoL/Valorant/R6/Dota/MLBB candidates get misclassified as
    CS2 and dropped.
    """
    value = (url_or_path or "").strip()
    if not value:
        return value
    full = abs_bo3_url(value)
    parsed = urlparse(full)
    path = canonical_match_path(parsed.path)
    prefix = game_prefix(game)
    if prefix and re.fullmatch(r"/matches/[^/]+", path, flags=re.I):
        return urljoin(BASE_URL, prefix + path)
    return full


def normalize_segment_url_for_game(item: Dict[str, Any], game: str) -> Dict[str, Any]:
    if not item:
        return item
    out = dict(item)
    url = out.get("url", "") or ""
    if url:
        out["url"] = scope_root_match_url_for_game(url, game)
    return out


def canonical_match_path(path: str) -> str:
    path = path or ""
    # BO3 can prepend language prefixes, e.g. /en/valorant/matches/...
    m = re.match(r"^/[a-z]{2}(?:-[a-z]{2})?(/(?:(?:" + GAME_PREFIX_PATTERN + r")/)?matches/.*)$", path, flags=re.I)
    if m:
        return m.group(1)
    return path


def is_match_collection_path(path: str) -> bool:
    path = canonical_match_path(path).rstrip("/").lower()
    return bool(re.fullmatch(r"/(?:" + GAME_PREFIX_PATTERN + r"/)?matches(?:/(?:current|finished))?", path))


def is_match_detail_path(path: str) -> bool:
    path = canonical_match_path(path).rstrip("/")
    if is_match_collection_path(path):
        return False
    return bool(re.match(r"^/(?:" + GAME_PREFIX_PATTERN + r"/)?matches/[^/]+$", path, flags=re.I))


def extract_anchors_htmlparser(raw_html: str) -> List[Dict[str, str]]:
    parser = AnchorExtractor()
    parser.feed(raw_html)
    out: List[Dict[str, str]] = []
    for item in parser.anchors:
        href = abs_bo3_url(item.get("href", ""))
        text = collapse_ws(item.get("text", ""))
        if href:
            out.append({"href": href, "text": text})
    return out


def extract_anchors_regex(raw_html: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    # This fallback is intentionally simple and robust for SSR anchor blocks.
    for m in re.finditer(r"<a\b(?P<attrs>[^>]*)>(?P<body>.*?)</a>", raw_html, flags=re.I | re.S):
        attrs = m.group("attrs") or ""
        href_m = re.search(r"\bhref\s*=\s*(['\"])(.*?)\1", attrs, flags=re.I | re.S)
        if not href_m:
            href_m = re.search(r"\bhref\s*=\s*([^\s>]+)", attrs, flags=re.I | re.S)
        if not href_m:
            continue
        href = href_m.group(2 if href_m.lastindex and href_m.lastindex >= 2 else 1)
        href = abs_bo3_url(html.unescape(href))
        text = strip_tags(m.group("body") or "")
        if href:
            out.append({"href": href, "text": text})
    return out


def extract_json_ld_anchors(raw_html: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for m in re.finditer(
        r"<script\b[^>]*type\s*=\s*(['\"])application/ld\+json\1[^>]*>(.*?)</script>",
        raw_html,
        flags=re.I | re.S,
    ):
        blob = html.unescape(m.group(2) or "").strip()
        try:
            data = json.loads(blob)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                url = item.get("url") or item.get("@id") or ""
                name = item.get("name") or item.get("headline") or ""
                if isinstance(url, str) and "/matches/" in url:
                    out.append({"href": abs_bo3_url(url), "text": collapse_ws(str(name))})
                for value in item.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(item, list):
                stack.extend(item)
    return out


def extract_anchors(raw_html: str) -> List[Dict[str, str]]:
    all_items = []
    all_items.extend(extract_anchors_htmlparser(raw_html))
    all_items.extend(extract_anchors_regex(raw_html))
    all_items.extend(extract_json_ld_anchors(raw_html))

    deduped: List[Dict[str, str]] = []
    seen = set()
    for item in all_items:
        href = abs_bo3_url(item.get("href", ""))
        text = collapse_ws(item.get("text", ""))
        key = (href, text)
        if href and key not in seen:
            deduped.append({"href": href, "text": text})
            seen.add(key)
    return deduped


def extract_visible_text(raw_html: str) -> str:
    parser = TextExtractor()
    parser.feed(raw_html)
    text = parser.text()
    if text:
        return text
    return strip_tags(raw_html).replace(" | ", "\n")


def visible_lines(visible: str) -> List[str]:
    lines = []
    for raw in visible.splitlines():
        line = collapse_ws(raw)
        if line:
            lines.append(line)
    return lines


def first_meta_content(raw_html: str, names: Iterable[str]) -> str:
    for name in names:
        # <meta property="og:title" content="...">
        pat1 = (
            r"<meta\b(?=[^>]*(?:property|name)\s*=\s*(['\"])"
            + re.escape(name)
            + r"\1)(?=[^>]*content\s*=\s*(['\"])(.*?)\2)[^>]*>"
        )
        m = re.search(pat1, raw_html, flags=re.I | re.S)
        if m:
            return collapse_ws(m.group(3))
    return ""


def parse_html_title(raw_html: str) -> str:
    h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", raw_html, flags=re.I | re.S)
    if h1:
        title = strip_tags(h1.group(1))
        if title:
            return title

    meta_title = first_meta_content(raw_html, ["og:title", "twitter:title"])
    if meta_title:
        return meta_title

    title = re.search(r"<title\b[^>]*>(.*?)</title>", raw_html, flags=re.I | re.S)
    if title:
        return strip_tags(title.group(1))

    return ""


def cleanup_page_title(title: str) -> str:
    title = collapse_ws(title)
    title = re.sub(r"\s+-\s+(?:CS2|Valorant|R6SIEGE|R6|Dota2?|LoL|MLBB)\s+Match.*$", "", title, flags=re.I)
    title = re.sub(r"\s+\|\s+BO3\.gg.*$", "", title, flags=re.I)
    title = re.sub(r"\s+\|\s+bo3\.gg.*$", "", title, flags=re.I)
    return collapse_ws(title)


def slug_to_name(value: str) -> str:
    value = re.sub(r"[-_]+", " ", value or "").strip()
    words = []
    for word in value.split():
        lower = word.lower()
        if lower in {"cs", "cs2", "gg"}:
            words.append(lower.upper())
        elif lower in {"g2", "t1", "m80", "og", "big"}:
            words.append(lower.upper())
        elif lower in {"esports", "gaming"}:
            words.append(word.capitalize())
        else:
            words.append(word.capitalize())
    return collapse_ws(" ".join(words))


def teams_from_match_slug(path_or_url: str) -> Tuple[str, str]:
    parsed = urlparse(path_or_url)
    path = parsed.path if parsed.scheme or parsed.netloc else path_or_url
    path = canonical_match_path(path)
    slug = path.rstrip("/").split("/")[-1]
    slug = re.sub(r"-\d{1,2}-\d{1,2}-\d{4}$", "", slug)
    if "-vs-" not in slug:
        return "", ""
    left, right = slug.split("-vs-", 1)
    return slug_to_name(left), slug_to_name(right)


def parse_title(raw_html: str, visible: str, source_url: str) -> str:
    title = cleanup_page_title(parse_html_title(raw_html))
    if title and " vs " in title:
        return title

    for line in visible_lines(visible):
        if " vs " in line and (" at " in line or line.lower().startswith("# ")):
            return cleanup_page_title(line.lstrip("# "))

    t1, t2 = teams_from_match_slug(source_url)
    if t1 and t2:
        return "%s vs %s" % (t1, t2)
    return title


def parse_teams_from_title(title: str) -> Tuple[str, str, str]:
    title = cleanup_page_title(title)
    m = re.search(r"(.+?)\s+vs\s+(.+?)(?:\s+at\s+(.+))?$", title, flags=re.I)
    if not m:
        return "", "", ""
    team1 = collapse_ws(m.group(1))
    team2 = collapse_ws(m.group(2))
    tournament = collapse_ws(m.group(3) or "")
    return team1, team2, tournament


def clean_team_text(value: str) -> str:
    value = collapse_ws(value)
    value = re.sub(r"^(Live|Ended)\s+", "", value, flags=re.I)
    value = re.sub(r"^[A-Z][a-z]{2}\s+\d{1,2},\s*\d{1,2}:\d{2}\s+", "", value)
    value = re.sub(r"^\d{1,2}:\d{2}\s+", "", value)
    value = re.sub(r"^Full\s+", "", value, flags=re.I)
    value = re.sub(r"^(Live|Ended)\s+", "", value, flags=re.I)
    value = re.sub(r"\bBo[1357]\b", "", value, flags=re.I)
    value = re.sub(r"\s+\d+\s+\d+\s*-\s*\d+\s*$", "", value)  # prediction tail
    value = re.sub(r"\s+\d+\s*$", "", value)  # prediction/team-pick tail
    return collapse_ws(value)


def parse_score(value: str) -> Optional[Tuple[int, int]]:
    m = re.search(r"\b(\d+)\s*-\s*(\d+)\b", clean_text(value))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def infer_status_from_text(value: str, hint: str) -> str:
    lower = value.lower()
    if re.search(r"\blive\b", lower):
        return "live"
    if re.search(r"\b(ended|full)\b", lower) or hint == "finished":
        return "finished"
    if hint in {"current", "live"}:
        return "live" if re.search(r"\blive\b", lower) else "upcoming"
    return hint or "unknown"


def _display_status_label(status: str) -> str:
    status = (status or "").lower().strip()
    if status == "live":
        return "Live"
    if status == "finished":
        return "Finished"
    if status in {"upcoming", "scheduled"}:
        return "Upcoming"
    return status.title() if status else ""


def _format_clean_match_row_text(status: str, team1: str, team2: str, score1: Any = None, score2: Any = None) -> str:
    """Build a verifier-safe row summary without BO3 prediction/odds tails.

    BO3 cards/pages can place prediction snippets such as "2 2 - 3" near the
    match card text.  Those numbers are odds/prediction content, not the live
    series score.  Keep raw debug text separately, but expose raw_text as a clean
    structured summary so callers do not accidentally parse prediction tails.
    """
    team1 = collapse_ws(str(team1 or ""))
    team2 = collapse_ws(str(team2 or ""))
    label = _display_status_label(status)
    parts: List[str] = []
    if label:
        parts.append(label)
    if team1:
        parts.append(team1)
    if score1 is not None and score2 is not None:
        parts.append("%s - %s" % (score1, score2))
    elif team1 and team2:
        parts.append("vs")
    if team2:
        parts.append(team2)
    return collapse_ws(" ".join(parts))


def _sanitize_source_row_for_payload(source_row: Optional[Dict[str, Any]], match: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a copy of source_row with stale/prediction raw_text normalized.

    The list row is only a candidate pointer for /details.  Once the match detail
    parser has fresher structured score/status, mirror that structure into
    source_row too.  The original BO3 card text is preserved under
    raw_text_original for debugging.
    """
    if source_row is None:
        return None
    row = dict(source_row)
    original_raw = collapse_ws(str(row.get("raw_text_original") or row.get("raw_text") or ""))

    status = (match.get("status") or row.get("status") or "").lower().strip()
    team1 = match.get("team1") or row.get("team1") or ""
    team2 = match.get("team2") or row.get("team2") or ""
    score1 = match.get("score1") if match.get("score1") is not None else row.get("score1")
    score2 = match.get("score2") if match.get("score2") is not None else row.get("score2")
    winner = match.get("winner") or row.get("winner") or ""

    # Keep source_row consistent with the verifier-safe match summary when the
    # detail/API path found a real score.  This avoids stale list-card values
    # like live 0-0 lingering beside match.score1/match.score2 = 0-1.
    if match.get("score1") is not None and match.get("score2") is not None:
        row["status"] = status or row.get("status", "")
        row["team1"] = team1
        row["team2"] = team2
        row["score1"] = match.get("score1")
        row["score2"] = match.get("score2")
        row["winner"] = winner
        row["raw_text"] = _format_clean_match_row_text(status, team1, team2, match.get("score1"), match.get("score2"))
        row["raw_text_source"] = "rebuilt_from_match_detail"
    else:
        cleaned = _format_clean_match_row_text(status, team1, team2, score1, score2)
        if cleaned:
            row["raw_text"] = cleaned
            row["raw_text_source"] = "rebuilt_from_candidate_row"

    if original_raw and original_raw != row.get("raw_text"):
        row["raw_text_original"] = original_raw
        row["raw_text_note"] = "original BO3 card text may include prediction/odds tail; raw_text is normalized"
    return row


def parse_match_anchor(text: str, href: str, status_hint: str) -> Optional[Dict[str, Any]]:
    raw = collapse_ws(text)
    href = abs_bo3_url(href)
    href_path = canonical_match_path(urlparse(href).path)
    if not is_match_detail_path(href_path):
        return None

    status = infer_status_from_text(raw, status_hint)
    slug_team1, slug_team2 = teams_from_match_slug(href_path)

    # Some BO3 anchor text can be empty if icons/images wrap the card. In that
    # case still return a useful shell from the slug, but list parsing below will
    # usually find text via regex anchors.
    if not raw and not (slug_team1 and slug_team2):
        return None

    started_at = ""
    m_time = re.search(r"\b((?:[A-Z][a-z]{2}\s+\d{1,2},\s*)?\d{1,2}:\d{2})\b", raw)
    if m_time:
        started_at = m_time.group(1)

    bo = ""
    m_bo = re.search(r"\bBo([1357])\b", raw, flags=re.I)
    if m_bo:
        bo = "bo" + m_bo.group(1)

    score1: Optional[int] = None
    score2: Optional[int] = None
    team1 = slug_team1
    team2 = slug_team2

    score_match = re.search(r"\b(\d+)\s*-\s*(\d+)\b", raw)
    if score_match:
        score1 = int(score_match.group(1))
        score2 = int(score_match.group(2))
        left = clean_team_text(raw[: score_match.start()])
        right = clean_team_text(raw[score_match.end() :])

        # Prefer names parsed from the visible row when they are usable, because
        # they preserve BO3 capitalization like UNiTY/FOKUS/ASTRAL.
        if left and right and not any(x.lower() in NOISE_LINES for x in [left, right]):
            team1 = left or team1
            team2 = right or team2
        elif left and not team1:
            team1 = left
        elif right and not team2:
            team2 = right
    else:
        # Upcoming/live rows sometimes have no series score yet. The slug is the
        # safest source for team names.
        cleaned = clean_team_text(raw)
        if cleaned and not (team1 and team2):
            if " vs " in cleaned:
                bits = re.split(r"\s+vs\s+", cleaned, maxsplit=1, flags=re.I)
                team1 = bits[0]
                team2 = bits[1]
            else:
                team1 = team1 or cleaned

    winner = ""
    if score1 is not None and score2 is not None and team1 and team2:
        winner = team1 if score1 > score2 else team2 if score2 > score1 else "draw"

    # BO3's /matches/current page can include upcoming cards with prediction
    # tails like "1 3 - 2". Those numbers are not actual scores. Keep the row
    # searchable as upcoming, but never expose a fake score/winner for it.
    if status in {"upcoming", "scheduled"}:
        score1 = None
        score2 = None
        winner = ""

    clean_raw = _format_clean_match_row_text(status, team1, team2, score1, score2) or raw
    row = {
        "raw_text": clean_raw,
        "status": status,
        "time": started_at,
        "bo": bo,
        "team1": team1,
        "team2": team2,
        "score1": score1,
        "score2": score2,
        "winner": winner,
        "url": href,
    }
    if raw and raw != clean_raw:
        row["raw_text_original"] = raw
        row["raw_text_note"] = "original BO3 card text may include prediction/odds tail; raw_text is normalized"
    return row


def parse_match_list_from_anchors(raw_html: str, status_hint: str) -> List[Dict[str, Any]]:
    seen = set()
    segments: List[Dict[str, Any]] = []
    for anchor in extract_anchors(raw_html):
        href = anchor.get("href", "")
        text = anchor.get("text", "")
        item = parse_match_anchor(text, href, status_hint)
        if not item:
            continue
        key = canonical_match_path(urlparse(item["url"]).path)
        # Prefer the first populated item for each match URL.
        if key in seen:
            continue
        seen.add(key)
        segments.append(item)
    return segments


def parse_match_list_from_text(raw_html: str, status_hint: str) -> List[Dict[str, Any]]:
    """Fallback when anchor text is unavailable.

    Pull match URLs from href attributes and use the slug as team fallback.
    """
    segments: List[Dict[str, Any]] = []
    seen = set()
    for m in re.finditer(r"href\s*=\s*(['\"])(?P<href>[^'\"]*?/matches/[^'\"]+?)\1", raw_html, flags=re.I):
        href = abs_bo3_url(html.unescape(m.group("href")))
        path = canonical_match_path(urlparse(href).path)
        if not is_match_detail_path(path) or path in seen:
            continue
        seen.add(path)
        t1, t2 = teams_from_match_slug(path)
        if not (t1 and t2):
            continue
        segments.append(
            {
                "raw_text": "",
                "status": infer_status_from_text("", status_hint),
                "time": "",
                "bo": "",
                "team1": t1,
                "team2": t2,
                "score1": None,
                "score2": None,
                "winner": "",
                "url": href,
            }
        )
    return segments


def parse_match_list_from_visible_lines(raw_html: str, status_hint: str) -> List[Dict[str, Any]]:
    visible = extract_visible_text(raw_html)
    lines = visible_lines(visible)
    segments: List[Dict[str, Any]] = []
    seen = set()
    for line in lines:
        raw = collapse_ws(line)
        m = re.match(
            r"^(?P<time>(?:[A-Z][a-z]{2}\s+\d{1,2},\s*)?\d{1,2}:\d{2})\s+(?P<rest>.+?)$",
            raw,
        )
        if not m:
            continue
        rest = m.group("rest")
        status_word = ""
        if rest.lower().startswith("live "):
            status_word = "live"
            rest = rest[5:]
        elif rest.lower().startswith("full "):
            status_word = "finished"
            rest = rest[5:]
        score = re.search(r"\b(\d+)\s*-\s*(\d+)\b", rest)
        if not score:
            continue
        team1 = clean_team_text(rest[: score.start()])
        team2 = clean_team_text(rest[score.end() :])
        if not (team1 and team2):
            continue
        key = (m.group("time"), norm_key(team1), norm_key(team2), score.group(1), score.group(2))
        if key in seen:
            continue
        seen.add(key)
        s1 = int(score.group(1))
        s2 = int(score.group(2))
        winner = team1 if s1 > s2 else team2 if s2 > s1 else "draw"
        segments.append(
            {
                "raw_text": _format_clean_match_row_text(status_word or infer_status_from_text(raw, status_hint), team1, team2, s1, s2) or raw,
                "raw_text_original": raw,
                "status": status_word or infer_status_from_text(raw, status_hint),
                "time": m.group("time"),
                "bo": "",
                "team1": team1,
                "team2": team2,
                "score1": s1,
                "score2": s2,
                "winner": winner,
                "url": "",
            }
        )
    return segments


def parse_match_list(raw_html: str, status_hint: str, include_unscored_finished: bool = False) -> Dict[str, Any]:
    segments = parse_match_list_from_anchors(raw_html, status_hint)
    if not segments:
        segments = parse_match_list_from_text(raw_html, status_hint)
    if not segments:
        segments = parse_match_list_from_visible_lines(raw_html, status_hint)

    # BO3 finished/list cards sometimes expose only href/team/tournament text and
    # leave the final score for the detail page. For plain list endpoints, keep
    # the old safer behaviour and only return scored finished rows. For details
    # and search endpoints, keep unscored finished candidates so we can fetch the
    # detail page before deciding whether the match is truly finished.
    if status_hint == "finished" and not include_unscored_finished:
        segments = [item for item in segments if segment_has_real_result(item)]

    return {"status": 200, "segments": segments, "count": len(segments)}



def _first_present(obj: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in obj and obj.get(key) not in (None, ""):
            return obj.get(key)
    return None


def _nested_name(value: Any) -> str:
    if isinstance(value, str):
        return collapse_ws(value)
    if isinstance(value, dict):
        for key in ("name", "title", "short_name", "shortName", "name_en", "full_name", "fullName", "slug"):
            if value.get(key) not in (None, ""):
                if key == "slug":
                    return slug_to_name(str(value.get(key)))
                return collapse_ws(str(value.get(key)))
        for key in ("team", "participant", "opponent"):
            nested = _nested_name(value.get(key))
            if nested:
                return nested
    return ""


def _extract_api_teams(obj: Dict[str, Any], url: str) -> Tuple[str, str]:
    team1 = _nested_name(_first_present(obj, [
        "team1", "team_1", "teamA", "team_a", "home", "homeTeam", "home_team",
        "firstTeam", "first_team", "opponent1", "opponent_1", "participant1", "participant_1",
    ]))
    team2 = _nested_name(_first_present(obj, [
        "team2", "team_2", "teamB", "team_b", "away", "awayTeam", "away_team",
        "secondTeam", "second_team", "opponent2", "opponent_2", "participant2", "participant_2",
    ]))

    if not (team1 and team2):
        for key in ("teams", "opponents", "participants", "competitors"):
            value = obj.get(key)
            if isinstance(value, list) and len(value) >= 2:
                names = [_nested_name(x) for x in value]
                names = [x for x in names if x]
                if len(names) >= 2:
                    team1 = team1 or names[0]
                    team2 = team2 or names[1]
                    break

    if not (team1 and team2) and url:
        team1, team2 = teams_from_match_slug(url)
    return team1, team2


def _extract_api_score(obj: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    def as_int(value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except Exception:
            m = re.search(r"\d+", str(value))
            return int(m.group(0)) if m else None

    s1 = as_int(_first_present(obj, [
        "score1", "score_1", "team1_score", "team1Score", "home_score", "homeScore",
        "first_score", "firstScore", "scoreTeam1", "firstTeamScore",
    ]))
    s2 = as_int(_first_present(obj, [
        "score2", "score_2", "team2_score", "team2Score", "away_score", "awayScore",
        "second_score", "secondScore", "scoreTeam2", "secondTeamScore",
    ]))
    if s1 is not None and s2 is not None:
        return s1, s2

    score = obj.get("score") or obj.get("scores") or obj.get("result")
    if isinstance(score, str):
        parsed = parse_score(score)
        if parsed:
            return parsed
    if isinstance(score, list) and len(score) >= 2:
        a = as_int(score[0])
        b = as_int(score[1])
        if a is not None and b is not None:
            return a, b
    if isinstance(score, dict):
        a = as_int(_first_present(score, ["team1", "score1", "home", "first", "a", "left"]))
        b = as_int(_first_present(score, ["team2", "score2", "away", "second", "b", "right"]))
        if a is not None and b is not None:
            return a, b

    # BO3 API v1 often nests series scores on the two team objects instead of
    # exposing score1/score2 on the match object.
    for key in ("teams", "opponents", "participants", "competitors"):
        teams = obj.get(key)
        if isinstance(teams, list) and len(teams) >= 2:
            vals: List[Optional[int]] = []
            for team_obj in teams[:2]:
                if isinstance(team_obj, dict):
                    vals.append(as_int(_first_present(team_obj, [
                        "score", "match_score", "matchScore", "series_score", "seriesScore",
                        "games_won", "gamesWon", "maps_won", "mapsWon", "result",
                        "points", "wins", "team_score", "teamScore",
                    ])))
                else:
                    vals.append(as_int(team_obj))
            if len(vals) >= 2 and vals[0] is not None and vals[1] is not None:
                return vals[0], vals[1]

    return None, None


def _normalize_api_status(value: Any, status_hint: str) -> str:
    raw = collapse_ws(str(value or "")).casefold()
    if raw in {"finished", "finish", "ended", "end", "done", "completed", "complete", "full", "past", "result", "results"}:
        return "finished"
    if raw in {"live", "current", "running", "ongoing", "in_progress", "in progress", "started"}:
        return "live"
    if raw in {"upcoming", "scheduled", "schedule", "not_started", "not started", "pending"}:
        return "upcoming"
    if re.search(r"\b(live|ongoing|in[ _-]?progress)\b", raw):
        return "live"
    if re.search(r"\b(ended|finished|completed|complete|full)\b", raw):
        return "finished"
    if re.search(r"\b(upcoming|scheduled|not[ _-]?started)\b", raw):
        return "upcoming"
    return infer_status_from_text(raw, status_hint)


def _api_match_url(obj: Dict[str, Any], game: str) -> str:
    value = _first_present(obj, ["url", "href", "link", "path", "match_url", "matchUrl", "page_url", "pageUrl"])
    if value:
        value = str(value)
        if "/matches/" in value:
            return scope_root_match_url_for_game(value, game)

    slug = _first_present(obj, ["slug", "match_slug", "matchSlug", "seo_slug", "seoSlug"])
    if slug:
        slug = str(slug).strip("/")
        if slug:
            if "/matches/" in slug:
                return abs_bo3_url("/" + slug)
            return urljoin(BASE_URL, game_prefix(game) + "/matches/" + slug)
    return ""


def _api_item_is_for_game(url: str, game: str) -> bool:
    if not url:
        return False
    path = canonical_match_path(urlparse(url).path).lower()
    expected = game_prefix(game).lower()
    if expected:
        return path.startswith(expected + "/matches/")
    # CS2 is the root namespace. Reject other explicit game namespaces.
    return bool(re.match(r"^/matches/[^/]+$", path))


def _iter_json_dicts(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            for item in _iter_json_dicts(child):
                yield item
    elif isinstance(value, list):
        for child in value:
            for item in _iter_json_dicts(child):
                yield item


def _api_obj_to_segment(obj: Dict[str, Any], game: str, status_hint: str) -> Optional[Dict[str, Any]]:
    url = _api_match_url(obj, game)
    if not url or not _api_item_is_for_game(url, game):
        return None

    team1, team2 = _extract_api_teams(obj, url)
    if not (team1 and team2):
        return None

    score1, score2 = _extract_api_score(obj)
    status = _normalize_api_status(_first_present(obj, ["status", "state", "stage", "match_status", "matchStatus", "type"]), status_hint)
    if status == "finished" and (score1 is None or score2 is None):
        # Keep it as a detail candidate. The detail page often has the score.
        winner = ""
    elif score1 is not None and score2 is not None:
        winner = team1 if score1 > score2 else team2 if score2 > score1 else "draw"
    else:
        winner = ""

    bo_raw = _first_present(obj, ["bo", "best_of", "bestOf", "format", "match_format", "matchFormat", "maps_count", "mapsCount"])
    bo = ""
    if bo_raw not in (None, ""):
        m = re.search(r"([1357])", str(bo_raw))
        if m:
            bo = "bo" + m.group(1)

    time_value = _first_present(obj, ["time", "date", "start_at", "startAt", "started_at", "startedAt", "scheduled_at", "scheduledAt", "begin_at", "beginAt"])
    tournament = _nested_name(_first_present(obj, ["tournament", "event", "league", "championship"]))
    raw_text = collapse_ws(" ".join(str(x) for x in [time_value or "", team1, bo, team2, score1 if score1 is not None else "", "-" if score1 is not None and score2 is not None else "", score2 if score2 is not None else "", tournament] if str(x) != ""))

    return {
        "raw_text": raw_text,
        "status": status,
        "time": collapse_ws(str(time_value or "")),
        "bo": bo,
        "team1": team1,
        "team2": team2,
        "score1": score1,
        "score2": score2,
        "winner": winner,
        "url": url,
    }


def parse_match_list_from_api_json(raw_text: str, game: str, status_hint: str) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(raw_text)
    except Exception:
        return []

    out: List[Dict[str, Any]] = []
    seen = set()
    for obj in _iter_json_dicts(payload):
        item = _api_obj_to_segment(obj, game, status_hint)
        if not item:
            continue
        key = canonical_match_path(urlparse(item.get("url", "")).path).rstrip("/")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _api_raw_looks_like_game(raw_text: str, game: str) -> bool:
    """Guard against accepting the wrong BO3 discipline ID.

    BO3's numeric discipline IDs have changed before.  For prefixed games, the
    API payload normally contains game-specific slugs such as -lol or /lol/. If
    those markers are absent, skip that candidate instead of accidentally
    rescoping CS2 root slugs into /lol/matches/... .
    """
    canonical = normalize_game(game)
    if canonical == "cs2":
        return True
    lower = (raw_text or "").casefold()
    markers = GAME_API_RAW_MARKERS.get(canonical) or GAME_API_SLUGS.get(canonical) or [canonical]
    return any(marker.casefold() in lower for marker in markers)


async def fetch_match_list_from_api_v1(game: str, q_norm: str, status_hint: str) -> Tuple[Dict[str, Any], str]:
    """Use BO3's real browser API host as the primary JSON fallback.

    The public HTML page can be SEO-only in serverless fetches.  BO3's own
    browser client reads /api/v1/matches from api.bo3.gg with filters such as
    filter[matches.status][in]=finished and filter[matches.discipline_id][eq]=N.
    """
    canonical = normalize_game(game)
    if q_norm in {"finished", "results"}:
        status_values = ["finished", "finished,defwin"]
    elif q_norm in {"schedule", "upcoming"}:
        status_values = ["upcoming"]
    else:
        status_values = ["current"]

    client = await get_client()
    errors: List[str] = []
    seen_urls = set()

    # Try the most likely discipline IDs first, but validate the returned raw
    # payload markers before accepting results for prefixed games like LoL.
    for disc_id in DISCIPLINE_ID_CANDIDATES.get(canonical, [])[:12]:
        for status_value in status_values:
            # Match BO3's public results pages more closely: finished pages are
            # grouped by most-recent date first, then tier.  The old
            # tier-first sort could return older S-tier CS2 finals before
            # today's lower-tier finished matches.
            sort_value = "-start_date,tier_rank" if status_hint == "finished" else "tier_rank,-start_date"
            params = {
                "scope": "widget-matches",
                "page[offset]": "0",
                "page[limit]": "100",
                "sort": sort_value,
                "filter[matches.status][in]": status_value,
                "filter[matches.discipline_id][eq]": str(disc_id),
                "with": "teams,tournament,ai_predictions,games,streams",
            }
            url = API_V1_BASE_URL + "/matches?" + urlencode(params)
            now = time.time()
            ttl = CACHE_TTL_SECONDS
            cached = _cache.get(url) if cache_enabled(ttl) else None
            if cached and now - cached.ts <= ttl:
                raw = cached.value
            else:
                try:
                    request_headers = dict(API_V1_HEADERS)
                    if not cache_enabled(ttl):
                        request_headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache"})

                    resp = await client.get(url, headers=request_headers)
                    if resp.status_code == 429:
                        retry_after = resp.headers.get("Retry-After")
                        delay = float(retry_after) if retry_after and retry_after.isdigit() else 1.5
                        await asyncio.sleep(min(delay, 8.0))
                        resp = await client.get(url, headers=request_headers)
                    resp.raise_for_status()
                    raw = resp.text or ""
                    if cache_enabled(ttl):
                        _cache[url] = CacheEntry(time.time(), raw)
                except Exception as exc:
                    if len(errors) < 3:
                        errors.append("%s: %s" % (url, exc))
                    continue

            if not raw.strip():
                continue
            if not _api_raw_looks_like_game(raw, canonical):
                continue

            segments = parse_match_list_from_api_json(raw, canonical, status_hint)
            if not segments:
                continue

            deduped: List[Dict[str, Any]] = []
            for item in segments:
                key = canonical_match_path(urlparse(item.get("url", "")).path).rstrip("/")
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                item = dict(item)
                item["api_source_path"] = url
                item["api_discipline_id"] = str(disc_id)
                deduped.append(item)

            if deduped:
                return {"status": 200, "segments": deduped, "count": len(deduped)}, url

    if errors:
        return {"status": 502, "segments": [], "count": 0, "error": " | ".join(errors)}, ""
    return {"status": 200, "segments": [], "count": 0}, ""


async def fetch_match_list_from_api(game: str, q_norm: str, status_hint: str) -> Tuple[Dict[str, Any], str]:
    """Fallback for BO3 pages whose HTML no longer contains match cards.

    Prefer BO3's real api.bo3.gg v1 browser endpoint.  Keep the older /api/v2
    guesses as a final compatibility fallback.
    """
    api_v1_data, api_v1_path = await fetch_match_list_from_api_v1(game, q_norm, status_hint)
    if api_v1_data.get("segments"):
        return api_v1_data, api_v1_path

    state = "finished" if q_norm in {"finished", "results"} else "current"
    prefix = game_prefix(game)

    # CS2 is BO3's root namespace: /matches/finished and /api/v2/matches/finished.
    # Other games use a URL prefix for HTML (/lol/matches/finished). BO3 has also
    # served game-scoped API paths on some builds, so try those before the shared
    # root API. Results are still filtered back to the requested namespace.
    base_paths: List[str] = []
    if prefix:
        base_paths.append(prefix + "/api/v2/matches/" + state)
        base_paths.append("/api/v2" + prefix + "/matches/" + state)
    base_paths.append("/api/v2/matches/" + state)

    today = datetime.utcnow().date()
    from_date = (today - timedelta(days=7)).isoformat()
    to_date = (today + timedelta(days=1)).isoformat()
    date_windows = [
        {"from_date": from_date, "to_date": to_date},
        {"date_from": from_date, "date_to": to_date},
        {"start_date": from_date, "end_date": to_date},
    ]

    query_sets: List[Dict[str, Any]] = []
    seen_query_keys = set()

    def add_query(params: Dict[str, Any]) -> None:
        variants = [dict(params)]
        for win in date_windows:
            dated = dict(params)
            dated.update(win)
            variants.append(dated)
        for variant in variants:
            variant.setdefault("page", "1")
            variant.setdefault("utc_offset", "0")
            key = tuple(sorted((str(k), str(v)) for k, v in variant.items()))
            if key in seen_query_keys:
                continue
            seen_query_keys.add(key)
            query_sets.append(variant)

    add_query({})
    for slug in GAME_API_SLUGS.get(game, [game])[:4]:
        add_query({"game": slug})
        add_query({"discipline": slug})
        add_query({"discipline_slug": slug})
    for disc_id in DISCIPLINE_ID_CANDIDATES.get(game, [])[:12]:
        add_query({"discipline_id": str(disc_id)})
        add_query({"disciplineId": str(disc_id)})
        add_query({"discipline": str(disc_id)})

    errors: List[str] = []
    seen_urls = set()
    for base in base_paths:
        for params in query_sets:
            path = base + "?" + urlencode(params)
            try:
                raw = await fetch_html(path, ttl=CACHE_TTL_SECONDS)
                segments = parse_match_list_from_api_json(raw, game, status_hint)
                if not segments:
                    continue
                deduped: List[Dict[str, Any]] = []
                for item in segments:
                    key = canonical_match_path(urlparse(item.get("url", "")).path).rstrip("/")
                    if key in seen_urls:
                        continue
                    seen_urls.add(key)
                    item = dict(item)
                    item["api_source_path"] = path
                    deduped.append(item)
                if deduped:
                    return {"status": 200, "segments": deduped, "count": len(deduped)}, path
            except Exception as exc:
                if len(errors) < 3:
                    errors.append("%s: %s" % (path, exc))
                continue
    if errors:
        return {"status": 502, "segments": [], "count": 0, "error": " | ".join(errors)}, ""
    return {"status": 200, "segments": [], "count": 0}, ""


async def fetch_match_list_data(game: str, q_norm: str, include_unscored_finished: bool = False) -> Tuple[Dict[str, Any], str, str, str, int]:
    canonical = normalize_game(game)
    path, hint, ttl = match_list_path(canonical, q_norm)

    # In no-cache mode the caller is asking for the freshest possible live
    # state. BO3's SSR/HTML list cards can lag behind the browser-rendered
    # score, so try the browser JSON API before the HTML parser.
    if BO3API_PREFER_JSON_WHEN_NO_CACHE and not cache_enabled(CACHE_TTL_SECONDS) and q_norm in {
        "current", "live", "finished", "results"
    }:
        try:
            api_data, api_path = await fetch_match_list_from_api(canonical, q_norm, hint)
            if api_data.get("segments"):
                api_data["segments"] = [normalize_segment_url_for_game(item, canonical) for item in (api_data.get("segments") or [])]
                api_data["count"] = len(api_data.get("segments") or [])
                return api_data, api_path or path, "json-api", hint, ttl
        except Exception:
            pass

    raw = await fetch_html(path, ttl=ttl)
    data = parse_match_list(raw, hint, include_unscored_finished=include_unscored_finished)
    data["segments"] = [normalize_segment_url_for_game(item, canonical) for item in (data.get("segments") or [])]
    data["count"] = len(data.get("segments") or [])
    source_path = path
    mode = response_mode(raw)

    # If BO3's SSR/HTML page returns only SEO text, use the browser JSON API.
    # Also do this for finished pages when the HTML parser only saw bare URLs
    # without a real result. Otherwise /details?q=finished can fetch detail
    # pages from unscored candidates and filter everything back to zero.
    segments_now = list(data.get("segments") or [])
    needs_api_fallback = not segments_now
    if hint == "finished" and not any(segment_has_real_result(item) for item in segments_now):
        needs_api_fallback = True

    if needs_api_fallback:
        api_data, api_path = await fetch_match_list_from_api(canonical, q_norm, hint)
        if api_data.get("segments"):
            data = api_data
            source_path = api_path or path
            mode = "json-api"
        elif api_data.get("error"):
            data["api_error"] = api_data.get("error")

    return data, source_path, mode, hint, ttl

def segment_has_real_result(item: Dict[str, Any]) -> bool:
    return (
        item.get("score1") is not None
        and item.get("score2") is not None
        and bool(item.get("winner"))
    )


def filter_segments_for_request(segments: List[Dict[str, Any]], q_norm: str) -> List[Dict[str, Any]]:
    """Apply the public q= intent after BO3's mixed current page is parsed.

    BO3 uses /matches/current for both live/current and scheduled cards. The API
    exposes q=live and default details live+finished, so those modes must not
    return rows that the parser already identified as upcoming. q=current keeps
    the raw BO3 current-page behavior for backwards compatibility.
    """
    requested = (q_norm or "current").lower().strip()
    if requested == "results":
        requested = "finished"

    if requested == "live":
        return [item for item in segments if (item.get("status") or "").lower() == "live"]

    if requested in {"upcoming", "schedule"}:
        return [
            item for item in segments
            if (item.get("status") or "").lower() in {"upcoming", "scheduled"}
        ]

    if requested == "finished":
        return [
            item for item in segments
            if (item.get("status") or "").lower() == "finished" and segment_has_real_result(item)
        ]

    # q=current intentionally means BO3's current page, which may include live
    # and scheduled rows. Use q=live when callers need only live games.
    return segments


def status_from_detail_lines(lines: List[str]) -> str:
    # Only inspect the match header, not the global nav.  The nav contains
    # "Finished" links on every BO3 page and caused live matches to be labelled
    # finished.
    top_lines = [collapse_ws(x).lower() for x in detail_top_lines(lines, max_lines=140)[:80]]
    top = "\n".join(top_lines)

    if re.search(r"\b(postponed)\b", top):
        return "postponed"
    if re.search(r"\b(cancelled|canceled)\b", top):
        return "cancelled"

    # Treat "live" as a status only when it appears as its own short label, not
    # when it is part of generic SEO text like "live score" / "live scores".
    for line in top_lines:
        if line in {"live", "match live"}:
            return "live"
        if re.fullmatch(r"live(?:\s+now|\s+match)?", line):
            return "live"
        if re.search(r"\bstatus\s*[:：-]\s*live\b", line):
            return "live"

    # Finished/cancelled labels are only trusted after the live checks above and
    # only inside the match header.
    if re.search(r"\b(ended|finished|completed|complete|full time|final)\b", top):
        return "finished"
    return "unknown"


def useful_team_line(line: str) -> bool:
    if not line:
        return False
    lk = line.casefold()
    if lk in NOISE_LINES or lk in STATUS_WORDS:
        return False
    if parse_score(line):
        return False
    if re.fullmatch(r"\d+(?:\.\d+)?[Kk%]?", line):
        return False
    if re.fullmatch(r"[wl]{1,5}", lk):
        return False
    if re.fullmatch(r"[+-]?\d+%", line):
        return False
    if len(line) > 80:
        return False
    return True


def find_line_index(lines: List[str], target: str, start: int = 0, end: Optional[int] = None) -> Optional[int]:
    if not target:
        return None
    end_i = len(lines) if end is None else min(end, len(lines))
    target_key = norm_key(target)
    for i in range(start, end_i):
        if norm_key(lines[i]) == target_key:
            return i
    return None


def is_score_line(line: str) -> bool:
    return parse_score(line) is not None


def is_int_line(line: str) -> bool:
    return bool(re.fullmatch(r"\d+", collapse_ws(line)))


def is_section_stop(line: str) -> bool:
    line = collapse_ws(line)
    return bool(
        re.search(
            r"^(Full stats|Overview|Performance|Aim|Grenades|Devices|Economy|Score predict|Stream|Analytics Insights|Team Form|Teams advantage|Lineups|Picks\s*&\s*bans|Historical|Head to head|Comments|Latest top news)",
            line,
            flags=re.I,
        )
        or re.search(r"\bScoreboard$", line, flags=re.I)
    )


def detail_top_lines(lines: List[str], max_lines: int = 120) -> List[str]:
    """Return only the actual match header block.

    BO3 detail pages include global navigation near the top with words such as
    "Finished" and "Schedule and Live".  The old parser kept those pre-title
    lines, which let nav text mark live matches as finished.  Start at the H1
    line containing "vs" and stop before prediction/stats sections.
    """
    out: List[str] = []
    title_seen = False
    for line in lines[:max_lines]:
        if " vs " in line:
            title_seen = True
        if not title_seen:
            continue
        if is_section_stop(line):
            break
        out.append(line)
    return out if out else lines[:max_lines]


def lines_between_markers(lines: List[str], start_regex: str, stop_regex: str) -> List[str]:
    start = None
    for i, line in enumerate(lines):
        if re.search(start_regex, line, flags=re.I):
            start = i + 1
            break
    if start is None:
        return []
    end = len(lines)
    for j in range(start, len(lines)):
        if re.search(stop_regex, lines[j], flags=re.I):
            end = j
            break
    return lines[start:end]


def parse_bo_from_lines(lines: List[str]) -> str:
    for line in lines[:120]:
        m = re.search(r"\bBo([1357])\b", line, flags=re.I)
        if m:
            return "bo" + m.group(1)
    return ""


def infer_bo_from_series(score1: Optional[int], score2: Optional[int], maps: List[Dict[str, Any]]) -> str:
    if score1 is not None and score2 is not None:
        wins_needed = max(score1, score2)
        if wins_needed >= 3:
            return "bo5"
        if wins_needed == 2:
            return "bo3"
        if wins_needed == 1:
            return "bo1"
    if len(maps) >= 4:
        return "bo5"
    if len(maps) >= 2:
        return "bo3"
    if len(maps) == 1:
        return "bo1"
    return ""


def make_series(team1: str, team2: str, score1: Optional[int], score2: Optional[int]) -> Dict[str, Any]:
    winner = ""
    if score1 is not None and score2 is not None and team1 and team2:
        winner = team1 if score1 > score2 else team2 if score2 > score1 else "draw"
    return {"score1": score1, "score2": score2, "winner": winner}


def parse_series_score(lines: List[str], visible: str, team1: str, team2: str) -> Dict[str, Any]:
    """Parse only the match header score, never odds/Score predict/history."""
    top = detail_top_lines(lines)
    top_flat = " ".join(top[:80])

    if team1 and team2:
        pattern = re.compile(
            re.escape(team1) + r"\s+(\d+)\s*-\s*(\d+)\s+" + re.escape(team2),
            flags=re.I | re.S,
        )
        m = pattern.search(top_flat)
        if m:
            return make_series(team1, team2, int(m.group(1)), int(m.group(2)))

        pattern2 = re.compile(
            re.escape(team2) + r"\s+(\d+)\s*-\s*(\d+)\s+" + re.escape(team1),
            flags=re.I | re.S,
        )
        m2 = pattern2.search(top_flat)
        if m2:
            return make_series(team1, team2, int(m2.group(2)), int(m2.group(1)))

    # Common finished detail header:
    # Ended / seed-or-rank / Team A / 0 - 2 / Team B / Full stats
    t1_idx = find_line_index(top, team1, 0, len(top)) if team1 else None
    t2_idx = find_line_index(top, team2, 0, len(top)) if team2 else None
    if t1_idx is not None and t2_idx is not None:
        lo = min(t1_idx, t2_idx)
        hi = max(t1_idx, t2_idx)
        for i in range(lo, hi + 1):
            score = parse_score(top[i])
            if score:
                a, b = score
                if t1_idx < t2_idx:
                    return make_series(team1, team2, a, b)
                return make_series(team1, team2, b, a)

    # Fallback: first score-like line in the header only. This deliberately
    # excludes Score predict, odds, Team Form, H2H, and historical stats.
    for line in top[:80]:
        score = parse_score(line)
        if score:
            return make_series(team1, team2, score[0], score[1])

    return {"score1": None, "score2": None, "winner": ""}

def canonical_map_name(value: str) -> str:
    raw = collapse_ws(value)
    # BO3 labels unknown/decider maps as Map 3 / Map 5 on live pages.
    if re.fullmatch(r"(?:Map|Game)\s*\d+", raw, flags=re.I):
        return raw.title().replace("Game", "Game")

    # Some live tabs come through as "Mirage LIVE".
    raw = re.sub(r"\bLIVE\b", "", raw, flags=re.I).strip()
    key = norm_key(raw)
    for name in MAP_NAMES:
        if norm_key(name) == key:
            return "Dust II" if name == "Dust 2" else name
    return ""


def map_name_from_line(value: str) -> str:
    """Return a canonical map/game label from either a bare or noisy line."""
    raw = collapse_ws(value)
    if not raw:
        return ""
    bare = canonical_map_name(raw)
    if bare:
        return bare

    # Generic map/game labels for MOBAs and unconfirmed deciders.
    m_generic = re.search(r"\b(?:Map|Game)\s*(\d+)\b", raw, flags=re.I)
    if m_generic:
        word = "Game" if re.search(r"\bGame\b", raw, flags=re.I) else "Map"
        return "%s %s" % (word, m_generic.group(1))

    for name in MAP_NAMES:
        if re.search(r"\b" + re.escape(name) + r"\b", raw, flags=re.I):
            return "Dust II" if name == "Dust 2" else name
    return ""


def expected_maps_from_series(score1: Optional[int], score2: Optional[int], bo: str = "") -> int:
    if score1 is not None and score2 is not None:
        total = int(score1) + int(score2)
        if total > 0:
            return total
    bo_m = re.search(r"bo([1357])", bo or "", flags=re.I)
    if bo_m:
        return int(bo_m.group(1))
    return 0


def trim_maps_for_series(
    maps: List[Dict[str, Any]],
    score1: Optional[int],
    score2: Optional[int],
    bo: str = "",
) -> List[Dict[str, Any]]:
    """Keep only plausible actual map rows.

    BO3 pages repeat map names in predictions, H2H, veto, and stats sections.
    Actual match maps appear first and their count should equal the current or
    final series score total.  This makes the endpoint verifier-safe instead
    of inventing extra historical maps.
    """
    if not maps:
        return maps
    limit = expected_maps_from_series(score1, score2, bo)
    if limit and len(maps) > limit:
        return maps[:limit]
    return maps


def _score_from_int_lines(candidates: List[str]) -> Optional[Tuple[int, int]]:
    ints: List[int] = []
    for value in candidates:
        value = collapse_ws(value)
        if is_int_line(value):
            try:
                ints.append(int(value))
            except Exception:
                pass
        if len(ints) >= 2:
            return ints[0], ints[1]
    return None


def _append_map_score(
    maps: List[Dict[str, Any]],
    map_name: str,
    score1: Optional[int],
    score2: Optional[int],
    team1: str,
    team2: str,
) -> None:
    if not map_name:
        return
    winner = ""
    if score1 is not None and score2 is not None and team1 and team2:
        winner = team1 if score1 > score2 else team2 if score2 > score1 else "draw"
    maps.append(
        {
            "game": len(maps) + 1,
            "map": map_name,
            "team1": team1,
            "team2": team2,
            "score1": score1,
            "score2": score2,
            "winner": winner,
        }
    )


def _dedupe_map_scores(maps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    seen = set()
    for item in maps:
        map_name = item.get("map", "") or ""
        score1 = item.get("score1")
        score2 = item.get("score2")
        key = (norm_key(map_name), score1, score2)
        if key in seen:
            continue
        # Prefer scored rows. Bare map tabs without score are not useful for a
        # verifier once scored rows are available for the same map.
        if score1 is None or score2 is None:
            scored_same_map = any(
                norm_key(x.get("map", "")) == norm_key(map_name)
                and x.get("score1") is not None
                and x.get("score2") is not None
                for x in maps
            )
            if scored_same_map:
                continue
        seen.add(key)
        item = dict(item)
        item["game"] = len(deduped) + 1
        deduped.append(item)
    return deduped


def _scan_map_scores_from_lines(lines: List[str], team1: str, team2: str) -> List[Dict[str, Any]]:
    maps: List[Dict[str, Any]] = []
    stop_after_match_re = re.compile(
        r"^(?:Score predict|Stream|Analytics Insights|Team Form|Teams advantage|Lineups|"
        r"Picks\s*&\s*bans|Historical|Head to head|Comments|Latest top news)$",
        flags=re.I,
    )

    # Prefer the upper match-detail block.  It normally contains:
    #   Full stats / Ancient / 10 - 13 / Inferno / 13 - 7 / Mirage / 13 - 9
    # and appears before predictions, streams, lineups, H2H, etc.
    cutoff = len(lines)
    for i, line in enumerate(lines):
        if stop_after_match_re.search(line):
            cutoff = i
            break
    scan_lines = lines[: min(cutoff, 260)]

    for i, line in enumerate(scan_lines):
        map_name = map_name_from_line(line)
        if not map_name:
            continue

        # Avoid navigation/sport names that can accidentally contain a map word.
        if line.casefold() in {"maps", "map", "games", "game"}:
            continue

        score: Optional[Tuple[int, int]] = parse_score(line)
        if score is None:
            next_lines: List[str] = []
            for j in range(i + 1, min(i + 10, len(scan_lines))):
                if is_section_stop(scan_lines[j]) or stop_after_match_re.search(scan_lines[j]):
                    break
                if map_name_from_line(scan_lines[j]):
                    break
                next_lines.append(scan_lines[j])
                score = parse_score(scan_lines[j])
                if score is not None:
                    break
            if score is None:
                score = _score_from_int_lines(next_lines)

        if score is not None:
            _append_map_score(maps, map_name, int(score[0]), int(score[1]), team1, team2)
        else:
            # Keep unscored map tabs for live matches, but dedupe/trim later.
            _append_map_score(maps, map_name, None, None, team1, team2)

    return _dedupe_map_scores(maps)


def _scan_map_scores_from_text_blob(text: str, team1: str, team2: str) -> List[Dict[str, Any]]:
    maps: List[Dict[str, Any]] = []
    text = collapse_ws(text)
    if not text:
        return maps

    map_alt = "|".join(re.escape(x) for x in MAP_NAMES)
    generic_alt = r"(?:Map|Game)\s*\d+"
    any_map = r"(?:" + map_alt + r"|" + generic_alt + r")"

    # Visible inline form: Ancient 10 - 13
    for m in re.finditer(r"\b(?P<map>" + any_map + r")\b\s{1,80}(?P<s1>\d{1,2})\s*-\s*(?P<s2>\d{1,2})\b", text, flags=re.I):
        map_name = map_name_from_line(m.group("map"))
        _append_map_score(maps, map_name, int(m.group("s1")), int(m.group("s2")), team1, team2)

    # JSON-ish form from Nuxt payloads, when the visible text only exposes the
    # header but the page still embeds map score objects.
    json_score_keys_1 = r"(?:score1|score_1|team1Score|firstTeamScore|homeScore|scoreTeam1)"
    json_score_keys_2 = r"(?:score2|score_2|team2Score|secondTeamScore|awayScore|scoreTeam2)"
    for m in re.finditer(
        r"(?P<map>" + any_map + r")(?:(?!" + any_map + r").){0,350}?"
        r"(?:\"|'|\b)" + json_score_keys_1 + r"(?:\"|'|\b)\s*[:=]\s*(?P<s1>\d{1,2})"
        r"(?:(?!" + any_map + r").){0,180}?"
        r"(?:\"|'|\b)" + json_score_keys_2 + r"(?:\"|'|\b)\s*[:=]\s*(?P<s2>\d{1,2})",
        text,
        flags=re.I,
    ):
        map_name = map_name_from_line(m.group("map"))
        _append_map_score(maps, map_name, int(m.group("s1")), int(m.group("s2")), team1, team2)

    return _dedupe_map_scores(maps)


def parse_map_scores(lines: List[str], visible: str, raw_html: str, team1: str, team2: str) -> List[Dict[str, Any]]:
    """Parse actual played map/game rows only.

    This endpoint is meant for verification, so it should return the real match
    winner plus real per-map/game winners.  It intentionally ignores streams,
    lineups, picks/bans, odds, prediction widgets, and H2H/history sections.
    """
    maps = _scan_map_scores_from_lines(lines, team1, team2)
    if maps and any(item.get("score1") is not None and item.get("score2") is not None for item in maps):
        return maps

    # Fallback to a compact text blob.  This recovers pages where BO3 embeds
    # map scores in Nuxt/JSON-ish payloads but the visible parser only saw the
    # match header.
    blob = "\n".join([visible or "", strip_tags(raw_html or ""), html.unescape(raw_html or "")])
    maps2 = _scan_map_scores_from_text_blob(blob, team1, team2)
    if maps2:
        return maps2
    return maps

def slice_between(lines: List[str], start_regex: str, stop_regex: str) -> List[str]:
    start = None
    for i, line in enumerate(lines):
        if re.search(start_regex, line, flags=re.I):
            start = i + 1
            break
    if start is None:
        return []
    end = len(lines)
    for j in range(start, len(lines)):
        if re.search(stop_regex, lines[j], flags=re.I):
            end = j
            break
    return lines[start:end]


def parse_picks_bans(lines: List[str]) -> List[Dict[str, str]]:
    chunk = slice_between(lines, r"^Picks\s*&\s*bans$", r"^(Historical|Head to head|Comments|Latest top news)")
    if not chunk:
        return []

    out: List[Dict[str, str]] = []
    i = 0
    while i < len(chunk):
        map_name = canonical_map_name(chunk[i])
        if map_name:
            action = ""
            for j in range(i + 1, min(i + 5, len(chunk))):
                if chunk[j].lower() in {"ban", "pick", "decider"}:
                    action = chunk[j].lower()
                    break
            if action:
                out.append({"map": map_name, "action": action})
        i += 1
    return out


def parse_streams(lines: List[str]) -> List[str]:
    chunk = slice_between(lines, r"^Stream$", r"^(Score predict|Team Form|Teams advantage|Lineups|Picks)")
    out: List[str] = []
    for line in chunk:
        if not re.match(r"^\d+(?:\.\d+)?[Kk]?$", line):
            out.append(line)
    return out[:10]


def parse_lineups(lines: List[str], team1: str, team2: str) -> Dict[str, List[str]]:
    chunk = slice_between(lines, r"^Lineups$", r"^Picks\s*&\s*bans$")
    if not chunk:
        return {}

    result: Dict[str, List[str]] = {}
    current_team = ""
    for line in chunk:
        if team1 and norm_key(line) == norm_key(team1):
            current_team = team1
            result.setdefault(current_team, [])
            continue
        if team2 and norm_key(line) == norm_key(team2):
            current_team = team2
            result.setdefault(current_team, [])
            continue
        if not current_team:
            continue
        lk = line.lower()
        if lk in {"lineup", "starter", "coach", "substitute"}:
            continue
        if useful_team_line(line) and line not in result[current_team]:
            result[current_team].append(line)
    return result


def parse_match_detail(raw_html: str, source_url: str) -> Dict[str, Any]:
    visible = extract_visible_text(raw_html)
    lines = visible_lines(visible)

    title = parse_title(raw_html, visible, source_url)
    team1, team2, tournament = parse_teams_from_title(title)
    if not (team1 and team2):
        team1, team2 = teams_from_match_slug(source_url)
        if team1 and team2 and not title:
            title = "%s vs %s" % (team1, team2)

    # Tournament sometimes appears right after the title if <title> did not have it.
    if not tournament:
        title_idx = find_line_index(lines, title, 0, 20)
        if title_idx is not None and title_idx + 1 < len(lines):
            cand = lines[title_idx + 1]
            if useful_team_line(cand) and " vs " not in cand:
                tournament = cand

    status = status_from_detail_lines(lines)
    series = parse_series_score(lines, visible, team1, team2)
    if status == "unknown" and series.get("score1") is not None and series.get("score2") is not None and series.get("winner"):
        # BO3 sometimes omits an explicit "Ended" label from prerendered
        # detail HTML even though the header contains the final series score.
        status = "finished"
    bo_from_page = parse_bo_from_lines(lines)
    maps = parse_map_scores(lines, visible, raw_html, team1, team2)
    maps = trim_maps_for_series(maps, series["score1"], series["score2"], bo_from_page)
    bo = bo_from_page or infer_bo_from_series(series["score1"], series["score2"], maps)

    match_summary = {
        "title": title,
        "url": source_url,
        "status": status,
        "tournament": tournament,
        "bo": bo,
        "team1": team1,
        "team2": team2,
        "score1": series["score1"],
        "score2": series["score2"],
        "winner": series["winner"],
    }

    # Keep the old single-segment shape, but only include verifier-relevant data.
    segment = dict(match_summary)
    segment["teams"] = [
        {"name": team1, "score": series["score1"]},
        {"name": team2, "score": series["score2"]},
    ]
    segment["maps"] = maps

    return {
        "status": 200,
        "match": match_summary,
        "maps": maps,
        "segments": [segment],
    }


def merge_detail_with_list_item(data: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    """Use /matches/current or /matches/finished as a safe summary fallback.

    BO3 live detail pages can omit the opposing team's series score in the
    header, while the list row has it. This prevents Score predict odds from
    being misread as the actual winner.
    """
    if not item:
        return data

    match = data.get("match") or {}
    segments = data.get("segments") or []
    segment = segments[0] if segments else {}

    score1 = item.get("score1")
    score2 = item.get("score2")
    has_list_score = score1 is not None and score2 is not None

    if item.get("team1"):
        match["team1"] = item.get("team1")
    if item.get("team2"):
        match["team2"] = item.get("team2")

    # Detail-page status is usually more authoritative than BO3's mixed current
    # list, but the detail parser must not let generic "live scores" SEO text
    # override a finished list/API row with a real score.
    detail_status = (match.get("status") or "").lower()
    list_status = (item.get("status") or "").lower()
    if list_status == "finished" and has_list_score:
        match["status"] = "finished"
    elif list_status in {"upcoming", "scheduled"} and not has_list_score and detail_status == "live":
        # Avoid turning future schedule cards into fake live matches just because
        # the page contains generic "live score" wording.
        match["status"] = list_status
    elif detail_status in {"", "unknown"} and list_status:
        match["status"] = list_status
    elif detail_status in {"upcoming", "scheduled"} and list_status in {"live", "finished"}:
        match["status"] = list_status

    if item.get("bo"):
        match["bo"] = item.get("bo")

    # Never let a scheduled/current-card prediction tail overwrite detail data.
    # List scores are safe only from finished/live rows, and live 0-0 list cards
    # must not overwrite a fresher detail/API score such as 0-1 / 1-0.
    if has_list_score and list_status not in {"upcoming", "scheduled"}:
        detail_s1 = match.get("score1")
        detail_s2 = match.get("score2")
        detail_has_score = detail_s1 is not None and detail_s2 is not None
        try:
            list_total = int(score1) + int(score2)
        except Exception:
            list_total = 0
        try:
            detail_total = int(detail_s1) + int(detail_s2) if detail_has_score else -1
        except Exception:
            detail_total = -1

        should_use_list_score = (
            not detail_has_score
            or list_status == "finished"
            or detail_status in {"", "unknown"}
            or (list_status == "live" and list_total >= detail_total and list_total > 0)
        )

        if should_use_list_score:
            match["score1"] = score1
            match["score2"] = score2
            match["winner"] = item.get("winner", "")
            if not match.get("bo"):
                match["bo"] = infer_bo_from_series(score1, score2, data.get("maps") or [])

    # Mirror into the old segment object.
    for key, value in match.items():
        segment[key] = value
    segment["teams"] = [
        {"name": match.get("team1", ""), "score": match.get("score1")},
        {"name": match.get("team2", ""), "score": match.get("score2")},
    ]
    segment["maps"] = data.get("maps") or []

    data["match"] = match
    data["segments"] = [segment]
    return data


async def find_match_list_item_for_detail(full_url: str, game: str) -> Dict[str, Any]:
    target_path = canonical_match_path(urlparse(full_url).path).rstrip("/")
    candidates = []
    for q_norm in ("current", "finished"):
        try:
            parsed, _source_path, _mode, _hint, _ttl = await fetch_match_list_data(game, q_norm, include_unscored_finished=True)
            candidates.extend(parsed.get("segments") or [])
        except Exception:
            continue

    for item in candidates:
        item_path = canonical_match_path(urlparse(item.get("url", "")).path).rstrip("/")
        if item_path == target_path:
            return item
    return {}


# Extra tokens to ignore when comparing BO3 team names against Polymarket names.
# This makes "Team Nemesis" match "Nemesis", "TEC Esports" match "TEC", etc.
TEAM_NAME_DROP_WORDS = {
    "team", "esport", "esports", "gaming", "gg", "cs", "cs2", "csgo",
    "valorant", "val", "lol", "dota", "dota2", "r6", "r6s", "siege",
    "mobile", "legends", "mlbb", "club", "clan", "academy",
}


def team_match_key(value: str) -> str:
    words = re.findall(r"[a-z0-9]+", collapse_ws(value).casefold())
    kept = [w for w in words if w not in TEAM_NAME_DROP_WORDS]
    if not kept:
        kept = words
    return "".join(kept)


def team_names_close(a: str, b: str) -> bool:
    ka = team_match_key(a)
    kb = team_match_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    # Allow one side to include a harmless suffix/prefix that the other omits,
    # but avoid matching tiny tokens like "g" against "g2ares".
    if min(len(ka), len(kb)) >= 4 and (ka in kb or kb in ka):
        return True
    return False


def item_matches_team_filters(item: Dict[str, Any], team1: str = "", team2: str = "", search: str = "") -> bool:
    i1 = item.get("team1", "") or ""
    i2 = item.get("team2", "") or ""
    haystack = " ".join([
        item.get("raw_text", "") or "",
        i1,
        i2,
        item.get("url", "") or "",
    ]).casefold()

    search = collapse_ws(search or "")
    if search:
        # Search is intentionally forgiving: every non-trivial token must appear
        # somewhere in team names/raw/url after normalization.
        tokens = [
            t for t in re.findall(r"[a-z0-9]+", search.casefold())
            if len(t) >= 2 and t not in TEAM_NAME_DROP_WORDS
        ]
        normalized_haystack = norm_key(haystack)
        if tokens and not all(t in normalized_haystack for t in tokens):
            return False

    team1 = collapse_ws(team1 or "")
    team2 = collapse_ws(team2 or "")
    if team1 and team2:
        direct = team_names_close(team1, i1) and team_names_close(team2, i2)
        swapped = team_names_close(team1, i2) and team_names_close(team2, i1)
        return direct or swapped
    if team1:
        return team_names_close(team1, i1) or team_names_close(team1, i2)
    if team2:
        return team_names_close(team2, i1) or team_names_close(team2, i2)
    return True


def _payload_game_from_match(match: Dict[str, Any], source_row: Optional[Dict[str, Any]] = None) -> str:
    """Best-effort game key from a BO3 detail/list payload."""
    for obj in (match, source_row or {}):
        if not isinstance(obj, dict):
            continue
        for key in ("game", "game_slug", "gameSlug"):
            val = str(obj.get(key) or "").strip().lower()
            if val:
                return val
        url = str(obj.get("url") or "")
        path = urlparse(url).path.lower() if url else ""
        for game_key in ("lol", "cs2", "valorant", "dota2", "r6s", "mlbb"):
            if ("/" + game_key + "/") in path:
                return game_key
    return ""


def _as_int_or_none(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def _looks_like_lol_kill_score(score1: Optional[int], score2: Optional[int]) -> bool:
    """LoL map rows from BO3 detail HTML often expose champion kills, not game score."""
    if score1 is None or score2 is None:
        return False
    return max(int(score1), int(score2)) > 3


def _safe_lol_map_result_from_series(
    match: Dict[str, Any],
    map_index: int,
    map_count: int,
    team1: str,
    team2: str,
) -> Tuple[Optional[int], Optional[int], str]:
    """Return a verifier-safe 0/1 LoL game result when series state proves it.

    BO3 LoL detail pages can expose per-map *kill* scores (for example 10-30).
    Those are useful debug numbers, but they must not decide a Polymarket
    Game 1/2/3 winner.  We only infer a per-map result from the series score
    in cases where the map order is unambiguous:
      • exactly one map has completed; or
      • the current/finished series is a sweep, so every played map has the
        same winner.
    Otherwise we return unresolved so callers fail closed instead of treating
    kill differential as a map winner.
    """
    s1 = _as_int_or_none(match.get("score1"))
    s2 = _as_int_or_none(match.get("score2"))
    if s1 is None or s2 is None or not team1 or not team2:
        return None, None, ""
    total = s1 + s2
    if total <= 0 or int(map_index) > total:
        return None, None, ""

    # Only one completed map: the series score tells us that exact map winner.
    if total == 1 and int(map_index) == 1:
        if s1 == 1 and s2 == 0:
            return 1, 0, team1
        if s1 == 0 and s2 == 1:
            return 0, 1, team2

    # Sweep: every completed map was won by the same side, so map order is safe.
    if total == map_count and (s1 == 0 or s2 == 0):
        if s1 > s2:
            return 1, 0, team1
        if s2 > s1:
            return 0, 1, team2

    return None, None, ""


def compact_detail_payload(data: Dict[str, Any], source_row: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    match = dict(data.get("match") or {})
    maps = list(data.get("maps") or [])

    source_status = ((source_row or {}).get("status") or "").lower()
    source_has_result = bool(
        source_row
        and source_row.get("score1") is not None
        and source_row.get("score2") is not None
        and source_row.get("winner")
    )
    if source_status == "finished" and source_has_result:
        # The BO3 API/list row was fetched with finished status and has a real
        # series score.  Do not let detail-page SEO text label it as live.
        match["status"] = "finished"

    # BO3's /matches/current list is "Schedule and Live".  The detail page can
    # contain generic SEO words like "finished" / "live scores" and fake
    # prediction snippets, so an unscored upcoming list row must remain upcoming
    # unless BO3 gives an actual series result.  This prevents default
    # live+finished from leaking tomorrow's scheduled games as finished/live.
    source_is_unscored_upcoming = source_status in {"upcoming", "scheduled"} and not source_has_result
    match_has_result_now = (
        match.get("score1") is not None
        and match.get("score2") is not None
        and bool(match.get("winner"))
    )
    if source_is_unscored_upcoming and not match_has_result_now:
        match["status"] = source_status

    score1 = match.get("score1")
    score2 = match.get("score2")
    if score1 is not None and score2 is not None:
        try:
            if max(int(score1), int(score2)) >= 3:
                match["bo"] = "bo5"
            elif not match.get("bo") and (match.get("status") or "").lower() == "finished":
                match["bo"] = infer_bo_from_series(score1, score2, maps)
        except Exception:
            pass

    # Upcoming/scheduled cards can contain BO3 prediction widgets that look like
    # scores. If an upcoming row reaches detail mode, do not expose those as
    # verifier-safe results.
    if (match.get("status") or "").lower() in {"upcoming", "scheduled"}:
        match["score1"] = None
        match["score2"] = None
        match["winner"] = ""
        maps = []

    # Keep map winner labels consistent with the final match team labels.  BO3
    # live/detail pages can abbreviate team names differently than list rows.
    #
    # Important LoL guard: BO3 detail pages often expose per-map champion kills
    # as score1/score2 (for example 10-30).  Kills are not the map/game score,
    # and a team can win LoL with fewer or tied kills, so never use those numbers
    # to derive a Polymarket Game winner.  When the series score makes the map
    # winner unambiguous, publish a 0/1 map result and keep the kills only as
    # debug fields.  Otherwise leave the map unresolved so the caller fails closed.
    team1 = match.get("team1", "") or ""
    team2 = match.get("team2", "") or ""
    game_key = _payload_game_from_match(match, source_row)
    cleaned_maps: List[Dict[str, Any]] = []
    for idx, mp in enumerate(maps, 1):
        raw_score1 = _as_int_or_none(mp.get("score1"))
        raw_score2 = _as_int_or_none(mp.get("score2"))
        score1 = raw_score1
        score2 = raw_score2
        winner = ""

        if game_key == "lol" and _looks_like_lol_kill_score(raw_score1, raw_score2):
            safe_s1, safe_s2, safe_winner = _safe_lol_map_result_from_series(match, idx, len(maps), team1, team2)
            score1, score2, winner = safe_s1, safe_s2, safe_winner
        elif score1 is not None and score2 is not None and team1 and team2:
            winner = team1 if score1 > score2 else team2 if score2 > score1 else "draw"

        cleaned = {
            "game": mp.get("game") or idx,
            "map": mp.get("map", "") or "",
            "team1": team1,
            "team2": team2,
            "score1": score1,
            "score2": score2,
            "winner": winner or (mp.get("winner", "") or "" if game_key != "lol" else ""),
        }
        if game_key == "lol" and _looks_like_lol_kill_score(raw_score1, raw_score2):
            cleaned["kill_score1"] = raw_score1
            cleaned["kill_score2"] = raw_score2
            cleaned["score_type"] = "map_result_from_series; kills kept separately" if winner else "kills_only_unresolved"
        cleaned_maps.append(cleaned)

    # If detail-page map parsing conflicts with the authoritative finished
    # series score, drop map rows rather than publishing wrong game winners.
    if (match.get("status") or "").lower() == "finished" and cleaned_maps:
        try:
            m_s1 = int(match.get("score1")) if match.get("score1") is not None else None
            m_s2 = int(match.get("score2")) if match.get("score2") is not None else None
        except Exception:
            m_s1 = m_s2 = None
        if m_s1 is not None and m_s2 is not None:
            w1 = sum(1 for mp in cleaned_maps if (mp.get("winner") or "") == team1)
            w2 = sum(1 for mp in cleaned_maps if (mp.get("winner") or "") == team2)
            if (w1, w2) != (m_s1, m_s2):
                cleaned_maps = []

    match["map_count"] = len(cleaned_maps)
    match["map_winners"] = [
        {"game": mp.get("game"), "map": mp.get("map", ""), "winner": mp.get("winner", "")}
        for mp in cleaned_maps
    ]

    out = {
        "match": match,
        "maps": cleaned_maps,
    }
    if source_row is not None:
        out["source_row"] = _sanitize_source_row_for_payload(source_row, match)
    return out


async def fetch_compact_detail_for_item(item: Dict[str, Any], game: str) -> Dict[str, Any]:
    full_url = item.get("url", "") or ""
    if not full_url:
        # Fallback when BO3 list rows were parsed without hrefs. No map data is
        # possible without a detail URL, but the series result is still useful.
        match_summary = {
            "title": "%s vs %s" % (item.get("team1", ""), item.get("team2", "")),
            "url": "",
            "status": item.get("status", ""),
            "tournament": "",
            "bo": item.get("bo", "") or infer_bo_from_series(item.get("score1"), item.get("score2"), []),
            "team1": item.get("team1", ""),
            "team2": item.get("team2", ""),
            "score1": item.get("score1"),
            "score2": item.get("score2"),
            "winner": item.get("winner", ""),
        }
        return compact_detail_payload({"match": match_summary, "maps": []}, item)

    raw = await fetch_html(full_url, ttl=CACHE_TTL_SECONDS)
    data = parse_match_detail(raw, full_url)
    data = merge_detail_with_list_item(data, item)
    compact = compact_detail_payload(data, item)
    compact["render_mode"] = response_mode(raw)
    return compact


def compact_payload_has_real_result(payload: Dict[str, Any]) -> bool:
    match = payload.get("match") or {}
    return (
        match.get("score1") is not None
        and match.get("score2") is not None
        and bool(match.get("winner"))
    )


def compact_payload_matches_request(payload: Dict[str, Any], requested: str) -> bool:
    req = (requested or "current").lower().strip()
    if req in {"", "all", "both", "live_finished", "live+finished", "current+finished"}:
        req = "live+finished"
    if req == "results":
        req = "finished"

    match = payload.get("match") or {}
    status = (match.get("status") or "").lower()
    has_result = compact_payload_has_real_result(payload)

    if req == "current":
        return True
    if req == "live":
        return status == "live"
    if req in {"upcoming", "schedule"}:
        return status in {"upcoming", "scheduled"}
    if req == "finished":
        # Finished endpoints should only expose verifier-safe completed results.
        return status == "finished" and has_result
    if req == "live+finished":
        # Do not include schedule/upcoming cards misparsed as finished unless
        # they have a real completed score.
        return status == "live" or (status == "finished" and has_result)
    return True


async def build_details_from_filters(
    q: str,
    game: str,
    team1: str = "",
    team2: str = "",
    search: str = "",
    max_results: int = 25,
) -> Dict[str, Any]:
    q_norm = (q or "both").lower().strip()
    canonical = normalize_game(game)

    # BO3.gg's live page path is /matches/current, but expose it as "live"
    # in API metadata because callers/verifiers reason about live vs finished.
    # Internally we still fetch "current" because that is the real BO3 path.
    if q_norm in {"", "all", "both", "live_finished", "live+finished", "current+finished"}:
        # Tuple shape: (BO3 fetch q, public/requested q, response label).
        # BO3 fetches live rows from /matches/current, but that page also
        # contains upcoming rows, so the requested q must stay "live".
        query_plan = [("current", "live", "live"), ("finished", "finished", "finished")]
        display_query_plan = ["live", "finished"]
        query_label = "live+finished"
    else:
        fetch_q = "current" if q_norm == "live" else q_norm
        query_plan = [(fetch_q, q_norm, "live" if q_norm == "current" else q_norm)]
        display_query_plan = [query_plan[0][2]]
        query_label = display_query_plan[0]

    if max_results <= 0:
        max_results = 25
    max_results = max(1, min(int(max_results), 80))

    all_rows: List[Dict[str, Any]] = []
    source_paths: List[str] = []
    render_modes: List[str] = []
    query_errors: Dict[str, str] = {}

    for fetch_q, requested_q, source_label in query_plan:
        try:
            parsed, source_path, mode, _hint, _ttl = await fetch_match_list_data(canonical, fetch_q, include_unscored_finished=True)
            path = source_path
            source_paths.append(source_path)
            render_modes.append(mode)
            # For details, list rows are only candidates. BO3 can label current
            # rows as upcoming before the detail page moves to live/ended, and
            # finished rows can omit the score on the list card. Apply the
            # strict live/finished decision only after fetching compact detail.
            if requested_q in {"live", "finished", "results"}:
                scoped_segments = list(parsed.get("segments") or [])
            else:
                scoped_segments = filter_segments_for_request(list(parsed.get("segments") or []), requested_q)
            for item in scoped_segments:
                item = dict(item)
                item["source_query"] = source_label
                item["source_path"] = path
                all_rows.append(item)
        except Exception as exc:
            query_errors[source_label] = str(exc)

    filtered = [
        item for item in all_rows
        if item_matches_team_filters(item, team1=team1, team2=team2, search=search)
    ]

    # If exact token search failed but two teams were supplied, retry with a
    # looser combined search so "Team Nemesis" can still find BO3's "Nemesis".
    if not filtered and (team1 or team2) and not search:
        combined = " ".join([team1 or "", team2 or ""]).strip()
        if combined:
            filtered = [item for item in all_rows if item_matches_team_filters(item, search=combined)]

    # Deduplicate current/finished overlap by match URL. Prefer live/current rows
    # over finished only when the same URL appears in both lists.
    deduped_rows: List[Dict[str, Any]] = []
    seen_urls = set()
    for item in filtered:
        key = canonical_match_path(urlparse(item.get("url", "") or "").path).rstrip("/") or (
            norm_key(item.get("team1", "")), norm_key(item.get("team2", "")), str(item.get("score1")), str(item.get("score2"))
        )
        if key in seen_urls:
            continue
        seen_urls.add(key)
        deduped_rows.append(item)

    # Candidate rows can be mislabelled on BO3's mixed current page, so fetch a
    # few extra detail pages and then trim after the detail-status filter.
    candidate_limit = max_results
    if q_norm in {"", "all", "both", "live_finished", "live+finished", "current+finished", "live", "finished", "results"}:
        candidate_limit = min(80, max(max_results * 4, 20))
    deduped_rows = deduped_rows[:candidate_limit]

    sem = asyncio.Semaphore(6)

    async def guarded(item: Dict[str, Any]) -> Dict[str, Any]:
        async with sem:
            try:
                return await fetch_compact_detail_for_item(item, canonical)
            except Exception as exc:
                return {
                    "match": {
                        "title": "%s vs %s" % (item.get("team1", ""), item.get("team2", "")),
                        "url": item.get("url", ""),
                        "status": item.get("status", ""),
                        "bo": item.get("bo", ""),
                        "team1": item.get("team1", ""),
                        "team2": item.get("team2", ""),
                        "score1": item.get("score1"),
                        "score2": item.get("score2"),
                        "winner": item.get("winner", ""),
                        "error": str(exc),
                    },
                    "maps": [],
                    "source_row": item,
                }

    matches = await asyncio.gather(*[guarded(item) for item in deduped_rows]) if deduped_rows else []
    matches = [m for m in matches if compact_payload_matches_request(m, q_norm)]
    matches = matches[:max_results]

    render_mode = "+".join(sorted(set(render_modes))) if render_modes else "unknown"
    return {
        "status": 200,
        "game": canonical,
        "query": query_label,
        "queries": display_query_plan,
        "source_path": source_paths[0] if len(source_paths) == 1 else "",
        "source_paths": source_paths,
        "render_mode": render_mode,
        "filters": {
            "team1": team1 or "",
            "team2": team2 or "",
            "search": search or "",
            "max_results": max_results,
        },
        "count": len(matches),
        "matches": matches,
        "errors": query_errors,
    }


def is_client_shell(raw_html: str) -> bool:
    if not raw_html:
        return True
    visible = extract_visible_text(raw_html)
    anchors = extract_anchors(raw_html)
    if len(visible) >= 100 or anchors:
        return False
    lower = raw_html[:20000].lower()
    return "_nuxt/" in lower and "<script" in lower


def response_mode(raw_html: str) -> str:
    if is_client_shell(raw_html):
        return "nuxt-client-shell"
    return "html"


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(headers=HEADERS, timeout=DEFAULT_TIMEOUT, follow_redirects=True)
    return _client


async def fetch_html(url_or_path: str, ttl: int = CACHE_TTL_SECONDS) -> str:
    url = normalize_url(url_or_path)
    now = time.time()

    # Important: ttl <= 0 means absolutely no in-process cache read/write.
    # This makes BO3API_CACHE_TTL=0 safe for live result verification.
    use_cache = cache_enabled(ttl)
    if use_cache:
        cached = _cache.get(url)
        if cached and now - cached.ts <= ttl:
            return cached.value

    client = await get_client()
    last_exc: Optional[Exception] = None
    last_text = ""

    for profile_name, headers in HEADER_PROFILES:
        for attempt in range(1, 3):
            try:
                request_headers = dict(headers)
                if not use_cache:
                    request_headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache"})

                resp = await client.get(url, headers=request_headers)
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else 1.5 * attempt
                    await asyncio.sleep(min(delay, 10.0))
                    continue
                resp.raise_for_status()
                text = resp.text or ""
                last_text = text

                # Keep trying with bot-style profiles if BO3 only gave the Nuxt
                # empty app shell. The parser needs rendered/prerendered text.
                if is_client_shell(text) and profile_name != HEADER_PROFILES[-1][0]:
                    break

                if use_cache:
                    _cache[url] = CacheEntry(time.time(), text)
                return text
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * attempt)

    if last_text:
        if use_cache:
            _cache[url] = CacheEntry(time.time(), last_text)
        return last_text
    raise HTTPException(status_code=502, detail="BO3.gg fetch failed: %s" % (last_exc,))


app = FastAPI(
    title="bo3ggapi",
    description="Unofficial REST API wrapper for public BO3.gg esports pages.",
    docs_url="/",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    for key, value in NO_CACHE_HEADERS.items():
        response.headers[key] = value
    return response


@app.on_event("shutdown")
async def shutdown() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()


@app.get("/version", tags=["Meta"])
def version() -> Dict[str, str]:
    return {"version": "0.7.1-source-row-safe", "default_api": "v2", "source": "bo3.gg"}


@app.get("/v2/games", tags=["Meta"])
def games() -> Dict[str, Any]:
    return {"status": "success", "data": list(CANONICAL_GAMES.values())}


@app.get("/v2/health", tags=["Meta"])
async def health(game: str = Query("cs2", description="cs2/valorant/r6s/dota2/lol/mlbb")) -> Dict[str, Any]:
    try:
        canonical = normalize_game(game)
        parsed, source_path, mode, _hint, _ttl = await fetch_match_list_data(canonical, "current")
        return {
            "status": "success",
            "upstream": "ok",
            "game": canonical,
            "render_mode": mode,
            "source_path": source_path,
            "match_count": parsed["count"],
        }
    except Exception as exc:
        return {"status": "error", "upstream": "failed", "error": str(exc)}


@app.get("/v2/match", tags=["Matches"])
async def match(
    q: str = Query(..., description="current/live/schedule/upcoming/finished/results"),
    game: str = Query("cs2", description="cs2/valorant/r6s/dota2/lol/mlbb"),
) -> Dict[str, Any]:
    q_norm = q.lower().strip()
    try:
        canonical = normalize_game(game)
        path, hint, ttl = match_list_path(canonical, q_norm)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    data, source_path, mode, _hint, _ttl = await fetch_match_list_data(canonical, q_norm)
    data["segments"] = filter_segments_for_request(list(data.get("segments") or []), q_norm)
    data["count"] = len(data["segments"])
    data["game"] = canonical
    data["source_path"] = source_path
    data["render_mode"] = mode
    return {"status": "success", "data": data}


@app.get("/v2/match/all", tags=["Matches"])
async def match_all(
    q: str = Query("current", description="current/live/schedule/upcoming/finished/results"),
) -> Dict[str, Any]:
    q_norm = q.lower().strip()
    out: Dict[str, Any] = {}
    for canonical in CANONICAL_GAMES:
        try:
            parsed, source_path, mode, _hint, _ttl = await fetch_match_list_data(canonical, q_norm)
            parsed["segments"] = filter_segments_for_request(list(parsed.get("segments") or []), q_norm)
            parsed["count"] = len(parsed["segments"])
            parsed["game"] = canonical
            parsed["source_path"] = source_path
            parsed["render_mode"] = mode
            out[canonical] = parsed
        except Exception as exc:
            out[canonical] = {"status": 502, "segments": [], "count": 0, "error": str(exc)}
    return {"status": "success", "data": out}


@app.get("/v2/match/details", tags=["Matches"])
async def match_details(
    q: Optional[str] = Query(None, description="Optional: live/current/schedule/upcoming/finished/results. Omit to search live+finished."),
    game: str = Query("cs2", description="cs2/valorant/r6s/dota2/lol/mlbb"),
    team1: str = Query("", description="Optional team filter, e.g. Team Nemesis"),
    team2: str = Query("", description="Optional opponent filter, e.g. FOKUS"),
    search: str = Query("", description="Optional loose text search over team names/raw row/url"),
    max_results: int = Query(25, ge=1, le=80, description="Maximum details to fetch when no URL/path is provided"),
    url: Optional[str] = Query(None, description="Optional full BO3.gg match URL; kept for backwards compatibility"),
    path: Optional[str] = Query(None, description="Optional BO3.gg match path; kept for backwards compatibility"),
) -> Dict[str, Any]:
    """Return compact match + individual map/game winners.

    Preferred verifier usage does NOT need a URL or q:
      /v2/match/details?game=cs2&team1=Team%20Nemesis&team2=FOKUS
      /v2/match/details?game=cs2&search=Nemesis%20FOKUS

    When q is omitted, this endpoint searches live/current and finished/results
    together. Response metadata reports live+finished even though BO3's live
    HTML path is /matches/current. You can still pass q=live or q=finished.

    URL/path mode is still supported for manual debugging/backwards compatibility.
    """
    target = url or path
    if target:
        try:
            full_url = normalize_url(target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        if not is_match_detail_path(urlparse(full_url).path):
            raise HTTPException(status_code=400, detail="match details URL must be a BO3 match detail URL under /matches/")

        raw = await fetch_html(full_url, ttl=CACHE_TTL_SECONDS)
        canonical = game_from_path(urlparse(full_url).path)
        data = parse_match_detail(raw, full_url)

        # Detail pages are best for map results. The list page is best for live
        # series score/winner when the detail page omits a side score.
        list_item = await find_match_list_item_for_detail(full_url, canonical)
        if list_item:
            data = merge_detail_with_list_item(data, list_item)

        compact = compact_detail_payload(data, list_item or None)
        compact["status"] = 200
        compact["game"] = canonical
        compact["render_mode"] = response_mode(raw)
        # Keep old VLR-style segment shape for existing callers.
        flat = dict(compact.get("match") or {})
        flat["maps"] = compact.get("maps") or []
        compact["segments"] = [flat]
        return {"status": "success", "data": compact}

    try:
        data = await build_details_from_filters(
            q=q or "both",
            game=game,
            team1=team1,
            team2=team2,
            search=search,
            max_results=max_results,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "success", "data": data}


@app.get("/v2/search", tags=["Search"])
async def search(
    q: str = Query(..., min_length=2),
    source: str = Query("finished"),
    game: str = Query("cs2", description="cs2/valorant/r6s/dota2/lol/mlbb"),
) -> Dict[str, Any]:
    source_norm = source.lower().strip()
    canonical = normalize_game(game)
    list_q = "current" if source_norm in {"current", "live", "upcoming", "schedule"} else "finished"
    data, path, _mode, hint, _ttl = await fetch_match_list_data(canonical, list_q, include_unscored_finished=True)
    if source_norm in {"finished", "results"}:
        # Search should be able to find finished-page candidates even when the
        # score is only available after opening the detail page.
        scoped_segments = list(data.get("segments") or [])
    else:
        scoped_segments = filter_segments_for_request(list(data.get("segments") or []), source_norm)
    q_fold = q.casefold()
    matches = []
    for item in scoped_segments:
        haystack = " ".join(
            [
                item.get("raw_text", ""),
                item.get("team1", ""),
                item.get("team2", ""),
                item.get("url", ""),
            ]
        ).casefold()
        if q_fold in haystack:
            matches.append(item)
    return {"status": "success", "data": {"status": 200, "game": canonical, "segments": matches, "count": len(matches)}}


@app.get("/v2/debug/fetch", tags=["Debug"])
async def debug_fetch(
    url: Optional[str] = Query(None, description="Full BO3.gg URL"),
    path: Optional[str] = Query(None, description="BO3.gg path"),
    game: str = Query("cs2", description="Used only when path/url is omitted"),
    q: str = Query("finished", description="current or finished; used only when path/url is omitted"),
) -> Dict[str, Any]:
    if url or path:
        target = url or path or "/matches/finished"
    else:
        try:
            target, _hint, _ttl = match_list_path(normalize_game(game), q.lower().strip())
        except ValueError:
            target = game_prefix(game) + "/matches/finished"
    full_url = normalize_url(target)
    raw = await fetch_html(full_url, ttl=0)
    visible = extract_visible_text(raw)
    anchors = extract_anchors(raw)
    match_anchors = [a for a in anchors if is_match_detail_path(canonical_match_path(urlparse(abs_bo3_url(a.get("href", ""))).path))]
    return {
        "status": "success",
        "url": full_url,
        "game": game_from_path(urlparse(full_url).path),
        "render_mode": response_mode(raw),
        "bytes": len(raw),
        "visible_chars": len(visible),
        "anchor_count": len(anchors),
        "match_anchor_count": len(match_anchors),
        "first_visible_text": visible[:DEBUG_FETCH_CHARS],
        "first_html": raw[:DEBUG_FETCH_CHARS],
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=API_PORT)
