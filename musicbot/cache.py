"""Disk cache for audio. A song is saved after its first play and later plays come straight
from disk, with no yt-dlp lookup and no download. Files not played for `days` are deleted,
and the oldest go first when the cache is over its size cap."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from .extractor import Track

log = logging.getLogger(__name__)

YT_ID_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/)([\w-]{11})")
CHUNK = 10 * 1024 * 1024  # YouTube throttles big single requests; fetch 10 MB ranges like yt-dlp


def cache_key(url: str) -> str:
    m = YT_ID_RE.search(url)
    return f"yt-{m.group(1)}" if m else "h-" + hashlib.sha1(url.encode()).hexdigest()[:20]


class AudioCache:
    def __init__(self, root: Path, session: aiohttp.ClientSession, max_gb: float, days: int, max_minutes: int):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.session = session
        self.max_bytes = int(max_gb * 1024**3)
        self.max_age = days * 86400
        self.max_seconds = max_minutes * 60
        self._busy: set[str] = set()
        self._sem = asyncio.Semaphore(1)  # one download at a time

    @property
    def enabled(self) -> bool:
        return self.max_bytes > 0

    def _path(self, track: Track) -> Path:
        return self.root / f"{cache_key(track.play_url or track.url)}.opus.webm"

    def lookup(self, track: Track) -> Path | None:
        """Cached file for this track, marked as just played (resets its 7-day clock)."""
        if not self.enabled:
            return None
        path = self._path(track)
        try:
            os.utime(path)
            return path
        except OSError:
            return None

    def eligible(self, track: Track) -> bool:
        return (
            self.enabled
            and track.is_opus  # cached files are played with codec copy, so only Opus sources
            and bool(track.stream_url)
            and track.stream_url.startswith("http")
            and ".m3u8" not in track.stream_url  # HLS can't be fetched as one file
            and bool(track.duration)
            and track.duration <= self.max_seconds
        )

    async def store(self, track: Track) -> None:
        if not self.eligible(track):
            return
        path = self._path(track)
        key = path.name
        if key in self._busy or path.exists():
            return
        self._busy.add(key)
        tmp = path.with_suffix(".part")
        try:
            await track.wait_available()
            async with self._sem:
                await self._download(track.stream_url, track.headers or {}, tmp)
            os.replace(tmp, path)
            log.info("cached %s (%.1f MB)", track.title, path.stat().st_size / 1e6)
            await asyncio.to_thread(self.cleanup)
        except Exception as e:  # cache is best-effort, playback never depends on it
            log.warning("cache download failed for %s: %s", track.title, e)
            tmp.unlink(missing_ok=True)
        finally:
            self._busy.discard(key)

    async def _download(self, url: str, headers: dict, dest: Path) -> None:
        start = 0
        timeout = aiohttp.ClientTimeout(total=300, sock_read=30)
        with open(dest, "wb") as f:
            while True:
                hdrs = {**headers, "Range": f"bytes={start}-{start + CHUNK - 1}"}
                async with self.session.get(url, headers=hdrs, timeout=timeout) as r:
                    r.raise_for_status()
                    async for block in r.content.iter_chunked(64 * 1024):
                        f.write(block)
                    if r.status != 206:  # server ignored Range and sent everything
                        return
                    total = int((r.headers.get("Content-Range") or "*/0").rsplit("/", 1)[-1] or 0)
                start = f.tell()
                if not total or start >= total:
                    return

    def cleanup(self) -> None:
        """Delete files unplayed for too long, then the oldest until under the size cap."""
        now = time.time()
        files = []
        for entry in os.scandir(self.root):
            if not entry.is_file():
                continue
            st = entry.stat()
            stale_part = entry.name.endswith(".part") and now - st.st_mtime > 3600
            if stale_part or now - st.st_mtime > self.max_age:
                self._remove(entry.path)
            elif not entry.name.endswith(".part"):
                files.append((st.st_mtime, st.st_size, entry.path))
        total = sum(size for _, size, _ in files)
        for _, size, path in sorted(files):
            if total <= self.max_bytes:
                break
            if self._remove(path):
                total -= size

    @staticmethod
    def _remove(path: str) -> bool:
        try:
            os.remove(path)
            return True
        except OSError:  # e.g. Windows: file is playing right now; retry next cleanup
            return False

    def stats(self) -> tuple[int, int]:
        files = [e.stat().st_size for e in os.scandir(self.root) if e.is_file() and e.name.endswith(".webm")]
        return len(files), sum(files)
