"""
Telegram bot handler — processes commands ONLY from the allowed group.
PocketFM uses MPEG-DASH (.mpd) — this handler manages the full capture pipeline.
"""

import asyncio
import os
import sys
import time
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command
from aiogram.enums import ParseMode, ChatType

from logger import setup_logger
from config import BOT_TOKEN, ALLOWED_GROUP_ID, BASE_DIR

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
        "🤖 <b>PocketFM Audio Capture Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📋 <b>Commands:</b>\n"
        "├ /capture — Start full capture pipeline\n"
        "├ /status — Check current status\n"
        "├ /screenshot — Take a single screenshot\n"
        "├ /urls — Show all captured media URLs\n"
        "├ /debug — Show all network requests\n"
        "├ /update — Pull latest code & restart bot\n"
        "├ /stop — Stop current session\n"
        "└ /help — Show this message\n\n"
        "📡 <b>Supports:</b> MPD/DASH, HLS/M3U8, Direct files\n"
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
    """Main workflow — launch browser, navigate, detect MPD, download via ffmpeg, send."""
    global _capture_task

    if _capture_task and not _capture_task.done():
        await message.answer("⚠️ A capture session is already running. Use /stop first.")
        return

    _capture_task = asyncio.create_task(_run_capture(message))


async def _run_capture(message: Message):
    """Execute the full capture pipeline for MPD/DASH audio."""
    bot = message.bot
    chat_id = ALLOWED_GROUP_ID
    status_msg = await bot.send_message(
        chat_id,
        "🚀 <b>Starting PocketFM capture pipeline...</b>\n"
        "📡 Looking for MPEG-DASH (.mpd) streams",
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
            f"📄 Page loaded: <b>{page_info['title']}</b>\n"
            f"🔍 Looking for episodes..."
        )

        # ── Step 4: Play Episode ────────────────────────────────
        await _update_status(bot, status_msg, "▶️ Attempting to play first episode...")
        play_ok = await _browser_mgr.find_and_play_episode()

        if play_ok:
            await _update_status(
                bot, status_msg,
                "✅ Episode play triggered!\n📡 Scanning for MPD/DASH streams..."
            )
        else:
            await _update_status(
                bot, status_msg,
                "⚠️ Could not auto-click play.\n📡 Scanning network anyway..."
            )

        # ── Step 5: Wait for Audio/MPD URL ──────────────────────
        await _update_status(
            bot, status_msg,
            "🔍 Monitoring network traffic for .mpd / audio URLs...\n"
            "⏳ This may take up to 90 seconds..."
        )
        audio_url = await _browser_mgr.wait_for_audio_detection(timeout=90)

        if not audio_url:
            # Show debug info
            all_urls = _browser_mgr.network_monitor.get_all_urls()
            debug_reqs = _browser_mgr.network_monitor.get_debug_requests(20)

            debug_text = "❌ <b>No audio/MPD URL detected after 90s</b>\n\n"

            if all_urls:
                debug_text += "📡 <b>Captured media URLs:</b>\n"
                for u in all_urls[:10]:
                    debug_text += f"• [Score {u['score']}] <code>{u['url'][:80]}</code>\n"
            else:
                debug_text += "📡 No media URLs captured at all.\n"

            debug_text += f"\n📊 Total network requests: {len(_browser_mgr.network_monitor.all_requests)}"

            if debug_reqs:
                debug_text += "\n\n🔗 <b>Recent requests:</b>\n"
                for r in debug_reqs[-10:]:
                    debug_text += f"• [{r['type']}] <code>{r['url'][:70]}</code>\n"

            # Split if too long
            if len(debug_text) > 4000:
                debug_text = debug_text[:4000] + "..."

            await bot.send_message(chat_id, debug_text, parse_mode=ParseMode.HTML)
            await _update_status(bot, status_msg, "❌ Capture failed — no streams found.")
            return

        # Identify stream type
        stream_type = "MPD/DASH" if ".mpd" in audio_url.lower() else "HLS" if ".m3u8" in audio_url.lower() else "Direct"

        await _update_status(
            bot, status_msg,
            f"🎵 <b>{stream_type} stream found!</b>\n"
            f"<code>{audio_url[:120]}</code>\n\n"
            f"🔍 Probing stream info..."
        )

        # ── Step 5.5: Probe the stream ──────────────────────────
        probe_info = await _audio_downloader.probe_url(audio_url)
        if probe_info:
            streams = probe_info.get("streams", [])
            fmt = probe_info.get("format", {})
            duration = fmt.get("duration", "Unknown")
            format_name = fmt.get("format_name", "Unknown")

            audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
            info_text = (
                f"📊 <b>Stream Info:</b>\n"
                f"├ Format: {format_name}\n"
                f"├ Duration: {float(duration):.0f}s\n" if duration != "Unknown" else ""
                f"├ Audio tracks: {len(audio_streams)}\n"
            )
            if audio_streams:
                a = audio_streams[0]
                info_text += (
                    f"├ Codec: {a.get('codec_name', '?')}\n"
                    f"├ Sample rate: {a.get('sample_rate', '?')}Hz\n"
                    f"└ Bitrate: {int(a.get('bit_rate', 0)) // 1000}kbps\n"
                )
            await bot.send_message(chat_id, info_text, parse_mode=ParseMode.HTML)

        # ── Step 6: Download via FFmpeg ─────────────────────────
        await _update_status(
            bot, status_msg,
            f"⬇️ <b>Downloading {stream_type} stream via FFmpeg...</b>\n"
            f"This uses ffmpeg to properly download and mux the audio."
        )

        last_progress_update = [0]

        async def progress_cb(status_text, pct):
            now = time.time()
            if now - last_progress_update[0] < 3:
                return
            last_progress_update[0] = now

            if pct > 0:
                bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
                text = f"⬇️ <b>FFmpeg Download</b>\n[{bar}] {pct:.1f}%\n{status_text}"
            else:
                text = f"⬇️ <b>FFmpeg Download</b>\n{status_text}"

            try:
                await _update_status(bot, status_msg, text)
            except Exception:
                pass

        filepath = await _audio_downloader.download(audio_url, progress_callback=progress_cb)

        if not filepath:
            await _update_status(bot, status_msg, "❌ FFmpeg download failed after all retries.")

            # Show all URLs as fallback options
            all_urls = _browser_mgr.network_monitor.get_all_urls()
            if all_urls:
                text = "📡 <b>All detected URLs (try manually):</b>\n\n"
                for i, u in enumerate(all_urls[:10], 1):
                    text += f"{i}. [Score {u['score']}]\n<code>{u['url'][:120]}</code>\n\n"
                await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
            return

        file_size = os.path.getsize(filepath)
        size_mb = file_size / 1024 / 1024

        await _update_status(
            bot, status_msg,
            f"✅ Download complete! ({size_mb:.2f}MB)\n📤 Sending audio to group..."
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
                    f"📡 Stream: {stream_type}\n"
                    f"📄 {page_info.get('title', 'Unknown')}\n"
                    f"💾 Size: {size_mb:.2f}MB"
                ),
                parse_mode=ParseMode.HTML,
            )

        await _update_status(bot, status_msg, "✅ <b>Capture complete!</b> Audio sent successfully.")

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
    total_reqs = len(_browser_mgr.network_monitor.all_requests) if _browser_mgr else 0
    mpd_count = len(_browser_mgr.network_monitor.get_mpd_urls()) if _browser_mgr else 0

    page_info = {"title": "N/A", "url": "N/A"}
    if browser_running:
        page_info = await _browser_mgr.get_page_info()

    text = (
        "📊 <b>Bot Status</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🌐 Browser: {'🟢 Running' if browser_running else '🔴 Stopped'}\n"
        f"🎯 Capture: {'🟢 Active' if capture_running else '🔴 Idle'}\n"
        f"📡 Media URLs: {urls_count}\n"
        f"📺 MPD URLs: {mpd_count}\n"
        f"🔗 Total Requests: {total_reqs}\n"
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
    """Show all captured media URLs with types."""
    if not _browser_mgr:
        await message.answer("⚠️ Browser manager not initialized.")
        return

    urls = _browser_mgr.network_monitor.get_all_urls()
    if not urls:
        await message.answer("📡 No media URLs captured yet.")
        return

    text = "📡 <b>Captured Media URLs:</b>\n\n"
    for i, u in enumerate(urls[:15], 1):
        url_str = u['url']
        # Tag the type
        if ".mpd" in url_str.lower():
            tag = "🔴 MPD"
        elif ".m3u8" in url_str.lower():
            tag = "🟠 HLS"
        elif ".m4s" in url_str.lower() or ".ts" in url_str.lower():
            tag = "🟡 SEG"
        elif any(ext in url_str.lower() for ext in [".mp3", ".m4a", ".aac"]):
            tag = "🟢 FILE"
        else:
            tag = "⚪ OTHER"

        text += (
            f"<b>{i}.</b> {tag} | Score: {u['score']}\n"
            f"   <code>{url_str[:100]}</code>\n\n"
        )

    await message.answer(text, parse_mode=ParseMode.HTML)


# ─── /debug command ─────────────────────────────────────────────────
@router.message(Command("debug"), F.chat.id == ALLOWED_GROUP_ID)
async def cmd_debug(message: Message):
    """Show recent network requests for debugging."""
    if not _browser_mgr:
        await message.answer("⚠️ Browser not initialized.")
        return

    reqs = _browser_mgr.network_monitor.get_debug_requests(30)
    if not reqs:
        await message.answer("🔗 No network requests logged yet.")
        return

    text = f"🔗 <b>Recent Network Requests</b> ({len(_browser_mgr.network_monitor.all_requests)} total)\n\n"

    # Group by type
    type_counts = {}
    for r in _browser_mgr.network_monitor.all_requests:
        t = r['type']
        type_counts[t] = type_counts.get(t, 0) + 1

    text += "<b>Request types:</b>\n"
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        text += f"  {t}: {c}\n"

    text += "\n<b>Last 15 requests:</b>\n"
    for r in reqs[-15:]:
        text += f"• [{r['type']}] <code>{r['url'][:70]}</code>\n"

    if len(text) > 4000:
        text = text[:4000] + "..."

    await message.answer(text, parse_mode=ParseMode.HTML)


# ─── /update command ────────────────────────────────────────────────
@router.message(Command("update"), F.chat.id == ALLOWED_GROUP_ID)
async def cmd_update(message: Message):
    """Pull latest code from git, install deps, and restart the bot."""
    bot = message.bot
    chat_id = ALLOWED_GROUP_ID

    status_msg = await message.answer(
        "🔄 <b>Updating bot...</b>\n\n⏳ Running git pull...",
        parse_mode=ParseMode.HTML,
    )

    try:
        # ── Step 1: git pull ─────────────────────────────────────
        git_proc = await asyncio.create_subprocess_exec(
            "git", "pull", "--force",
            cwd=BASE_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        git_stdout, git_stderr = await asyncio.wait_for(git_proc.communicate(), timeout=60)
        git_output = git_stdout.decode("utf-8", errors="replace").strip()
        git_err = git_stderr.decode("utf-8", errors="replace").strip()
        git_code = git_proc.returncode

        if git_code != 0:
            await bot.edit_message_text(
                text=(
                    f"❌ <b>git pull failed</b> (code {git_code})\n\n"
                    f"<code>{git_err[:500] or git_output[:500]}</code>"
                ),
                chat_id=chat_id,
                message_id=status_msg.message_id,
                parse_mode=ParseMode.HTML,
            )
            return

        await bot.edit_message_text(
            text=(
                f"🔄 <b>Updating bot...</b>\n\n"
                f"✅ git pull done:\n<code>{git_output[:300]}</code>\n\n"
                f"⏳ Installing dependencies..."
            ),
            chat_id=chat_id,
            message_id=status_msg.message_id,
            parse_mode=ParseMode.HTML,
        )

        # ── Step 2: pip install ──────────────────────────────────
        pip_proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "--quiet",
            cwd=BASE_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        pip_stdout, pip_stderr = await asyncio.wait_for(pip_proc.communicate(), timeout=120)
        pip_output = pip_stdout.decode("utf-8", errors="replace").strip()
        pip_code = pip_proc.returncode

        pip_status = "✅" if pip_code == 0 else "⚠️"

        # ── Step 3: Create restart flag ──────────────────────────
        flag_path = os.path.join(BASE_DIR, ".update_restart")
        with open(flag_path, "w") as f:
            f.write(f"{int(time.time())}\n{git_output[:200]}")

        await bot.edit_message_text(
            text=(
                f"🔄 <b>Update complete!</b>\n\n"
                f"✅ git pull: <code>{git_output[:200]}</code>\n"
                f"{pip_status} pip install: code {pip_code}\n\n"
                f"🔁 <b>Restarting bot now...</b>"
            ),
            chat_id=chat_id,
            message_id=status_msg.message_id,
            parse_mode=ParseMode.HTML,
        )

        # Small delay so the message is sent before exit
        await asyncio.sleep(1)

        # ── Step 4: Exit — systemd will auto-restart ────────────
        logger.info("Bot exiting for update restart...")
        os._exit(0)

    except asyncio.TimeoutError:
        await bot.edit_message_text(
            text="❌ <b>Update timed out.</b> Check VPS manually.",
            chat_id=chat_id,
            message_id=status_msg.message_id,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Update error: {e}", exc_info=True)
        try:
            await bot.edit_message_text(
                text=f"❌ <b>Update failed:</b>\n<code>{str(e)[:500]}</code>",
                chat_id=chat_id,
                message_id=status_msg.message_id,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


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

    if _audio_downloader:
        await _audio_downloader.cancel()
        await _audio_downloader.close()
        actions.append("⬇️ Downloader stopped")

    if _browser_mgr and _browser_mgr.is_running:
        await _browser_mgr.close()
        actions.append("🌐 Browser closed")

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
