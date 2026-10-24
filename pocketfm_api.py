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
import random
import string
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

# ── Base URL ───────────────────────────────────────────────────────────────
_BASE_WEB = "https://pocketfm.com"

# ── Captured Next.js Server Action hashes (pocketfm.com build) ──────────────
# Search box on the homepage → POST https://pocketfm.com/
# (override with the PFM_ACTION_SEARCH / PFM_ACTION_EPISODES env vars if pocketfm.com
# redeploys and the hashes change - no code edit needed)
_ACTION_SEARCH = os.environ.get("PFM_ACTION_SEARCH", "408978df9ec2be478467a61e5211d591d404284654")
# "Load more episodes" on a show page → POST https://pocketfm.com/show/<id>
_ACTION_EPISODES = os.environ.get("PFM_ACTION_EPISODES", "40fcf5bff259b98f5b39b1cba3bff405dc326aa82f")

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


def _rsc_post(path: str, action: str, router_state_tree: str, payload: list, referer: str,
              extra_headers: Optional[dict] = None, cookies: Optional[dict] = None) -> dict:
    """POST a Next.js Server Action call and return its parsed Flight chunks."""
    url = f"{_BASE_WEB}{path}"
    headers = {
        "next-action": action,
        "next-router-state-tree": router_state_tree,
        "Referer": referer,
    }
    if extra_headers:
        headers.update(extra_headers)
    body = json.dumps(payload, separators=(",", ":"))

    for attempt in range(3):
        try:
            r = _session.post(url, headers=headers, data=body, timeout=20, cookies=cookies)
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


def _rsc_get(path: str, router_state_tree: str, referer: str) -> dict:
    """GET a page the way Next.js client navigation does (header `rsc: 1`) and
    return its parsed Flight chunks. The show page answers with the show data
    (title, total episode count, the first stories WITH their stream URLs)."""
    rnd = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(16))
    url = f"{_BASE_WEB}{path}?_rsc={rnd}"
    headers = {
        "Accept": "*/*",
        "rsc": "1",
        "next-router-state-tree": router_state_tree,
        "next-url": "/",
        "Referer": referer,
    }
    for attempt in range(3):
        try:
            r = _session.get(url, headers=headers, timeout=25)
            r.raise_for_status()
            return _parse_rsc_stream(r.text)
        except requests.exceptions.HTTPError as e:
            logger.warning(f"GET {url} → HTTP {e.response.status_code} (attempt {attempt+1})")
            if e.response.status_code in (401, 403, 404):
                break
        except Exception as e:
            logger.warning(f"GET {url} error (attempt {attempt+1}): {e}")
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    return {}


def _find_show_dict(node) -> Optional[dict]:
    """Walk parsed Flight chunks and return the dict that holds a show's
    "stories" list (it sits deep inside the React-Query dehydrated state)."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            st = cur.get("stories")
            if isinstance(st, list) and st and isinstance(st[0], dict) and st[0].get("story_id"):
                return cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


# ────────────────────────────────────────────────────────────────────────────
# SEARCH
# ────────────────────────────────────────────────────────────────────────────
# ISO code used for the Accept-Language header / language cookies
_LANG_ISO = {
    "hindi": "hi", "english": "en", "tamil": "ta", "telugu": "te", "bengali": "bn",
    "marathi": "mr", "gujarati": "gu", "punjabi": "pa", "malayalam": "ml", "kannada": "kn",
}
# Cookie names a Next.js site commonly reads its locale from. They are only sent on a
# language-hinted search request, so unknown names are harmless.
_LANG_COOKIE_NAMES = ("NEXT_LOCALE", "lang", "language", "locale")


def _lang_hint(lang: Optional[str]):
    """(extra headers, cookies) that ask pocketfm.com for results in `lang`."""
    iso = _LANG_ISO.get((lang or "").lower())
    if not iso:
        return None, None
    headers = {"Accept-Language": f"{iso}-IN,{iso};q=0.9,en;q=0.4"}
    return headers, {name: iso for name in _LANG_COOKIE_NAMES}


def search_shows(query: str, lang: Optional[str] = None) -> list:
    """
    Search PocketFM shows via pocketfm.com's homepage search action.
    Returns the raw list of entity dicts (as sent by pocketfm.com) —
    pass to parse_search_results() to normalize. `lang` (e.g. "tamil") asks the
    site for that language; None = the site's default.
    """
    headers, cookies = _lang_hint(lang)
    chunks = _rsc_post(
        "/",
        _ACTION_SEARCH,
        _router_tree_home(),
        [{"queryString": query}],
        referer=f"{_BASE_WEB}/",
        extra_headers=headers,
        cookies=cookies,
    )
    data = chunks.get("1")
    return data if isinstance(data, list) else []


def search_multi(query: str, lang: Optional[str] = None) -> list:
    """
    Language-aware search. pocketfm.com answers a search with the shows of ONE
    language (English by default), so a Tamil user only ever saw English shows.
    This runs a few variants in parallel and merges them (order kept, duplicates
    removed):
      1. the query asked for `lang`   (language headers/cookies)
      2. the plain query              (the site default - keeps the usual results)
      3. "<query> <Language>"         (titles of alt-language shows end in "(Tamil)" etc.)
    """
    query = (query or "").strip()
    variants = []
    if lang and lang != "all":
        variants.append((query, lang))
    variants.append((query, None))
    if lang and lang != "all" and query_language(query) is None:
        variants.append((f"{query} {lang.capitalize()}", lang))

    def run(v):
        try:
            return search_shows(v[0], v[1])
        except Exception as e:
            logger.warning(f"search variant {v} failed: {e}")
            return []

    with ThreadPoolExecutor(max_workers=len(variants)) as pool:
        batches = list(pool.map(run, variants))

    merged, seen = [], set()
    for batch in batches:
        for item in batch:
            if not isinstance(item, dict):
                continue
            key = item.get("entity_id") or item.get("show_id") or item.get("id")
            if key and key not in seen:
                seen.add(key)
                merged.append(item)
    return merged


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
    Fetch ONE page of the episode list via pocketfm.com's "load more episodes"
    server action. Returns the raw "result" dict (show_title, episodes_count,
    stories, next_ptr, ...). Use fetch_episode_page() for the normalized,
    fallback-protected version.
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


def get_show_page(show_id: str) -> dict:
    """
    The show page itself (GET /show/<id> with `rsc: 1`). Needs no server-action
    hash, so it keeps working after a pocketfm.com redeploy. Returns the show dict
    (show_title, episodes_count, next_ptr, language, stories = the first 10
    episodes with ALL their stream URLs) or {} if the page can't be read.
    """
    chunks = _rsc_get(f"/show/{show_id}", _router_tree_show(show_id), f"{_BASE_WEB}/show/{show_id}")
    for value in chunks.values():
        found = _find_show_dict(value)
        if found:
            return found
    return {}


def _merge_urls(dst: list, src: list) -> None:
    """Copy stream URLs from `src` episodes into `dst` episodes (same story id)
    wherever `dst` has none - the show page carries URLs the action leaves blank."""
    by_id = {e["id"]: e for e in src}
    for ep in dst:
        other = by_id.get(ep["id"])
        if not other:
            continue
        for key, value in other["urls"].items():
            if value and not ep["urls"].get(key):
                ep["urls"][key] = value
        ep["media_url"] = ep["urls"].get("media_url", "")
        ep["hls_url"] = ep["urls"].get("hls_url", "")
        if not ep.get("duration") and other.get("duration"):
            ep["duration"] = other["duration"]


def fetch_episode_page(show_id: str, ptr: int = 0, page_size: int = 50) -> dict:
    """
    One normalized page of episodes starting at episode index `ptr`:
    { title, total, episodes: [...], next_ptr, language, description }

    First page (ptr == 0) is enriched / rescued from the show page, which holds the
    stream URLs of the first episodes and still works when the server-action hash
    went stale.
    """
    raw = get_episodes(show_id, ptr, page_size) or {}
    episodes = parse_episodes(raw)

    page = {}
    if ptr == 0 or not episodes:
        page = get_show_page(show_id) if ptr == 0 else {}
        if page:
            page_eps = parse_episodes(page)
            if not episodes:
                raw, episodes = page, page_eps
            else:
                _merge_urls(episodes, page_eps)

    def _int(*values):
        for v in values:
            try:
                if v not in (None, "", False) and int(v) >= 0:
                    return int(v)
            except (TypeError, ValueError):
                continue
        return 0

    total = _int(raw.get("episodes_count"), raw.get("tab_count"), page.get("episodes_count"), page.get("tab_count"))
    next_ptr = raw.get("next_ptr")
    try:
        next_ptr = int(next_ptr)
    except (TypeError, ValueError):
        next_ptr = None
    if next_ptr is not None and next_ptr < 0:
        next_ptr = None  # -1 = no more pages
    return {
        "title": raw.get("show_title") or page.get("show_title") or "",
        "total": total,
        "episodes": episodes,
        "next_ptr": next_ptr,
        "language": raw.get("language") or page.get("language") or "",
        "description": raw.get("show_desc_en") or page.get("show_desc_en") or "",
    }


def refresh_episode_urls(show_id: str, idx: int, ep: dict) -> bool:
    """Re-read the page that holds episode `idx` and copy any stream URL it carries
    into `ep` (in place). True if `ep` gained at least one URL."""
    try:
        page = fetch_episode_page(show_id, max(0, idx), 5)
    except Exception as e:
        logger.warning(f"refresh_episode_urls failed: {e}")
        return False
    fresh = next((e for e in page["episodes"] if e["id"] == ep["id"]), None)
    if not fresh:
        return False
    before = {k: v for k, v in ep["urls"].items() if v}
    _merge_urls([ep], [fresh])
    return {k: v for k, v in ep["urls"].items() if v} != before


# ────────────────────────────────────────────────────────────────────────────
# DOWNLOAD
# ────────────────────────────────────────────────────────────────────────────
_CDN_REFERER = "https://pocketfm.com/"


def _cdn_headers() -> dict:
    """Headers the CDNs expect (they hotlink-check Referer/Origin)."""
    h = {
        "User-Agent": _HEADERS["User-Agent"],
        "Accept": "*/*",
        "Referer": _CDN_REFERER,
        "Origin": "https://pocketfm.com",
    }
    cookie_hdr = cookie_header_for()
    if cookie_hdr:
        h["Cookie"] = cookie_hdr
    return h


def _download_direct(stream_url: str, dest_path: str) -> bool:
    """Plain streamed HTTP download — for direct audio file URLs (mp3/aac).
    Uses the shared session so any loaded cookies ride along."""
    dl_headers = {"Accept": "*/*", "Range": "bytes=0-", "Referer": _CDN_REFERER}
    with _session.get(
        stream_url, headers=dl_headers, stream=True, timeout=120, allow_redirects=True
    ) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
    return True


def _ffmpeg_audio(stream_url: str, dest_path: str, timeout: int = 1200) -> bool:
    """
    Let ffmpeg read an HLS (.m3u8) / DASH (.mpd) / any other stream URL and write
    ONLY its audio to `dest_path` as mp3 (the video track, if any, is dropped).

    ffmpeg's own demuxers resolve master playlists / manifests, fetch every segment
    and join them in one step; -reconnect* adds resilience against CDN drops, and
    the Referer/Origin/Cookie headers ride along on every segment request.
    """
    headers = "".join(f"{k}: {v}\r\n" for k, v in _cdn_headers().items())

    cmd = [
        "ffmpeg", "-y",
        "-headers", headers,
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_on_network_error", "1",
        "-reconnect_delay_max", "5",
        "-i", stream_url,
        "-vn",                 # drop video track — this is an audio download
        "-map", "0:a:0?",      # first audio stream (if the stream has one)
        "-acodec", "libmp3lame",
        "-q:a", "2",
        "-loglevel", "error",
        dest_path,
    ]

    last_err = ""
    for attempt in range(2):  # one retry on transient CDN failures
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last_err = "ffmpeg timed out"
            break
        if proc.returncode == 0 and os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            return True
        last_err = proc.stderr.decode(errors="ignore")[:500]
        if attempt == 0:
            time.sleep(2)
    logger.error(f"ffmpeg failed [{stream_url[:70]}]: {last_err}")
    return False


def _download_hls(stream_url: str, dest_path: str) -> bool:
    """Download + remux an HLS (.m3u8) or DASH (.mpd) stream into one mp3 file."""
    return _ffmpeg_audio(stream_url, dest_path)


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
      - .m3u8 (HLS) / .mpd (DASH) → ffmpeg download + remux to mp3
      - anything else (direct mp3/aac file)  → plain streamed HTTP GET
    """
    try:
        low = stream_url.lower().split("?")[0]
        streamed = low.endswith(".m3u8") or low.endswith(".mpd") or ".m3u8" in low or ".mpd" in low
        ok = _download_hls(stream_url, dest_path) if streamed else _download_direct(stream_url, dest_path)
        return ok and os.path.exists(dest_path) and os.path.getsize(dest_path) > 0
    except Exception as e:
        logger.error(f"download_audio failed [{stream_url[:60]}]: {e}")
        return False


# ── Stream selection ─────────────────────────────────────────────────────────
# Per story pocketfm.com sends several URLs. Observed on a real show page:
#   video_url               unencrypted HLS (.m3u8) of the episode            → usable
#   media_url               HLS master; on some shows the SAME url for every
#                           episode (a teaser/placeholder)                    → last resort
#   hls_url                 ".../mock_mp3/hls_adaptive_non_encrypted_mock.m3u8"
#                           (a placeholder, never the episode)                → skipped
#   media_url_enc(_std)     DASH (.mpd) in a "drm-aac" folder - Widevine
#                           encrypted → tried only if its manifest has no
#                           <ContentProtection>
#   media_url_enc_fairplay  FairPlay HLS                                      → never usable
_PLACEHOLDER_MARKERS = ("mock",)
_DRM_MANIFEST_MARKERS = ("<contentprotection", "edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",
                         "9a04f079-9840-4286-ab92-e65be0885f95", "skd://",
                         "com.apple.streamingkeydelivery", "method=sample-aes")


def shared_urls(episodes) -> set:
    """Stream URLs that more than one episode points at (= placeholders)."""
    seen, shared = {}, set()
    eps = list(episodes or [])
    if len(eps) < 3:
        return shared
    for ep in eps:
        for key in ("media_url", "video_url", "hls_url"):
            u = (ep.get("urls") or {}).get(key) or ""
            if u:
                seen[u] = seen.get(u, 0) + 1
    for u, n in seen.items():
        if n >= 2:
            shared.add(u)
    return shared


def stream_candidates(ep: dict, shared=()) -> list:
    """Every URL worth trying for this episode, best first: [(url, label)]."""
    urls = ep.get("urls") or {}
    if not urls:  # episode dict from an older parse
        urls = {"media_url": ep.get("media_url", ""), "hls_url": ep.get("hls_url", "")}
    shared = set(shared or ())

    order = ["video_url", "media_url", "hls_url", "share_media_url",
             "media_url_enc_std", "media_url_enc"]
    primary, last = [], []
    seen = set()
    for key in order:
        u = (urls.get(key) or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        if any(m in u.lower() for m in _PLACEHOLDER_MARKERS):
            continue
        (last if u in shared else primary).append((u, key))
    return primary + last


def _manifest_text(url: str) -> tuple:
    """(status_code, text) of a playlist/manifest, or (0, '') on a network error."""
    try:
        r = _session.get(url, headers=_cdn_headers(), timeout=25)
        return r.status_code, (r.text if r.status_code == 200 else "")
    except Exception as e:
        logger.warning(f"manifest fetch failed [{url[:70]}]: {e}")
        return 0, ""


def probe_duration(path: str) -> Optional[float]:
    """Length in seconds of an audio file (ffprobe), None if unknown."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, timeout=60,
        )
        return float(out.stdout.decode().strip())
    except Exception:
        return None


def download_episode_audio(ep: dict, dest_path: str, shared=()) -> tuple:
    """
    Get the audio of one episode into `dest_path` (mp3) by trying every stream URL
    pocketfm.com gave for it (HLS, DASH, direct file) until one produces real audio.

    A result is only accepted when its length matches the episode's length (±40%),
    so a teaser / placeholder stream is never mistaken for the episode.

    Returns (ok, reason, source):
      ok      True on success
      reason  "" | "drm" (only DRM-encrypted streams exist) | "none" (no stream URL)
              | "failed" (URLs exist but none could be downloaded)
      source  which field worked (e.g. "video_url"), for logging
    """
    candidates = stream_candidates(ep, shared)
    if not candidates:
        return False, "none", ""

    expected = float(ep.get("duration") or 0)
    saw_drm = False

    for url, label in candidates:
        low = url.lower().split("?")[0]
        is_dash = low.endswith(".mpd")
        is_hls = low.endswith(".m3u8")
        try:
            if is_dash or is_hls:
                status, text = _manifest_text(url)
                if status == 200 and any(m in text.lower() for m in _DRM_MANIFEST_MARKERS):
                    logger.info(f"[{label}] DRM protected, skipped: {url[:80]}")
                    saw_drm = True
                    continue
                if status in (401, 403, 404):
                    logger.info(f"[{label}] HTTP {status}, skipped: {url[:80]}")
                    continue
                ok = _ffmpeg_audio(url, dest_path)
            else:
                ok = _download_direct(url, dest_path)
        except Exception as e:
            logger.warning(f"[{label}] download error: {e}")
            ok = False

        if not ok or not os.path.exists(dest_path) or os.path.getsize(dest_path) < 2048:
            _silent_remove(dest_path)
            continue

        got = probe_duration(dest_path)
        if expected > 20 and got:
            ratio = got / expected
            if ratio < 0.6 or ratio > 1.4:
                logger.info(f"[{label}] length {got:.0f}s != episode {expected:.0f}s, rejected")
                _silent_remove(dest_path)
                continue
        return True, "", label

    return False, ("drm" if saw_drm else "failed"), ""


def _silent_remove(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def shrink_to_limit(path: str, limit_mb: float = 49.0) -> bool:
    """Re-encode an mp3 at a lower bitrate until it fits Telegram's bot upload
    limit. True if the file is now small enough."""
    if os.path.getsize(path) <= limit_mb * 1024 * 1024:
        return True
    for bitrate, channels in (("96k", "2"), ("64k", "1"), ("40k", "1"), ("24k", "1")):
        tmp = path + ".small.mp3"
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-vn", "-ac", channels, "-b:a", bitrate,
             "-loglevel", "error", tmp],
            capture_output=True, timeout=1800,
        )
        if proc.returncode == 0 and os.path.exists(tmp):
            os.replace(tmp, path)
            if os.path.getsize(path) <= limit_mb * 1024 * 1024:
                return True
        else:
            _silent_remove(tmp)
            break
    return os.path.getsize(path) <= limit_mb * 1024 * 1024


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


_LANG_TAG_ANY_RE = re.compile(r"\(([a-zA-Z]+)\)")


def guess_language(title: str) -> Optional[str]:
    """
    Like detect_language() but honest: None when the title gives no signal (no
    "(Tamil)" style tag, no Indic script). Used for ranking search results so an
    untagged show is not wrongly treated as Hindi.
    """
    if not title:
        return None
    tags = [m.lower() for m in _LANG_TAG_ANY_RE.findall(title)]
    for tag in reversed(tags):
        if tag in _LANG_TAG_MAP:
            return _LANG_TAG_MAP[tag]
    return _script_lang(title)


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


_URL_FIELDS = (
    "video_url", "media_url", "hls_url", "share_media_url",
    "media_url_enc_std", "media_url_enc", "media_url_enc_fairplay",
)


def parse_episodes(raw: dict) -> list[dict]:
    """
    Normalize pocketfm.com's episode list ("stories") into a flat list:
    [{ id, title, number, duration, urls, media_url, hls_url, coins_required,
       is_locked, image_url }]

    `urls` keeps EVERY stream URL the site sends for the story (see _URL_FIELDS) so
    stream_candidates() can try them all instead of only media_url / hls_url.
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
        try:
            duration = float(ep.get("story_duration") or ep.get("duration") or ep.get("video_duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        try:
            coins = int(ep.get("coins_required") or 0)
        except (TypeError, ValueError):
            coins = 0

        urls = {k: (ep.get(k) or "") for k in _URL_FIELDS}
        results.append({
            "id":            ep_id,
            "title":         title,
            "number":        number,
            "duration":      duration,
            "urls":          urls,
            "media_url":     urls["media_url"],
            "hls_url":       urls["hls_url"],
            "coins_required": coins,
            "is_locked":     bool(ep.get("is_locked")) or coins > 0,
            # Usually the show cover, repeated per story — used as a
            # fallback show image when a show wasn't reached via search
            # (e.g. opened directly by ID/URL via /download).
            "image_url":     ep.get("image_url") or "",
        })
    return results
