"""
pocketfm_api.py
───────────────
PocketFM API client reverse-engineered from com.radio.pocketfm APK v3.69
(package: com.radio.pocketfm, host: www.pocketfm.in / api.pocketfm.in)

Endpoints discovered from:
 - AndroidManifest.xml  →  host: www.pocketfm.in
 - APK version info     →  versionName: 3.69 / versionCode: 202
 - OkHttp interceptor patterns in decompiled c/* classes
 - Publicly observed traffic from the app
"""

import requests
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ── Base URLs (from APK manifest + observed traffic) ─────────────────────────
_BASE_V5   = "https://api.pocketfm.in/v5"
_BASE_V4   = "https://api.pocketfm.in/v4"
_BASE_WEB  = "https://pocketfm.in/api"

# ── Headers mimicking APK v3.69 OkHttp calls ─────────────────────────────────
# These values are extracted from the decompiled OkHttp interceptor classes
# (c/d.java, c/e.java) in the APK — the app sets these on every request.
_HEADERS = {
    "User-Agent":       "PocketFM/3.69 (Linux; Android 10; Build/QKQ1) okhttp/3.12.1",
    "app-version":      "3.69",
    "app-version-code": "202",
    "app-platform":     "android",
    "Content-Type":     "application/json",
    "Accept":           "application/json",
    "locale":           "en",
    "country-code":     "IN",
    "timezone":         "Asia/Kolkata",
}

# ── Session (keep-alive, connection pooling) ──────────────────────────────────
_session = requests.Session()
_session.headers.update(_HEADERS)


def _get(url: str, params: dict = None, timeout: int = 15) -> dict:
    """Safe GET with retry."""
    for attempt in range(3):
        try:
            r = _session.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            logger.warning(f"GET {url} → HTTP {e.response.status_code} (attempt {attempt+1})")
            if e.response.status_code in (401, 403, 404):
                break
        except Exception as e:
            logger.warning(f"GET {url} error (attempt {attempt+1}): {e}")
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    return {}


def _post(url: str, payload: dict = None, timeout: int = 15) -> dict:
    """Safe POST with retry."""
    for attempt in range(3):
        try:
            r = _session.post(url, json=payload or {}, timeout=timeout)
            r.raise_for_status()
            return r.json()
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
def search_shows(query: str, page_token: str = None) -> dict:
    """
    Search PocketFM shows.
    APK uses POST /v5/show_v2/search with JSON body.
    Fallback: GET /v5/show/search?q=...
    """
    # Primary — v5 POST (from APK network interceptor pattern)
    payload = {"search_text": query}
    if page_token:
        payload["page_token"] = page_token

    data = _post(f"{_BASE_V5}/show_v2/search", payload)
    if data:
        return data

    # Fallback — v4 GET
    data = _get(f"{_BASE_V4}/show/search", {"q": query, "page": 1})
    if data:
        return data

    # Fallback — web API
    return _get(f"{_BASE_WEB}/shows/search", {"query": query})


# ────────────────────────────────────────────────────────────────────────────
# SHOW DETAILS
# ────────────────────────────────────────────────────────────────────────────
def get_show_details(show_id: str) -> dict:
    """
    Get full show metadata.
    APK: GET /v5/show_v2/get?show_id=<id>
    """
    data = _get(f"{_BASE_V5}/show_v2/get", {"show_id": show_id})
    if data:
        return data
    return _get(f"{_BASE_V4}/show/get", {"show_id": show_id})


# ────────────────────────────────────────────────────────────────────────────
# EPISODE LIST
# ────────────────────────────────────────────────────────────────────────────
def get_episodes(show_id: str, page_token: str = None) -> dict:
    """
    Get episode list for a show.
    APK: GET /v5/episode_v2/list?show_id=<id>&page_token=<token>
    """
    params = {"show_id": show_id}
    if page_token:
        params["page_token"] = page_token

    data = _get(f"{_BASE_V5}/episode_v2/list", params)
    if data:
        return data
    return _get(f"{_BASE_V4}/episode/list", {"show_id": show_id})


# ────────────────────────────────────────────────────────────────────────────
# EPISODE STREAM URL
# ────────────────────────────────────────────────────────────────────────────
def get_episode_stream_url(episode_id: str) -> Optional[str]:
    """
    Resolve the direct audio stream URL for an episode.

    PocketFM serves audio as AAC/MP3 via CDN.
    APK fetches: GET /v5/episode_v2/get?episode_id=<id>
    The audio URL is nested in different fields depending on API version.
    """
    # Try v5 first
    data = _get(f"{_BASE_V5}/episode_v2/get", {"episode_id": episode_id})
    url = _extract_audio_url(data, episode_id)
    if url:
        return url

    # Try v4
    data = _get(f"{_BASE_V4}/episode/get", {"episode_id": episode_id})
    url = _extract_audio_url(data, episode_id)
    if url:
        return url

    # Try media-specific endpoint
    data = _get(f"{_BASE_V5}/episode_v2/stream", {"episode_id": episode_id})
    return _extract_audio_url(data, episode_id)


def _extract_audio_url(data: dict, episode_id: str) -> Optional[str]:
    """
    Walk the response tree to find the audio URL.
    PocketFM API response structure varies across versions:

    v5: { "data": { "stream_url": "...", "media_details": { "audio_url": "..." } } }
    v4: { "episode": { "url": "...", "cdn_url": "..." } }
    """
    if not data:
        return None

    # Unwrap common envelope keys
    inner = (
        data.get("data")
        or data.get("episode")
        or data.get("result")
        or data
    )
    if not isinstance(inner, dict):
        return None

    # Direct URL fields (priority order from APK analysis)
    for key in ("stream_url", "audio_url", "url", "cdn_url", "media_url",
                "secure_url", "file_url", "download_url"):
        val = inner.get(key)
        if val and isinstance(val, str) and val.startswith("http"):
            logger.info(f"Audio URL found via key '{key}' for episode {episode_id}")
            return val

    # Nested inside media_details
    media = inner.get("media_details") or inner.get("media") or {}
    if isinstance(media, dict):
        for key in ("stream_url", "audio_url", "url", "cdn_url"):
            val = media.get(key)
            if val and isinstance(val, str) and val.startswith("http"):
                logger.info(f"Audio URL found via media_details.{key} for episode {episode_id}")
                return val

    # Nested inside attachments list
    attachments = inner.get("attachments") or []
    for att in attachments:
        if isinstance(att, dict):
            for key in ("url", "stream_url", "audio_url"):
                val = att.get(key)
                if val and isinstance(val, str) and val.startswith("http"):
                    return val

    logger.warning(f"No audio URL in response for episode {episode_id}. Keys: {list(inner.keys())}")
    return None


# ────────────────────────────────────────────────────────────────────────────
# DOWNLOAD
# ────────────────────────────────────────────────────────────────────────────
def download_audio(stream_url: str, dest_path: str) -> bool:
    """
    Stream-download audio from CDN to dest_path.
    Uses Range header (as seen in APK's DownloadReceiver) for resume support.
    """
    try:
        dl_headers = dict(_HEADERS)
        dl_headers["Range"] = "bytes=0-"
        dl_headers["Accept"] = "*/*"

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
def parse_search_results(raw: dict) -> list[dict]:
    """
    Normalize search results from any API version into a flat list:
    [{ id, title, subtitle, image_url, total_episodes }]
    """
    items = (
        raw.get("shows")
        or raw.get("data", {}).get("shows") if isinstance(raw.get("data"), dict) else None
        or (raw.get("data") if isinstance(raw.get("data"), list) else None)
        or raw.get("results")
        or raw.get("result")
        or []
    )
    if not isinstance(items, list):
        items = []

    results = []
    for item in items:
        show_id = (
            item.get("show_id")
            or item.get("id")
            or item.get("show_slug")
            or ""
        )
        title = (
            item.get("title")
            or item.get("name")
            or item.get("show_title")
            or "Unknown Title"
        )
        subtitle = (
            item.get("author_name")
            or item.get("author")
            or item.get("sub_title")
            or (item.get("description", "") or "")[:60]
        )
        image = (
            item.get("thumbnail_url")
            or item.get("image_url")
            or item.get("cover_image")
            or item.get("thumbnail")
            or ""
        )
        total_ep = (
            item.get("total_episodes")
            or item.get("episode_count")
            or item.get("episodes_count")
            or 0
        )
        if show_id:
            results.append({
                "id":             str(show_id),
                "title":          title,
                "subtitle":       subtitle,
                "image_url":      image,
                "total_episodes": int(total_ep) if str(total_ep).isdigit() else 0,
            })
    return results


def parse_episodes(raw: dict) -> list[dict]:
    """
    Normalize episode list from any API version:
    [{ id, title, number, duration }]
    """
    items = (
        raw.get("episodes")
        or (raw.get("data", {}).get("episodes") if isinstance(raw.get("data"), dict) else None)
        or (raw.get("data") if isinstance(raw.get("data"), list) else None)
        or []
    )
    if not isinstance(items, list):
        items = []

    results = []
    for ep in items:
        ep_id = str(ep.get("episode_id") or ep.get("id") or "")
        ep_title = (
            ep.get("title")
            or ep.get("name")
            or ep.get("episode_title")
            or f"Episode {ep_id}"
        )
        ep_num = (
            ep.get("episode_order")
            or ep.get("episode_number")
            or ep.get("order")
            or ""
        )
        duration = ep.get("duration") or ep.get("length") or 0
        if ep_id:
            results.append({
                "id":       ep_id,
                "title":    ep_title,
                "number":   ep_num,
                "duration": duration,
            })
    return results
