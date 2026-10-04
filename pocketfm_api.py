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

import http.cookiejar
import json
import logging
import os
import re
import subprocess
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
# cookies.txt support — lets the session ride on a real pocketfm.com login,
# same idea as kukufm-dl's HttpClientPair / kuku.py's MozillaCookieJar.
# NOTE: this makes requests go out as a logged-in user; it does NOT unlock
# coin-locked/paid episodes (that's a purchase gate, not a login gate) — it's
# for session legitimacy (avoiding anonymous-session rate limits etc.), and
# for any content that genuinely only requires being logged in.
# ─────────────────────────────────────────────────────────────────────────────
def load_cookies_file(path: str) -> int:
    """
    Load cookies into the shared session from either:
      - a standard Netscape cookies.txt (what browser "export cookies"
        extensions produce — tab-separated, works with MozillaCookieJar), or
      - a raw `document.cookie`-style single line: "name=value; name2=value2"
    Returns the number of cookies loaded (0 on failure).
    """
    try:
        jar = http.cookiejar.MozillaCookieJar()
        jar.load(path, ignore_discard=True, ignore_expires=True)
        _session.cookies.update(jar)
        count = len(list(jar))
        if count:
            logger.info(f"Loaded {count} cookies from Netscape-format {path}")
            return count
    except Exception:
        pass  # not Netscape format — try the raw "k=v; k2=v2" fallback below

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        count = _load_cookie_header_string(raw)
        if count:
            logger.info(f"Loaded {count} cookies from raw cookie string in {path}")
        return count
    except Exception as e:
        logger.error(f"load_cookies_file failed for {path}: {e}")
        return 0


def load_cookie_string(raw: str) -> int:
    """Public entry point for loading a pasted raw cookie string (as opposed
    to a cookies.txt file — see load_cookies_file())."""
    return _load_cookie_header_string(raw)


def _load_cookie_header_string(raw: str) -> int:
    count = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for pair in line.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            name, _, value = pair.partition("=")
            name, value = name.strip(), value.strip()
            if name:
                _session.cookies.set(name, value, domain="pocketfm.com")
                count += 1
    return count


def cookie_header_for(domain_hint: str = "") -> str:
    """Build a 'name=value; name2=value2' Cookie header from the loaded
    jar — ffmpeg (used for HLS downloads) doesn't share Python's requests
    cookie jar, so the download path passes this through explicitly."""
    parts = [f"{c.name}={c.value}" for c in _session.cookies]
    return "; ".join(parts)


def has_cookies() -> bool:
    return len(_session.cookies) > 0


def cookie_count() -> int:
    return len(_session.cookies)


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
def _download_direct(stream_url: str, dest_path: str) -> bool:
    """Plain streamed HTTP download — for direct audio file URLs (mp3/aac).
    Uses the shared session so any loaded cookies ride along."""
    dl_headers = {"Accept": "*/*", "Range": "bytes=0-"}
    with _session.get(
        stream_url, headers=dl_headers, stream=True, timeout=120, allow_redirects=True
    ) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
    return True


def _download_hls(stream_url: str, dest_path: str) -> bool:
    """
    Download and remux an HLS (.m3u8) stream into a single playable audio
    file via ffmpeg. A plain HTTP GET on an .m3u8 URL only saves the
    playlist text, not the actual audio — this is why episodes were
    "downloading" a few hundred bytes of garbage before.

    ffmpeg's own HLS demuxer resolves the master/media playlist, fetches
    every .ts segment and concatenates them in one step — equivalent to
    (and more battle-tested than) hand-rolling segment-by-segment
    downloading. -reconnect* flags add resilience against flaky CDN drops
    mid-stream; cookies ride along via an explicit header since ffmpeg
    doesn't share Python's requests cookie jar.
    """
    headers = f"User-Agent: {_HEADERS['User-Agent']}\r\n"
    cookie_hdr = cookie_header_for()
    if cookie_hdr:
        headers += f"Cookie: {cookie_hdr}\r\n"

    cmd = [
        "ffmpeg", "-y",
        "-headers", headers,
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_on_network_error", "1",
        "-reconnect_delay_max", "5",
        "-i", stream_url,
        "-vn",                 # drop video track — this is an audio download
        "-acodec", "libmp3lame",
        "-q:a", "2",
        "-loglevel", "error",
        dest_path,
    ]

    last_err = ""
    for attempt in range(2):  # one retry on transient CDN failures
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        if proc.returncode == 0:
            return True
        last_err = proc.stderr.decode(errors="ignore")[:500]
        if attempt == 0:
            time.sleep(2)
    logger.error(f"ffmpeg failed [{stream_url[:60]}]: {last_err}")
    return False


def tag_audio(file_path: str, title: str, artist: str, album: str, cover_url: str = "") -> None:
    """Write ID3 tags (title/artist/album/cover art) onto a downloaded mp3,
    so it shows up properly in Telegram's audio player and any music app.
    Best-effort — a tagging failure never blocks the download/upload flow."""
    try:
        from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, APIC
    except ImportError:
        logger.warning("mutagen not installed — skipping audio tagging")
        return
    try:
        try:
            tags = ID3(file_path)
        except ID3NoHeaderError:
            tags = ID3()
        tags["TIT2"] = TIT2(encoding=3, text=title)
        tags["TPE1"] = TPE1(encoding=3, text=artist)
        tags["TALB"] = TALB(encoding=3, text=album)
        if cover_url:
            try:
                img = _session.get(cover_url, timeout=15).content
                tags["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=img)
            except Exception as e:
                logger.warning(f"tag_audio: cover fetch failed: {e}")
        tags.save(file_path)
    except Exception as e:
        logger.warning(f"tag_audio failed: {e}")


def download_audio(stream_url: str, dest_path: str) -> bool:
    """
    Download audio from CDN to dest_path (always ends up a playable file at
    dest_path regardless of source format). Picks the right method:
      - .m3u8 (HLS adaptive stream) → ffmpeg download + remux to mp3
      - anything else (direct mp3/aac file)  → plain streamed HTTP GET
    """
    try:
        is_hls = ".m3u8" in stream_url.lower()
        ok = _download_hls(stream_url, dest_path) if is_hls else _download_direct(stream_url, dest_path)
        return ok and os.path.exists(dest_path) and os.path.getsize(dest_path) > 0
    except Exception as e:
        logger.error(f"download_audio failed [{stream_url[:60]}]: {e}")
        return False


# ────────────────────────────────────────────────────────────────────────────
# LANGUAGE DETECTION
# ────────────────────────────────────────────────────────────────────────────
# PocketFM's search API doesn't return an explicit language field — only the
# show title does, either via an explicit "(Language)" suffix (used mainly
# for alt-language versions of a show, e.g. "My Vampire System (English)")
# or implicitly via the script the title is written in. This is a best-effort
# heuristic, not a guarantee — titles with no non-Latin script and no
# explicit tag default to "hindi" since that's PocketFM's primary catalog.
_LANG_TAG_RE = re.compile(r"\(([a-zA-Z]+)\)\s*$")

_LANG_TAG_MAP = {
    "hindi": "hindi", "english": "english", "tamil": "tamil",
    "telugu": "telugu", "bengali": "bengali", "marathi": "marathi",
    "gujarati": "gujarati", "punjabi": "punjabi", "malayalam": "malayalam",
    "kannada": "kannada",
}

# (unicode block start, end, language code)
_SCRIPT_RANGES = [
    (0x0900, 0x097F, "hindi"),      # Devanagari
    (0x0B80, 0x0BFF, "tamil"),      # Tamil
    (0x0C00, 0x0C7F, "telugu"),     # Telugu
    (0x0980, 0x09FF, "bengali"),    # Bengali
    (0x0A80, 0x0AFF, "gujarati"),   # Gujarati
    (0x0A00, 0x0A7F, "punjabi"),    # Gurmukhi
    (0x0D00, 0x0D7F, "malayalam"),  # Malayalam
    (0x0C80, 0x0CFF, "kannada"),    # Kannada
]


def _script_lang(text: str) -> Optional[str]:
    """Language code from the first non-Latin script character found, or
    None if the text is pure Latin script (no signal either way)."""
    for ch in text:
        cp = ord(ch)
        for lo, hi, lang in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                return lang
    return None


def detect_language(title: str) -> str:
    """
    Best-effort language code for a SHOW, from its title. See module notes
    above. Defaults to "hindi" when there's no tag/script signal, since
    that's PocketFM's primary, majority-untagged catalog language.
    """
    if not title:
        return "hindi"
    m = _LANG_TAG_RE.search(title.strip())
    if m and m.group(1).lower() in _LANG_TAG_MAP:
        return _LANG_TAG_MAP[m.group(1).lower()]
    return _script_lang(title) or "hindi"


def query_language(query: str) -> Optional[str]:
    """
    Best-effort language code for a SEARCH QUERY the user typed. Unlike
    detect_language(), this returns None (no guess) for plain Latin-script
    queries like "Avatar" — a Latin query gives no real language signal, so
    callers should fall back to the user's set /lang preference instead of
    assuming "hindi". Only a query actually written in a distinct Indic
    script (e.g. Devanagari, Tamil, ...) returns a language here.
    """
    if not query:
        return None
    return _script_lang(query)


# ────────────────────────────────────────────────────────────────────────────
# SHOW ID / URL PARSING
# ────────────────────────────────────────────────────────────────────────────
# PocketFM show IDs are 40-char hex strings, e.g.
#   https://pocketfm.com/show/5e8f749e24f0c94a6fc09f375b9e3edc1bb5d71a
#   https://pocketfm.com/the-beast-guru/460702ff409f87d178f7821534c645030c2db71e
# This matches the raw ID alone too, so it works whether the user pastes a
# full show URL or just the ID off the end of one.
_SHOW_ID_RE = re.compile(r"\b([0-9a-fA-F]{40})\b")


def extract_show_id(text: str) -> Optional[str]:
    """Pull a 40-hex-char PocketFM show ID out of a pasted URL or raw ID."""
    if not text:
        return None
    m = _SHOW_ID_RE.search(text.strip())
    return m.group(1) if m else None


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
            # Usually the show cover, repeated per story — used as a
            # fallback show image when a show wasn't reached via search
            # (e.g. opened directly by ID/URL via /download).
            "image_url":  ep.get("image_url") or "",
        })
    return results
