"""
pocketfm_api.py
───────────────
PocketFM API client — talks to the **real** internal API that pocketfm.com's
own website (Next.js, App Router) uses for search and episode listing.

This replaces the earlier version of this file, which guessed at a mobile-app
REST API (api.pocketfm.in /v4 /v5) that turned out to not exist (confirmed
404 on every guessed route). The endpoints below were captured directly from
pocketfm.com's own network traffic and are confirmed working.

How it works
────────────
pocketfm.com is a Next.js app. Instead of a normal REST API, search and the
show/episode list are implemented as **Next.js Server Actions**: the browser
POSTs to the page URL itself (e.g. "/" for search, "/show/<id>" for episode
list) with special headers:
    - Accept: text/x-component
    - next-action: <per-build action hash>
    - next-router-state-tree: <url-encoded JSON describing the current route>
and a JSON array body of the action's arguments.

The response is a React Server Components "Flight" stream: newline-separated
chunks shaped like "<index>:<json-or-reference>". The chunk holding the
actual payload is referenced from chunk "0" (e.g. "a":"$@1" → chunk "1" has
the data). `_parse_rsc_stream()` below decodes that.

Caveat: `next-action` hashes are generated per deployment build of
pocketfm.com. If pocketfm.com redeploys, these hashes can change and this
file will need the same network-capture process repeated (browser dev tools
→ Network tab → trigger a search / open a show → copy the new `next-action`
value and request body shape).
"""

import json
import logging
import re
import time
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

# ── Base URL ───────────────────────────────────────────────────────────────
_BASE_WEB = "https://pocketfm.com"

# ── Captured Next.js Server Action hashes (pocketfm.com build) ──────────────
# Search box on the homepage → POST https://pocketfm.com/
_ACTION_SEARCH = "408978df9ec2be478467a61e5211d591d404284654"
# "Load more episodes" on a show page → POST https://pocketfm.com/show/<id>
_ACTION_EPISODES = "40fcf5bff259b98f5b39b1cba3bff405dc326aa82f"

# ── Headers mimicking a real desktop browser hitting pocketfm.com ───────────
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "Accept": "text/x-component",
    "Content-Type": "text/plain;charset=UTF-8",
    "Origin": "https://pocketfm.com",
}

_session = requests.Session()
_session.headers.update(_HEADERS)


# ─────────────────────────────────────────────────────────────────────────────
# Next.js router-state-tree builders
# ─────────────────────────────────────────────────────────────────────────────
def _encode_tree(tree) -> str:
    """JSON-encode + URL-encode exactly like the browser's encodeURIComponent
    (which, unlike Python's default quote(), leaves !~*'() unescaped)."""
    return quote(json.dumps(tree, separators=(",", ":")), safe="!~*'()")


def _router_tree_home() -> str:
    tree = ["", {"children": ["(home)", {"children": ["__PAGE__", {}, None, None, 4096]},
                               None, None, 4096]}, None, None, 4112]
    return _encode_tree(tree)


def _router_tree_show(show_id: str) -> str:
    tree = ["", {"children": ["(show-episode)", {"children": ["show", {"children": [
        ["slug", show_id, "c", None],
        {"children": ["__PAGE__", {}, None, None, 4096]}, None, None, 4096
    ]}, None, None, 4096]}, None, None, 4096]}, None, None, 4112]
    return _encode_tree(tree)


# ─────────────────────────────────────────────────────────────────────────────
# RSC ("Flight") stream parsing
# ─────────────────────────────────────────────────────────────────────────────
_CHUNK_RE = re.compile(r"^([0-9a-fA-F]+):(.*)$")


def _parse_rsc_stream(text: str) -> dict:
    """Parse a Next.js Flight response into {chunk_index: parsed_json}."""
    chunks = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _CHUNK_RE.match(line)
        if not m:
            continue
        idx, payload = m.group(1), m.group(2)
        try:
            chunks[idx] = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            chunks[idx] = payload
    return chunks


def _rsc_post(path: str, action: str, router_state_tree: str, payload: list, referer: str) -> dict:
    """POST a Next.js Server Action call and return its parsed Flight chunks."""
    url = f"{_BASE_WEB}{path}"
    headers = {
        "next-action": action,
        "next-router-state-tree": router_state_tree,
        "Referer": referer,
    }
    body = json.dumps(payload, separators=(",", ":"))

    for attempt in range(3):
        try:
            r = _session.post(url, headers=headers, data=body, timeout=15)
            r.raise_for_status()
            return _parse_rsc_stream(r.text)
        except requests.exceptions.HTTPError as e:
            logger.warning(f"POST {url} → HTTP {e.response.status_code} (attempt {attempt+1})")
            if e.response.status_code in (401, 403, 404):
                break
        except Exception as e:
            logger.warning(f"POST {url} error (attempt {attempt+1}): {e}")
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    return {}


# ────────────────────────────────────────────────────────────────────────────
# SEARCH
# ────────────────────────────────────────────────────────────────────────────
def search_shows(query: str) -> list:
    """
    Search PocketFM shows via pocketfm.com's homepage search action.
    Returns the raw list of entity dicts (as sent by pocketfm.com) —
    pass to parse_search_results() to normalize.
    """
    chunks = _rsc_post(
        "/",
        _ACTION_SEARCH,
        _router_tree_home(),
        [{"queryString": query}],
        referer=f"{_BASE_WEB}/",
    )
    data = chunks.get("1")
    return data if isinstance(data, list) else []


# ────────────────────────────────────────────────────────────────────────────
# SHOW DETAILS
# ────────────────────────────────────────────────────────────────────────────
def get_show_details(show_id: str) -> dict:
    """
    No standalone "show details" endpoint has been captured yet — pocketfm.com
    bundles the show title into the episode-list response instead (see
    get_episodes()'s "show_title" field). Kept as a safe no-op so any caller
    expecting a dict doesn't crash; callers should prefer the show_title from
    get_episodes().
    """
    return {}


# ────────────────────────────────────────────────────────────────────────────
# EPISODE LIST
# ────────────────────────────────────────────────────────────────────────────
def get_episodes(show_id: str, curr_ptr: int = 0, page_size: int = 50) -> dict:
    """
    Fetch the episode list for a show via pocketfm.com's show-page action.
    Returns the raw "result" dict (show_title, episodes_count, stories, ...).
    """
    chunks = _rsc_post(
        f"/show/{show_id}",
        _ACTION_EPISODES,
        _router_tree_show(show_id),
        [{"showId": show_id, "campaignName": "", "currPtr": curr_ptr, "pageSize": page_size}],
        referer=f"{_BASE_WEB}/show/{show_id}",
    )
    data = chunks.get("1")
    if isinstance(data, dict):
        return data.get("result") or {}
    return {}


# ────────────────────────────────────────────────────────────────────────────
# DOWNLOAD
# ────────────────────────────────────────────────────────────────────────────
def download_audio(stream_url: str, dest_path: str) -> bool:
    """
    Stream-download audio from CDN to dest_path.
    Works for direct audio file URLs (mp3/aac). NOTE: if an episode's only
    available URL is an HLS playlist (.m3u8), this will save the raw
    playlist text, not a playable audio file — PocketFM's web app embeds
    direct media_url for free episodes, so this path is for that case.
    """
    try:
        dl_headers = {
            "User-Agent": _HEADERS["User-Agent"],
            "Accept": "*/*",
            "Range": "bytes=0-",
        }
        with requests.get(
            stream_url,
            headers=dl_headers,
            stream=True,
            timeout=120,
            allow_redirects=True,
        ) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception as e:
        logger.error(f"download_audio failed [{stream_url[:60]}]: {e}")
        return False


# ────────────────────────────────────────────────────────────────────────────
# PARSE HELPERS
# ────────────────────────────────────────────────────────────────────────────
def parse_search_results(raw: list) -> list[dict]:
    """
    Normalize pocketfm.com search results into a flat list:
    [{ id, title, subtitle, image_url, total_episodes }]

    Real shape (captured from pocketfm.com):
    {"entity_id": "...", "entity_type": "show", "title": "...", "plays": "...",
     "avg_rating": 4.7, "image_url": "...", "creator_name": "...",
     "genre_searchable": [...], "slugify_path": "..."}
    """
    items = raw if isinstance(raw, list) else []
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("entity_type") and item.get("entity_type") != "show":
            continue

        show_id = item.get("entity_id") or item.get("show_id") or item.get("id") or ""
        if not show_id:
            continue

        title = item.get("title") or item.get("show_title") or "Unknown Title"

        genres = item.get("genre_searchable") or []
        subtitle = item.get("creator_name") or (", ".join(genres) if genres else "")

        image = item.get("image_url") or item.get("thumbnail_url") or ""

        results.append({
            "id":             str(show_id),
            "title":          title,
            "subtitle":       subtitle,
            "image_url":      image,
            # Episode count isn't included in search results on pocketfm.com —
            # it's only known once the show is opened (get_episodes()).
            "total_episodes": 0,
        })
    return results


def parse_episodes(raw: dict) -> list[dict]:
    """
    Normalize pocketfm.com's episode list ("stories") into a flat list:
    [{ id, title, number, duration, media_url, hls_url, is_locked }]

    Real shape (captured from pocketfm.com, per story):
    {"story_id": "...", "story_title": "...", "seq_number": 21,
     "story_duration": 800, "media_url": "", "hls_url": "", "is_locked": true,
     "coins_required": 11, ...}
    """
    items = raw.get("stories") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        items = []

    results = []
    for ep in items:
        if not isinstance(ep, dict):
            continue
        ep_id = str(ep.get("story_id") or ep.get("episode_id") or ep.get("id") or "")
        if not ep_id:
            continue

        title = ep.get("story_title") or ep.get("title") or f"Episode {ep_id}"
        number = ep.get("seq_number") or ep.get("natural_sequence_number") or ""
        duration = ep.get("story_duration") or ep.get("duration") or 0

        results.append({
            "id":         ep_id,
            "title":      title,
            "number":     number,
            "duration":   duration,
            # Free episodes carry a direct URL here; paid/locked ones are
            # blank until unlocked — see cb_download in bot.py.
            "media_url":  ep.get("media_url") or "",
            "hls_url":    ep.get("hls_url") or "",
            "is_locked":  bool(ep.get("is_locked")),
        })
    return results
