"""yt-dlp wrapper. All blocking calls run in a thread; heavy extraction is serialized
because each YouTube resolve may spawn deno (~60-80 MB for a moment)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

import yt_dlp

from .config import ROOT

URL_RE = re.compile(r"^https?://", re.I)
SEARCH_PREFIX = {"youtube": "ytsearch1:", "soundcloud": "scsearch1:"}
STREAM_TTL = 1800  # re-resolve stream URLs older than 30 min (YouTube's expire after ~6h)


class ExtractError(Exception):
    pass


@dataclass(slots=True)
class Track:
    url: str
    title: str
    duration: int | None = None
    thumbnail: str | None = None
    uploader: str | None = None
    requester_id: int = 0
    stream_url: str | None = None
    is_opus: bool = False
    headers: dict | None = None
    resolved_at: float = 0.0
    search_query: str | None = None  # set for Spotify/Deezer: matched on YouTube at play time
    play_url: str | None = None      # the matched YouTube URL
    available_at: float = 0.0        # epoch time the stream URL starts working (YouTube 403s before it)

    async def wait_available(self) -> None:
        delay = self.available_at - time.time()
        if delay > 0:
            await asyncio.sleep(min(delay + 0.3, 15))

    @property
    def fresh(self) -> bool:
        return bool(self.stream_url) and time.monotonic() - self.resolved_at < STREAM_TTL

    def apply_stream(self, info: dict) -> None:
        self.stream_url = info["url"]
        self.is_opus = info.get("acodec") == "opus" and info.get("asr") in (48000, None)
        self.headers = info.get("http_headers")
        self.available_at = float(info.get("available_at") or 0)
        self.resolved_at = time.monotonic()
        if self.search_query:  # keep the Spotify/Deezer title, artist and cover art
            self.duration = self.duration or _int(info.get("duration"))
            return
        self.title = info.get("title") or self.title
        self.duration = _int(info.get("duration")) if not info.get("is_live") else None
        self.thumbnail = info.get("thumbnail") or self.thumbnail
        self.uploader = info.get("uploader") or info.get("channel") or self.uploader


def _int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _thumb(entry: dict) -> str | None:
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    thumbs = entry.get("thumbnails") or []
    return thumbs[-1].get("url") if thumbs else None


def _from_entry(entry: dict) -> Track | None:
    url = entry.get("webpage_url") or entry.get("url")
    if not url:
        return None
    if not URL_RE.match(url) and entry.get("ie_key") == "Youtube":
        url = f"https://www.youtube.com/watch?v={url}"
    return Track(
        url=url,
        title=entry.get("title") or url,
        duration=_int(entry.get("duration")),
        thumbnail=_thumb(entry),
        uploader=entry.get("uploader") or entry.get("channel"),
    )


MIX_LIMIT = 50  # ~3 s to load; YouTube Mixes run to hundreds of songs


class Extractor:
    def __init__(self, cookies_file: str = "", playlist_limit: int = 500):
        base = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,  # watch?v=X&list=Y plays just X; pure playlist URLs still work
            "socket_timeout": 10,
            "source_address": "0.0.0.0",  # force IPv4; datacenter IPv6 is often blocked
            "format": "bestaudio[acodec=opus]/bestaudio/best",
            "cachedir": str(ROOT / "cache"),
            "playlistend": playlist_limit,
        }
        if cookies_file:
            base["cookiefile"] = cookies_file
        self._ydl = yt_dlp.YoutubeDL({**base, "extract_flat": "in_playlist"})
        self._ac_ydl = yt_dlp.YoutubeDL({**base, "extract_flat": True})
        self._sem = asyncio.Semaphore(1)
        self._ac_lock = threading.Lock()
        self._ac_cache: OrderedDict[str, list[tuple[str, str]]] = OrderedDict()
        # Spotify/Deezer -> YouTube matches, saved so each song is only searched once
        self._matches_path = ROOT / "cache" / "matches.json"
        try:
            self._matches: dict[str, str] = json.loads(self._matches_path.read_text("utf-8"))
        except (OSError, ValueError):
            self._matches = {}

    def apply_cached_match(self, track: Track) -> None:
        if track.search_query and not track.play_url:
            track.play_url = self._matches.get(track.search_query.lower())

    def _save_match(self, query: str, url: str) -> None:
        self._matches[query.lower()] = url
        while len(self._matches) > 5000:
            self._matches.pop(next(iter(self._matches)))
        try:
            self._matches_path.parent.mkdir(exist_ok=True)
            tmp = self._matches_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._matches), "utf-8")
            tmp.replace(self._matches_path)
        except OSError as e:
            logging.getLogger(__name__).warning("can't save match cache: %s", e)

    async def _extract(self, query: str, mix: bool = False) -> dict:
        async with self._sem:
            params = self._ydl.params
            saved = params["noplaylist"], params["playlistend"]
            if mix:  # YouTube Mix (list=RD...): only loads from the watch link, and is near-endless
                params["noplaylist"], params["playlistend"] = False, MIX_LIMIT
            try:
                info = await asyncio.to_thread(self._ydl.extract_info, query, download=False)
            except yt_dlp.utils.DownloadError as e:
                msg = str(e).removeprefix("ERROR: ").split("\n")[0]
                raise ExtractError(msg[:300]) from None
            finally:
                params["noplaylist"], params["playlistend"] = saved
        if not info:
            raise ExtractError("Nothing found.")
        return self._ydl.sanitize_info(info)

    async def search(self, query: str, source: str = "youtube", mix: bool = False) -> tuple[list[Track], str | None]:
        """Returns (tracks, playlist_title). Single videos come back already resolved."""
        query = query.strip()
        info = await self._extract(query if URL_RE.match(query) else SEARCH_PREFIX[source] + query, mix)
        if "entries" in info:
            tracks = [t for e in info["entries"] if e and (t := _from_entry(e))]
            if not tracks:
                raise ExtractError("Nothing found.")
            is_search = info.get("extractor_key", "").lower().endswith("search")
            return tracks, None if is_search else info.get("title") or "playlist"
        track = _from_entry(info) or Track(url=query, title=query)
        if info.get("url"):
            track.apply_stream(info)
        return [track], None

    async def _match(self, track: Track) -> str:
        """Find the YouTube upload for a Spotify/Deezer track: top 3 results, closest duration wins."""
        info = await self._extract(f"ytsearch3:{track.search_query} audio")
        entries = [e for e in info.get("entries") or [] if e and e.get("url")]
        if not entries:
            raise ExtractError("No YouTube match found.")
        if track.duration:
            near = [e for e in entries if e.get("duration") and abs(e["duration"] - track.duration) <= 15]
            if near:
                entries = near  # keep YouTube's ranking among close-enough lengths
        best = _from_entry(entries[0])
        if not best:
            raise ExtractError("No YouTube match found.")
        self._save_match(track.search_query, best.url)
        return best.url

    async def resolve(self, track: Track) -> Track:
        if not track.fresh:
            self.apply_cached_match(track)
            if track.search_query and not track.play_url:
                track.play_url = await self._match(track)
            info = await self._extract(track.play_url or track.url)
            if "entries" in info:  # some extractors wrap a single item
                info = next((e for e in info["entries"] if e), None) or {}
                if not info.get("url"):
                    raise ExtractError("Could not get a playable stream.")
            if not info.get("url"):
                raise ExtractError("Could not get a playable stream.")
            track.apply_stream(info)
        return track

    async def autocomplete(self, query: str) -> list[tuple[str, str]]:
        """[(label, url)] suggestions. Never blocks long; returns [] if busy."""
        key = query.strip().lower()
        if len(key) < 3 or URL_RE.match(key):
            return []
        if key in self._ac_cache:
            self._ac_cache.move_to_end(key)
            return self._ac_cache[key]

        def run():
            if not self._ac_lock.acquire(blocking=False):
                return None
            try:
                return self._ac_ydl.extract_info(f"ytsearch5:{query}", download=False)
            except Exception:
                return None
            finally:
                self._ac_lock.release()

        try:
            info = await asyncio.wait_for(asyncio.to_thread(run), timeout=2.3)
        except asyncio.TimeoutError:
            return []
        if not info:
            return []
        results = []
        for e in info.get("entries") or []:
            t = e and _from_entry(e)
            if t and len(t.url) <= 100:
                label = f"{t.title} [{fmt_time(t.duration)}]" if t.duration else t.title
                results.append((label[:100], t.url))
        self._ac_cache[key] = results
        if len(self._ac_cache) > 64:
            self._ac_cache.popitem(last=False)
        return results


def fmt_time(seconds: float | None) -> str:
    if seconds is None:
        return "LIVE"
    s = int(seconds)
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


if __name__ == "__main__":  # quick host check: python -m musicbot.extractor "query"
    import sys

    from .ffmpeg import check_deno, prepare_path

    prepare_path()
    print("deno:", check_deno() or "NOT FOUND (YouTube will likely fail)")

    async def main():
        ex = Extractor()
        tracks, pl = await ex.search(" ".join(sys.argv[1:]) or "never gonna give you up")
        print("playlist:", pl, "| tracks:", len(tracks))
        t = await ex.resolve(tracks[0])
        print(t.title, fmt_time(t.duration), "opus copy:", t.is_opus)
        print(t.stream_url[:120], "...")

    asyncio.run(main())
