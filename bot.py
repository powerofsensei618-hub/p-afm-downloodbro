"""
bot.py — PocketFM Downloader Telegram Bot
Uses PocketFM API reverse-engineered from com.radio.pocketfm APK v3.69
"""

import os
import logging
import random
import asyncio
import tempfile

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

from config import (
    BOT_TOKEN,
    WELCOME_IMAGES,
    FALLBACK_BANNER,
    RESULTS_PER_PAGE,
    GITHUB_PAGES_URL,
)
import pocketfm_api as pfm

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── In-memory session state ───────────────────────────────────────────────────
# uid → { query, results, page, current_show: { id, title, episodes, ep_page } }
user_state: dict[int, dict] = {}


# ─────────────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user  = update.effective_user
    photo = next((u for u in WELCOME_IMAGES if u), FALLBACK_BANNER)

    pages_line = (
        f"\n🌐 *Web Player:* [Open Here]({GITHUB_PAGES_URL})\n"
        if GITHUB_PAGES_URL else ""
    )

    caption = (
        f"🎙️ *Welcome, {user.first_name}!*\n\n"
        "✨ Search & download any audio story, podcast or audiobook\n"
        "from *Pocket FM* — instantly, for free.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 *How to use:*\n"
        "  Just type the name of any show below.\n\n"
        "📥 *Tap a result* → pick an episode → download!\n"
        f"{pages_line}"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 *Try searching:*\n"
        "  • `Love Story`\n"
        "  • `Horror Night`\n"
        "  • `Motivational`\n\n"
        "🚀 *Type your search below!*"
    )

    try:
        await update.message.reply_photo(
            photo=photo,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN)


# ─────────────────────────────────────────────────────────────────────────────
# Text search handler
# ─────────────────────────────────────────────────────────────────────────────
async def handle_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    if not query:
        return

    uid = update.effective_user.id
    wait = await update.message.reply_text(
        f"🔍 Searching *{query}*…", parse_mode=ParseMode.MARKDOWN
    )

    loop = asyncio.get_event_loop()
    raw     = await loop.run_in_executor(None, pfm.search_shows, query)
    results = pfm.parse_search_results(raw)

    await wait.delete()

    if not results:
        await update.message.reply_text(
            f"😕 No results for *{query}*.\nTry a different keyword.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    user_state[uid] = {"query": query, "results": results, "page": 0}
    await _send_results(update, ctx, uid, edit=False)


# ─────────────────────────────────────────────────────────────────────────────
# Paginated show results
# ─────────────────────────────────────────────────────────────────────────────
async def _send_results(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    uid: int,
    edit: bool = False,
):
    s       = user_state[uid]
    results = s["results"]
    page    = s["page"]
    query   = s["query"]
    total   = len(results)
    si      = page * RESULTS_PER_PAGE
    ei      = min(si + RESULTS_PER_PAGE, total)
    page_r  = results[si:ei]
    tpages  = (total + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE

    buttons = []
    for i, show in enumerate(page_r, start=si + 1):
        ep = f" · {show['total_episodes']} eps" if show["total_episodes"] else ""
        lbl = f"{i}. {show['title']}{ep}"
        buttons.append([InlineKeyboardButton(lbl[:64], callback_data=f"show:{show['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data="pg:prev"))
    if ei < total:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data="pg:next"))
    if nav:
        buttons.append(nav)

    markup = InlineKeyboardMarkup(buttons)
    text = (
        f"🎵 Results for: `{query}`\n"
        f"📄 Page {page+1}/{tpages} · {total} shows\n\n"
        "👇 Tap a show:"
    )
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
        )


# ─────────────────────────────────────────────────────────────────────────────
# Callbacks: show list pagination
# ─────────────────────────────────────────────────────────────────────────────
async def cb_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    s   = user_state.get(uid)
    if not s:
        await q.answer("Session expired — search again.", show_alert=True)
        return
    direction = q.data.split(":")[1]
    if direction == "next":
        s["page"] += 1
    else:
        s["page"] = max(0, s["page"] - 1)
    await _send_results(update, ctx, uid, edit=True)


# ─────────────────────────────────────────────────────────────────────────────
# Callback: show selected → fetch episodes
# ─────────────────────────────────────────────────────────────────────────────
async def cb_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    show_id = q.data.split(":", 1)[1]
    uid     = q.from_user.id

    await q.edit_message_text("⏳ Fetching episodes…")

    loop    = asyncio.get_event_loop()
    sh_raw  = await loop.run_in_executor(None, pfm.get_show_details, show_id)
    ep_raw  = await loop.run_in_executor(None, pfm.get_episodes, show_id)

    sh_data = sh_raw.get("data") or sh_raw.get("show") or {}
    if isinstance(sh_data, list):
        sh_data = sh_data[0] if sh_data else {}
    title = sh_data.get("title") or sh_data.get("name") or "Unknown Show"

    episodes = pfm.parse_episodes(ep_raw)

    if not episodes:
        await q.edit_message_text(
            f"😕 No episodes found for *{title}*.\n"
            "This show may require a login or be unavailable.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if uid not in user_state:
        user_state[uid] = {}
    user_state[uid]["current_show"] = {
        "id":       show_id,
        "title":    title,
        "episodes": episodes,
        "ep_page":  0,
    }
    await _send_episodes(q, uid)


# ─────────────────────────────────────────────────────────────────────────────
# Episode list page
# ─────────────────────────────────────────────────────────────────────────────
async def _send_episodes(q, uid: int):
    cs       = user_state[uid]["current_show"]
    episodes = cs["episodes"]
    page     = cs.get("ep_page", 0)
    title    = cs["title"]
    total    = len(episodes)
    si       = page * RESULTS_PER_PAGE
    ei       = min(si + RESULTS_PER_PAGE, total)
    tpages   = (total + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE

    buttons = []
    for ep in episodes[si:ei]:
        num = f"{ep['number']}. " if ep["number"] else ""
        lbl = f"🎧 {num}{ep['title']}"
        buttons.append([InlineKeyboardButton(lbl[:64], callback_data=f"dl:{ep['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data="ep:prev"))
    if ei < total:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data="ep:next"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔙 Back to results", callback_data="back:search")])

    text = (
        f"📚 *{title}*\n"
        f"📄 Page {page+1}/{tpages} · {total} episodes\n\n"
        "👇 Tap an episode to download:"
    )
    await q.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=ParseMode.MARKDOWN
    )


# ─────────────────────────────────────────────────────────────────────────────
# Callbacks: episode pagination
# ─────────────────────────────────────────────────────────────────────────────
async def cb_ep_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = q.from_user.id
    cs  = user_state.get(uid, {}).get("current_show")
    if not cs:
        await q.answer("Session expired.", show_alert=True)
        return
    direction = q.data.split(":")[1]
    if direction == "next":
        cs["ep_page"] = cs.get("ep_page", 0) + 1
    else:
        cs["ep_page"] = max(0, cs.get("ep_page", 0) - 1)
    await _send_episodes(q, uid)


# ─────────────────────────────────────────────────────────────────────────────
# Callback: back to search results
# ─────────────────────────────────────────────────────────────────────────────
async def cb_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    await _send_results(update, ctx, uid, edit=True)


# ─────────────────────────────────────────────────────────────────────────────
# Callback: download episode
# ─────────────────────────────────────────────────────────────────────────────
async def cb_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q          = update.callback_query
    await q.answer("Wait... Im Downloading ✨", show_alert=False)
    episode_id = q.data.split(":", 1)[1]
    uid        = q.from_user.id

    cs         = user_state.get(uid, {}).get("current_show", {})
    show_title = cs.get("title", "Pocket FM")
    ep_title   = next(
        (ep["title"] for ep in cs.get("episodes", []) if ep["id"] == episode_id),
        "Episode",
    )

    prog = await q.message.reply_text(
        f"⏳ *Wait... Im Downloading* ✨\n\n"
        f"📖 *{show_title}*\n"
        f"🎧 *{ep_title}*\n\n"
        "_Fetching audio URL…_",
        parse_mode=ParseMode.MARKDOWN,
    )

    tmp_path = None
    try:
        loop = asyncio.get_event_loop()

        # 1. Get stream URL
        stream_url = await loop.run_in_executor(
            None, pfm.get_episode_stream_url, episode_id
        )
        if not stream_url:
            await prog.edit_text(
                "❌ *Download Failed*\n\n"
                "Could not get audio URL for this episode.\n"
                "It may require a premium account.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await prog.edit_text(
            f"📥 *Downloading audio…*\n\n"
            f"📖 *{show_title}*\n"
            f"🎧 *{ep_title}*",
            parse_mode=ParseMode.MARKDOWN,
        )

        # 2. Download to temp file
        with tempfile.NamedTemporaryFile(
            suffix=".mp3", prefix=f"pfm_{episode_id}_", delete=False
        ) as tmp:
            tmp_path = tmp.name

        ok = await loop.run_in_executor(None, pfm.download_audio, stream_url, tmp_path)
        if not ok or not os.path.exists(tmp_path):
            await prog.edit_text(
                "❌ *Download error.* Please try again.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
        if size_mb > 50:
            await prog.edit_text(
                f"⚠️ *File too large* ({size_mb:.1f} MB)\n"
                "Telegram bot limit is 50 MB.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # 3. Upload
        await prog.edit_text(
            f"📤 *Uploading to Telegram…*\n🎧 *{ep_title}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        me = await ctx.bot.get_me()
        pages_line = f"\n🌐 [Web Player]({GITHUB_PAGES_URL})" if GITHUB_PAGES_URL else ""
        caption = (
            f"🎙️ *{show_title}*\n"
            f"📌 *{ep_title}*"
            f"{pages_line}\n\n"
            f"_Via @{me.username}_"
        )
        with open(tmp_path, "rb") as af:
            await q.message.reply_audio(
                audio=af,
                title=ep_title,
                performer=show_title,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
            )
        await prog.delete()

    except Exception as e:
        logger.exception(f"cb_download error ep={episode_id}: {e}")
        try:
            await prog.edit_text(
                "❌ *Something went wrong.* Please try again.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN env var is not set!")

    # main() runs inside a background thread (see main.py). PTB's
    # run_polling() needs a current event loop for *this* thread, and
    # Python no longer auto-creates one outside the main thread — without
    # this it crashes with:
    #   RuntimeError: There is no current event loop in thread 'Thread-x'
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cb_page,     pattern=r"^pg:"))
    app.add_handler(CallbackQueryHandler(cb_show,     pattern=r"^show:"))
    app.add_handler(CallbackQueryHandler(cb_ep_page,  pattern=r"^ep:"))
    app.add_handler(CallbackQueryHandler(cb_download, pattern=r"^dl:"))
    app.add_handler(CallbackQueryHandler(cb_back,     pattern=r"^back:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))

    logger.info("🚀 PocketFM Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
