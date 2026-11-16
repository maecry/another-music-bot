from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from ..cache import AudioCache
from ..config import ROOT
from ..extractor import ExtractError, Extractor, fmt_time
from ..player import FILTERS, GuildPlayer
from ..sources import MetaSources, is_meta_url
from ..ui import SOURCE_ICON, PanelView, QueueView, esc

log = logging.getLogger(__name__)


MUSIC_SETTINGS = ("default_source", "idle_timeout", "alone_timeout", "panel_channel_id")


class UserError(app_commands.AppCommandError):
    pass


OEMBED = {  # free, no key, ~100 ms: used to show a pasted link's title in /play autocomplete
    "youtube.com": "https://www.youtube.com/oembed?format=json&url=",
    "youtu.be": "https://www.youtube.com/oembed?format=json&url=",
    "spotify.com": "https://open.spotify.com/oembed?url=",
    "soundcloud.com": "https://soundcloud.com/oembed?format=json&url=",
    "deezer.com": "https://api.deezer.com/oembed?url=",
}
COLLECTION_RE = re.compile(r"/(playlist|album|artist|sets)/|[?&]list=(?!.*[?&]v=)|youtube\.com/playlist", re.I)


def tidy_url(url: str) -> str:
    """Drop tracking params (?si=, &pp=, utm_...) so links fit Discord's 100-char choice value."""
    parts = urlsplit(url)
    keep = {k: v for k, v in parse_qs(parts.query).items() if k in ("v", "list", "t", "index")}
    return urlunsplit(parts._replace(query=urlencode(keep, doseq=True), fragment=""))


def parse_time(text: str) -> int:
    if not re.fullmatch(r"\d+(:\d{1,2}){0,2}", text.strip()):
        raise UserError("Use a time like `90`, `1:30` or `1:02:03`.")
    secs = 0
    for part in text.strip().split(":"):
        secs = secs * 60 + int(part)
    return secs


class PlaylistPrompt(discord.ui.View):
    """Asked when a YouTube link is a video inside a playlist: whole playlist or just the song?"""
    def __init__(self, user_id: int):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.whole = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("That's not your prompt.", ephemeral=True)
            return False
        return True

    async def _pick(self, interaction: discord.Interaction, whole: bool):
        self.whole = whole
        await interaction.response.edit_message(
            content="📃 Queuing the whole playlist..." if whole else "🎵 Queuing just this song...", view=None)
        self.stop()

    @discord.ui.button(label="Whole playlist", emoji="📃", style=discord.ButtonStyle.primary)
    async def playlist(self, interaction: discord.Interaction, _):
        await self._pick(interaction, True)

    @discord.ui.button(label="Just this song", emoji="🎵")
    async def song(self, interaction: discord.Interaction, _):
        await self._pick(interaction, False)


class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self.extractor = Extractor(bot.config.cookies_file, bot.config.max_queue)
        self.session: aiohttp.ClientSession | None = None
        self.meta: MetaSources | None = None
        self.settings = bot.settings  # shared with other cogs
        self._link_titles: OrderedDict[str, str | None] = OrderedDict()

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        self.meta = MetaSources(self.session, self.bot.config.max_queue)
        c = self.bot.config
        self.cache = AudioCache(ROOT / "cache" / "audio", self.session, c.cache_size_gb, c.cache_days, c.cache_max_minutes)
        self._cleaner = asyncio.create_task(self._clean_loop()) if self.cache.enabled else None

    async def _clean_loop(self) -> None:
        while True:  # at startup, then every 6 hours: drop songs unplayed for cache_days
            try:
                await asyncio.to_thread(self.cache.cleanup)
            except Exception:
                log.exception("cache cleanup failed")
            await asyncio.sleep(6 * 3600)

    async def cog_unload(self) -> None:
        if self._cleaner:
            self._cleaner.cancel()
        for p in list(self.players.values()):
            await p.destroy()
        if self.session:
            await self.session.close()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            raise UserError("Music commands only work in servers.")
        return True

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, UserError):
            msg = f"❌ {error}"
        else:
            log.exception("command error", exc_info=error)
            msg = "❌ Something went wrong."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # ---- helpers ---------------------------------------------------------
    def _player(self, interaction: discord.Interaction, *, same_channel: bool = True) -> GuildPlayer:
        player = self.players.get(interaction.guild_id)
        if not player:
            raise UserError("I'm not playing anything.")
        if same_channel:
            voice = interaction.user.voice
            if not voice or voice.channel != player.vc.channel:
                raise UserError("You need to be in my voice channel.")
        return player

    async def _connect(self, interaction: discord.Interaction) -> GuildPlayer:
        voice = interaction.user.voice
        if not voice or not voice.channel:
            raise UserError("Join a voice channel first.")
        quiz = self.bot.get_cog("Quiz")
        if quiz and interaction.guild_id in quiz.games:
            raise UserError("A music quiz is running. Use `/quiz stop` first.")
        player = self.players.get(interaction.guild_id)
        if player:
            if player.vc.channel != voice.channel:
                if any(not m.bot for m in player.vc.channel.members):
                    raise UserError(f"I'm already playing in {player.vc.channel.mention}.")
                await player.vc.move_to(voice.channel)
            player.text_channel = self._panel_channel(interaction)
            return player
        perms = voice.channel.permissions_for(interaction.guild.me)
        if not (perms.connect and perms.speak):
            raise UserError(f"I need Connect and Speak permission in {voice.channel.mention}.")
        try:
            vc = interaction.guild.voice_client or await voice.channel.connect(self_deaf=True, timeout=20)
        except RuntimeError as e:  # discord.py raises this when davey (voice encryption) can't load
            log.error("voice unavailable: %s", e)
            raise UserError("Voice isn't available on this host: the `davey` library failed to load (see bot log).")
        except asyncio.TimeoutError:
            raise UserError("Timed out connecting to voice. Try again, or check the host allows UDP.")
        player = GuildPlayer(
            interaction.guild, vc, self._panel_channel(interaction), self.extractor,
            self.bot.config, self.bot.ffmpeg, lambda gid: self.players.pop(gid, None), self.cache,
            self.settings.get(interaction.guild_id), self.bot.stats,
        )
        self.players[interaction.guild_id] = player
        return player

    def _panel_channel(self, interaction: discord.Interaction):
        """The server's panel channel from /settings, if set and usable; else where the command was used."""
        cid = self.settings.get(interaction.guild_id).panel_channel_id
        channel = interaction.guild.get_channel(cid) if cid else None
        if channel and channel.permissions_for(interaction.guild.me).send_messages:
            return channel
        return interaction.channel

    # ---- commands --------------------------------------------------------
    @app_commands.command(description="Play a song or playlist: search, or a YouTube/Spotify/SoundCloud/Deezer/... link")
    @app_commands.describe(query="Search text or a link", source="Where to search (ignored for links)")
    @app_commands.choices(source=[
        app_commands.Choice(name="YouTube", value="youtube"),
        app_commands.Choice(name="SoundCloud", value="soundcloud"),
    ])
    async def play(self, interaction: discord.Interaction, query: str,
                   source: app_commands.Choice[str] | None = None):
        await self._play(interaction, query, source)

    @app_commands.command(description="Add a song or playlist to the FRONT of the queue")
    @app_commands.describe(query="Search text or a link", source="Where to search (ignored for links)")
    @app_commands.choices(source=[
        app_commands.Choice(name="YouTube", value="youtube"),
        app_commands.Choice(name="SoundCloud", value="soundcloud"),
    ])
    async def playnext(self, interaction: discord.Interaction, query: str,
                       source: app_commands.Choice[str] | None = None):
        await self._play(interaction, query, source, front=True)

    async def _play(self, interaction: discord.Interaction, query: str,
                    source: app_commands.Choice[str] | None, front: bool = False):
        if not interaction.user.voice:
            raise UserError("Join a voice channel first.")
        await interaction.response.defer(thinking=True)
        query = await self._ask_playlist(interaction, query)
        try:
            if is_meta_url(query):
                tracks, playlist = await self.meta.fetch(query)
            else:
                default = self.settings.get(interaction.guild_id).default_source
                tracks, playlist = await self.extractor.search(query, source.value if source else default)
        except ExtractError as e:
            raise UserError(f"Couldn't load that: {e}")
        player = await self._connect(interaction)
        for t in tracks:
            t.requester_id = interaction.user.id
        added = player.enqueue(tracks, front=front)
        if player.current:
            await player.refresh_panel()  # update queue count + dropdown
        if not added:
            raise UserError(f"The queue is full ({self.bot.config.max_queue} tracks).")
        if playlist:
            msg = f"📃 Queued **{added}** tracks from **{esc(playlist)}**" + (" at the front" if front else "")
            if added < len(tracks):
                msg += f" (queue limit hit, {len(tracks) - added} skipped)"
        else:
            t = tracks[0]
            pos = 1 if front else len(player.queue)
            where = "Playing next" if not player.current or pos <= 1 else f"#{pos} in queue"
            msg = f"🎵 Queued **[{esc(t.title)}](<{t.url}>)** `{fmt_time(t.duration)}` · {where}"
        await interaction.followup.send(msg)

    async def _ask_playlist(self, interaction: discord.Interaction, query: str) -> str:
        """watch?v=X&list=Y -> ask; returns the playlist URL or the original (song-only) link."""
        parts = urlsplit(query.strip())
        qs = parse_qs(parts.query)
        list_id = qs.get("list", [""])[0]
        host = parts.netloc.lower()
        # RD... = YouTube auto-mixes, which yt-dlp can't load as a playlist
        if "youtu" not in host or not list_id or list_id.startswith("RD") or not ("v" in qs or "youtu.be" in host):
            return query
        view = PlaylistPrompt(interaction.user.id)
        msg = await interaction.followup.send("This song is part of a playlist. Queue the whole playlist?",
                                              view=view, wait=True)
        if await view.wait():  # timed out: default to just the song
            await msg.edit(content="🎵 No answer, queuing just this song...", view=None)
        return f"https://www.youtube.com/playlist?list={list_id}" if view.whole else query

    @playnext.autocomplete("query")
    @play.autocomplete("query")
    async def play_autocomplete(self, interaction: discord.Interaction, current: str):
        if re.match(r"^https?://", current.strip(), re.I):
            return await self._link_choice(current.strip())
        source = interaction.namespace.source or self.settings.get(interaction.guild_id).default_source
        if not self.bot.config.autocomplete or source == "soundcloud":
            return []
        results = await self.extractor.autocomplete(current)
        return [app_commands.Choice(name=label, value=url) for label, url in results]

    async def _link_choice(self, url: str) -> list[app_commands.Choice[str]]:
        """A pasted link -> one choice labelled with its title (via oEmbed), so you see what you're adding."""
        value = url if len(url) <= 100 else tidy_url(url)
        if len(value) > 100:
            return []
        host = urlsplit(url).netloc.lower()
        endpoint = next((e for d, e in OEMBED.items() if host == d or host.endswith("." + d)), None)
        if value not in self._link_titles:
            title = None
            if endpoint and self.session:
                try:
                    async with self.session.get(endpoint + quote(value, safe=""),
                                                timeout=aiohttp.ClientTimeout(total=2)) as r:
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            title = data.get("title")
                            author = data.get("author_name")
                            if title and author and author.lower() not in title.lower() and author != "Music":
                                title = f"{title} — {author}"
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    pass
            self._link_titles[value] = title
            if len(self._link_titles) > 128:
                self._link_titles.popitem(last=False)
        title = self._link_titles[value]
        icon = next((i for k, i in SOURCE_ICON.items() if k in host), "🔗")
        kind = "📃 " if COLLECTION_RE.search(value) else ""
        label = f"{icon} {kind}{title}" if title else f"🔗 {value}"
        return [app_commands.Choice(name=label[:100], value=value)]

    @app_commands.command(description="Skip the current track")
    async def skip(self, interaction: discord.Interaction):
        player = self._player(interaction)
        if not player.current:
            raise UserError("Nothing is playing.")
        title = player.current.title
        player.skip()
        await interaction.response.send_message(f"⏭️ Skipped **{esc(title)}**")

    @app_commands.command(description="Play the previous track again")
    async def previous(self, interaction: discord.Interaction):
        player = self._player(interaction)
        track = player.previous()
        if not track:
            raise UserError("No previous track.")
        await interaction.response.send_message(f"⏮️ Back to **{esc(track.title)}**")

    @app_commands.command(description="Pause playback")
    async def pause(self, interaction: discord.Interaction):
        player = self._player(interaction)
        if player.paused or not player.toggle_pause():
            raise UserError("Nothing to pause.")
        await player.refresh_panel()
        await interaction.response.send_message("⏸️ Paused")

    @app_commands.command(description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        player = self._player(interaction)
        if not player.paused:
            raise UserError("I'm not paused.")
        player.toggle_pause()
        await player.refresh_panel()
        await interaction.response.send_message("▶️ Resumed")

    @app_commands.command(description="Stop, clear the queue and leave")
    async def stop(self, interaction: discord.Interaction):
        player = self._player(interaction)
        await player.destroy()
        await interaction.response.send_message("⏹️ Stopped and left.")

    @app_commands.command(description="Leave the voice channel")
    async def leave(self, interaction: discord.Interaction):
        player = self.players.get(interaction.guild_id)
        if player:
            self._player(interaction)  # same-channel check
            await player.destroy()
        elif interaction.guild.voice_client:
            await interaction.guild.voice_client.disconnect(force=True)
        else:
            raise UserError("I'm not in a voice channel.")
        await interaction.response.send_message("👋 Bye!")

    @app_commands.command(description="Join your voice channel")
    async def join(self, interaction: discord.Interaction):
        player = await self._connect(interaction)
        await interaction.response.send_message(f"🔊 Joined {player.vc.channel.mention}")

    @app_commands.command(description="Show the queue")
    async def queue(self, interaction: discord.Interaction):
        player = self._player(interaction, same_channel=False)
        await interaction.response.send_message(view=QueueView(player), ephemeral=True)

    @app_commands.command(description="Show the now-playing panel")
    async def nowplaying(self, interaction: discord.Interaction):
        player = self._player(interaction, same_channel=False)
        await interaction.response.send_message(view=PanelView(player))
        old, player.panel = player.panel, await interaction.original_response()
        player.text_channel = interaction.channel
        if old:
            try:
                await old.delete()
            except discord.HTTPException:
                pass

    @app_commands.command(description="Shuffle the queue")
    async def shuffle(self, interaction: discord.Interaction):
        player = self._player(interaction)
        player.shuffle()
        await player.refresh_panel()
        await interaction.response.send_message(f"🔀 Shuffled {len(player.queue)} tracks")

    @app_commands.command(description="Set loop mode")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Off", value="off"),
        app_commands.Choice(name="Current track", value="track"),
        app_commands.Choice(name="Whole queue", value="queue"),
    ])
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        player = self._player(interaction)
        player.loop = mode.value
        await player.refresh_panel()
        await interaction.response.send_message(f"🔁 Loop: **{mode.name}**")

    @app_commands.command(description="Remove a track from the queue")
    @app_commands.describe(position="Position in /queue")
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1]):
        player = self._player(interaction)
        if position > len(player.queue):
            raise UserError(f"The queue only has {len(player.queue)} tracks.")
        t = player.queue[position - 1]
        del player.queue[position - 1]
        await player.refresh_panel()
        await interaction.response.send_message(f"🗑️ Removed **{esc(t.title)}**")

    @app_commands.command(description="Remove every track from the queue (keeps the current song)")
    async def clear(self, interaction: discord.Interaction):
        player = self._player(interaction)
        n = player.clear()
        await player.refresh_panel()
        await interaction.response.send_message(f"🧹 Cleared {n} tracks from the queue")

    @app_commands.command(description="Remove duplicate songs from the queue")
    async def dedupe(self, interaction: discord.Interaction):
        player = self._player(interaction)
        n = player.dedupe()
        await player.refresh_panel()
        msg = f"🧹 Removed {n} duplicate" + ("s" if n != 1 else "") if n else "No duplicates in the queue."
        await interaction.response.send_message(msg)

    @app_commands.command(description="Move a track to another position")
    async def move(self, interaction: discord.Interaction,
                   from_pos: app_commands.Range[int, 1], to_pos: app_commands.Range[int, 1]):
        player = self._player(interaction)
        n = len(player.queue)
        if from_pos > n or to_pos > n:
            raise UserError(f"The queue only has {n} tracks.")
        t = player.queue[from_pos - 1]
        del player.queue[from_pos - 1]
        player.queue.insert(to_pos - 1, t)
        await player.refresh_panel()
        await interaction.response.send_message(f"↕️ Moved **{esc(t.title)}** to #{to_pos}")

    @remove.autocomplete("position")
    @move.autocomplete("from_pos")
    @move.autocomplete("to_pos")
    async def queue_autocomplete(self, interaction: discord.Interaction, current: str):
        """Lists queued songs as '3. Title' so you can pick by name; typed digits or text filter it."""
        player = self.players.get(interaction.guild_id)
        if not player:
            return []
        q = current.strip().lower()
        hits = [(i, t) for i, t in enumerate(player.queue, 1)
                if not q or str(i).startswith(q) or q in t.title.lower()]
        return [app_commands.Choice(name=f"{i}. {t.title}"[:100], value=i) for i, t in hits[:25]]

    @app_commands.command(description="Jump to a time in the current track")
    @app_commands.describe(time="e.g. 90, 1:30 or 1:02:03")
    async def seek(self, interaction: discord.Interaction, time: str):
        player = self._player(interaction)
        t = player.current
        if not t:
            raise UserError("Nothing is playing.")
        if not t.duration:
            raise UserError("Can't seek in a live stream.")
        secs = parse_time(time)
        if secs >= t.duration:
            raise UserError(f"Track is only {fmt_time(t.duration)} long.")
        player.seek(secs)
        await interaction.response.send_message(f"⏩ Seeking to `{fmt_time(secs)}`")

    @app_commands.command(description="Audio filter for this session (uses more CPU while on)")
    @app_commands.choices(preset=[app_commands.Choice(name=label, value=key) for key, (label, _, _) in FILTERS.items()])
    async def filter(self, interaction: discord.Interaction, preset: app_commands.Choice[str]):
        player = self._player(interaction)
        player.set_filter(preset.value)
        await player.refresh_panel()
        await interaction.response.send_message(
            "🎛️ Filter off" if preset.value == "off" else f"🎛️ Filter: **{preset.name}** (applies in a moment)")

    # ---- per-server settings ---------------------------------------------
    settings_group = app_commands.Group(
        name="settings", description="Music settings for this server",
        default_permissions=discord.Permissions(manage_guild=True), guild_only=True,
    )

    def _settings_text(self, guild: discord.Guild) -> str:
        s, c = self.settings.get(guild.id), self.bot.config

        def mins(value, default):
            v = default if value is None else value
            return "never" if not v else f"{v // 60} min" + ("" if value is not None else " (default)")

        ch = guild.get_channel(s.panel_channel_id) if s.panel_channel_id else None
        return (
            "### ⚙️ Music settings\n"
            f"**Default search:** {'SoundCloud' if s.default_source == 'soundcloud' else 'YouTube'}\n"
            f"**Leave when idle after:** {mins(s.idle_timeout, c.idle_timeout)}\n"
            f"**Leave when alone after:** {mins(s.alone_timeout, c.alone_timeout)}\n"
            f"**Panel channel:** {ch.mention if ch else 'where /play is used'}"
        )

    @settings_group.command(name="view", description="Show this server's music settings")
    async def settings_view(self, interaction: discord.Interaction):
        await interaction.response.send_message(self._settings_text(interaction.guild), ephemeral=True)

    @settings_group.command(name="edit", description="Change this server's music settings")
    @app_commands.describe(
        default_source="Where /play searches when no source is picked",
        idle_minutes="Leave after this long with nothing playing (0 = never, 24/7)",
        alone_minutes="Leave after this long alone in voice (0 = never)",
        panel_channel="Always post the now-playing panel in this channel",
    )
    @app_commands.choices(default_source=[
        app_commands.Choice(name="YouTube", value="youtube"),
        app_commands.Choice(name="SoundCloud", value="soundcloud"),
    ])
    async def settings_edit(
        self, interaction: discord.Interaction,
        default_source: app_commands.Choice[str] | None = None,
        idle_minutes: app_commands.Range[int, 0, 1440] | None = None,
        alone_minutes: app_commands.Range[int, 0, 1440] | None = None,
        panel_channel: discord.TextChannel | None = None,
    ):
        s = self.settings.get(interaction.guild_id)
        if default_source:
            s.default_source = default_source.value
        if idle_minutes is not None:
            s.idle_timeout = idle_minutes * 60
        if alone_minutes is not None:
            s.alone_timeout = alone_minutes * 60
        if panel_channel:
            if not panel_channel.permissions_for(interaction.guild.me).send_messages:
                raise UserError(f"I can't send messages in {panel_channel.mention}.")
            s.panel_channel_id = panel_channel.id
        self.settings.save()
        self._apply_settings(interaction.guild_id)
        await interaction.response.send_message("✅ Saved.\n" + self._settings_text(interaction.guild), ephemeral=True)

    @settings_group.command(name="reset", description="Reset this server's music settings to defaults")
    async def settings_reset(self, interaction: discord.Interaction):
        self.settings.get(interaction.guild_id).reset(MUSIC_SETTINGS)
        self.settings.save()
        self._apply_settings(interaction.guild_id)
        await interaction.response.send_message("♻️ Reset.\n" + self._settings_text(interaction.guild), ephemeral=True)

    def _apply_settings(self, guild_id: int) -> None:
        player = self.players.get(guild_id)
        if player:  # restart the alone timer with the new value; idle timeout applies next time the queue ends
            if player._alone_task:
                player._alone_task.cancel()
                player._alone_task = None
            player.update_alone()

    # ---- voice events ----------------------------------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before, after):
        player = self.players.get(member.guild.id)
        if not player:
            return
        if member.id == self.bot.user.id:
            if after.channel is None:  # kicked / disconnected
                await player.destroy()
            else:
                player.update_alone()
            return
        if player.vc.channel in (before.channel, after.channel):
            player.update_alone()


async def setup(bot):
    await bot.add_cog(Music(bot))
