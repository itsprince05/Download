"""
Configuration constants for PocketFM Audio Capture Bot.
"""

import os

# ─── Telegram Credentials ───────────────────────────────────────────
BOT_TOKEN = "8668800584:AAHcjDEOI4pTt3FeGVTpenLB6hHriKSv-e4"
API_ID = 34896897
API_HASH = "33046911038f33514cb3b1128be1895d"
ALLOWED_GROUP_ID = -1003972987274

# ─── Target URL ─────────────────────────────────────────────────────
POCKETFM_URL = "https://pocketfm.com/show/4befca3da259b11359969ac8d4813650c587b440"

# ─── Directories ────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
SCREENSHOTS_DIR = os.path.join(BASE_DIR, "screenshots")

# ─── Browser Settings ───────────────────────────────────────────────
VIEWPORT_WIDTH = 1920
VIEWPORT_HEIGHT = 1080
SCREENSHOT_INTERVAL = 3  # seconds
SCREENSHOT_QUALITY = 60  # JPEG quality (1-100) for Telegram compression
MAX_SCREENSHOT_SIZE_KB = 9500  # Telegram limit ~10MB, keep under

# ─── Network Monitor Settings ───────────────────────────────────────
# PocketFM uses MPEG-DASH (.mpd) for streaming, NOT direct mp3/mp4
AUDIO_EXTENSIONS = [
    ".mpd",       # DASH manifest (PRIMARY for PocketFM)
    ".m3u8",      # HLS manifest
    ".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac", ".opus", ".webm",
]
AUDIO_MIME_TYPES = [
    "application/dash+xml",              # MPD content type
    "application/xml",                    # MPD sometimes served as XML
    "text/xml",                           # MPD sometimes served as text/xml
    "audio/",
    "video/",                             # DASH audio can be in video/ container
    "application/octet-stream",
    "application/x-mpegURL",
    "application/vnd.apple.mpegurl",
]
MEDIA_URL_PATTERNS = [
    ".mpd", "dash", "manifest",           # DASH patterns (PocketFM primary)
    "cdn", "media", "audio", "stream", "play", "hls",
    ".m3u8", ".ts", ".mp3", ".m4a", ".aac",
    "cloudfront", "akamai", "fastly",
    "pocketfm", "episode", "content",
]

# ─── Download Settings ──────────────────────────────────────────────
DOWNLOAD_TIMEOUT = 600  # 10 minutes max per download (DASH can be slow)
DOWNLOAD_CHUNK_SIZE = 1024 * 64  # 64KB chunks
MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds
FFMPEG_TIMEOUT = 600  # 10 minutes max for ffmpeg process

# ─── Browser User Agent (Mobile Spoofing to Bypass DRM) ───────
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36"
)

# ─── Logging ─────────────────────────────────────────────────────────
LOG_FILE = os.path.join(BASE_DIR, "bot.log")
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
