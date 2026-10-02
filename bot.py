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

from config import BOT_TOKEN, WELCOME_IMAGES, RESULTS_PER_PAGE
import pocketfm_api as pfm

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── In-memory state ────────────────────────────────────────────────────────
# user_id → { query, results, page }
user_state: dict[int, dict] = {}


# ─── /start ─────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    image_url = random.choice(WELCOME_IMAGES)

    caption = (
        f"🎙️ *Welcome to Pocket FM Downloader, {user.first_name}!*\n\n"
        "✨ Your one-stop destination to search and download your favourite\n"
        "audio stories, podcasts & audiobooks from *Pocket FM* — for free.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 *How to use me:*\n"
        "  Just type the name of any story or show and I'll find it for you instantly.\n\n"
        "📥 *Then tap any result* to download its episodes directly to Telegram.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 *Examples you can try:*\n"
        "  • `Love Story`\n"
        "  • `Horror Night`\n"
        "  • `Motivational`\n\n"
        "🚀 *Go ahead — type your search below!*"
    )

    try:
        await update.message.reply_photo(
            photo=image_url,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        # Fallback to text if image fails
        await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN)


# ─── Search handler ──────────────────────────────────────────────────────────
async def handle_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    if not query:
        return

    uid = update.effective_user.id
    wait_msg = await update.message.reply_text(
        f"🔍 Searching for *{query}*…", parse_mode=ParseMode.MARKDOWN
    )

    raw = await asyncio.get_event_loop().run_in_executor(
        None, pfm.search_shows, query
    )
    results = pfm.parse_search_results(raw)

    await wait_msg.delete()

    if not results:
        await update.message.reply_text(
            f"😕 No results found for *{query}*.\n\n"
            "Try a different keyword or check the spelling.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    user_state[uid] = {"query": query, "results": results, "page": 0}
    await send_results_page(update, ctx, uid, edit=False)


# ─── Send paginated results ──────────────────────────────────────────────────
async def send_results_page(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    uid: int,
    edit: bool = False,
):
    state = user_state.get(uid)
    if not state:
        return

    results = state["results"]
    page = state["page"]
    query = state["query"]
    total = len(results)
    start_idx = page * RESULTS_PER_PAGE
    end_idx = min(start_idx + RESULTS_PER_PAGE, total)
    page_results = results[start_idx:end_idx]
    total_pages = (total + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE

    # Build result buttons (one per row)
    buttons = []
    for i, show in enumerate(page_results, start=start_idx + 1):
        ep_txt = f" · {show['total_episodes']} eps" if show["total_episodes"] else ""
        label = f"{i}. {show['title']}{ep_txt}"
        buttons.append(
            [InlineKeyboardButton(label, callback_data=f"show:{show['id']}")]
        )

    # Navigation row
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Previous", callback_data="page:prev"))
    if end_idx < total:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data="page:next"))
    if nav:
        buttons.append(nav)

    markup = InlineKeyboardMarkup(buttons)
    text = (
        f"🎵 *Search results for:* `{query}`\n"
        f"📄 Page {page + 1} of {total_pages}  |  {total} shows found\n\n"
        "👇 *Tap a show to download its episodes:*"
    )

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
        )


# ─── Callback: pagination ────────────────────────────────────────────────────
async def handle_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    state = user_state.get(uid)
    if not state:
        await query.answer("Session expired. Please search again.", show_alert=True)
        return

    direction = query.data.split(":")[1]
    if direction == "next":
        state["page"] += 1
    elif direction == "prev":
        state["page"] = max(0, state["page"] - 1)

    await send_results_page(update, ctx, uid, edit=True)


# ─── Callback: show selected ─────────────────────────────────────────────────
async def handle_show_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    show_id = query.data.split(":", 1)[1]
    uid = query.from_user.id

    await query.edit_message_text(
        "⏳ Fetching show details…", parse_mode=ParseMode.MARKDOWN
    )

    # Get show details + episodes
    loop = asyncio.get_event_loop()
    show_raw = await loop.run_in_executor(None, pfm.get_show_details, show_id)
    ep_raw = await loop.run_in_executor(None, pfm.get_episodes, show_id)

    show_data = (
        show_raw.get("data", {}) or show_raw.get("show", {}) or {}
    )
    title = (
        show_data.get("title")
        or show_data.get("name")
        or "Unknown Show"
    )
    description = (
        show_data.get("description", "")
        or show_data.get("synopsis", "")
        or ""
    )[:200]
    author = show_data.get("author_name") or show_data.get("author") or ""

    episodes = (
        ep_raw.get("episodes")
        or ep_raw.get("data", {}).get("episodes")
        or ep_raw.get("data")
        or []
    )

    if not isinstance(episodes, list):
        episodes = []

    if not episodes:
        await query.edit_message_text(
            f"😕 No episodes found for *{title}*.\n\nThis show may require a login or be unavailable.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Store episodes in state
    user_state[uid] = user_state.get(uid, {})
    user_state[uid]["current_show"] = {
        "id": show_id,
        "title": title,
        "episodes": episodes,
        "ep_page": 0,
    }

    await send_episode_page(query, uid)


# ─── Send episode list ────────────────────────────────────────────────────────
async def send_episode_page(query, uid: int, edit: bool = True):
    state = user_state.get(uid, {}).get("current_show")
    if not state:
        return

    episodes = state["episodes"]
    page = state.get("ep_page", 0)
    title = state["title"]
    total = len(episodes)
    start_idx = page * RESULTS_PER_PAGE
    end_idx = min(start_idx + RESULTS_PER_PAGE, total)
    page_eps = episodes[start_idx:end_idx]
    total_pages = (total + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE

    buttons = []
    for ep in page_eps:
        ep_id = str(ep.get("episode_id") or ep.get("id") or "")
        ep_title = ep.get("title") or ep.get("name") or f"Episode {ep_id}"
        ep_num = ep.get("episode_order") or ep.get("episode_number") or ""
        label = f"🎧 {ep_num}. {ep_title}" if ep_num else f"🎧 {ep_title}"
        buttons.append(
            [InlineKeyboardButton(label[:60], callback_data=f"dl:{ep_id}")]
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data="ep:prev"))
    if end_idx < total:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data="ep:next"))
    if nav:
        buttons.append(nav)

    buttons.append(
        [InlineKeyboardButton("🔙 Back to results", callback_data="back:search")]
    )

    markup = InlineKeyboardMarkup(buttons)
    text = (
        f"📚 *{title}*\n"
        f"📄 Page {page + 1}/{total_pages}  |  {total} episodes\n\n"
        "👇 *Tap an episode to download:*"
    )

    if edit:
        await query.edit_message_text(
            text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
        )
    else:
        await query.message.reply_text(
            text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN
        )


# ─── Callback: episode pagination ────────────────────────────────────────────
async def handle_ep_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    show_state = user_state.get(uid, {}).get("current_show")
    if not show_state:
        await query.answer("Session expired. Search again.", show_alert=True)
        return

    direction = query.data.split(":")[1]
    if direction == "next":
        show_state["ep_page"] = show_state.get("ep_page", 0) + 1
    elif direction == "prev":
        show_state["ep_page"] = max(0, show_state.get("ep_page", 0) - 1)

    await send_episode_page(query, uid)


# ─── Callback: back to search ─────────────────────────────────────────────────
async def handle_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    await send_results_page(update, ctx, uid, edit=True)


# ─── Callback: download episode ──────────────────────────────────────────────
async def handle_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Wait... Im Downloading ✨", show_alert=False)

    episode_id = query.data.split(":", 1)[1]
    uid = query.from_user.id
    show_state = user_state.get(uid, {}).get("current_show", {})
    show_title = show_state.get("title", "Pocket FM")

    # Find episode title from stored list
    ep_title = "Episode"
    for ep in show_state.get("episodes", []):
        eid = str(ep.get("episode_id") or ep.get("id") or "")
        if eid == episode_id:
            ep_title = ep.get("title") or ep.get("name") or "Episode"
            break

    progress_msg = await query.message.reply_text(
        f"⏳ *Wait... Im Downloading* ✨\n\n"
        f"📖 *Show:* {show_title}\n"
        f"🎧 *Episode:* {ep_title}\n\n"
        "_Please wait a moment…_",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        loop = asyncio.get_event_loop()

        # Get stream URL
        stream_url = await loop.run_in_executor(
            None, pfm.get_episode_stream_url, episode_id
        )

        if not stream_url:
            await progress_msg.edit_text(
                "❌ *Download Failed*\n\n"
                "Could not retrieve the audio URL for this episode.\n"
                "It may require a premium account or is unavailable.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # Download to temp file
        with tempfile.NamedTemporaryFile(
            suffix=".mp3", prefix=f"pfm_{episode_id}_", delete=False
        ) as tmp:
            tmp_path = tmp.name

        success = await loop.run_in_executor(
            None, pfm.download_audio, stream_url, tmp_path
        )

        if not success or not os.path.exists(tmp_path):
            await progress_msg.edit_text(
                "❌ *Download Failed*\n\nFailed to download audio. Please try again.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        file_size = os.path.getsize(tmp_path)
        # Telegram limit: 50MB for bots
        if file_size > 50 * 1024 * 1024:
            await progress_msg.edit_text(
                "⚠️ *File Too Large*\n\n"
                "This episode exceeds Telegram's 50MB upload limit.\n"
                f"File size: {file_size // (1024*1024)} MB",
                parse_mode=ParseMode.MARKDOWN,
            )
            os.remove(tmp_path)
            return

        await progress_msg.edit_text(
            f"📤 *Uploading to Telegram…*\n\n"
            f"🎧 *{ep_title}*",
            parse_mode=ParseMode.MARKDOWN,
        )

        caption = (
            f"🎙️ *{show_title}*\n"
            f"📌 *{ep_title}*\n\n"
            f"_Downloaded via @{(await ctx.bot.get_me()).username}_"
        )

        with open(tmp_path, "rb") as audio_file:
            await query.message.reply_audio(
                audio=audio_file,
                title=ep_title,
                performer=show_title,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
            )

        await progress_msg.delete()
        os.remove(tmp_path)

    except Exception as e:
        logger.exception(f"Download error for episode {episode_id}: {e}")
        try:
            await progress_msg.edit_text(
                "❌ *Something went wrong!*\n\nPlease try again later.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        try:
            os.remove(tmp_path)
        except Exception:
            pass


# ─── Unknown messages ─────────────────────────────────────────────────────────
async def handle_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💬 Just type any story or show name to search!\n\n"
        "Example: `Love Story` or `Horror Night`",
        parse_mode=ParseMode.MARKDOWN,
    )


# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN is not set in environment variables!")

    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_page, pattern="^page:"))
    app.add_handler(CallbackQueryHandler(handle_show_select, pattern="^show:"))
    app.add_handler(CallbackQueryHandler(handle_ep_page, pattern="^ep:"))
    app.add_handler(CallbackQueryHandler(handle_download, pattern="^dl:"))
    app.add_handler(CallbackQueryHandler(handle_back, pattern="^back:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))

    logger.info("🚀 PocketFM Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
