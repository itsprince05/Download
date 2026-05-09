"""
PocketFM Audio Capture Bot — Main Entry Point

Launches the Telegram bot with browser automation capabilities.
Uses long polling — no port needed, safe alongside other bots on the same VPS.
"""

import asyncio
import signal
import sys
import os
import subprocess

from logger import setup_logger
from config import DOWNLOADS_DIR, SCREENSHOTS_DIR, ALLOWED_GROUP_ID
from browser_manager import BrowserManager
from screenshot_streamer import ScreenshotStreamer
from audio_downloader import AudioDownloader
from telegram_handler import create_bot_and_dispatcher, set_dependencies

logger = setup_logger("main")


async def main():
    """Initialize all components and start the bot."""
    logger.info("=" * 60)
    logger.info("PocketFM Audio Capture Bot — Starting Up")
    logger.info("=" * 60)

    # ── Setup Virtual Display & Audio for DRM Recording ─────────
    logger.info("Setting up Xvfb and PulseAudio for real-time DRM recording...")
    os.environ["DISPLAY"] = ":99"
    subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1920x1080x24"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    # Start PulseAudio and create a virtual sink
    os.system("pulseaudio -D --exit-idle-time=-1 2>/dev/null")
    os.system("pactl load-module module-null-sink sink_name=VirtualSink 2>/dev/null")
    os.system("pactl set-default-sink VirtualSink 2>/dev/null")
    os.system("pactl set-default-source VirtualSink.monitor 2>/dev/null")

    # ── Create directories ──────────────────────────────────────
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    logger.info(f"Downloads dir: {DOWNLOADS_DIR}")
    logger.info(f"Screenshots dir: {SCREENSHOTS_DIR}")

    # ── Initialize components ───────────────────────────────────
    bot, dp = create_bot_and_dispatcher()
    browser_mgr = BrowserManager()
    screenshot_streamer = ScreenshotStreamer(bot)
    audio_downloader = AudioDownloader()

    # ── Inject dependencies into telegram handler ───────────────
    set_dependencies(browser_mgr, screenshot_streamer, audio_downloader)

    logger.info(f"Allowed Group ID: {ALLOWED_GROUP_ID}")
    logger.info("Bot initialized — starting polling (no port needed)...")

    # ── Graceful shutdown handler ───────────────────────────────
    async def shutdown():
        logger.info("Shutting down gracefully...")
        try:
            await screenshot_streamer.stop()
        except Exception as e:
            logger.debug(f"Screenshot stop error: {e}")
        try:
            await audio_downloader.cancel()
            await audio_downloader.close()
        except Exception as e:
            logger.debug(f"Downloader close error: {e}")
        try:
            await browser_mgr.close()
        except Exception as e:
            logger.debug(f"Browser close error: {e}")
        try:
            await bot.session.close()
        except Exception as e:
            logger.debug(f"Bot session close error: {e}")
        logger.info("Shutdown complete.")

    # ── Signal handlers for graceful cleanup ────────────────────
    loop = asyncio.get_event_loop()

    def signal_handler(sig):
        logger.info(f"Received signal {sig}")
        asyncio.ensure_future(shutdown())
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: signal_handler(s))
        except NotImplementedError:
            pass

    # ── Check if this is a restart after /update ──────────────────
    flag_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".update_restart")
    is_update_restart = False
    update_info = ""

    if os.path.exists(flag_path):
        is_update_restart = True
        try:
            with open(flag_path, "r") as f:
                lines = f.read().strip().split("\n", 1)
                update_info = lines[1] if len(lines) > 1 else "No details"
            os.remove(flag_path)
            logger.info("Update restart flag detected and removed")
        except Exception as e:
            logger.debug(f"Flag read error: {e}")

    # ── Send startup notification ───────────────────────────────
    try:
        if is_update_restart:
            await bot.send_message(
                ALLOWED_GROUP_ID,
                "✅ <b>Bot updated and restarted successfully!</b>\n\n"
                f"📋 Git: <code>{update_info[:300]}</code>\n\n"
                "🟢 Bot is now running with latest code.\n"
                "Send /help for all commands.",
                parse_mode="HTML",
            )
        else:
            await bot.send_message(
                ALLOWED_GROUP_ID,
                "🟢 <b>PocketFM Capture Bot is online!</b>\n\n"
                "📡 Supports: MPD/DASH, HLS, Direct files\n"
                "Send /capture to start audio extraction.\n"
                "Send /help for all commands.",
                parse_mode="HTML",
            )
    except Exception as e:
        logger.error(f"Failed to send startup message: {e}")

    # ── Start polling ───────────────────────────────────────────
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message"],
            drop_pending_updates=True,
        )
    except Exception as e:
        logger.error(f"Polling error: {e}", exc_info=True)
    finally:
        await shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by keyboard interrupt")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
