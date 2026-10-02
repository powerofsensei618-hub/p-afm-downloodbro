"""
main.py — Entry point for Render deployment.
Runs Flask web server (for health checks / keep-alive) alongside the Telegram bot
in a separate thread.
"""

import threading
import logging
import os
from flask import Flask, jsonify
from bot import main as run_bot
from config import PORT

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Flask App ────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)


@flask_app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": "online",
        "service": "PocketFM Downloader Bot",
        "message": "Bot is running 🚀"
    })


@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"}), 200


# ─── Start Bot in Background Thread ──────────────────────────────────────────
def start_bot():
    try:
        logger.info("Starting Telegram bot thread...")
        run_bot()
    except Exception as e:
        logger.exception(f"Bot thread crashed: {e}")


if __name__ == "__main__":
    # Start bot in background thread
    bot_thread = threading.Thread(target=start_bot, daemon=True)
    bot_thread.start()
    logger.info(f"Bot thread started.")

    # Start Flask on main thread
    logger.info(f"Starting Flask on port {PORT}...")
    flask_app.run(host="0.0.0.0", port=PORT, debug=False)
