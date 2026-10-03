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
    InputMediaPhoto,
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
import lang_store

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── In-memory session state ───────────────────────────────────────────────────
# uid → { query, results, page, list_is_photo,
#         current_show: { id, title, image_url, episodes, ep_page, is_photo } }
user_state: dict[int, dict] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Photo-card helpers — send/edit a message as a photo with caption + buttons,
# falling back to a plain text message if the image can't be fetched/sent.
# Each caller tracks the returned is_photo flag so pagination knows whether
# to edit via edit_message_media (photo) or edit_message_text (text).
# ─────────────────────────────────────────────────────────────────────────────
async def _reply_card(target_message, photo_url, caption, markup):
    if photo_url:
        try:
            msg = await target_message.reply_photo(
                photo=photo_url, caption=caption, reply_markup=markup,
                parse_mode=ParseMode.MARKDOWN,
            )
            return msg, True
        except Exception as e:
            logger.warning(f"reply_photo failed, falling back to text: {e}")
    msg = await target_message.reply_text(
        caption, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
    )
    return msg, False


async def _edit_card(q, photo_url, caption, markup, is_photo):
    """Returns the (possibly changed) is_photo state of the message."""
    if is_photo:
        try:
            if photo_url:
                await q.edit_message_media(
                    media=InputMediaPhoto(
                        media=photo_url, caption=caption, parse_mode=ParseMode.MARKDOWN
                    ),
                    reply_markup=markup,
                )
            else:
                await q.edit_message_caption(
                    caption=caption, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
                )
            return True
        except Exception as e:
            logger.warning(f"edit photo card failed: {e}")
            return is_photo
    else:
        try:
            await q.edit_message_text(
                caption, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
            )
            return False
        except Exception as e:
            logger.warning(f"edit text card failed: {e}")
            return is_photo


# ─────────────────────────────────────────────────────────────────────────────
# /lang — language picker
# ─────────────────────────────────────────────────────────────────────────────
def _lang_keyboard() -> InlineKeyboardMarkup:
    codes = list(lang_store.LANGUAGES.items())
    rows = [
        [
            InlineKeyboardButton(codes[i][1], callback_data=f"lang:{codes[i][0]}"),
            InlineKeyboardButton(codes[i + 1][1], callback_data=f"lang:{codes[i + 1][0]}"),
        ]
        for i in range(0, len(codes) - 1, 2)
    ]
    if len(codes) % 2:
        rows.append([InlineKeyboardButton(codes[-1][1], callback_data=f"lang:{codes[-1][0]}")])
    return InlineKeyboardMarkup(rows)


async def lang_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌐 *Choose your preferred language:*\n"
        "_Search results will prioritize shows in this language._",
        reply_markup=_lang_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cb_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid  = q.from_user.id
    code = q.data.split(":", 1)[1]
    lang_store.set_user_lang(uid, code)
    name = lang_store.LANGUAGES.get(code, code)
    await q.edit_message_text(
        f"✅ Language set to *{name}*.\n\n"
        "Now just type the name of any show to search!",
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user  = update.effective_user
    photo = next((u for u in WELCOME_IMAGES if u), FALLBACK_BANNER)

    if not lang_store.get_user_lang(user.id):
        await update.message.reply_text(
            f"🎙️ *Welcome, {user.first_name}!*\n\n"
            "Before we start, please set your preferred language with "
            "/lang — search results will prioritize shows in that language.",
            reply_markup=_lang_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

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
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 *Try searching:*\n"
        "  • `Love Story`\n"
        "  • `Horror Night`\n"
        "  • `Motivational`\n\n"
        "🚀 *Type your search below!*\n\n"
        "✨*Bot Made By: @SmartBoy_ApnaMS*"
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

    # Priority: shows matching the user's set language first, then the rest
    # (original relevance order preserved within each group).
    pref_lang = lang_store.get_user_lang(uid)
    if pref_lang:
        results = sorted(
            results, key=lambda s: pfm.detect_language(s["title"]) != pref_lang
        )

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
    # Banner image: cover of the top result on this page.
    image = page_r[0]["image_url"] if page_r and page_r[0].get("image_url") else None

    if edit and update.callback_query:
        s["list_is_photo"] = await _edit_card(
            update.callback_query, image, text, markup, s.get("list_is_photo", False)
        )
    else:
        msg, is_photo = await _reply_card(update.message, image, text, markup)
        s["list_is_photo"] = is_photo


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
    # Short toast instead of editing the message text/caption here — the
    # message may now be a photo card, which edit_message_text can't touch
    # (only edit_message_caption/edit_message_media can); _send_episodes()
    # below replaces the content properly either way once data is ready.
    await q.answer("⏳ Fetching episodes…")
    show_id = q.data.split(":", 1)[1]
    uid     = q.from_user.id

    loop    = asyncio.get_event_loop()
    ep_raw  = await loop.run_in_executor(None, pfm.get_episodes, show_id)

    # pocketfm.com has no standalone show-details endpoint — the title is
    # bundled into the episode-list response itself.
    title = ep_raw.get("show_title") or "Unknown Show"

    episodes = pfm.parse_episodes(ep_raw)

    if not episodes:
        no_ep_text = (
            f"😕 No episodes found for *{title}*.\n"
            "This show may require a login or be unavailable."
        )
        is_photo = user_state.get(uid, {}).get("list_is_photo", False)
        await _edit_card(q, None, no_ep_text, None, is_photo)
        return

    if uid not in user_state:
        user_state[uid] = {}

    # Cover image: pulled from the search result the user tapped (search
    # results carry it; the episode-list response does not, at show level).
    image_url = next(
        (s["image_url"] for s in user_state[uid].get("results", []) if s["id"] == show_id),
        None,
    )

    # The message being edited is whatever _send_results last made it (photo
    # or text) — reuse that same state so the first _edit_card call below
    # uses the right Telegram edit method for the message as it exists now.
    user_state[uid]["current_show"] = {
        "id":        show_id,
        "title":     title,
        "image_url": image_url,
        "episodes":  episodes,
        "ep_page":   0,
        "is_photo":  user_state[uid].get("list_is_photo", False),
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

    lang_name = lang_store.LANGUAGES.get(pfm.detect_language(title), "")
    lang_line = f"🌐 Language: {lang_name}\n" if lang_name else ""
    text = (
        f"📚 *{title}*\n"
        f"{lang_line}"
        f"📄 Page {page+1}/{tpages} · {total} episodes\n\n"
        "👇 Tap an episode to download:"
    )
    markup = InlineKeyboardMarkup(buttons)
    cs["is_photo"] = await _edit_card(
        q, cs.get("image_url"), text, markup, cs.get("is_photo", False)
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
    # Sync to the message's actual current state (set by _send_episodes)
    # before _send_results edits it back to the results card.
    cs = user_state.get(uid, {}).get("current_show", {})
    user_state[uid]["list_is_photo"] = cs.get("is_photo", user_state[uid].get("list_is_photo", False))
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

        # 1. Stream URL is already embedded in the episode data fetched when
        #    the show's episode list was loaded (pocketfm.com returns it
        #    directly on each story — no separate "get stream" endpoint).
        ep_data = next(
            (ep for ep in cs.get("episodes", []) if ep["id"] == episode_id), None
        )
        stream_url = (ep_data or {}).get("media_url") or (ep_data or {}).get("hls_url")
        if not stream_url:
            reason = (
                "🔒 This episode is locked/paid on PocketFM — no free audio URL available."
                if (ep_data or {}).get("is_locked")
                else "Could not get audio URL for this episode.\nIt may require a premium account."
            )
            await prog.edit_text(
                f"❌ *Download Failed*\n\n{reason}",
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
    app.add_handler(CommandHandler("lang",  lang_cmd))
    app.add_handler(CallbackQueryHandler(cb_lang,     pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(cb_page,     pattern=r"^pg:"))
    app.add_handler(CallbackQueryHandler(cb_show,     pattern=r"^show:"))
    app.add_handler(CallbackQueryHandler(cb_ep_page,  pattern=r"^ep:"))
    app.add_handler(CallbackQueryHandler(cb_download, pattern=r"^dl:"))
    app.add_handler(CallbackQueryHandler(cb_back,     pattern=r"^back:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))

    logger.info("🚀 PocketFM Bot started.")
    # stop_signals=None: run_polling() normally registers OS signal handlers
    # (SIGINT/SIGTERM) via loop.add_signal_handler -> signal.set_wakeup_fd,
    # which only works in the main thread of the main interpreter. Since
    # this runs in a background thread (see main.py), that call raises:
    #   ValueError: set_wakeup_fd only works in main thread of the main interpreter
    # Skipping signal handler registration avoids the crash; Flask on the
    # main thread still owns process-level shutdown.
    app.run_polling(drop_pending_updates=True, stop_signals=None)


if __name__ == "__main__":
    main()
