import os

# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID    = int(os.environ.get("API_ID", "38498066"))
API_HASH  = os.environ.get("API_HASH", "c9696114751feacdeb1b4487f5839a1a")

# Optional: restrict /cookies (sets the bot-wide PocketFM session cookies)
# to one Telegram user ID. Leave unset to allow any user (fine for a
# single-operator bot).
ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"]) if os.environ.get("ADMIN_USER_ID") else None

# ── Flask / Render ────────────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", 8000))

# ── GitHub Pages URL ─────────────────────────────────────────────────────────
# Set this after deploying your GitHub Pages site from the repo.
# Example: "https://yourusername.github.io/p-fm-bot"
GITHUB_PAGES_URL = os.environ.get("GITHUB_PAGES_URL", "https://powerofsensei618-hub.github.io/p-fm-bot")   # ← fill after deploy

# ── Bot display ───────────────────────────────────────────────────────────────
RESULTS_PER_PAGE = 5

# ── Welcome images (add your 5 URLs here) ────────────────────────────────────
WELCOME_IMAGES = [
    "https://graph.org/file/a7f25e5a63e4a3f337c63-74ad9194a339ff0d30.jpg",   # Image 1
    "https://graph.org/file/eecec30fb2475822e7c81-609deb3a4c0bfa25e2.jpg",   # Image 2
    "https://graph.org/file/91ab0e15e4bbac16e53e4-b362b6c9fbf421d385.jpg",   # Image 3
    "https://graph.org/file/a7f25e5a63e4a3f337c63-74ad9194a339ff0d30.jpg",   # Image 4
    "https://graph.org/file/2597ffc960b822d3e0d6b-7976d271ae3a1e32fc.jpg",   # Image 5
]
# Fallback banner shown when all image slots are empty
FALLBACK_BANNER = "https://graph.org/file/a7f25e5a63e4a3f337c63-74ad9194a339ff0d30.jpg"
