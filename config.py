import os

# ── Telegram ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID    = int(os.environ.get("API_ID", "0"))
API_HASH  = os.environ.get("API_HASH", "")

# ── Flask / Render ────────────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", 8000))

# ── GitHub Pages URL ─────────────────────────────────────────────────────────
# Set this after deploying your GitHub Pages site from the repo.
# Example: "https://yourusername.github.io/p-fm-bot"
GITHUB_PAGES_URL = os.environ.get("GITHUB_PAGES_URL", "")   # ← fill after deploy

# ── Bot display ───────────────────────────────────────────────────────────────
RESULTS_PER_PAGE = 5

# ── Welcome images (add your 5 URLs here) ────────────────────────────────────
WELCOME_IMAGES = [
    "",   # Image 1
    "",   # Image 2
    "",   # Image 3
    "",   # Image 4
    "",   # Image 5
]
# Fallback banner shown when all image slots are empty
FALLBACK_BANNER = "https://telegra.ph/file/placeholder.jpg"
