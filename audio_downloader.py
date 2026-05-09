"""
Audio downloader — downloads captured audio URLs with retry logic.
"""

import asyncio
import os
import time
import re
from typing import Optional
from urllib.parse import urlparse, unquote

import aiohttp

from logger import setup_logger
from config import (
    DOWNLOADS_DIR,
    DOWNLOAD_TIMEOUT,
    DOWNLOAD_CHUNK_SIZE,
    MAX_RETRIES,
    RETRY_DELAY,
    USER_AGENT,
)

logger = setup_logger("audio_downloader")


class AudioDownloader:
    """Downloads audio files from captured URLs with retry and progress tracking."""

    def __init__(self):
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Connection": "keep-alive",
                },
            )
        return self._session

    def _generate_filename(self, url: str, content_type: str = "") -> str:
        """Generate a clean filename from URL or content type."""
        parsed = urlparse(url)
        path = unquote(parsed.path)
        basename = os.path.basename(path)

        # Clean up the filename
        basename = re.sub(r'[<>:"/\\|?*]', '_', basename)

        if not basename or basename == "/" or len(basename) < 3:
            # Generate from timestamp
            timestamp = int(time.time())
            ext = self._guess_extension(url, content_type)
            basename = f"pocketfm_audio_{timestamp}{ext}"

        # Ensure it has an audio extension
        _, ext = os.path.splitext(basename)
        if ext.lower() not in [".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac", ".opus", ".webm", ".ts"]:
            guessed = self._guess_extension(url, content_type)
            basename = f"{basename}{guessed}"

        return basename

    def _guess_extension(self, url: str, content_type: str = "") -> str:
        """Guess file extension from URL or content type."""
        url_lower = url.lower()

        if ".mp3" in url_lower:
            return ".mp3"
        if ".m4a" in url_lower:
            return ".m4a"
        if ".aac" in url_lower:
            return ".aac"
        if ".m3u8" in url_lower:
            return ".m3u8"
        if ".ts" in url_lower:
            return ".ts"

        ct = content_type.lower()
        if "mp3" in ct or "mpeg" in ct:
            return ".mp3"
        if "mp4" in ct or "m4a" in ct:
            return ".m4a"
        if "aac" in ct:
            return ".aac"
        if "ogg" in ct:
            return ".ogg"

        return ".mp3"  # Default

    async def download(self, url: str, progress_callback=None) -> Optional[str]:
        """
        Download audio from URL. Returns the local file path on success.

        Args:
            url: The audio URL to download
            progress_callback: Optional async callback(downloaded_bytes, total_bytes)
        """
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(f"Download attempt {attempt}/{MAX_RETRIES}: {url[:150]}")
                result = await self._do_download(url, progress_callback)
                if result:
                    return result
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Download attempt {attempt} failed: {e}")

            if attempt < MAX_RETRIES:
                logger.info(f"Retrying in {RETRY_DELAY}s...")
                await asyncio.sleep(RETRY_DELAY)

        logger.error(f"All {MAX_RETRIES} download attempts failed for: {url[:150]}")
        return None

    async def _do_download(self, url: str, progress_callback=None) -> Optional[str]:
        """Execute the actual download."""
        session = await self._get_session()

        # Check if it's an M3U8 (HLS) URL
        if ".m3u8" in url.lower():
            return await self._download_hls(url, progress_callback)

        async with session.get(url) as response:
            if response.status != 200:
                logger.error(f"HTTP {response.status} for {url[:100]}")
                return None

            content_type = response.headers.get("Content-Type", "")
            total = int(response.headers.get("Content-Length", 0))
            filename = self._generate_filename(url, content_type)
            filepath = os.path.join(DOWNLOADS_DIR, filename)

            logger.info(f"Downloading: {filename} | Size: {total / 1024 / 1024:.2f}MB")

            downloaded = 0
            with open(filepath, "wb") as f:
                async for chunk in response.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                    f.write(chunk)
                    downloaded += len(chunk)

                    if progress_callback and total > 0:
                        try:
                            await progress_callback(downloaded, total)
                        except Exception:
                            pass

            file_size = os.path.getsize(filepath)
            logger.info(f"Download complete: {filename} ({file_size / 1024 / 1024:.2f}MB)")

            if file_size < 1000:
                logger.warning(f"File too small ({file_size}B), likely not audio")
                os.remove(filepath)
                return None

            return filepath

    async def _download_hls(self, m3u8_url: str, progress_callback=None) -> Optional[str]:
        """Download HLS stream by fetching the M3U8 and concatenating segments."""
        session = await self._get_session()

        logger.info(f"Fetching HLS manifest: {m3u8_url[:100]}")
        async with session.get(m3u8_url) as resp:
            if resp.status != 200:
                logger.error(f"Failed to fetch M3U8: HTTP {resp.status}")
                return None
            manifest = await resp.text()

        # Check if it's a master playlist (contains other m3u8 references)
        if "#EXT-X-STREAM-INF" in manifest:
            # Parse and pick highest quality variant
            lines = manifest.strip().split("\n")
            best_bandwidth = 0
            best_url = None

            for i, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF"):
                    bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                    if bw_match:
                        bw = int(bw_match.group(1))
                        if bw > best_bandwidth and i + 1 < len(lines):
                            best_bandwidth = bw
                            variant_url = lines[i + 1].strip()
                            if not variant_url.startswith("http"):
                                # Relative URL
                                base = m3u8_url.rsplit("/", 1)[0]
                                variant_url = f"{base}/{variant_url}"
                            best_url = variant_url

            if best_url:
                logger.info(f"Selected highest quality variant: BW={best_bandwidth}")
                return await self._download_hls(best_url, progress_callback)
            else:
                logger.error("No variant found in master playlist")
                return None

        # It's a media playlist — download segments
        lines = manifest.strip().split("\n")
        segment_urls = []
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                if not line.startswith("http"):
                    base = m3u8_url.rsplit("/", 1)[0]
                    line = f"{base}/{line}"
                segment_urls.append(line)

        if not segment_urls:
            logger.error("No segments found in HLS playlist")
            return None

        logger.info(f"Found {len(segment_urls)} HLS segments")

        timestamp = int(time.time())
        filepath = os.path.join(DOWNLOADS_DIR, f"pocketfm_audio_{timestamp}.aac")

        downloaded = 0
        total_segments = len(segment_urls)

        with open(filepath, "wb") as f:
            for idx, seg_url in enumerate(segment_urls, 1):
                try:
                    async with session.get(seg_url) as seg_resp:
                        if seg_resp.status == 200:
                            data = await seg_resp.read()
                            f.write(data)
                            downloaded += len(data)

                            if progress_callback:
                                try:
                                    await progress_callback(idx, total_segments)
                                except Exception:
                                    pass
                        else:
                            logger.warning(f"Segment {idx} failed: HTTP {seg_resp.status}")
                except Exception as e:
                    logger.warning(f"Segment {idx} error: {e}")

        file_size = os.path.getsize(filepath)
        logger.info(f"HLS download complete: {filepath} ({file_size / 1024 / 1024:.2f}MB)")

        if file_size < 1000:
            os.remove(filepath)
            return None

        return filepath

    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
