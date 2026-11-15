from __future__ import annotations

import asyncio
import logging
import random
import shlex
import time
from collections import deque
from typing import TYPE_CHECKING, Callable

import discord

from .extractor import ExtractError, Extractor, Track

if TYPE_CHECKING:
    from pathlib import Path

    from .cache import AudioCache
    from .config import Config
    from .settings import GuildSettings
    from .stats import PlayStats

log = logging.getLogger(__name__)

RECONNECT = "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
LOOP_MODES = ("off", "track", "queue")
HISTORY_SIZE = 20

# name -> (label, ffmpeg -af chain, playback speed). Filters force a re-encode (~5-10% of a CPU core
# per playing server) instead of the zero-CPU Opus copy, so they're off by default.
FILTERS: dict[str, tuple[str, str, float]] = {
    "off": ("Off", "", 1.0),
    "bassboost": ("Bass boost", "bass=g=8:f=110:w=0.6,volume=0.8", 1.0),
    "nightcore": ("Nightcore", "aresample=48000,asetrate=60000,aresample=48000", 1.25),
    "vaporwave": ("Vaporwave", "aresample=48000,asetrate=38400,aresample=48000", 0.8),
    "slowed": ("Slowed + reverb", "aresample=48000,asetrate=40800,aresample=48000,aecho=0.8:0.88:60:0.35", 0.85),
    "8d": ("8D", "apulsator=hz=0.08", 1.0),
    "karaoke": ("Karaoke", "stereotools=mlev=0.03", 1.0),
    "treble": ("Treble boost", "treble=g=6:f=3000,volume=0.85", 1.0),
}


class GuildPlayer:
    """One per guild: queue + a single asyncio task that feeds the voice client."""

    def __init__(
        self,
        guild: discord.Guild,
        vc: discord.VoiceClient,
        text_channel: discord.abc.Messageable,
        extractor: Extractor,
        config: Config,
        ffmpeg: str,
        on_destroy: Callable[[int], None],
        cache: AudioCache | None = None,
        settings: GuildSettings | None = None,
        stats: PlayStats | None = None,
    ):
        self.guild = guild
        self.vc = vc
        self.text_channel = text_channel
        self.extractor = extractor
        self.config = config
        self.ffmpeg = ffmpeg
        self._on_destroy = on_destroy
        self.cache = cache
        if settings is None:
            from .settings import GuildSettings
            settings = GuildSettings()
        self.settings = settings
        self.stats = stats
        self._play_id: int | None = None  # stats row of the current track
        self.filter = "off"
        self._vc_status: str | None = None  # last voice channel status we set
        self._speed = 1.0
        self._bg: set[asyncio.Task] = set()  # keep refs so background tasks aren't garbage-collected

        self.queue: deque[Track] = deque()
        self.history: deque[Track] = deque(maxlen=HISTORY_SIZE)  # finished tracks, newest last
        self.current: Track | None = None
        self.loop = "off"
        self.panel: discord.Message | None = None

        self._wake = asyncio.Event()
        self._done = asyncio.Event()
        self._skip = False
        self._back = False
        self._seek_to: float | None = None
        self._offset = 0.0
        self._started = 0.0
        self._paused_at: float | None = None
        self._alone_task: asyncio.Task | None = None
        self._destroyed = False
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run(), name=f"player-{guild.id}")

    # ---- queue ops -------------------------------------------------------
    def enqueue(self, tracks: list[Track], front: bool = False) -> int:
        room = max(0, self.config.max_queue - len(self.queue))
        if front:
            self.queue.extendleft(reversed(tracks[:room]))
        else:
            self.queue.extend(tracks[:room])
        self._wake.set()
        return min(room, len(tracks))

    def skip(self) -> None:
        self._skip = True
        self.vc.stop()

    def jump(self, track: Track) -> None:
        """Play `track` from the queue now."""
        self.queue.remove(track)
        self.queue.appendleft(track)
        self.skip()

    def previous(self) -> Track | None:
        """Play the last finished track now; the current one goes back to the front of the queue."""
        if not self.history:
            return None
        track = self.history.pop()
        if self.current:
            self.queue.appendleft(self.current)
        self.queue.appendleft(track)
        self._back = True
        if self.current:
            self.vc.stop()
        else:  # queue had finished: wake the idle loop
            self._wake.set()
        return track

    def clear(self) -> int:
        n = len(self.queue)
        self.queue.clear()
        return n

    def dedupe(self) -> int:
        """Drop queued tracks that repeat the current track or an earlier queued one."""
        seen = {self.current.url} if self.current else set()
        kept = deque()
        for t in self.queue:
            if t.url not in seen:
                seen.add(t.url)
                kept.append(t)
        removed = len(self.queue) - len(kept)
        self.queue = kept
        return removed

    def seek(self, seconds: float) -> None:
        self._seek_to = max(0.0, seconds)
        self.vc.stop()

    def toggle_pause(self) -> bool:
        """Returns True if now paused."""
        if self.vc.is_paused():
            self.vc.resume()
            if self._paused_at:
                self._started += time.monotonic() - self._paused_at
            self._paused_at = None
            return False
        if self.vc.is_playing():
            self.vc.pause()
            self._paused_at = time.monotonic()
            return True
        return False

    @property
    def paused(self) -> bool:
        return self.vc.is_paused()

    def cycle_loop(self) -> str:
        self.loop = LOOP_MODES[(LOOP_MODES.index(self.loop) + 1) % len(LOOP_MODES)]
        return self.loop

    def shuffle(self) -> None:
        items = list(self.queue)
        random.shuffle(items)
        self.queue = deque(items)

    @property
    def position(self) -> float:
        if not self.current:
            return 0.0
        return self._offset + ((self._paused_at or time.monotonic()) - self._started) * self._speed

    def set_filter(self, name: str) -> None:
        """Switch filter; the current track restarts from the same spot with the new sound."""
        pos = self.position
        self.filter = name
        if self.current:
            self.seek(pos if self.current.duration else 0)

    @property
    def idle_timeout(self) -> float | None:
        t = self.settings.idle_timeout
        t = self.config.idle_timeout if t is None else t
        return t or None  # 0 = stay forever

    @property
    def alone_timeout(self) -> float | None:
        t = self.settings.alone_timeout
        t = self.config.alone_timeout if t is None else t
        return t or None

    # ---- playback loop ---------------------------------------------------
    def _next_track(self) -> Track | None:
        prev, skipped, back = self.current, self._skip, self._back
        self._skip = self._back = False
        if back:  # previous(): the wanted track and the old current are already at the front
            return self.queue.popleft() if self.queue else None
        if prev and self.loop == "track" and not skipped:
            return prev
        if prev:
            self.history.append(prev)
        if prev and self.loop == "queue":
            self.queue.append(prev)
        return self.queue.popleft() if self.queue else None

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def _cached(self, track: Track) -> Path | None:
        self.extractor.apply_cached_match(track)
        return self.cache.lookup(track) if self.cache else None

    def _source(self, track: Track, start: float, local: Path | None = None) -> discord.FFmpegOpusAudio:
        af = FILTERS[self.filter][1]
        options = f"-vn -af {shlex.quote(af)}" if af else "-vn"
        if local:  # cached file: no network, always Opus -> copy (unless a filter needs re-encoding)
            before = "-nostdin" + (f" -ss {start:.2f}" if start else "")
            return discord.FFmpegOpusAudio(str(local), executable=self.ffmpeg, before_options=before,
                                           options=options, codec=None if af else "copy", bitrate=96)
        before = RECONNECT
        if track.headers:
            hdr = "".join(f"{k}: {v}\r\n" for k, v in track.headers.items())
            before += f" -headers {shlex.quote(hdr)}"
        if start:
            before += f" -ss {start:.2f}"
        return discord.FFmpegOpusAudio(
            track.stream_url,
            executable=self.ffmpeg,
            before_options=before,
            options=options,
            # Opus source -> remux only, ~zero CPU. Anything else (or a filter) gets encoded.
            codec="copy" if track.is_opus and not af else None,
            bitrate=96,
        )

    def _after(self, error: Exception | None) -> None:
        if error:
            log.warning("playback error in %s: %s", self.guild.id, error)
        self._loop.call_soon_threadsafe(self._done.set)

    async def _run(self) -> None:
        while True:
            start = 0.0
            if self._seek_to is not None and self.current:
                track, start, self._seek_to = self.current, self._seek_to, None
            else:
                self._seek_to = None
                self._wake.clear()  # clear before checking the queue so an enqueue can't be missed
                track = self._next_track()
                if track is None:
                    self.current = None
                    await self.refresh_panel()
                    await self.set_channel_status(None)
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=self.idle_timeout)
                    except asyncio.TimeoutError:
                        await self.destroy()
                        return
                    continue

            local = self._cached(track)
            if not local:
                try:
                    await self.extractor.resolve(track)
                except ExtractError as e:
                    await self._say(f"⚠️ Skipped **{discord.utils.escape_markdown(track.title)}**: {e}")
                    self.current = None if self.current is track else self.current
                    continue
                await track.wait_available()

            if not self.vc.is_connected():
                await self.destroy()
                return

            new_track = track is not self.current or start == 0
            self.current = track
            self._done.clear()
            self._offset, self._started, self._paused_at = start, time.monotonic(), None
            self._speed = FILTERS[self.filter][2]
            log.info("playing %s (%s, opus=%s, start=%.0f)", track.title, "cache" if local else "stream", track.is_opus, start)
            try:
                self.vc.play(self._source(track, start, local), after=self._after)
            except discord.ClientException as e:
                log.warning("play failed: %s", e)
                await asyncio.sleep(1)
                continue

            if new_track:
                if self.stats:
                    listeners = [m.id for m in getattr(self.vc.channel, "members", []) if not m.bot]
                    self._play_id = self.stats.start(self.guild.id, track, listeners)
                await self._post_panel()
                self._spawn(self.set_channel_status(f"🎶 {track.title}"))
            if not local and self.cache:
                self._spawn(self.cache.store(track))
            self._spawn(self._prefetch())
            try:
                await self._done.wait()
            finally:  # also on destroy (task cancelled): credit the time actually played
                if self.stats:
                    self.stats.add_time(self._play_id, (self._paused_at or time.monotonic()) - self._started)

    async def _prefetch(self) -> None:
        if self.queue and not self.queue[0].fresh and not self._cached(self.queue[0]):
            try:
                await self.extractor.resolve(self.queue[0])
            except (ExtractError, IndexError):
                pass  # the play loop will report it when it gets there

    # ---- panel -----------------------------------------------------------
    async def _say(self, text: str) -> None:
        try:
            await self.text_channel.send(text, delete_after=30)
        except discord.HTTPException:
            pass

    async def _post_panel(self) -> None:
        """New track: replace the old panel with a fresh one at the bottom of the chat."""
        from .ui import PanelView

        old, self.panel = self.panel, None
        try:
            self.panel = await self.text_channel.send(view=PanelView(self))
        except discord.HTTPException as e:
            log.warning("cannot post panel: %s", e)
        if old:
            try:
                await old.delete()
            except discord.HTTPException:
                pass

    async def refresh_panel(self, ended: bool = False) -> None:
        from .ui import PanelView

        if self.panel:
            try:
                await self.panel.edit(view=PanelView(self, ended=ended))
            except discord.HTTPException:
                self.panel = None

    async def set_channel_status(self, text: str | None) -> None:
        """Show the song as the voice channel's status (needs Set Voice Channel Status). Best-effort."""
        channel = self.vc.channel if self.vc else None
        text = text[:500] if text else None
        if not isinstance(channel, discord.VoiceChannel) or text == self._vc_status:
            return
        if not channel.permissions_for(self.guild.me).set_voice_channel_status:
            return
        try:
            await channel.edit(status=text)
            self._vc_status = text
        except discord.HTTPException as e:
            log.debug("can't set voice status in %s: %s", channel.id, e)

    # ---- lifecycle -------------------------------------------------------
    def update_alone(self) -> None:
        channel = self.vc.channel if self.vc else None
        alone = channel is not None and not any(not m.bot for m in channel.members) and bool(self.alone_timeout)
        if alone and not self._alone_task:
            self._alone_task = asyncio.create_task(self._alone_timer())
        elif not alone and self._alone_task:
            self._alone_task.cancel()
            self._alone_task = None

    async def _alone_timer(self) -> None:
        await asyncio.sleep(self.alone_timeout)
        self._alone_task = None
        await self._say("👋 Left because everyone else left the channel.")
        await self.destroy()

    async def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        self._on_destroy(self.guild.id)
        me = asyncio.current_task()
        for task in (self._task, self._alone_task):
            if task and task is not me:
                task.cancel()
        self.queue.clear()
        self.current = None
        await self.set_channel_status(None)  # must happen while still connected
        if self.vc:
            self.vc.stop()
            try:
                await self.vc.disconnect(force=True)
            except Exception:
                pass
        await self.refresh_panel(ended=True)
