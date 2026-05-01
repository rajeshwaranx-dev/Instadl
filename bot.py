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
BOT_TOKEN    = "8352705831:AAH7auZWJWENgCIEtEVzGSiAcrK4ILFmwwU"      # From @BotFather
ADMIN_ID     = 7246154050                  # Your Telegram ID (@userinfobot)

# Force-Subscribe Channel
# Public  → "@mychannel"
# Private → -1001234567890  (forward a msg to @userinfobot to get ID)
FSUB_CHANNEL     = "@your_channel"
FSUB_INVITE_LINK = ""    # Private channel invite link, else leave ""

DAILY_LIMIT  = 10        # Max downloads per user per day
DOWNLOAD_DIR = "downloads"
DB_PATH      = "bot.db"
COOKIES_FILE = "cookies.txt"   # YouTube cookies (needed for age-restricted/blocked)

# Max file size Telegram allows (50 MB for bots)
MAX_FILE_BYTES = 50 * 1024 * 1024
# ╚══════════════════════════════════════════╝

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

YOUTUBE_REGEX = re.compile(
    r"(https?://)?(www\.)?"
    r"(youtube\.com/(watch\?v=|shorts/|playlist\?list=)|youtu\.be/)[\w\-]+"
)

# Quality options shown to user
QUALITY_OPTIONS = [
    ("🎵 Audio only (MP3)", "audio"),
    ("📱 360p  (smallest)", "360"),
    ("📺 480p  (medium)",   "480"),
    ("🖥️ 720p  (HD)",       "720"),
    ("🖥️ 1080p (Full HD — large file)", "1080"),
]

# ══════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                user_id INTEGER,
                day     TEXT,
                count   INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, day)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending (
                user_id INTEGER PRIMARY KEY,
                url     TEXT,
                title   TEXT
            )
        """)
        conn.execute("INSERT OR IGNORE INTO settings VALUES ('fsub_enabled','1')")
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
            INSERT INTO usage (user_id, day, count) VALUES (?,?,1)
            ON CONFLICT(user_id, day) DO UPDATE SET count=count+1
        """, (user_id, today))
        conn.commit()


def is_fsub_enabled() -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='fsub_enabled'"
        ).fetchone()
    return row[0] == "1" if row else True


def set_fsub(enabled: bool):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE settings SET value=? WHERE key='fsub_enabled'",
            ("1" if enabled else "0"),
        )
        conn.commit()


def save_pending(user_id: int, url: str, title: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO pending (user_id, url, title)
            VALUES (?,?,?)
        """, (user_id, url, title))
        conn.commit()


def get_pending(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT url, title FROM pending WHERE user_id=?", (user_id,)
        ).fetchone()
    return row  # (url, title) or None


def clear_pending(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM pending WHERE user_id=?", (user_id,))
        conn.commit()


# ══════════════════════════════════════════
#  FORCE-SUBSCRIBE
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
        logger.warning(f"is_member error: {e}")
        return True


def subscribe_keyboard() -> InlineKeyboardMarkup:
    if FSUB_INVITE_LINK:
        join_url = FSUB_INVITE_LINK
    else:
        join_url = f"https://t.me/{str(FSUB_CHANNEL).lstrip('@')}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=join_url)],
        [InlineKeyboardButton("✅ I've Joined — Check Again", callback_data="verify_sub")],
    ])


async def check_fsub(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    if not is_fsub_enabled():
        return True
    if await is_member(ctx.bot, update.effective_user.id):
        return True
    await update.message.reply_text(
        "🔒 *Access Restricted*\n\n"
        "You must join our channel to use this bot.\n\n"
        "1️⃣ Tap *Join Channel*\n"
        "2️⃣ Then tap *I've Joined — Check Again*",
        reply_markup=subscribe_keyboard(),
        parse_mode="Markdown",
    )
    return False


# ══════════════════════════════════════════
#  DOWNLOADER  — yt-dlp + Invidious fallback
# ══════════════════════════════════════════

import re as _re
import urllib.request as _req
import json as _json
import subprocess

# Public Invidious instances — tried in order if yt-dlp fails
INVIDIOUS_INSTANCES = [
    "https://inv.nadeko.net",
    "https://invidious.nerdvpn.de",
    "https://invidious.privacyredirect.com",
    "https://iv.datura.network",
]

QUALITY_HEIGHT = {"360": 360, "480": 480, "720": 720, "1080": 1080}


def _extract_video_id(url: str) -> str | None:
    """Pull YouTube video ID from any YouTube URL format."""
    patterns = [
        r"(?:v=|youtu\.be/|shorts/|embed/)([a-zA-Z0-9_-]{11})",
    ]
    for p in patterns:
        m = _re.search(p, url)
        if m:
            return m.group(1)
    return None


# ── Strategy 1: yt-dlp ──────────────────────────────────────────

def _ytdlp_opts(extra: dict = {}) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "nocheckcertificate": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["tv_embedded", "android_vr", "android", "ios"],
                "skip": ["translated_subs"],
            }
        },
        "http_headers": {
            "User-Agent": "com.google.android.youtube/19.09.37 (Linux; U; Android 11)",
        },
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    opts.update(extra)
    return opts


def _ytdlp_info(url: str) -> dict | None:
    try:
        opts = _ytdlp_opts({"skip_download": True})
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        logger.warning(f"yt-dlp info failed: {e}")
        return None


def _ytdlp_download(url: str, quality: str) -> str | None:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    template = f"{DOWNLOAD_DIR}/%(id)s_{quality}.%(ext)s"

    fmt = "bestaudio/best"
    post = []
    merge = None

    if quality == "audio":
        post = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        h = QUALITY_HEIGHT.get(quality, 360)
        fmt   = (f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
                 f"best[height<={h}][ext=mp4]/best[height<={h}]")
        merge = "mp4"

    extra = {"outtmpl": template, "format": fmt}
    if post:  extra["postprocessors"]    = post
    if merge: extra["merge_output_format"] = merge

    try:
        opts = _ytdlp_opts(extra)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info     = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            base     = os.path.splitext(filename)[0]
            for ext in (".mp4", ".mp3", ".webm", ".mkv", ".m4a"):
                c = base + ext
                if os.path.exists(c):
                    return c
            if os.path.exists(filename):
                return filename
    except Exception as e:
        logger.warning(f"yt-dlp download failed: {e}")
    return None


# ── Strategy 2: Invidious API ───────────────────────────────────

def _invidious_info(video_id: str) -> dict | None:
    """Fetch video metadata from Invidious API."""
    fields = "title,lengthSeconds,viewCount,formatStreams,adaptiveFormats,author"
    for instance in INVIDIOUS_INSTANCES:
        try:
            api_url = f"{instance}/api/v1/videos/{video_id}?fields={fields}"
            req     = _req.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
            with _req.urlopen(req, timeout=10) as r:
                return _json.loads(r.read().decode())
        except Exception as e:
            logger.warning(f"Invidious {instance} failed: {e}")
    return None


def _invidious_download(video_id: str, quality: str) -> str | None:
    """Download via Invidious stream URL."""
    data = _invidious_info(video_id)
    if not data:
        return None

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    out_path = f"{DOWNLOAD_DIR}/{video_id}_{quality}"

    if quality == "audio":
        # Get best audio stream
        audio_streams = [
            f for f in data.get("adaptiveFormats", [])
            if f.get("type", "").startswith("audio/mp4")
        ]
        if not audio_streams:
            audio_streams = [
                f for f in data.get("adaptiveFormats", [])
                if "audio" in f.get("type", "")
            ]
        if not audio_streams:
            return None

        audio_streams.sort(key=lambda x: int(x.get("bitrate", 0)), reverse=True)
        stream_url = audio_streams[0]["url"]
        raw_path   = out_path + ".m4a"
        mp3_path   = out_path + ".mp3"

        _download_url(stream_url, raw_path)
        if os.path.exists(raw_path):
            try:
                subprocess.run(
                    ["ffmpeg", "-i", raw_path, "-q:a", "2", "-y", mp3_path],
                    capture_output=True, timeout=120
                )
                os.remove(raw_path)
                return mp3_path if os.path.exists(mp3_path) else None
            except Exception:
                return raw_path

    else:
        h = QUALITY_HEIGHT.get(quality, 360)
        # formatStreams = combined video+audio (no merging needed)
        combined = data.get("formatStreams", [])
        # Pick closest quality
        combined_sorted = sorted(
            [f for f in combined if int(f.get("resolution", "0p").replace("p","") or 0) <= h],
            key=lambda x: int(x.get("resolution", "0p").replace("p","") or 0),
            reverse=True
        )
        if not combined_sorted:
            combined_sorted = sorted(
                combined,
                key=lambda x: int(x.get("resolution", "0p").replace("p","") or 0)
            )

        if not combined_sorted:
            return None

        stream_url = combined_sorted[0]["url"]
        mp4_path   = out_path + ".mp4"
        _download_url(stream_url, mp4_path)
        return mp4_path if os.path.exists(mp4_path) else None


def _download_url(url: str, dest: str):
    """Download a direct URL to a file."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36",
        "Referer":    "https://www.youtube.com/",
    }
    req = _req.Request(url, headers=headers)
    with _req.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)


# ── Public API ──────────────────────────────────────────────────

def _fetch_info_sync(url: str) -> dict:
    """Try yt-dlp first, fall back to Invidious."""
    # Try yt-dlp
    info = _ytdlp_info(url)
    if info:
        return info

    # Fall back to Invidious
    vid_id = _extract_video_id(url)
    if not vid_id:
        raise yt_dlp.utils.DownloadError("Could not extract video ID from URL")

    data = _invidious_info(vid_id)
    if not data:
        raise yt_dlp.utils.DownloadError("All download methods failed. Try again later.")

    # Normalise to yt-dlp-style dict
    return {
        "title":      data.get("title", "Unknown"),
        "duration":   data.get("lengthSeconds", 0),
        "view_count": data.get("viewCount", 0),
        "uploader":   data.get("author", ""),
        "_via":       "invidious",
        "_vid_id":    vid_id,
    }


def _download_sync(url: str, quality: str) -> str | None:
    """Try yt-dlp first, fall back to Invidious."""
    # Strategy 1: yt-dlp
    path = _ytdlp_download(url, quality)
    if path and os.path.exists(path):
        return path

    # Strategy 2: Invidious
    logger.info("yt-dlp failed, trying Invidious fallback…")
    vid_id = _extract_video_id(url)
    if vid_id:
        path = _invidious_download(vid_id, quality)
        if path and os.path.exists(path):
            return path

    return None


async def fetch_info(url: str) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_info_sync, url)


async def download_file(url: str, quality: str) -> str | None:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _download_sync, url, quality)

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
    await update.message.reply_text("✅ *Force-subscribe is ON.*", parse_mode="Markdown")


@admin_only
async def cmd_fsub_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    set_fsub(False)
    await update.message.reply_text("🔓 *Force-subscribe is OFF.*", parse_mode="Markdown")


@admin_only
async def cmd_fsub_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = "✅ ON" if is_fsub_enabled() else "🔓 OFF"
    await update.message.reply_text(
        f"📋 *FSub Status*\n\nStatus  : {state}\nChannel : `{FSUB_CHANNEL}`",
        parse_mode="Markdown",
    )


@admin_only
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠 *Admin Commands*\n\n"
        "`/fsubon`     — Enable force-subscribe\n"
        "`/fsuboff`    — Disable force-subscribe\n"
        "`/fsubstatus` — Check fsub status\n"
        "`/admin`      — This menu",
        parse_mode="Markdown",
    )


# ══════════════════════════════════════════
#  USER HANDLERS
# ══════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_fsub(update, ctx):
        return
    user      = update.effective_user
    remaining = max(DAILY_LIMIT - get_count(user.id), 0)
    await update.message.reply_text(
        f"👋 Hello, *{user.first_name}*!\n\n"
        "📥 *YouTube Downloader Bot*\n\n"
        "Supported:\n"
        "• 🎵 Audio (MP3)\n"
        "• 📱 360p Video\n"
        "• 📺 480p Video\n"
        "• 🖥️ 720p HD Video\n"
        "• 🖥️ 1080p Full HD Video\n"
        "• ▶️ YouTube Shorts\n\n"
        f"📊 Daily limit : *{DAILY_LIMIT}* downloads\n"
        f"✅ Remaining   : *{remaining}* today\n\n"
        "Just paste any YouTube link ⬇️",
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
            "Send me any YouTube link 🚀",
            parse_mode="Markdown",
        )
    else:
        await query.message.edit_text(
            "❌ *Still not joined!*\n\nPlease join the channel first.",
            reply_markup=subscribe_keyboard(),
            parse_mode="Markdown",
        )


async def on_quality_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User tapped a quality button."""
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if not query.data.startswith("dl_"):
        return

    quality = query.data[3:]   # e.g. "720", "audio"

    # Check limit again at download time
    if get_count(user.id) >= DAILY_LIMIT:
        await query.message.edit_text(
            f"⛔ *Daily limit reached!*\n\n"
            f"You've used all *{DAILY_LIMIT}* downloads for today.\n"
            "Come back tomorrow 🌅",
            parse_mode="Markdown",
        )
        return

    pending = get_pending(user.id)
    if not pending:
        await query.message.edit_text("❌ Session expired. Please send the link again.")
        return

    url, title = pending
    label = next((l for l, v in QUALITY_OPTIONS if v == quality), quality)

    await query.message.edit_text(
        f"⏳ Downloading *{quality.upper() if quality != 'audio' else 'MP3'}*…\n\n"
        f"📹 {title[:60]}",
        parse_mode="Markdown",
    )

    try:
        filepath = await download_file(url, quality)

        if not filepath or not os.path.exists(filepath):
            await query.message.edit_text(
                "❌ Download failed. The video may be unavailable in this quality.\n"
                "Try a lower quality."
            )
            return

        size = os.path.getsize(filepath)
        if size > MAX_FILE_BYTES:
            os.remove(filepath)
            size_mb = size / 1024 / 1024
            await query.message.edit_text(
                f"⚠️ *File too large for Telegram!*\n\n"
                f"This video is *{size_mb:.0f} MB* at {quality}p.\n"
                f"Telegram bots allow max 50 MB.\n\n"
                f"Please try a lower quality (360p or 480p).",
                parse_mode="Markdown",
            )
            return

        add_count(user.id)
        clear_pending(user.id)
        remaining = max(DAILY_LIMIT - get_count(user.id), 0)

        await query.message.edit_text(
            f"📤 Uploading… please wait.",
        )

        ext = os.path.splitext(filepath)[1].lower()
        with open(filepath, "rb") as f:
            caption = (
                f"📹 *{title[:100]}*\n"
                f"🎚 Quality: {label}\n"
                f"📊 Remaining today: *{remaining}*"
            )
            if ext == ".mp3" or quality == "audio":
                await ctx.bot.send_audio(
                    chat_id=user.id,
                    audio=f,
                    caption=caption,
                    parse_mode="Markdown",
                )
            else:
                await ctx.bot.send_video(
                    chat_id=user.id,
                    video=f,
                    caption=caption,
                    parse_mode="Markdown",
                    supports_streaming=True,
                )

        os.remove(filepath)
        await query.message.edit_text(
            f"✅ *Done!*\n📊 Remaining today: *{remaining}*",
            parse_mode="Markdown",
        )

    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        logger.error(f"yt-dlp error: {e}")
        if "age" in err or "login" in err or "cookie" in err:
            msg = "❌ This video is *age-restricted*. Add `cookies.txt` to the bot folder."
        elif "private" in err or "not available" in err:
            msg = "❌ This video is *private or unavailable*."
        elif "copyright" in err or "blocked" in err:
            msg = "❌ This video is *blocked/copyrighted* in your region."
        else:
            msg = "❌ Download failed. Try a different quality or try again later."
        await query.message.edit_text(msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        await query.message.edit_text("❌ Something went wrong. Please try again.")
        if filepath and os.path.exists(filepath):
            os.remove(filepath)


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_fsub(update, ctx):
        return

    user = update.effective_user
    text = (update.message.text or "").strip()

    if not YOUTUBE_REGEX.search(text):
        await update.message.reply_text(
            "❌ Please send a valid YouTube link.\n\n"
            "Examples:\n"
            "`youtube.com/watch?v=...`\n"
            "`youtu.be/...`\n"
            "`youtube.com/shorts/...`",
            parse_mode="Markdown",
        )
        return

    if get_count(user.id) >= DAILY_LIMIT:
        await update.message.reply_text(
            f"⛔ *Daily limit reached!*\n\n"
            f"You've used all *{DAILY_LIMIT}* downloads for today.\n"
            "Come back tomorrow 🌅",
            parse_mode="Markdown",
        )
        return

    # Fetch video info first
    fetching = await update.message.reply_text("🔍 Fetching video info…")

    try:
        info  = await fetch_info(text)
        title = info.get("title", "Unknown title")
        dur   = info.get("duration", 0)
        views = info.get("view_count", 0)

        # Format duration
        mins, secs = divmod(int(dur), 60)
        hrs,  mins = divmod(mins, 60)
        dur_str = f"{hrs}:{mins:02d}:{secs:02d}" if hrs else f"{mins}:{secs:02d}"

        # Format views
        views_str = f"{views:,}" if views else "N/A"

        # Save pending so we can use it when quality is picked
        save_pending(user.id, text, title)

        # Build quality keyboard
        keyboard = [
            [InlineKeyboardButton(label, callback_data=f"dl_{val}")]
            for label, val in QUALITY_OPTIONS
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await fetching.edit_text(
            f"📹 *{title[:100]}*\n\n"
            f"⏱ Duration : {dur_str}\n"
            f"👁 Views    : {views_str}\n\n"
            "🎚 *Select quality to download:*\n\n"
            "⚠️ Higher quality = larger file\n"
            "Telegram limit = 50 MB",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )

    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        logger.error(f"Info fetch error: {e}")
        if "age" in err or "login" in err:
            msg = "❌ This video is *age-restricted*. Bot needs `cookies.txt`."
        elif "private" in err:
            msg = "❌ This video is *private*."
        elif "not available" in err:
            msg = "❌ Video not available in your region."
        else:
            msg = "❌ Could not fetch video info. Check the link and try again."
        await fetching.edit_text(msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        await fetching.edit_text("❌ Something went wrong. Please try again.")


# ══════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════

def main():
    init_db()
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    app = Application.builder().token(BOT_TOKEN).build()

    # User commands
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("status",     cmd_status))

    # Admin commands
    app.add_handler(CommandHandler("fsubon",     cmd_fsub_on))
    app.add_handler(CommandHandler("fsuboff",    cmd_fsub_off))
    app.add_handler(CommandHandler("fsubstatus", cmd_fsub_status))
    app.add_handler(CommandHandler("admin",      cmd_admin))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_verify_sub,   pattern="^verify_sub$"))
    app.add_handler(CallbackQueryHandler(on_quality_pick, pattern="^dl_"))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    logger.info("YouTube Bot is running…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
