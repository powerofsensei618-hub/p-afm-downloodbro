import requests
import logging
from config import PFM_BASE_URL, PFM_HEADERS

logger = logging.getLogger(__name__)


def search_shows(query: str, page_token: str = None) -> dict:
    """Search PocketFM shows/stories by keyword."""
    try:
        payload = {
            "search_text": query,
            "page_token": page_token,
        }
        r = requests.post(
            f"{PFM_BASE_URL}/show_v2/search",
            json=payload,
            headers=PFM_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"search_shows error: {e}")
        return {}


def get_show_details(show_id: str) -> dict:
    """Get full details of a show including episode list."""
    try:
        r = requests.get(
            f"{PFM_BASE_URL}/show_v2/get",
            params={"show_id": show_id},
            headers=PFM_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"get_show_details error: {e}")
        return {}


def get_episodes(show_id: str, page_token: str = None) -> dict:
    """Get episode list of a show."""
    try:
        params = {"show_id": show_id}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(
            f"{PFM_BASE_URL}/episode_v2/list",
            params=params,
            headers=PFM_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"get_episodes error: {e}")
        return {}


def get_episode_stream_url(episode_id: str) -> str | None:
    """
    Get the streamable/downloadable audio URL for an episode.
    PocketFM serves audio as AAC/MP3 streams.
    """
    try:
        r = requests.get(
            f"{PFM_BASE_URL}/episode_v2/get",
            params={"episode_id": episode_id},
            headers=PFM_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()

        ep = data.get("data", {}) or data.get("episode", {}) or {}

        # Try multiple known fields where audio URL lives
        for key in ("stream_url", "audio_url", "url", "media_url", "cdn_url"):
            url = ep.get(key)
            if url:
                return url

        # Some versions nest it inside media_details
        media = ep.get("media_details", {}) or {}
        for key in ("stream_url", "audio_url", "url"):
            url = media.get(key)
            if url:
                return url

        logger.warning(f"No audio URL found for episode {episode_id}. Response keys: {list(ep.keys())}")
        return None

    except Exception as e:
        logger.error(f"get_episode_stream_url error: {e}")
        return None


def download_audio(stream_url: str, dest_path: str) -> bool:
    """Download audio from stream_url to dest_path."""
    try:
        headers = dict(PFM_HEADERS)
        headers["Range"] = "bytes=0-"
        with requests.get(stream_url, headers=headers, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 64):
                    if chunk:
                        f.write(chunk)
        return True
    except Exception as e:
        logger.error(f"download_audio error: {e}")
        return False


def parse_search_results(raw: dict) -> list[dict]:
    """
    Normalize search results into a consistent list of dicts:
    [{ id, title, subtitle, image_url, total_episodes }]
    """
    results = []
    items = (
        raw.get("shows")
        or raw.get("data", {}).get("shows")
        or raw.get("data")
        or []
    )
    if not isinstance(items, list):
        items = []

    for item in items:
        show_id = item.get("show_id") or item.get("id") or ""
        title = item.get("title") or item.get("name") or "Unknown Title"
        subtitle = item.get("author_name") or item.get("author") or item.get("description", "")[:60]
        image = (
            item.get("thumbnail_url")
            or item.get("image_url")
            or item.get("cover_image")
            or ""
        )
        total_ep = item.get("total_episodes") or item.get("episode_count") or 0
        results.append({
            "id": str(show_id),
            "title": title,
            "subtitle": subtitle,
            "image_url": image,
            "total_episodes": total_ep,
        })
    return results
