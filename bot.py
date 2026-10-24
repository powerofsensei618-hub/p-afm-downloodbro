"""
bot.py — PocketFM Downloader Telegram Bot
Uses PocketFM API reverse-engineered from com.radio.pocketfm APK v3.69
"""

import os
import re
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
    ADMIN_USER_ID,
)
import pocketfm_api as pfm
import lang_store

# Bot-wide cookies.txt location — set via /cookies, survives for the life of
# the running instance (same ephemeral-disk caveat as lang_store.py).
_COOKIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
if os.path.exists(_COOKIES_PATH):
    pfm.load_cookies_file(_COOKIES_PATH)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Episodes are fetched from pocketfm.com in pages of this size (the show page
# loads 10 at a time; the "load more" action takes a page size).
EP_PAGE_SIZE = 50
# Telegram bot upload limit
TG_MAX_MB = 49


def _md(text) -> str:
    """Escape text for Telegram's legacy Markdown so titles / names containing
    _ * ` [ can never break a message ("can't parse entities")."""
    return re.sub(r"([_*`\[])", r"\\\1", str(text or ""))


# ── In-memory session state ───────────────────────────────────────────────────
# uid → { query, results, page, list_is_photo,
#         current_show: { id, title, image_url, episodes, ep_page, is_photo },
#         awaiting_search, awaiting_show_id, awaiting_cookies  (one-shot flags
#         set by /search, /download, /cookies — consumed by the next message) }
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

    # Verify the write actually landed (read-only disk / IO errors on some
    # hosts fail silently otherwise, which is what made /lang look like it
    # "wasn't changing" — now it tells you instead of lying).
    saved = lang_store.get_user_lang(uid)
    if saved != code:
        await q.edit_message_text(
            "⚠️ Couldn't save your language preference (storage error on the "
            "server). Please try /lang again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    name = lang_store.LANGUAGES.get(code, code)
    await q.edit_message_text(
        f"✅ Language set to *{name}*.\n\n"
        "Now just type the name of any show to search, or use /search!",
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────────────────────────────────────────────────────────────────────
# /search — prompt, then treat the next message as the search query
# ─────────────────────────────────────────────────────────────────────────────
async def search_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state.setdefault(uid, {})["awaiting_search"] = True
    await update.message.reply_text(
        "🔍 *Type the name of any show to search:*",
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────────────────────────────────────────────────────────────────────
# /download — open a show directly by its PocketFM link or 40-char show ID
# ─────────────────────────────────────────────────────────────────────────────
async def download_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state.setdefault(uid, {})["awaiting_show_id"] = True
    await update.message.reply_text(
        "🔗 *Open a story*\n\n"
        "Paste a Pocket FM show link or a 40-character show ID.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────────────────────────────────────────────────────────────────────
# /cookies — load a pocketfm.com session (cookies.txt or raw cookie string)
# so requests ride on a real logged-in session. Bot-wide, not per-user.
# Does NOT unlock paid/coin-locked episodes — that's a purchase gate, not a
# login gate.
# ─────────────────────────────────────────────────────────────────────────────
def _is_admin(uid: int) -> bool:
    return ADMIN_USER_ID is None or uid == ADMIN_USER_ID


async def cookies_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _is_admin(uid):
        await update.message.reply_text("🚫 Only the bot admin can set cookies.")
        return
    user_state.setdefault(uid, {})["awaiting_cookies"] = True
    status = (
        f"Currently loaded: {pfm.cookie_count()} cookies."
        if pfm.has_cookies() else "No cookies loaded yet."
    )
    await update.message.reply_text(
        "🍪 *Set PocketFM cookies*\n\n"
        "Send your `cookies.txt` file (Netscape format — export it from "
        "your browser after logging in to pocketfm.com) as a *document*, "
        "or paste the raw `document.cookie` string here as text.\n\n"
        f"_{status}_\n\n"
        "_Note: this sets the session for the whole bot, and only makes "
        "requests look like a logged-in user — it does not unlock paid/"
        "coin-locked episodes, those still require purchase on PocketFM._",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_cookies_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not (user_state.get(uid, {}).pop("awaiting_cookies", False) and _is_admin(uid)):
        return  # not something we asked for — ignore silently

    doc = update.message.document
    tg_file = await ctx.bot.get_file(doc.file_id)
    await tg_file.download_to_drive(_COOKIES_PATH)

    count = pfm.load_cookies_file(_COOKIES_PATH)
    if count:
        await update.message.reply_text(f"✅ Loaded {count} cookies.")
    else:
        await update.message.reply_text(
            "😕 Couldn't parse any cookies from that file.\n"
            "Make sure it's a Netscape-format cookies.txt export."
        )


# ─────────────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user  = update.effective_user
    photo = next((u for u in WELCOME_IMAGES if u), FALLBACK_BANNER)

    if not lang_store.get_user_lang(user.id):
        await update.message.reply_text(
            f"🎙️ *Welcome, {_md(user.first_name)}!*\n\n"
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
        f"🎙️ *Welcome, {_md(user.first_name)}!*\n\n"
        "✨ Search & download any audio story, podcast or audiobook\n"
        "from *Pocket FM* — instantly, for free.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 /search — find a show by name\n"
        "🔗 /download — open a show by link or ID\n"
        "🌐 /lang — change your preferred language\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 *Try searching:*\n"
        "  • `Love Story`\n"
        "  • `Horror Night`\n"
        "  • `Motivational`\n\n"
        "🚀 *Or just type the name of any show below!*"
        "✨*Bot Made By:*\n"
        "*@SmartBoy_ApnaMS*"
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
# Shared: fetch a show's episodes by ID into user_state (used by cb_show,
# the direct /download-by-ID flow, and nowhere else — single source of truth
# for "what does opening a show actually do").
# ─────────────────────────────────────────────────────────────────────────────
async def _load_show(uid: int, show_id: str) -> tuple[bool, str]:
    """Returns (ok, title_or_blank). On success, user_state[uid]['current_show']
    is populated and ready for _send_episodes().

    Only the FIRST page of episodes is fetched here (fast). The rest of the
    list - however long, some shows have 4000+ episodes - is fetched page by page
    by _ensure_range() as the user browses / jumps, so every episode is reachable.
    """
    loop = asyncio.get_event_loop()
    page = await loop.run_in_executor(None, pfm.fetch_episode_page, show_id, 0, EP_PAGE_SIZE)

    title    = page["title"] or "Unknown Show"
    episodes = page["episodes"]
    if not episodes:
        return False, title

    st = user_state.setdefault(uid, {})

    # Cover image: prefer the search result the user tapped (search results
    # carry it; the episode-list response doesn't, at show level) — falling
    # back to the per-episode image (same cover, repeated) for shows opened
    # directly by ID/URL via /download, which skip search entirely.
    image_url = next(
        (s["image_url"] for s in st.get("results", []) if s["id"] == show_id),
        None,
    ) or next((e["image_url"] for e in episodes if e.get("image_url")), None)

    total_known = bool(page["total"])
    total = max(page["total"], len(episodes))
    st["current_show"] = {
        "id":        show_id,
        "title":     title,
        "language":  page.get("language") or "",
        "image_url": image_url,
        # episode index (0-based) -> episode dict; filled page by page
        "eps":       {i: ep for i, ep in enumerate(episodes)},
        "total":     total,
        "total_known": total_known,
        "frontier":  len(episodes),   # indexes 0..frontier-1 are all loaded
        "next_ptr":  page["next_ptr"] if page["next_ptr"] is not None else len(episodes),
        "done":      (not total_known) and page["next_ptr"] is None,
        "random_ok": None,            # can an arbitrary page offset be fetched? (checked once)
        "lock":      asyncio.Lock(),
        "ep_page":   0,
        # The message being edited (if any) is whatever it already is —
        # _send_episodes()'s caller decides edit-vs-new and passes the right
        # starting state.
        "is_photo":  st.get("list_is_photo", False),
    }
    return True, title


def _find_ep(cs: dict, episode_id: str):
    """(index, episode dict) of an episode id in the loaded list, or (None, None)."""
    for idx, ep in cs.get("eps", {}).items():
        if ep["id"] == episode_id:
            return idx, ep
    return None, None


async def _ensure_range(cs: dict, start: int, end: int) -> None:
    """Make sure episodes [start, end) are loaded, fetching pages from pocketfm.com
    as needed. Jumping far ahead (e.g. to episode 3000 of 4192) fetches that page
    directly when the server supports random offsets (verified once per show by
    checking the first returned episode number); otherwise it walks the pages with the
    server's own next_ptr."""
    loop = asyncio.get_event_loop()
    async with cs["lock"]:
        for _ in range(120):  # hard stop - a run-away loop must never hang the bot
            hi = min(end, cs["total"]) if cs["total_known"] else end
            missing = next((i for i in range(start, hi) if i not in cs["eps"]), None)
            if missing is None or cs["done"]:
                return

            random_mode = cs["random_ok"] is not False
            if random_mode:
                ptr, base = missing, missing
            else:
                ptr = cs["next_ptr"] if cs["next_ptr"] is not None else cs["frontier"]
                base = cs["frontier"]

            page = await loop.run_in_executor(
                None, pfm.fetch_episode_page, cs["id"], ptr, EP_PAGE_SIZE
            )
            eps = page["episodes"]

            if not eps:
                if random_mode and cs["random_ok"] is None:
                    cs["random_ok"] = False      # maybe offsets are not supported - go sequential
                    continue
                if cs["total_known"] and base < cs["total"]:
                    logger.warning(f"episode page at {ptr} came back empty - will retry on next click")
                else:
                    cs["done"] = True
                    cs["total"], cs["total_known"] = (max(cs["eps"]) + 1 if cs["eps"] else 0), True
                return

            if random_mode and cs["random_ok"] is None and ptr > 0:
                try:
                    first_no = int(eps[0]["number"])
                except (TypeError, ValueError):
                    first_no = None
                if first_no != ptr + 1:
                    cs["random_ok"] = False      # offsets don't map 1:1 - redo with next_ptr
                    continue
                cs["random_ok"] = True

            contiguous = base <= cs["frontier"]   # this page extends the 0..frontier block
            for i, ep in enumerate(eps):
                cs["eps"][base + i] = ep
            while cs["frontier"] in cs["eps"]:
                cs["frontier"] += 1
            if contiguous:
                cs["next_ptr"] = page["next_ptr"]
                if page["next_ptr"] is None and not cs["total_known"]:
                    cs["done"] = True
            if base + len(eps) > cs["total"]:
                cs["total"] = base + len(eps)


# ─────────────────────────────────────────────────────────────────────────────
# Text message dispatcher — routes to whichever one-shot flag is pending
# (/search, /download, /cookies), else treats the text as a search query.
# ─────────────────────────────────────────────────────────────────────────────
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    uid = update.effective_user.id
    st  = user_state.setdefault(uid, {})

    if st.pop("awaiting_cookies", False) and _is_admin(uid):
        count = pfm.load_cookie_string(text)
        if count:
            with open(_COOKIES_PATH, "w", encoding="utf-8") as f:
                f.write(text)
            await update.message.reply_text(f"✅ Loaded {count} cookies.")
        else:
            await update.message.reply_text(
                "😕 Couldn't find any `name=value` cookie pairs in that text."
            )
        return

    if st.pop("awaiting_show_id", False):
        await _handle_direct_show(update, uid, text)
        return

    if st.pop("awaiting_jump", False) and st.get("current_show"):
        cs = st["current_show"]
        if text.isdigit() and int(text) >= 1:
            number = min(int(text), cs["total"])
            cs["ep_page"] = (number - 1) // RESULTS_PER_PAGE
            await _send_episodes(uid, reply_message=update.message)
        else:
            await update.message.reply_text("😕 Send just an episode number, e.g. `250`.", parse_mode=ParseMode.MARKDOWN)
        return

    st.pop("awaiting_search", None)  # consumed either way
    await handle_search(update, ctx, text)


async def _handle_direct_show(update: Update, uid: int, text: str):
    show_id = pfm.extract_show_id(text)
    if not show_id:
        await update.message.reply_text(
            "😕 Couldn't find a valid show ID in that.\n"
            "Paste a Pocket FM show link or the 40-character show ID.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    wait = await update.message.reply_text("🔎 Fetching show…", parse_mode=ParseMode.MARKDOWN)
    ok, title = await _load_show(uid, show_id)
    await wait.delete()

    if not ok:
        await update.message.reply_text(
            f"😕 No episodes found for *{title}*.\nCheck the ID/link and try again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await _send_episodes(uid, reply_message=update.message)


# ─────────────────────────────────────────────────────────────────────────────
# Search
# ─────────────────────────────────────────────────────────────────────────────
def _lang_rank(title: str, lang: str) -> int:
    """0 = show is in the wanted language, 1 = language unknown, 2 = another language."""
    guess = pfm.guess_language(title)
    if guess == lang:
        return 0
    return 1 if guess is None else 2


async def handle_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE, query: str):
    uid = update.effective_user.id
    wait = await update.message.reply_text(
        f"🔍 Searching *{_md(query)}*…", parse_mode=ParseMode.MARKDOWN
    )

    # Priority language: if the query itself is written in a distinct script
    # (e.g. Devanagari, Tamil...) that wins — the person is explicitly asking
    # for that language regardless of their saved /lang preference. Only when
    # the query gives no signal (plain Latin text like "Avatar") do we fall
    # back to the saved preference ("all" = no preference).
    effective_lang = pfm.query_language(query) or lang_store.get_user_lang(uid)
    if effective_lang == "all":
        effective_lang = None

    # The site answers a search with ONE language only (English by default), so
    # search_multi() asks for the wanted language as well as the default one and
    # merges the answers - every language shows up, the wanted one first.
    loop = asyncio.get_event_loop()
    raw     = await loop.run_in_executor(None, pfm.search_multi, query, effective_lang)
    results = pfm.parse_search_results(raw)

    await wait.delete()

    if not results:
        await update.message.reply_text(
            f"😕 No results for *{_md(query)}*.\nTry a different keyword.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Wanted language first, then untagged shows, then the other languages —
    # nothing is hidden, original relevance order is kept inside each group
    # (stable sort).
    if effective_lang:
        results = sorted(results, key=lambda s: _lang_rank(s["title"], effective_lang))

    user_state[uid] = {**user_state.get(uid, {}), "query": query, "results": results, "page": 0}
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
        f"🎵 Results for: `{query.replace(chr(96), chr(39))}`\n"
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

    ok, title = await _load_show(uid, show_id)
    if not ok:
        no_ep_text = (
            f"😕 No episodes found for *{title}*.\n"
            "This show may require a login or be unavailable."
        )
        is_photo = user_state.get(uid, {}).get("list_is_photo", False)
        await _edit_card(q, None, no_ep_text, None, is_photo)
        return

    await _send_episodes(uid, q=q)


# ─────────────────────────────────────────────────────────────────────────────
# Episode list page — edits an existing callback-query message (q) or sends
# a fresh one (reply_message), e.g. when a show was opened directly via
# /download instead of by tapping a search result.
# ─────────────────────────────────────────────────────────────────────────────
async def _send_episodes(uid: int, q=None, reply_message=None):
    cs    = user_state[uid]["current_show"]
    title = cs["title"]

    # clamp the page, make sure its episodes are loaded (fetches from pocketfm.com
    # on demand), then clamp again - the real total may turn out smaller
    for _ in range(2):
        total  = cs["total"]
        tpages = max(1, (total + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
        page   = min(max(0, cs.get("ep_page", 0)), tpages - 1)
        cs["ep_page"] = page
        si = page * RESULTS_PER_PAGE
        ei = min(si + RESULTS_PER_PAGE, total)
        await _ensure_range(cs, si, ei)
    shown = [cs["eps"][i] for i in range(si, ei) if i in cs["eps"]]

    buttons = []
    for ep in shown:
        num = f"{ep['number']}. " if ep["number"] else ""
        lbl = f"🎧 {num}{ep['title']}"
        buttons.append([InlineKeyboardButton(lbl[:64], callback_data=f"dl:{ep['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data="ep:prev"))
    if page < tpages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data="ep:next"))
    if nav:
        buttons.append(nav)
    if tpages > 3:  # long shows: big jumps instead of tapping Next hundreds of times
        buttons.append([
            InlineKeyboardButton("⏪ -10 pages", callback_data="ep:m10"),
            InlineKeyboardButton("🔢 Go to episode", callback_data="ep:jump"),
            InlineKeyboardButton("+10 pages ⏩", callback_data="ep:p10"),
        ])
    buttons.append([InlineKeyboardButton("🔙 Back to results", callback_data="back:search")])

    lang_code = (cs.get("language") or "").lower() or pfm.detect_language(title)
    lang_name = lang_store.LANGUAGES.get(lang_code, "")
    lang_line = f"🌐 Language: {lang_name}\n" if lang_name else ""
    count_txt = f"{total}" if cs["total_known"] else f"{total}+"
    text = (
        f"📚 *{_md(title)}*\n"
        f"{lang_line}"
        f"📄 Page {page+1}/{tpages} · {count_txt} episodes\n\n"
        "👇 Tap an episode to download:"
    )
    markup = InlineKeyboardMarkup(buttons)
    if q is not None:
        cs["is_photo"] = await _edit_card(
            q, cs.get("image_url"), text, markup, cs.get("is_photo", False)
        )
    else:
        msg, is_photo = await _reply_card(reply_message, cs.get("image_url"), text, markup)
        cs["is_photo"] = is_photo


# ─────────────────────────────────────────────────────────────────────────────
# Callbacks: episode pagination
# ─────────────────────────────────────────────────────────────────────────────
async def cb_ep_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    cs  = user_state.get(uid, {}).get("current_show")
    if not cs:
        await q.answer("Session expired.", show_alert=True)
        return
    action = q.data.split(":")[1]

    if action == "jump":
        user_state[uid]["awaiting_jump"] = True
        await q.answer()
        await q.message.reply_text(
            f"🔢 *Send the episode number* (1-{cs['total']}) you want to open.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await q.answer("⏳ Loading…" if action in ("next", "p10") else None)
    page = cs.get("ep_page", 0)
    if action == "next":
        page += 1
    elif action == "prev":
        page -= 1
    elif action == "p10":
        page += 10
    elif action == "m10":
        page -= 10
    cs["ep_page"] = max(0, page)
    await _send_episodes(uid, q=q)


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
    ep_idx, ep_data = _find_ep(cs, episode_id)
    ep_title   = (ep_data or {}).get("title", "Episode")

    prog = await q.message.reply_text(
        f"⏳ *Wait... Im Downloading* ✨\n\n"
        f"📖 *{_md(show_title)}*\n"
        f"🎧 *{_md(ep_title)}*\n\n"
        "_Fetching audio URL…_",
        parse_mode=ParseMode.MARKDOWN,
    )

    tmp_path = None
    try:
        loop = asyncio.get_event_loop()

        if not ep_data:
            await prog.edit_text(
                "❌ *Download Failed*\n\nSession expired - open the show again.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        await prog.edit_text(
            f"📥 *Downloading audio…*\n\n"
            f"📖 *{_md(show_title)}*\n"
            f"🎧 *{_md(ep_title)}*",
            parse_mode=ParseMode.MARKDOWN,
        )

        # 1. Download. Every stream URL pocketfm.com gave for the episode (video_url /
        #    media_url HLS, DASH, direct file) is tried until one yields the real audio.
        with tempfile.NamedTemporaryFile(
            suffix=".mp3", prefix=f"pfm_{episode_id}_", delete=False
        ) as tmp:
            tmp_path = tmp.name

        shared = pfm.shared_urls(list(cs.get("eps", {}).values()))
        ok, reason, source = await loop.run_in_executor(
            None, pfm.download_episode_audio, ep_data, tmp_path, shared
        )

        if not ok and reason in ("none", "failed") and ep_idx is not None:
            # the list page may have carried no URLs for this episode - read it fresh once
            changed = await loop.run_in_executor(
                None, pfm.refresh_episode_urls, cs["id"], ep_idx, ep_data
            )
            if changed:
                shared = pfm.shared_urls(list(cs.get("eps", {}).values()))
                ok, reason, source = await loop.run_in_executor(
                    None, pfm.download_episode_audio, ep_data, tmp_path, shared
                )

        if not ok or not os.path.exists(tmp_path):
            if reason == "drm":
                msg = ("🔐 This episode only exists as a DRM-encrypted stream on "
                       "PocketFM, which can't be downloaded.")
            elif reason == "none":
                msg = ("No stream URL was found for this episode."
                       + ("\n_(It is a paid/coin episode on PocketFM.)_" if ep_data.get("coins_required") else ""))
            else:
                msg = "Download error. Please try again."
            await prog.edit_text(f"❌ *Download Failed*\n\n{msg}", parse_mode=ParseMode.MARKDOWN)
            return
        logger.info(f"ep {episode_id} downloaded via {source}")

        # 2. Telegram bots can upload max 50 MB - squeeze long episodes under it
        if os.path.getsize(tmp_path) > TG_MAX_MB * 1024 * 1024:
            await prog.edit_text(
                f"🗜 *Compressing…*\n🎧 *{_md(ep_title)}*", parse_mode=ParseMode.MARKDOWN
            )
            await loop.run_in_executor(None, pfm.shrink_to_limit, tmp_path, TG_MAX_MB)
        size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
        if size_mb > 50:
            await prog.edit_text(
                f"⚠️ *File too large* ({size_mb:.1f} MB)\n"
                "Telegram bot limit is 50 MB.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # 3. Tag (title/artist/album/cover) so it looks right in any player
        await loop.run_in_executor(
            None, pfm.tag_audio, tmp_path, ep_title, show_title, show_title,
            cs.get("image_url") or "",
        )

        # 4. Upload (long timeouts: PTB's defaults are a few seconds, far too short for
        #    a multi-MB audio file on a slow host)
        await prog.edit_text(
            f"📤 *Uploading to Telegram…*\n🎧 *{_md(ep_title)}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        me = await ctx.bot.get_me()
        pages_line = f"\n🌐 [Web Player]({GITHUB_PAGES_URL})" if GITHUB_PAGES_URL else ""
        caption = (
            f"🎙️ *{_md(show_title)}*\n"
            f"📌 *{_md(ep_title)}*"
            f"{pages_line}\n\n"
            f"_Via @{_md(me.username)}_"
        )
        with open(tmp_path, "rb") as af:
            await q.message.reply_audio(
                audio=af,
                title=ep_title,
                performer=show_title,
                duration=int(ep_data.get("duration") or 0) or None,
                caption=caption,
                parse_mode=ParseMode.MARKDOWN,
                read_timeout=300, write_timeout=300, connect_timeout=30, pool_timeout=30,
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

    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("search",   search_cmd))
    app.add_handler(CommandHandler("download", download_cmd))
    app.add_handler(CommandHandler("lang",     lang_cmd))
    app.add_handler(CommandHandler("cookies",  cookies_cmd))
    app.add_handler(CallbackQueryHandler(cb_lang,     pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(cb_page,     pattern=r"^pg:"))
    app.add_handler(CallbackQueryHandler(cb_show,     pattern=r"^show:"))
    app.add_handler(CallbackQueryHandler(cb_ep_page,  pattern=r"^ep:"))
    app.add_handler(CallbackQueryHandler(cb_download, pattern=r"^dl:"))
    app.add_handler(CallbackQueryHandler(cb_back,     pattern=r"^back:"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_cookies_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

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
