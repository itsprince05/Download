"""
Network request monitor — intercepts all browser traffic to capture audio URLs.
"""

import asyncio
from typing import Optional, Callable, Awaitable
from urllib.parse import urlparse

from logger import setup_logger
from config import AUDIO_EXTENSIONS, AUDIO_MIME_TYPES, MEDIA_URL_PATTERNS

logger = setup_logger("network_monitor")


class NetworkMonitor:
    """Monitors Playwright page network traffic for audio/media URLs."""

    def __init__(self):
        self.captured_urls: list[dict] = []
        self.best_audio_url: Optional[str] = None
        self._on_audio_found: Optional[Callable[[str], Awaitable[None]]] = None
        self._lock = asyncio.Lock()
        self._monitoring = False

    def set_callback(self, callback: Callable[[str], Awaitable[None]]):
        """Set async callback triggered when a new audio URL is found."""
        self._on_audio_found = callback

    def _is_audio_url(self, url: str, content_type: str = "") -> bool:
        """Check if URL or content-type indicates audio content."""
        parsed = urlparse(url)
        path_lower = parsed.path.lower()
        url_lower = url.lower()

        # Check file extension
        for ext in AUDIO_EXTENSIONS:
            if path_lower.endswith(ext):
                return True

        # Check content-type
        for mime in AUDIO_MIME_TYPES:
            if mime in content_type.lower():
                return True

        # Check URL patterns (less reliable, used as hints)
        pattern_matches = sum(1 for p in MEDIA_URL_PATTERNS if p in url_lower)
        if pattern_matches >= 2:
            return True

        return False

    def _score_url(self, url: str, content_type: str = "", content_length: int = 0) -> int:
        """Score a captured URL — higher = more likely the actual audio source."""
        score = 0
        url_lower = url.lower()

        # Direct audio file extensions are strongest signal
        for ext in [".mp3", ".m4a", ".aac", ".flac"]:
            if ext in url_lower:
                score += 100

        # HLS master/segment
        if ".m3u8" in url_lower:
            score += 90
        if ".ts" in url_lower:
            score += 50

        # CDN indicators
        for cdn in ["cloudfront", "akamai", "fastly", "cdn"]:
            if cdn in url_lower:
                score += 20

        # Content type boost
        if "audio/" in content_type.lower():
            score += 80

        # Larger files more likely to be actual audio
        if content_length > 1_000_000:
            score += 40
        elif content_length > 100_000:
            score += 20

        # PocketFM specific patterns
        if "pocketfm" in url_lower and ("media" in url_lower or "audio" in url_lower):
            score += 60

        return score

    async def on_request(self, request):
        """Handle outgoing request event from Playwright."""
        url = request.url
        resource_type = request.resource_type

        if resource_type in ("media", "fetch", "xhr", "other"):
            logger.debug(f"[REQ] {resource_type}: {url[:150]}")

            if self._is_audio_url(url):
                await self._register_audio_url(url, "", 0)

    async def on_response(self, response):
        """Handle incoming response event from Playwright."""
        url = response.url
        status = response.status

        if status < 200 or status >= 400:
            return

        try:
            headers = response.headers
            content_type = headers.get("content-type", "")
            content_length = int(headers.get("content-length", "0"))
        except Exception:
            content_type = ""
            content_length = 0

        if self._is_audio_url(url, content_type):
            logger.info(f"[AUDIO DETECTED] {url[:200]}")
            logger.info(f"  Content-Type: {content_type}, Size: {content_length}")
            await self._register_audio_url(url, content_type, content_length)

    async def _register_audio_url(self, url: str, content_type: str, content_length: int):
        """Register a detected audio URL and update best candidate."""
        async with self._lock:
            score = self._score_url(url, content_type, content_length)
            entry = {
                "url": url,
                "content_type": content_type,
                "content_length": content_length,
                "score": score,
            }

            # Avoid duplicates
            if any(e["url"] == url for e in self.captured_urls):
                return

            self.captured_urls.append(entry)
            logger.info(f"[CAPTURED] Score={score} | {url[:150]}")

            # Update best URL
            current_best_score = 0
            if self.best_audio_url:
                for e in self.captured_urls:
                    if e["url"] == self.best_audio_url:
                        current_best_score = e["score"]
                        break

            if score > current_best_score:
                self.best_audio_url = url
                logger.info(f"[BEST AUDIO] Updated → {url[:150]}")

                if self._on_audio_found:
                    try:
                        await self._on_audio_found(url)
                    except Exception as e:
                        logger.error(f"Audio callback error: {e}")

    def get_best_url(self) -> Optional[str]:
        """Return the highest-scored audio URL captured so far."""
        if not self.captured_urls:
            return None
        best = max(self.captured_urls, key=lambda x: x["score"])
        return best["url"]

    def get_all_urls(self) -> list[dict]:
        """Return all captured audio URLs sorted by score descending."""
        return sorted(self.captured_urls, key=lambda x: x["score"], reverse=True)

    def reset(self):
        """Clear all captured data."""
        self.captured_urls.clear()
        self.best_audio_url = None
