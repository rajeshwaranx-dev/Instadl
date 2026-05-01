import os
import re
import sqlite3
import logging
import asyncio
from datetime import date
from functools import partial

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMember,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
import yt_dlp

# ─────────────────────────────────────────
#  CONFIG  ←  Edit these before running
# ─────────────────────────────────────────
BOT_TOKEN      = "8352705831:AAH7auZWJWENgCIEtEVzGSiAcrK4ILFmwwU"          # From @BotFather
FORCE_CHANNEL  = "@your_channel_username"        # e.g. "@mychannel"
DAILY_LIMIT    = 10                              # Max downloads per user/day
DOWNLOAD_DIR   = "downloads"                     # Temp folder for media
DB_PATH        = "bot.db"                        # SQLite database file
# ─────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

INSTAGRAM_REGEX = re.compile(
    r"(https?://)?(www\.)?instagram\.com/"
    r"(p|reel|reels|tv|stories)/[\w\-]+"
)

# ══════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                user_id  INTEGER,
                day      TEXT,
                count    INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, day)
            )
        """)
        conn.commit()


def get_count(user_id: int) -> int:
    today = str(date.today())
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT count FROM usage WHERE user_id=? AND day=?",
            (user_id, today),
        ).fetchone()
    return row[0] if row else 0


def add_count(user_id: int):
    today = str(date.today())
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO usage (user_id, day, count) VALUES (?, ?, 1)
            ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1
        """, (user_id, today))
        conn.commit()


# ══════════════════════════════════════════
#  FORCE-SUBSCRIBE HELPERS
# ══════════════════════════════════════════

async def is_member(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(FORCE_CHANNEL, user_id)
        return member.status in (
            ChatMember.MEMBER,
            ChatMember.ADMINISTRATOR,
            ChatMember.OWNER,
        )
    except Exception:
        return False


def subscribe_keyboard():
    channel = FORCE_CHANNEL.lstrip("@")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{channel}")],
        [InlineKeyboardButton("✅ I've Joined — Check Again", callback_data="verify_sub")],
    ])


SUBSCRIBE_TEXT = (
    "🔒 *Access Restricted*\n\n"
    "You must join our channel to use this bot.\n\n"
    "1️⃣ Tap *Join Channel*\n"
    "2️⃣ Then tap *I've Joined — Check Again*"
)


# ══════════════════════════════════════════
#  DOWNLOADER (runs in thread executor)
# ══════════════════════════════════════════

def _download_sync(url: str) -> list[str]:
    """Blocking yt-dlp download. Returns list of local file paths."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    ydl_opts = {
        "outtmpl": f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        # Limit file size pre-download (50 MB Telegram cap)
        "max_filesize": 50 * 1024 * 1024,
    }

    paths = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

        entries = info.get("entries") or [info]
        for entry in entries:
            if not entry:
                continue
            base = os.path.splitext(ydl.prepare_filename(entry))[0]
            # yt-dlp may choose different extensions
            for ext in (".mp4", ".jpg", ".jpeg", ".png", ".webp", ".mp3"):
                candidate = base + ext
                if os.path.exists(candidate):
                    paths.append(candidate)
                    break
            else:
                # fallback: use whatever yt-dlp decided
                fn = ydl.prepare_filename(entry)
                if os.path.exists(fn):
                    paths.append(fn)

    return paths


async def download_media(url: str) -> list[str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _download_sync, url)


# ══════════════════════════════════════════
#  HANDLERS
# ══════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not await is_member(ctx.bot, user.id):
        await update.message.reply_text(
            SUBSCRIBE_TEXT,
            reply_markup=subscribe_keyboard(),
            parse_mode="Markdown",
        )
        return

    used      = get_count(user.id)
    remaining = max(DAILY_LIMIT - used, 0)

    await update.message.reply_text(
        f"👋 Hello, *{user.first_name}*!\n\n"
        "📥 *Instagram Media Downloader*\n\n"
        "Supported links:\n"
        "• 📸 Photos & carousels\n"
        "• 🎥 Videos\n"
        "• 🎬 Reels\n"
        "• 📺 IGTV\n"
        "• 📖 Public Stories\n\n"
        f"📊 Daily limit : *{DAILY_LIMIT}* downloads\n"
        f"✅ Remaining   : *{remaining}* today\n\n"
        "Just paste any Instagram link below ⬇️",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user      = update.effective_user
    used      = get_count(user.id)
    remaining = max(DAILY_LIMIT - used, 0)

    await update.message.reply_text(
        f"📊 *Your Usage Today*\n\n"
        f"Used      : {used}/{DAILY_LIMIT}\n"
        f"Remaining : {remaining}\n\n"
        f"Resets at midnight 🌙",
        parse_mode="Markdown",
    )


async def on_verify_sub(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user  = query.from_user

    if await is_member(ctx.bot, user.id):
        remaining = max(DAILY_LIMIT - get_count(user.id), 0)
        await query.message.edit_text(
            f"✅ *Verified! Welcome, {user.first_name}!*\n\n"
            f"📊 Daily limit : *{DAILY_LIMIT}* downloads\n"
            f"✅ Remaining   : *{remaining}* today\n\n"
            "Send me any Instagram link to get started 🚀",
            parse_mode="Markdown",
        )
    else:
        await query.message.edit_text(
            "❌ *Still not joined!*\n\n"
            "Please join the channel first, then try again.",
            reply_markup=subscribe_keyboard(),
            parse_mode="Markdown",
        )


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip()

    # ── Force-subscribe check ──
    if not await is_member(ctx.bot, user.id):
        await update.message.reply_text(
            SUBSCRIBE_TEXT,
            reply_markup=subscribe_keyboard(),
            parse_mode="Markdown",
        )
        return

    # ── URL validation ──
    if not INSTAGRAM_REGEX.search(text):
        await update.message.reply_text(
            "❌ Please send a valid Instagram link.\n\n"
            "Examples:\n"
            "`instagram.com/p/...`\n"
            "`instagram.com/reel/...`\n"
            "`instagram.com/tv/...`",
            parse_mode="Markdown",
        )
        return

    # ── Daily limit check ──
    used = get_count(user.id)
    if used >= DAILY_LIMIT:
        await update.message.reply_text(
            f"⛔ *Daily limit reached!*\n\n"
            f"You've used all *{DAILY_LIMIT}* downloads for today.\n"
            "Come back tomorrow 🌅",
            parse_mode="Markdown",
        )
        return

    remaining_after = DAILY_LIMIT - used - 1
    status = await update.message.reply_text("⏳ Downloading… please wait.")

    try:
        files = await download_media(text)

        if not files:
            await status.edit_text(
                "❌ Could not download.\n\n"
                "Possible reasons:\n"
                "• Private account\n"
                "• Deleted post\n"
                "• Unsupported content\n\n"
                "Try again later."
            )
            return

        add_count(user.id)
        sent = 0

        for fpath in files:
            size = os.path.getsize(fpath)

            if size > 50 * 1024 * 1024:
                await update.message.reply_text(
                    f"⚠️ Skipped one file — too large for Telegram (>50 MB)."
                )
                os.remove(fpath)
                continue

            ext = os.path.splitext(fpath)[1].lower()
            try:
                with open(fpath, "rb") as f:
                    if ext in (".jpg", ".jpeg", ".png", ".webp"):
                        await update.message.reply_photo(f)
                    elif ext in (".mp4", ".mov", ".avi", ".mkv"):
                        await update.message.reply_video(f)
                    else:
                        await update.message.reply_document(f)
                sent += 1
            except Exception as send_err:
                logger.error(f"Send error: {send_err}")
            finally:
                if os.path.exists(fpath):
                    os.remove(fpath)

        if sent:
            await status.edit_text(
                f"✅ *Done!* Sent {sent} file(s).\n"
                f"📊 Remaining today: *{remaining_after}*",
                parse_mode="Markdown",
            )
        else:
            await status.edit_text("❌ Nothing could be sent. Try another link.")

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp error: {e}")
        await status.edit_text(
            "❌ Download failed!\n\n"
            "• The post may be private\n"
            "• Instagram may have blocked the request\n\n"
            "Try again in a few minutes."
        )
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        await status.edit_text("❌ Something went wrong. Please try again.")


# ══════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════

def main():
    init_db()
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_verify_sub, pattern="^verify_sub$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    logger.info("Bot is running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

