"""
Browser manager — Playwright-based Chromium controller for PocketFM automation.
Handles page navigation, episode detection, and network monitoring.
"""

import asyncio
import os
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from logger import setup_logger
from config import (
    POCKETFM_URL,
    VIEWPORT_WIDTH,
    VIEWPORT_HEIGHT,
    USER_AGENT,
    DOWNLOADS_DIR,
)
from network_monitor import NetworkMonitor

logger = setup_logger("browser_manager")


class BrowserManager:
    """Controls a Playwright Chromium instance for PocketFM audio capture."""

    def __init__(self):
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self.network_monitor = NetworkMonitor()
        self._launched = False

        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

    @property
    def page(self) -> Optional[Page]:
        return self._page

    @property
    def is_running(self) -> bool:
        return self._launched and self._page is not None and not self._page.is_closed()

    async def launch(self) -> Page:
        """Launch Chromium browser and open a blank page."""
        if self._launched:
            logger.warning("Browser already launched")
            return self._page

        logger.info("Launching Chromium browser...")
        self._playwright = await async_playwright().start()

        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas",
                "--disable-gpu",
                "--no-first-run",
                "--no-zygote",
                "--single-process",
                "--disable-background-networking",
                "--disable-extensions",
                "--disable-sync",
                "--disable-translate",
                "--mute-audio",
                "--disable-web-security",
            ],
        )

        self._context = await self._browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="Asia/Kolkata",
            accept_downloads=True,
            java_script_enabled=True,
            ignore_https_errors=True,
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9,hi;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "DNT": "1",
            },
        )

        self._page = await self._context.new_page()

        # Attach network monitors
        self._page.on("request", self.network_monitor.on_request)
        self._page.on("response", self.network_monitor.on_response)

        self._launched = True
        logger.info("Browser launched successfully")
        return self._page

    async def navigate_to_show(self) -> bool:
        """Navigate to the PocketFM show page."""
        if not self.is_running:
            logger.error("Browser not running — call launch() first")
            return False

        try:
            logger.info(f"Navigating to: {POCKETFM_URL}")
            await self._page.goto(
                POCKETFM_URL,
                wait_until="networkidle",
                timeout=60000,
            )
            logger.info("Page loaded successfully")

            # Wait for dynamic content to load
            await asyncio.sleep(5)
            return True

        except Exception as e:
            logger.error(f"Navigation error: {e}")
            return False

    async def find_and_play_episode(self) -> bool:
        """Find the first episode on the show page and click play."""
        if not self.is_running:
            return False

        try:
            logger.info("Looking for the first episode / play button...")

            # Strategy 1: Look for episode list items
            episode_selectors = [
                '[class*="episode"] >> nth=0',
                '[class*="Episode"] >> nth=0',
                '[data-testid*="episode"] >> nth=0',
                '[class*="track"] >> nth=0',
                '[class*="Track"] >> nth=0',
                'button[aria-label*="play" i] >> nth=0',
                'button[aria-label*="Play" i] >> nth=0',
                '[class*="play" i] >> nth=0',
                '[class*="Play" i] >> nth=0',
                'svg[class*="play" i] >> nth=0',
                '[role="button"][class*="play" i] >> nth=0',
                '[class*="episode-list"] a >> nth=0',
                '[class*="episodeList"] a >> nth=0',
                '[class*="episode_list"] a >> nth=0',
            ]

            for selector in episode_selectors:
                try:
                    el = self._page.locator(selector)
                    if await el.count() > 0:
                        logger.info(f"Found element: {selector}")
                        await el.click(timeout=5000)
                        logger.info("Clicked episode/play element!")
                        await asyncio.sleep(5)
                        return True
                except Exception:
                    continue

            # Strategy 2: JavaScript-based detection
            logger.info("Trying JS-based play detection...")
            played = await self._page.evaluate("""
                () => {
                    const selectors = [
                        '[class*="play"]',
                        '[class*="Play"]',
                        'button[class*="episode"]',
                        'button[class*="Episode"]',
                        '[class*="listen"]',
                        '[class*="Listen"]',
                    ];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el) {
                            el.click();
                            return sel;
                        }
                    }
                    const links = document.querySelectorAll('a[href*="episode"], a[href*="listen"]');
                    if (links.length > 0) {
                        links[0].click();
                        return 'link:' + links[0].href;
                    }
                    return null;
                }
            """)

            if played:
                logger.info(f"JS play triggered via: {played}")
                await asyncio.sleep(5)
                return True

            # Strategy 3: Look for audio/video elements already present
            has_media = await self._page.evaluate("""
                () => {
                    const audio = document.querySelector('audio');
                    const video = document.querySelector('video');
                    if (audio && audio.src) {
                        audio.play();
                        return 'audio:' + audio.src;
                    }
                    if (video && video.src) {
                        video.play();
                        return 'video:' + video.src;
                    }
                    return null;
                }
            """)

            if has_media:
                logger.info(f"Found existing media element: {has_media}")
                return True

            logger.warning("Could not find play button — may need manual interaction")
            return False

        except Exception as e:
            logger.error(f"Episode play error: {e}")
            return False

    async def wait_for_audio_detection(self, timeout: int = 60) -> Optional[dict]:
        """Wait up to `timeout` seconds for an audio/MPD URL to be captured."""
        logger.info(f"Waiting up to {timeout}s for audio/MPD URL detection...")

        for i in range(timeout):
            # Check for MPD URLs first (highest priority for PocketFM)
            mpd_urls = self.network_monitor.get_mpd_urls()
            if mpd_urls:
                logger.info(f"MPD URL detected after {i+1}s: {mpd_urls[0]['url'][:200]}")
                return mpd_urls[0]

            # Then check general best URL
            url_entry = self.network_monitor.get_best_url()
            if url_entry:
                logger.info(f"Audio URL detected after {i+1}s: {url_entry['url'][:200]}")
                return url_entry

            await asyncio.sleep(1)

        logger.warning(f"No audio/MPD URL detected after {timeout}s")
        return None

    async def get_playback_progress(self) -> Optional[dict]:
        """Get the playback progress of the active media element."""
        if not self.is_running:
            return None
        try:
            return await self._page.evaluate("""
                () => {
                    const media = document.querySelector('audio, video');
                    if (!media) return null;
                    return {
                        currentTime: media.currentTime,
                        duration: media.duration,
                        ended: media.ended,
                        paused: media.paused
                    };
                }
            """)
        except Exception as e:
            logger.debug(f"Playback progress error: {e}")
            return None

    async def get_page_info(self) -> dict:
        """Get current page title and URL."""
        if not self.is_running:
            return {"title": "N/A", "url": "N/A"}
        try:
            return {
                "title": await self._page.title(),
                "url": self._page.url,
            }
        except Exception:
            return {"title": "Error", "url": "Error"}

    async def close(self):
        """Gracefully close browser and Playwright."""
        logger.info("Closing browser...")
        try:
            if self._page and not self._page.is_closed():
                await self._page.close()
        except Exception as e:
            logger.debug(f"Page close error: {e}")

        try:
            if self._context:
                await self._context.close()
        except Exception as e:
            logger.debug(f"Context close error: {e}")

        try:
            if self._browser:
                await self._browser.close()
        except Exception as e:
            logger.debug(f"Browser close error: {e}")

        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.debug(f"Playwright stop error: {e}")

        self._launched = False
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        logger.info("Browser closed")
