"""
Telegram bot handler — processes commands ONLY from the allowed group.
"""

import asyncio
import os
import time
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command
from aiogram.enums import ParseMode, ChatType

from logger import setup_logger
from config import BOT_TOKEN, ALLOWED_GROUP_ID

logger = setup_logger("telegram_handler")

router = Router()

# ─── Global references (set by main.py) ─────────────────────────────
_browser_mgr = None
_screenshot_streamer = None
_audio_downloader = None
_capture_task: Optional[asyncio.Task] = None


def set_dependencies(browser_mgr, screenshot_streamer, audio_downloader):
    """Inject dependencies from main.py."""
    global _browser_mgr, _screenshot_streamer, _audio_downloader
    _browser_mgr = browser_mgr
    _screenshot_streamer = screenshot_streamer
    _audio_downloader = audio_downloader


def _is_allowed(message: Message) -> bool:
    """Check if message is from the allowed group."""
    return message.chat.id == ALLOWED_GROUP_ID


# ─── Silently ignore everything from non-allowed chats ──────────────
@router.message(~F.chat.id.in_({ALLOWED_GROUP_ID}))
async def ignore_other_chats(message: Message):
    """Completely silent — no reply, no error, no typing. Dead."""
    return


# ─── /start command ─────────────────────────────────────────────────
@router.message(Command("start"), F.chat.id == ALLOWED_GROUP_ID)
async def cmd_start(message: Message):
    """Send welcome info."""
    text = (
        "🤖 <b>PocketFM Audio Capture Bot</b>\n\n"
        "📋 <b>Available Commands:</b>\n"
        "├ /capture — Start browser, detect & download audio\n"
        "├ /status — Check current status\n"
        "├ /screenshot — Take a single screenshot\n"
        "├ /urls — Show all captured audio URLs\n"
        "├ /stop — Stop current capture session\n"
        "└ /help — Show this message\n\n"
        "⚡ Bot responds ONLY in this group."
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


# ─── /help command ──────────────────────────────────────────────────
@router.message(Command("help"), F.chat.id == ALLOWED_GROUP_ID)
async def cmd_help(message: Message):
    await cmd_start(message)


# ─── /capture command ───────────────────────────────────────────────
@router.message(Command("capture"), F.chat.id == ALLOWED_GROUP_ID)
async def cmd_capture(message: Message):
    """Main workflow — launch browser, navigate, detect audio, download, send."""
    global _capture_task

    if _capture_task and not _capture_task.done():
        await message.answer("⚠️ A capture session is already running. Use /stop first.")
        return

    _capture_task = asyncio.create_task(_run_capture(message))


async def _run_capture(message: Message):
    """Execute the full capture pipeline."""
    bot = message.bot
    chat_id = ALLOWED_GROUP_ID
    status_msg = await bot.send_message(
        chat_id,
        "🚀 <b>Starting capture pipeline...</b>",
        parse_mode=ParseMode.HTML,
    )

    try:
        # ── Step 1: Launch Browser ──────────────────────────────
        await _update_status(bot, status_msg, "🌐 Launching Chromium browser...")
        page = await _browser_mgr.launch()

        # ── Step 2: Start Screenshot Streaming ──────────────────
        await _update_status(bot, status_msg, "📸 Starting live screenshot stream...")
        await _screenshot_streamer.start(page)

        # ── Step 3: Navigate to PocketFM ────────────────────────
        await _update_status(bot, status_msg, "🔗 Navigating to PocketFM show page...")
        nav_ok = await _browser_mgr.navigate_to_show()
        if not nav_ok:
            await _update_status(bot, status_msg, "❌ Failed to load PocketFM page.")
            return

        page_info = await _browser_mgr.get_page_info()
        await _update_status(
            bot, status_msg,
            f"📄 Page loaded: <b>{page_info['title']}</b>\n🔍 Looking for episodes..."
        )

        # ── Step 4: Play Episode ────────────────────────────────
        await _update_status(bot, status_msg, "▶️ Attempting to play first episode...")
        play_ok = await _browser_mgr.find_and_play_episode()

        if play_ok:
            await _update_status(bot, status_msg, "✅ Episode play triggered! Monitoring network...")
        else:
            await _update_status(
                bot, status_msg,
                "⚠️ Could not auto-click play. Monitoring network anyway..."
            )

        # ── Step 5: Wait for Audio URL ──────────────────────────
        await _update_status(bot, status_msg, "🔍 Scanning network traffic for audio URLs...")
        audio_url = await _browser_mgr.wait_for_audio_detection(timeout=90)

        if not audio_url:
            # Show all captured URLs for debugging
            all_urls = _browser_mgr.network_monitor.get_all_urls()
            if all_urls:
                url_list = "\n".join(
                    f"• Score {u['score']}: <code>{u['url'][:80]}</code>"
                    for u in all_urls[:10]
                )
                await bot.send_message(
                    chat_id,
                    f"📡 <b>Captured URLs (no audio confirmed):</b>\n{url_list}",
                    parse_mode=ParseMode.HTML,
                )
            await _update_status(bot, status_msg, "❌ No audio URL detected after 90s.")
            return

        await _update_status(
            bot, status_msg,
            f"🎵 Audio URL found!\n<code>{audio_url[:100]}</code>\n\n⬇️ Downloading..."
        )

        # ── Step 6: Download Audio ──────────────────────────────
        last_progress_update = 0

        async def progress_cb(downloaded, total):
            nonlocal last_progress_update
            now = time.time()
            if now - last_progress_update < 3:
                return
            last_progress_update = now

            if total > 0:
                pct = (downloaded / total) * 100
                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                size_mb = downloaded / 1024 / 1024
                total_mb = total / 1024 / 1024
                text = (
                    f"⬇️ <b>Downloading...</b>\n"
                    f"[{bar}] {pct:.1f}%\n"
                    f"{size_mb:.1f}MB / {total_mb:.1f}MB"
                )
            else:
                text = f"⬇️ Downloading... {downloaded} segments"
            try:
                await _update_status(bot, status_msg, text)
            except Exception:
                pass

        filepath = await _audio_downloader.download(audio_url, progress_callback=progress_cb)

        if not filepath:
            await _update_status(bot, status_msg, "❌ Download failed after all retries.")
            return

        file_size = os.path.getsize(filepath)
        size_mb = file_size / 1024 / 1024

        await _update_status(
            bot, status_msg,
            f"✅ Download complete! ({size_mb:.2f}MB)\n📤 Sending to group..."
        )

        # ── Step 7: Send Audio to Group ─────────────────────────
        if file_size > 50 * 1024 * 1024:
            await bot.send_message(
                chat_id,
                f"⚠️ File too large for Telegram ({size_mb:.1f}MB > 50MB limit).\n"
                f"📁 Saved at: <code>{filepath}</code>",
                parse_mode=ParseMode.HTML,
            )
        else:
            audio_file = FSInputFile(filepath)
            page_info = await _browser_mgr.get_page_info()
            await bot.send_audio(
                chat_id,
                audio=audio_file,
                title=page_info.get("title", "PocketFM Audio"),
                caption=(
                    f"🎧 <b>PocketFM Audio Capture</b>\n"
                    f"📄 {page_info.get('title', 'Unknown')}\n"
                    f"💾 Size: {size_mb:.2f}MB"
                ),
                parse_mode=ParseMode.HTML,
            )

        await _update_status(bot, status_msg, "✅ <b>Capture complete!</b> Audio sent successfully.")

        # Show all captured URLs for reference
        all_urls = _browser_mgr.network_monitor.get_all_urls()
        if all_urls:
            url_list = "\n".join(
                f"• Score {u['score']}: <code>{u['url'][:80]}...</code>"
                for u in all_urls[:5]
            )
            await bot.send_message(
                chat_id,
                f"📡 <b>All captured audio URLs:</b>\n{url_list}",
                parse_mode=ParseMode.HTML,
            )

    except asyncio.CancelledError:
        await bot.send_message(chat_id, "🛑 Capture session cancelled.")
    except Exception as e:
        logger.error(f"Capture pipeline error: {e}", exc_info=True)
        try:
            await bot.send_message(
                chat_id,
                f"❌ <b>Capture error:</b>\n<code>{str(e)[:500]}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        # Stop screenshot streaming
        try:
            await _screenshot_streamer.stop()
        except Exception:
            pass


async def _update_status(bot: Bot, msg: Message, text: str):
    """Edit the status message with new text."""
    try:
        await bot.edit_message_text(
            text=text,
            chat_id=msg.chat.id,
            message_id=msg.message_id,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.debug(f"Status update error: {e}")


# ─── /status command ────────────────────────────────────────────────
@router.message(Command("status"), F.chat.id == ALLOWED_GROUP_ID)
async def cmd_status(message: Message):
    """Show current bot status."""
    browser_running = _browser_mgr.is_running if _browser_mgr else False
    capture_running = _capture_task is not None and not _capture_task.done()
    urls_count = len(_browser_mgr.network_monitor.captured_urls) if _browser_mgr else 0

    page_info = {"title": "N/A", "url": "N/A"}
    if browser_running:
        page_info = await _browser_mgr.get_page_info()

    text = (
        "📊 <b>Bot Status</b>\n\n"
        f"🌐 Browser: {'🟢 Running' if browser_running else '🔴 Stopped'}\n"
        f"🎯 Capture: {'🟢 Active' if capture_running else '🔴 Idle'}\n"
        f"📡 Captured URLs: {urls_count}\n"
        f"📄 Page: {page_info['title'][:50]}\n"
        f"🔗 URL: <code>{page_info['url'][:80]}</code>"
    )
    await message.answer(text, parse_mode=ParseMode.HTML)


# ─── /screenshot command ────────────────────────────────────────────
@router.message(Command("screenshot"), F.chat.id == ALLOWED_GROUP_ID)
async def cmd_screenshot(message: Message):
    """Take and send a single screenshot."""
    if not _browser_mgr or not _browser_mgr.is_running:
        await message.answer("⚠️ Browser is not running. Use /capture first.")
        return

    data = await _screenshot_streamer.take_single()
    if data:
        from aiogram.types import BufferedInputFile

        photo = BufferedInputFile(data, filename="screenshot.jpg")
        await message.answer_photo(photo, caption="📸 Manual screenshot")
    else:
        await message.answer("❌ Failed to take screenshot.")


# ─── /urls command ──────────────────────────────────────────────────
@router.message(Command("urls"), F.chat.id == ALLOWED_GROUP_ID)
async def cmd_urls(message: Message):
    """Show all captured audio URLs."""
    if not _browser_mgr:
        await message.answer("⚠️ Browser manager not initialized.")
        return

    urls = _browser_mgr.network_monitor.get_all_urls()
    if not urls:
        await message.answer("📡 No audio URLs captured yet.")
        return

    text = "📡 <b>Captured Audio URLs:</b>\n\n"
    for i, u in enumerate(urls[:15], 1):
        text += (
            f"<b>{i}.</b> Score: {u['score']}\n"
            f"   Type: {u['content_type'][:30] or 'Unknown'}\n"
            f"   <code>{u['url'][:100]}</code>\n\n"
        )
    await message.answer(text, parse_mode=ParseMode.HTML)


# ─── /stop command ──────────────────────────────────────────────────
@router.message(Command("stop"), F.chat.id == ALLOWED_GROUP_ID)
async def cmd_stop(message: Message):
    """Stop the current capture session and close browser."""
    global _capture_task

    actions = []

    if _capture_task and not _capture_task.done():
        _capture_task.cancel()
        _capture_task = None
        actions.append("🛑 Capture task cancelled")

    if _screenshot_streamer:
        await _screenshot_streamer.stop()
        actions.append("📸 Screenshot streaming stopped")

    if _browser_mgr and _browser_mgr.is_running:
        await _browser_mgr.close()
        actions.append("🌐 Browser closed")

    if _audio_downloader:
        await _audio_downloader.close()
        actions.append("⬇️ Downloader session closed")

    if actions:
        text = "✅ <b>Session stopped:</b>\n" + "\n".join(f"  {a}" for a in actions)
    else:
        text = "ℹ️ Nothing was running."

    await message.answer(text, parse_mode=ParseMode.HTML)


def create_bot_and_dispatcher() -> tuple[Bot, Dispatcher]:
    """Create and configure the Bot and Dispatcher instances."""
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    return bot, dp
