"""
Network request monitor — intercepts all browser traffic to capture audio/DASH URLs.
PocketFM uses MPEG-DASH (.mpd) streams, not direct mp3/mp4 files.
"""

import asyncio
from typing import Optional, Callable, Awaitable
from urllib.parse import urlparse

from logger import setup_logger
from config import AUDIO_EXTENSIONS, AUDIO_MIME_TYPES, MEDIA_URL_PATTERNS

logger = setup_logger("network_monitor")


class NetworkMonitor:
    """Monitors Playwright page network traffic for MPD/audio/media URLs."""

    def __init__(self):
        self.captured_urls: list[dict] = []
        self.best_audio_url: Optional[str] = None
        self._on_audio_found: Optional[Callable[[str], Awaitable[None]]] = None
        self._lock = asyncio.Lock()
        self._monitoring = False
        # Store ALL requests for debugging
        self.all_requests: list[dict] = []

    def set_callback(self, callback: Callable[[str], Awaitable[None]]):
        """Set async callback triggered when a new audio URL is found."""
        self._on_audio_found = callback

    def _is_audio_url(self, url: str, content_type: str = "") -> bool:
        """Check if URL or content-type indicates audio/DASH content."""
        parsed = urlparse(url)
        path_lower = parsed.path.lower()
        url_lower = url.lower()

        # MPD is the primary target for PocketFM
        if ".mpd" in url_lower:
            return True

        # Check file extension
        for ext in AUDIO_EXTENSIONS:
            if path_lower.endswith(ext):
                return True

        # Check content-type
        for mime in AUDIO_MIME_TYPES:
            if mime in content_type.lower():
                # Skip generic HTML/JSON responses
                if "text/html" in content_type.lower():
                    return False
                if "application/json" in content_type.lower():
                    return False
                return True

        # Check URL patterns — need at least 2 matches for non-extension URLs
        pattern_matches = sum(1 for p in MEDIA_URL_PATTERNS if p in url_lower)
        if pattern_matches >= 2:
            return True

        return False

    def _score_url(self, url: str, content_type: str = "", content_length: int = 0) -> int:
        """Score a captured URL — higher = more likely the actual audio source."""
        score = 0
        url_lower = url.lower()

        # ── MPD/DASH is highest priority for PocketFM ───────────
        if ".mpd" in url_lower:
            score += 200  # TOP PRIORITY
            if "audio" in url_lower:
                score += 50
            if "episode" in url_lower or "content" in url_lower:
                score += 40

        # ── DASH content types ──────────────────────────────────
        ct_lower = content_type.lower()
        if "dash+xml" in ct_lower:
            score += 180
        if "application/xml" in ct_lower and ".mpd" in url_lower:
            score += 170

        # ── Direct audio file extensions ────────────────────────
        for ext in [".mp3", ".m4a", ".aac", ".flac"]:
            if ext in url_lower:
                score += 100

        # ── HLS ─────────────────────────────────────────────────
        if ".m3u8" in url_lower:
            score += 90
        if ".ts" in url_lower:
            score += 50

        # ── DASH segment patterns ──────────────────────────────
        if "/dash/" in url_lower or "dash" in url_lower:
            score += 30
        if "init" in url_lower and (".mp4" in url_lower or ".m4s" in url_lower):
            score += 60  # DASH init segment
        if ".m4s" in url_lower:
            score += 40  # DASH media segment

        # ── CDN indicators ──────────────────────────────────────
        for cdn in ["cloudfront", "akamai", "fastly", "cdn", "cf-"]:
            if cdn in url_lower:
                score += 20

        # ── Audio content type ──────────────────────────────────
        if "audio/" in ct_lower:
            score += 80

        # ── Size indicators ─────────────────────────────────────
        if content_length > 1_000_000:
            score += 40
        elif content_length > 100_000:
            score += 20

        # ── PocketFM specific ───────────────────────────────────
        if "pocketfm" in url_lower:
            score += 30
            if "media" in url_lower or "audio" in url_lower or "episode" in url_lower:
                score += 40

        return score

    async def on_request(self, request):
        """Handle outgoing request event from Playwright."""
        url = request.url
        resource_type = request.resource_type

        # Log ALL non-trivial requests for debugging
        if resource_type not in ("image", "font", "stylesheet", "script"):
            self.all_requests.append({
                "url": url,
                "type": resource_type,
                "method": request.method,
            })

        if resource_type in ("media", "fetch", "xhr", "other"):
            logger.debug(f"[REQ] {resource_type}: {url[:200]}")

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

        # Check for MPD specifically (highest priority)
        if ".mpd" in url.lower() or "dash+xml" in content_type.lower():
            logger.info(f"[MPD DETECTED] {url}")
            logger.info(f"  Content-Type: {content_type}, Size: {content_length}")
            await self._register_audio_url(url, content_type, content_length)
            return

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
            logger.info(f"[CAPTURED] Score={score} | {url[:200]}")

            # Update best URL
            current_best_score = 0
            if self.best_audio_url:
                for e in self.captured_urls:
                    if e["url"] == self.best_audio_url:
                        current_best_score = e["score"]
                        break

            if score > current_best_score:
                self.best_audio_url = url
                logger.info(f"[BEST AUDIO] Updated → {url[:200]}")

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

    def get_mpd_urls(self) -> list[str]:
        """Return only MPD URLs found."""
        return [e["url"] for e in self.captured_urls if ".mpd" in e["url"].lower()]

    def get_debug_requests(self, limit: int = 30) -> list[dict]:
        """Return recent non-trivial requests for debugging."""
        return self.all_requests[-limit:]

    def reset(self):
        """Clear all captured data."""
        self.captured_urls.clear()
        self.all_requests.clear()
        self.best_audio_url = None
