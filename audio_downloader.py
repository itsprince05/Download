"""
Audio downloader — handles MPD/DASH, HLS/M3U8, and direct file downloads.
PocketFM uses MPEG-DASH (.mpd) — ffmpeg handles it natively.
"""

import asyncio
import os
import time
import re
import subprocess
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
    FFMPEG_TIMEOUT,
)

logger = setup_logger("audio_downloader")


class AudioDownloader:
    """Downloads audio from MPD/DASH, HLS/M3U8, or direct URLs using ffmpeg."""

    def __init__(self):
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        self._session: Optional[aiohttp.ClientSession] = None
        self._current_process: Optional[asyncio.subprocess.Process] = None

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

    def _detect_stream_type(self, url: str) -> str:
        """Detect if URL is MPD, HLS, or direct file."""
        url_lower = url.lower()
        if ".mpd" in url_lower:
            return "mpd"
        elif ".m3u8" in url_lower:
            return "hls"
        else:
            return "direct"

    async def download(self, url: str, progress_callback=None) -> Optional[str]:
        """
        Download audio from URL. Auto-detects MPD/HLS/direct.
        Returns the local file path on success.
        """
        stream_type = self._detect_stream_type(url)
        logger.info(f"Stream type detected: {stream_type} | URL: {url[:200]}")

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(f"Download attempt {attempt}/{MAX_RETRIES}")

                if stream_type == "mpd":
                    result = await self._download_mpd(url, progress_callback)
                elif stream_type == "hls":
                    result = await self._download_hls(url, progress_callback)
                else:
                    result = await self._download_direct(url, progress_callback)

                if result and os.path.exists(result):
                    file_size = os.path.getsize(result)
                    if file_size > 10000:  # At least 10KB for valid audio
                        logger.info(f"Download success: {result} ({file_size / 1024 / 1024:.2f}MB)")
                        return result
                    else:
                        logger.warning(f"File too small ({file_size}B), retrying...")
                        os.remove(result)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Download attempt {attempt} failed: {e}")

            if attempt < MAX_RETRIES:
                logger.info(f"Retrying in {RETRY_DELAY}s...")
                await asyncio.sleep(RETRY_DELAY)

        logger.error(f"All {MAX_RETRIES} download attempts failed")
        return None

    async def _download_mpd(self, mpd_url: str, progress_callback=None) -> Optional[str]:
        """
        Download MPEG-DASH stream using ffmpeg.
        ffmpeg handles MPD natively — parses manifest, picks best quality,
        downloads all segments, and muxes into a proper audio file.
        """
        timestamp = int(time.time())
        output_file = os.path.join(DOWNLOADS_DIR, f"pocketfm_{timestamp}.m4a")

        logger.info(f"[MPD] Downloading DASH stream via ffmpeg...")
        logger.info(f"[MPD] URL: {mpd_url}")
        logger.info(f"[MPD] Output: {output_file}")

        if progress_callback:
            try:
                await progress_callback("Starting MPD/DASH download via ffmpeg...", 0)
            except Exception:
                pass

        # ffmpeg command to download DASH audio stream
        # -i: input MPD URL
        # -map 0:a: select only audio stream (ignore video if present)
        # -c:a copy: copy audio codec without re-encoding (fastest, best quality)
        # -movflags +faststart: optimize for streaming
        cmd = [
            "ffmpeg",
            "-y",                        # Overwrite output
            "-loglevel", "info",         # Show progress info
            "-user_agent", USER_AGENT,   # Realistic browser UA
            "-headers", f"Referer: https://pocketfm.com/\r\n",
            "-i", mpd_url,               # Input MPD URL
            "-map", "0:a:0",             # Select first audio stream only
            "-c:a", "copy",              # Copy codec (no re-encoding)
            "-movflags", "+faststart",   # Optimize for playback
            output_file,
        ]

        try:
            result = await self._run_ffmpeg(cmd, progress_callback)
            if result:
                return output_file
        except Exception as e:
            logger.error(f"[MPD] ffmpeg copy failed: {e}")

        # Fallback: try re-encoding to AAC if copy fails
        logger.info("[MPD] Retrying with AAC re-encoding...")
        output_file_aac = os.path.join(DOWNLOADS_DIR, f"pocketfm_{timestamp}_aac.m4a")

        cmd_reencode = [
            "ffmpeg",
            "-y",
            "-loglevel", "info",
            "-user_agent", USER_AGENT,
            "-headers", f"Referer: https://pocketfm.com/\r\n",
            "-i", mpd_url,
            "-map", "0:a:0",
            "-c:a", "aac",              # Re-encode to AAC
            "-b:a", "192k",             # High quality bitrate
            "-movflags", "+faststart",
            output_file_aac,
        ]

        try:
            result = await self._run_ffmpeg(cmd_reencode, progress_callback)
            if result:
                return output_file_aac
        except Exception as e:
            logger.error(f"[MPD] ffmpeg re-encode also failed: {e}")

        # Fallback 2: try without -map (let ffmpeg auto-select)
        logger.info("[MPD] Retrying without stream mapping...")
        output_file_auto = os.path.join(DOWNLOADS_DIR, f"pocketfm_{timestamp}_auto.m4a")

        cmd_auto = [
            "ffmpeg",
            "-y",
            "-loglevel", "info",
            "-user_agent", USER_AGENT,
            "-headers", f"Referer: https://pocketfm.com/\r\n",
            "-i", mpd_url,
            "-vn",                       # No video
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            output_file_auto,
        ]

        try:
            result = await self._run_ffmpeg(cmd_auto, progress_callback)
            if result:
                return output_file_auto
        except Exception as e:
            logger.error(f"[MPD] All ffmpeg attempts failed: {e}")

        return None

    async def _download_hls(self, m3u8_url: str, progress_callback=None) -> Optional[str]:
        """Download HLS stream using ffmpeg."""
        timestamp = int(time.time())
        output_file = os.path.join(DOWNLOADS_DIR, f"pocketfm_{timestamp}_hls.m4a")

        logger.info(f"[HLS] Downloading via ffmpeg: {m3u8_url[:150]}")

        cmd = [
            "ffmpeg",
            "-y",
            "-loglevel", "info",
            "-user_agent", USER_AGENT,
            "-i", m3u8_url,
            "-vn",
            "-c:a", "copy",
            "-movflags", "+faststart",
            output_file,
        ]

        try:
            result = await self._run_ffmpeg(cmd, progress_callback)
            if result:
                return output_file
        except Exception as e:
            logger.error(f"[HLS] ffmpeg failed: {e}")

        # Fallback: re-encode
        output_file_aac = os.path.join(DOWNLOADS_DIR, f"pocketfm_{timestamp}_hls_aac.m4a")
        cmd_aac = [
            "ffmpeg",
            "-y",
            "-loglevel", "info",
            "-user_agent", USER_AGENT,
            "-i", m3u8_url,
            "-vn",
            "-c:a", "aac",
            "-b:a", "192k",
            output_file_aac,
        ]

        try:
            result = await self._run_ffmpeg(cmd_aac, progress_callback)
            if result:
                return output_file_aac
        except Exception as e:
            logger.error(f"[HLS] ffmpeg re-encode failed: {e}")

        return None

    async def _download_direct(self, url: str, progress_callback=None) -> Optional[str]:
        """Download a direct audio file URL."""
        session = await self._get_session()

        async with session.get(url) as response:
            if response.status != 200:
                logger.error(f"HTTP {response.status} for {url[:100]}")
                return None

            content_type = response.headers.get("Content-Type", "")
            total = int(response.headers.get("Content-Length", 0))

            # Generate filename
            parsed = urlparse(url)
            basename = os.path.basename(unquote(parsed.path))
            basename = re.sub(r'[<>:"/\\|?*]', '_', basename)

            if not basename or len(basename) < 3:
                timestamp = int(time.time())
                basename = f"pocketfm_{timestamp}.mp3"

            # Ensure audio extension
            _, ext = os.path.splitext(basename)
            if ext.lower() not in [".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac"]:
                basename += ".mp3"

            filepath = os.path.join(DOWNLOADS_DIR, basename)

            logger.info(f"[DIRECT] Downloading: {basename} | Size: {total / 1024 / 1024:.2f}MB")

            downloaded = 0
            with open(filepath, "wb") as f:
                async for chunk in response.content.iter_chunked(DOWNLOAD_CHUNK_SIZE):
                    f.write(chunk)
                    downloaded += len(chunk)

                    if progress_callback and total > 0:
                        try:
                            pct = (downloaded / total) * 100
                            await progress_callback(
                                f"Direct download: {pct:.1f}%", pct
                            )
                        except Exception:
                            pass

            return filepath

    async def _run_ffmpeg(self, cmd: list, progress_callback=None) -> bool:
        """
        Run ffmpeg subprocess asynchronously with real-time progress parsing.
        Returns True if successful.
        """
        logger.info(f"[FFMPEG] Running: {' '.join(cmd[:6])}...")

        try:
            self._current_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Read stderr (ffmpeg outputs progress to stderr)
            duration = None
            stderr_lines = []

            while True:
                line = await asyncio.wait_for(
                    self._current_process.stderr.readline(),
                    timeout=FFMPEG_TIMEOUT,
                )

                if not line:
                    break

                line_str = line.decode("utf-8", errors="replace").strip()
                stderr_lines.append(line_str)

                if line_str:
                    logger.debug(f"[FFMPEG] {line_str}")

                # Parse duration from input info
                dur_match = re.search(r'Duration:\s*(\d+):(\d+):(\d+)', line_str)
                if dur_match:
                    h, m, s = int(dur_match.group(1)), int(dur_match.group(2)), int(dur_match.group(3))
                    duration = h * 3600 + m * 60 + s
                    logger.info(f"[FFMPEG] Total duration: {duration}s")

                # Parse progress time
                time_match = re.search(r'time=(\d+):(\d+):(\d+)', line_str)
                if time_match and duration and duration > 0:
                    h, m, s = int(time_match.group(1)), int(time_match.group(2)), int(time_match.group(3))
                    current = h * 3600 + m * 60 + s
                    pct = min((current / duration) * 100, 100)

                    if progress_callback:
                        try:
                            await progress_callback(
                                f"FFmpeg encoding: {pct:.1f}% ({current}s/{duration}s)", pct
                            )
                        except Exception:
                            pass

                # Parse size info for download progress
                size_match = re.search(r'size=\s*(\d+)kB', line_str)
                if size_match and progress_callback:
                    size_kb = int(size_match.group(1))
                    try:
                        await progress_callback(
                            f"Downloading: {size_kb / 1024:.1f}MB", 0
                        )
                    except Exception:
                        pass

            await self._current_process.wait()
            return_code = self._current_process.returncode

            if return_code == 0:
                logger.info("[FFMPEG] Process completed successfully")
                return True
            else:
                # Log last 10 lines of stderr for debugging
                last_lines = "\n".join(stderr_lines[-10:])
                logger.error(f"[FFMPEG] Failed with code {return_code}\n{last_lines}")
                return False

        except asyncio.TimeoutError:
            logger.error(f"[FFMPEG] Timed out after {FFMPEG_TIMEOUT}s")
            if self._current_process:
                self._current_process.kill()
            return False
        except Exception as e:
            logger.error(f"[FFMPEG] Error: {e}")
            if self._current_process:
                try:
                    self._current_process.kill()
                except Exception:
                    pass
            return False
        finally:
            self._current_process = None

    async def probe_url(self, url: str) -> Optional[dict]:
        """
        Use ffprobe to get stream info from a URL.
        Returns dict with format and stream details.
        """
        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "-user_agent", USER_AGENT,
            url,
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)

            if process.returncode == 0:
                import json
                info = json.loads(stdout.decode("utf-8"))
                logger.info(f"[FFPROBE] Stream info: {json.dumps(info, indent=2)[:500]}")
                return info
            else:
                logger.warning(f"[FFPROBE] Failed: {stderr.decode('utf-8', errors='replace')[:200]}")
                return None

        except Exception as e:
            logger.error(f"[FFPROBE] Error: {e}")
            return None

    async def cancel(self):
        """Cancel current download/ffmpeg process."""
        if self._current_process:
            try:
                self._current_process.kill()
                logger.info("Current download process killed")
            except Exception as e:
                logger.debug(f"Process kill error: {e}")

    async def close(self):
        """Close the HTTP session and kill any running process."""
        await self.cancel()
        if self._session and not self._session.closed:
            await self._session.close()
