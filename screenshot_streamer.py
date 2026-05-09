"""
Screenshot streamer — captures full HD browser screenshots and sends them to Telegram.
"""

import asyncio
import os
import time
from io import BytesIO
from typing import Optional

from PIL import Image
from aiogram import Bot
from aiogram.types import BufferedInputFile

from logger import setup_logger
from config import (
    SCREENSHOTS_DIR,
    SCREENSHOT_INTERVAL,
    SCREENSHOT_QUALITY,
    MAX_SCREENSHOT_SIZE_KB,
    ALLOWED_GROUP_ID,
)

logger = setup_logger("screenshot_streamer")


class ScreenshotStreamer:
    """Continuously captures browser screenshots and streams them to Telegram."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._page = None
        self._message_id: Optional[int] = None

        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

    async def start(self, page):
        """Begin continuous screenshot streaming."""
        self._page = page
        self._running = True
        self._task = asyncio.create_task(self._stream_loop())
        logger.info("Screenshot streaming started")

    async def stop(self):
        """Stop the screenshot streaming."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._message_id = None
        logger.info("Screenshot streaming stopped")

    def _compress_screenshot(self, raw_bytes: bytes) -> bytes:
        """Compress screenshot to fit within Telegram's file size limit."""
        img = Image.open(BytesIO(raw_bytes))

        quality = SCREENSHOT_QUALITY
        while quality >= 10:
            buffer = BytesIO()
            img.save(buffer, format="JPEG", quality=quality, optimize=True)
            size_kb = buffer.tell() / 1024

            if size_kb <= MAX_SCREENSHOT_SIZE_KB:
                buffer.seek(0)
                return buffer.read()

            quality -= 10

        img = img.resize((1280, 720), Image.Resampling.LANCZOS)
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=40, optimize=True)
        buffer.seek(0)
        return buffer.read()

    async def _stream_loop(self):
        """Main loop — take screenshots and send/edit in Telegram."""
        frame_count = 0
        while self._running:
            try:
                if not self._page or self._page.is_closed():
                    logger.warning("Page is closed, stopping screenshots")
                    break

                raw = await self._page.screenshot(full_page=False, type="png")
                compressed = self._compress_screenshot(raw)
                frame_count += 1
                timestamp = time.strftime("%H:%M:%S")

                photo = BufferedInputFile(compressed, filename=f"live_{frame_count}.jpg")
                caption = f"🖥 Live Monitor | Frame #{frame_count} | {timestamp}"

                try:
                    if self._message_id:
                        from aiogram.types import InputMediaPhoto
                        media = InputMediaPhoto(media=photo, caption=caption)
                        await self.bot.edit_message_media(
                            chat_id=ALLOWED_GROUP_ID,
                            message_id=self._message_id,
                            media=media,
                        )
                    else:
                        msg = await self.bot.send_photo(
                            chat_id=ALLOWED_GROUP_ID,
                            photo=photo,
                            caption=caption,
                        )
                        self._message_id = msg.message_id
                except Exception as e:
                    error_str = str(e).lower()
                    if "message" in error_str or "not found" in error_str or "edit" in error_str:
                        try:
                            msg = await self.bot.send_photo(
                                chat_id=ALLOWED_GROUP_ID,
                                photo=photo,
                                caption=caption,
                            )
                            self._message_id = msg.message_id
                        except Exception as e2:
                            logger.error(f"Failed to send new screenshot: {e2}")
                    else:
                        logger.error(f"Screenshot send error: {e}")

                self._cleanup_old_files()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Screenshot loop error: {e}")

            await asyncio.sleep(SCREENSHOT_INTERVAL)

    def _cleanup_old_files(self):
        """Delete screenshot files older than 60 seconds."""
        try:
            now = time.time()
            for f in os.listdir(SCREENSHOTS_DIR):
                fpath = os.path.join(SCREENSHOTS_DIR, f)
                if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > 60:
                    os.remove(fpath)
        except Exception as e:
            logger.debug(f"Cleanup error: {e}")

    async def take_single(self) -> Optional[bytes]:
        """Take a single screenshot and return compressed bytes."""
        if not self._page or self._page.is_closed():
            return None
        try:
            raw = await self._page.screenshot(full_page=False, type="png")
            return self._compress_screenshot(raw)
        except Exception as e:
            logger.error(f"Single screenshot error: {e}")
            return None
