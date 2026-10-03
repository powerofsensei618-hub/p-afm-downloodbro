"""
lang_store.py
──────────────
Tiny JSON-file-backed store mapping Telegram user_id → preferred PocketFM
language code. No database dependency needed for this.

NOTE: on most free hosts (e.g. Render's free tier) the filesystem is
ephemeral and this file is wiped on redeploy/restart — preferences persist
for the life of the running instance, not forever. Good enough for a
per-session "set once with /lang" preference; swap for a real DB later if
permanent storage across redeploys is needed.
"""

import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

_STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_lang.json")
_lock = threading.Lock()

# Display name (with flag) for each supported language code.
# Hindi and English are required; a handful of other major Indian
# languages PocketFM publishes content in are included too.
LANGUAGES = {
    "hindi":     "🇮🇳 Hindi",
    "english":   "🇬🇧 English",
    "tamil":     "🎬 Tamil",
    "telugu":    "🎭 Telugu",
    "bengali":   "📿 Bengali",
    "marathi":   "🪔 Marathi",
    "gujarati":  "🦁 Gujarati",
    "punjabi":   "🌾 Punjabi",
}


def _load() -> dict:
    if not os.path.exists(_STORE_PATH):
        return {}
    try:
        with open(_STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"lang_store: failed to read store, starting fresh: {e}")
        return {}


def _save(data: dict) -> None:
    try:
        tmp_path = _STORE_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp_path, _STORE_PATH)
    except Exception as e:
        logger.error(f"lang_store: failed to save store: {e}")


def get_user_lang(user_id: int) -> str | None:
    with _lock:
        data = _load()
    return data.get(str(user_id))


def set_user_lang(user_id: int, lang_code: str) -> None:
    with _lock:
        data = _load()
        data[str(user_id)] = lang_code
        _save(data)
