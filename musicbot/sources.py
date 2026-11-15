"""Metadata-only sources (Spotify, Deezer). Their audio is DRM-locked, so we read the
song list and each Track is matched to a YouTube upload right before it plays."""
from __future__ import annotations

import html
import json
import re

import aiohttp

from .extractor import ExtractError, Track

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}

SPOTIFY_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z-]+/)?|spotify:)(track|album|playlist|artist)[/:]([A-Za-z0-9]{22})"
)
DEEZER_RE = re.compile(r"deezer\.com/(?:[a-z]{2}/)?(track|album|playlist)/(\d+)")
SHORT_RE = re.compile(r"^https?://(spotify\.link|link\.deezer\.com|deezer\.page\.link)/", re.I)
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', re.S)


def is_meta_url(text: str) -> bool:
    t = text.strip()
    return bool(SPOTIFY_RE.search(t) or DEEZER_RE.search(t) or SHORT_RE.match(t))


def _meta_track(url: str, title: str, artist: str, duration_s: float | None, thumb: str | None) -> Track:
    artist = artist.replace(" ", " ").strip()
    return Track(
        url=url,
        title=f"{artist} - {title}" if artist else title,
        duration=int(duration_s) if duration_s else None,
        thumbnail=thumb,
        uploader=artist or None,
        search_query=f"{artist} - {title}" if artist else title,
    )


class MetaSources:
    def __init__(self, session: aiohttp.ClientSession, limit: int):
        self.session = session
        self.limit = limit

    async def fetch(self, url: str) -> tuple[list[Track], str | None]:
        url = url.strip()
        try:
            if SHORT_RE.match(url):  # follow share links to the real URL
                async with self.session.get(url, headers=UA, allow_redirects=True) as r:
                    url = str(r.url)
                    if not (SPOTIFY_RE.search(url) or DEEZER_RE.search(url)):
                        url = html.unescape(await r.text())  # some short links redirect via JS
            if m := SPOTIFY_RE.search(url):
                return await self._spotify(*m.groups())
            if m := DEEZER_RE.search(url):
                return await self._deezer(*m.groups())
        except (aiohttp.ClientError, TimeoutError, KeyError, ValueError, TypeError) as e:
            raise ExtractError(f"Couldn't read that link ({type(e).__name__}).") from None
        raise ExtractError("Unsupported link.")

    # Spotify: the public embed page carries the track list as JSON, no API key needed.
    # Embeds list up to ~100 tracks for long playlists.
    async def _spotify(self, kind: str, sid: str) -> tuple[list[Track], str | None]:
        async with self.session.get(f"https://open.spotify.com/embed/{kind}/{sid}", headers=UA) as r:
            if r.status == 404:
                raise ExtractError("Spotify says that doesn't exist (private playlist?).")
            r.raise_for_status()
            page = await r.text()
        m = NEXT_DATA_RE.search(page)
        if not m:
            raise ExtractError("Spotify changed their page format; update the bot.")
        entity = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
        images = (entity.get("visualIdentity") or {}).get("image") or (entity.get("coverArt") or {}).get("sources") or []
        cover = images[-1]["url"] if images else None

        if kind == "track":
            artists = ", ".join(a["name"] for a in entity.get("artists", []))
            dur = (entity.get("duration") or 0) / 1000
            return [_meta_track(f"https://open.spotify.com/track/{sid}", entity["name"], artists, dur, cover)], None

        tracks = []
        for t in entity.get("trackList", [])[: self.limit]:
            tid = (t.get("uri") or "").rsplit(":", 1)[-1]
            link = f"https://open.spotify.com/track/{tid}" if tid else f"https://open.spotify.com/{kind}/{sid}"
            tracks.append(_meta_track(link, t["title"], t.get("subtitle", ""), (t.get("duration") or 0) / 1000, cover))
        if not tracks:
            raise ExtractError("That Spotify list is empty.")
        return tracks, entity.get("name") or entity.get("title") or f"Spotify {kind}"

    # Deezer: free public API, no key.
    async def _deezer(self, kind: str, did: str) -> tuple[list[Track], str | None]:
        async with self.session.get(f"https://api.deezer.com/{kind}/{did}", headers=UA) as r:
            data = await r.json(content_type=None)
        if "error" in data:
            raise ExtractError(f"Deezer: {data['error'].get('message', 'not found')}")

        def conv(t: dict, cover: str | None) -> Track:
            alb = t.get("album") or {}
            return _meta_track(t.get("link") or f"https://www.deezer.com/track/{t['id']}", t["title"],
                               (t.get("artist") or {}).get("name", ""), t.get("duration"),
                               alb.get("cover_xl") or alb.get("cover_big") or cover)

        if kind == "track":
            return [conv(data, None)], None
        cover = data.get("cover_xl") or data.get("picture_xl")
        tracks = [conv(t, cover) for t in (data.get("tracks") or {}).get("data", [])[: self.limit]]
        if not tracks:
            raise ExtractError("That Deezer list is empty.")
        return tracks, data.get("title") or f"Deezer {kind}"
