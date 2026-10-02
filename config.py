import os

# Telegram Bot Config
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")

# Flask
PORT = int(os.environ.get("PORT", 8000))

# PocketFM API
PFM_BASE_URL = "https://api.pocketfm.in/v5"
PFM_HEADERS = {
    "User-Agent": "okhttp/3.12.1",
    "app-version": "7.1.0",
    "Content-Type": "application/json",
    "app-platform": "android",
    "locale": "en",
}

# Bot settings
RESULTS_PER_PAGE = 5

# Welcome images (add your URLs here)
WELCOME_IMAGES = [
    "https://i.imgur.com/placeholder1.jpg",  # Image 1
    "https://i.imgur.com/placeholder2.jpg",  # Image 2
    "https://i.imgur.com/placeholder3.jpg",  # Image 3
    "https://i.imgur.com/placeholder4.jpg",  # Image 4
    "https://i.imgur.com/placeholder5.jpg",  # Image 5
]
