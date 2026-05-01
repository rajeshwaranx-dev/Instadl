import os
import re
import sqlite3
import logging
import asyncio
from datetime import date

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

# ╔══════════════════════════════════════════╗
#  CONFIG  ←  Edit these before running
# ╠══════════════════════════════════════════╣
BOT_TOKEN    = "8352705831:AAH7auZWJWENgCIEtEVzGSiAcrK4ILFmwwU"       # From @BotFather
ADMIN_ID     = 7246154050                   # Your Telegram user ID (get from @userinfobot)

# ── Force-Subscribe Channel ─────────────────
# PUBLIC channel  → use username:   "@mychannel"
# PRIVATE channel → use numeric ID: -1001234567890
#
# HOW TO GET PRIVATE CHANNEL ID:
#   1. Forward any message from the private channel to @userinfobot
#   2. It will show the channel ID (negative number like -1001234567890)
#   3. Paste that number below as an integer (keep the minus sign!)
#
FSUB_CHANNEL = "@your_channel_or_id_here"

# For PRIVATE channels you MUST provide an invite link
# Public channels: leave as empty string ""
# HOW TO GET INVITE LINK:
#   Channel Settings → Invite Links → Create invite link
FSUB_INVITE_LINK = ""   # e.g. "https://t.me/+AbCdEfGhIjKlMnOp"

DAILY_LIMIT  = 10                          # Max downloads per user/day
DOWNLOAD_DIR = "downloads"                 # Temp folder for media
DB_PATH      = "bot.db"                    # SQLite database file
# ╚══════════════════════════════════════════╝

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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # Default: fsub is ON
        conn.execute("""
            INSERT OR IGNORE INTO settings (key, value)
            VALUES ('fsub_enabled', '1')
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


def is_fsub_enabled() -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='fsub_enabled'"
        ).fetchone()
    return (row[0] == "1") if row else True


def set_fsub(enabled: bool):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE settings SET value=? WHERE key='fsub_enabled'",
            ("1" if enabled else "0"),
        )
        conn.commit()


# ══════════════════════════════════════════
#  FORCE-SUBSCRIBE HELPERS
# ══════════════════════════════════════════

async def is_member(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(FSUB_CHANNEL, user_id)
        return member.status in (
            ChatMember.MEMBER,
            ChatMember.ADMINISTRATOR,
            ChatMember.OWNER,
        )
    except Exception as e:
        logger.warning(f"is_member check failed: {e}")
        return True   # fail-open so bot doesn't break if channel misconfigured


def subscribe_keyboard() -> InlineKeyboardMarkup:
    # Private channel uses invite link; public channel uses @username link
    if FSUB_INVITE_LINK:
        join_url = FSUB_INVITE_LINK
    else:
        channel  = str(FSUB_CHANNEL).lstrip("@")
        join_url = f"https://t.me/{channel}"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=join_url)],
        [InlineKeyboardButton("✅ I've Joined — Check Again", callback_data="verify_sub")],
    ])


SUBSCRIBE_TEXT = (
    "🔒 *Access Restricted*\n\n"
    "You must join our channel to use this bot.\n\n"
    "1️⃣ Tap *Join Channel*\n"
    "2️⃣ Come back and tap *I've Joined — Check Again*"
)


async def check_fsub(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    if not is_fsub_enabled():
        return True
    user = update.effective_user
    if await is_member(ctx.bot, user.id):
        return True
    await update.message.reply_text(
        SUBSCRIBE_TEXT,
        reply_markup=subscribe_keyboard(),
        parse_mode="Markdown",
    )
    return False


# ══════════════════════════════════════════
#  DOWNLOADER
# ══════════════════════════════════════════

def _download_sync(url: str) -> list:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    ydl_opts = {
        "outtmpl": f"{DOWNLOAD_DIR}/%(id)s.%(ext)s",
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "max_filesize": 50 * 1024 * 1024,
    }
    paths = []
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info    = ydl.extract_info(url, download=True)
        entries = info.get("entries") or [info]
        for entry in entries:
            if not entry:
                continue
            base = os.path.splitext(ydl.prepare_filename(entry))[0]
            for ext in (".mp4", ".jpg", ".jpeg", ".png", ".webp"):
                candidate = base + ext
                if os.path.exists(candidate):
                    paths.append(candidate)
                    break
            else:
                fn = ydl.prepare_filename(entry)
                if os.path.exists(fn):
                    paths.append(fn)
    return paths


async def download_media(url: str) -> list:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _download_sync, url)


# ══════════════════════════════════════════
#  ADMIN DECORATOR
# ══════════════════════════════════════════

def admin_only(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Admin only.")
            return
        await func(update, ctx)
    return wrapper


# ══════════════════════════════════════════
#  ADMIN COMMANDS
# ══════════════════════════════════════════

@admin_only
async def cmd_fsub_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_fsub(True)
    await update.message.reply_text(
        "✅ *Force-subscribe is now ON.*\n"
        "Users must join the channel to use the bot.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_fsub_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_fsub(False)
    await update.message.reply_text(
        "🔓 *Force-subscribe is now OFF.*\n"
        "All users can use the bot freely.",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_fsub_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    enabled = is_fsub_enabled()
    state   = "✅ ON" if enabled else "🔓 OFF"
    private = "Yes — invite link set" if FSUB_INVITE_LINK else "No (public channel)"
    await update.message.reply_text(
        f"📋 *Force-Subscribe Status*\n\n"
        f"Status   : {state}\n"
        f"Channel  : `{FSUB_CHANNEL}`\n"
        f"Private  : {private}",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠 *Admin Panel*\n\n"
        "`/fsubon`     — Enable force-subscribe\n"
        "`/fsuboff`    — Disable force-subscribe\n"
        "`/fsubstatus` — Check fsub status\n"
        "`/admin`      — Show this menu",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════
#  USER HANDLERS
# ══════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_fsub(update, ctx):
        return
    user      = update.effective_user
    used      = get_count(user.id)
    remaining = max(DAILY_LIMIT - used, 0)
    await update.message.reply_text(
        f"👋 Hello, *{user.first_name}*!\n\n"
        "📥 *Instagram Media Downloader*\n\n"
        "Supported:\n"
        "• 📸 Photos & carousels\n"
        "• 🎥 Videos & IGTV\n"
        "• 🎬 Reels\n"
        "• 📖 Public Stories\n\n"
        f"📊 Daily limit : *{DAILY_LIMIT}* downloads\n"
        f"✅ Remaining   : *{remaining}* today\n\n"
        "Just paste any Instagram link ⬇️",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_fsub(update, ctx):
        return
    user      = update.effective_user
    used      = get_count(user.id)
    remaining = max(DAILY_LIMIT - used, 0)
    await update.message.reply_text(
        f"📊 *Your Usage Today*\n\n"
        f"Used      : {used} / {DAILY_LIMIT}\n"
        f"Remaining : {remaining}\n\n"
        "Resets at midnight 🌙",
        parse_mode="Markdown",
    )


async def on_verify_sub(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user  = query.from_user

    if not is_fsub_enabled() or await is_member(ctx.bot, user.id):
        remaining = max(DAILY_LIMIT - get_count(user.id), 0)
        await query.message.edit_text(
            f"✅ *Verified! Welcome, {user.first_name}!*\n\n"
            f"📊 Daily limit : *{DAILY_LIMIT}* downloads\n"
            f"✅ Remaining   : *{remaining}* today\n\n"
            "Send me any Instagram link 🚀",
            parse_mode="Markdown",
        )
    else:
        await query.message.edit_text(
            "❌ *Still not joined!*\n\n"
            "Please join the channel first, then tap the button again.",
            reply_markup=subscribe_keyboard(),
            parse_mode="Markdown",
        )


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_fsub(update, ctx):
        return

    user = update.effective_user
    text = (update.message.text or "").strip()

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
                "• Post may be private\n"
                "• Post may be deleted\n"
                "• Try again later"
            )
            return

        add_count(user.id)
        sent = 0

        for fpath in files:
            if os.path.getsize(fpath) > 50 * 1024 * 1024:
                await update.message.reply_text("⚠️ Skipped: file >50 MB (Telegram limit).")
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
            except Exception as e:
                logger.error(f"Send error: {e}")
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
            "• Post may be private\n"
            "• Instagram blocked the request\n\n"
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

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("status",     cmd_status))
    app.add_handler(CommandHandler("fsubon",     cmd_fsub_on))
    app.add_handler(CommandHandler("fsuboff",    cmd_fsub_off))
    app.add_handler(CommandHandler("fsubstatus", cmd_fsub_status))
    app.add_handler(CommandHandler("admin",      cmd_admin))
    app.add_handler(CallbackQueryHandler(on_verify_sub, pattern="^verify_sub$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    logger.info("Bot is running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
                         
