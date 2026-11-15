"""/quiz: Heardle-style music quiz in voice. Each round starts with a 1 s clip; every skip (or 20 s without a
right answer) unlocks a longer one: 1 s Impossible -> 5 s Hard -> 10 s Medium -> 20 s Easy. Players answer
with /guess, which autocompletes from the quiz's song list. Earlier = more points.

Cost: one short ffmpeg run per clip (Opus copy, no re-encode) and one yt-dlp resolve per round
(prefetched during the previous round). Uses the music cog's extractor + audio cache.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import shlex
import unicodedata
from dataclasses import dataclass, field

import discord
from discord import app_commands, ui
from discord.ext import commands

from ..extractor import ExtractError, Track
from ..player import RECONNECT
from ..sources import is_meta_url
from ..stats import MIN_PLAY, clean_artist
from ..ui import esc, short

log = logging.getLogger(__name__)

# (clip seconds, name, points, colour)
STAGES = [(1, "Impossible", 4, 0xE74C3C), (5, "Hard", 3, 0xE67E22), (10, "Medium", 2, 0xF1C40F), (20, "Easy", 1, 0x2ECC71)]
STAGE_TIME = 20      # seconds to guess at each stage before it auto-skips
MIN_SONGS = 4
JUNK = re.compile(r"official|lyrics?|music video|\bm/?v\b|\bmv\b|video|audio|visuali[sz]er|\bhd\b|\b4k\b|"
                  r"karaoke|remaster|\blive\b|vietsub|engsub|prod\.", re.I)
MEDALS = ["🥇", "🥈", "🥉"]


def norm(text: str) -> str:
    """Lowercase, no accents (Vietnamese-friendly), letters/digits only."""
    text = unicodedata.normalize("NFKD", text.replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in text if c.isalnum() and not unicodedata.combining(c)).lower()


def clean_title(title: str, artist: str | None) -> str:
    """'SƠN TÙNG M-TP | CHÚNG TA CỦA HIỆN TẠI | OFFICIAL MUSIC VIDEO' -> 'CHÚNG TA CỦA HIỆN TẠI'."""
    t = re.sub(r"[(\[【][^)\]】]*[)\]】]", lambda m: "" if JUNK.search(m.group()) or "feat" in m.group().lower()
               or "ft." in m.group().lower() else m.group(), title)
    parts = [p.strip() for p in re.split(r"\s[|｜/]\s|\s[-–—]\s", t) if p.strip()]
    keep = [p for p in parts if not JUNK.search(p) and (not artist or norm(p) != norm(artist))]
    t = (keep or parts or [title])[-1] if len(keep) != 1 else keep[0]
    if len(keep) > 1 and artist:  # "Artist - Song": drop parts that are the artist's name
        keep = [p for p in keep if norm(artist) not in norm(p)] or keep
        t = keep[-1]
    t = re.sub(r"\s+(ft\.?|feat\.?)\s.*$", "", t, flags=re.I).strip(" -|\"'“”")
    return t or title


@dataclass
class Song:
    track: Track
    name: str          # cleaned song title (the answer)
    artist: str | None

    @property
    def label(self) -> str:
        return short(f"{self.name} — {self.artist}" if self.artist else self.name, 100)


@dataclass
class Game:
    guild_id: int
    channel: discord.abc.Messageable
    vc: discord.VoiceClient
    host_id: int
    songs: list[Song]
    order: list[int]                       # indexes into songs, one per round
    scores: dict[int, int] = field(default_factory=dict)
    round: int = 0
    stage: int = 0
    answer: int = -1
    start: float = 0.0                     # clip start offset in the song (seconds)
    local: object = None                   # cached file path, if any
    guessed: set[int] = field(default_factory=set)   # users who already guessed at this stage
    wrong: list[str] = field(default_factory=list)
    winner: int | None = None
    message: discord.Message | None = None
    advance: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    over: bool = False


class RoundView(ui.LayoutView):
    def __init__(self, cog: Quiz, game: Game):
        super().__init__(timeout=None)
        secs, name, pts, colour = STAGES[game.stage]
        box = ui.Container(accent_colour=discord.Colour(colour))
        bar = " ".join(("🟩" if i < game.stage else "🟥" if i == game.stage else "⬛") + f" {s}s"
                       for i, (s, *_rest) in enumerate(STAGES))
        box.add_item(ui.TextDisplay(f"### 🎵 Round {game.round}/{len(game.order)} · {name}\n{bar}"))
        box.add_item(ui.TextDisplay(
            f"Listen to the **{secs} s** clip and answer with `/guess`. Right now it's worth **{pts} pt{'s' if pts > 1 else ''}**.\n"
            f"-# One guess per person per stage · auto-skips after {STAGE_TIME} s"))
        if game.wrong:
            box.add_item(ui.TextDisplay("❌ " + " · ".join(f"~~{g}~~" for g in game.wrong[-6:])))
        replay = ui.Button(label="Replay", emoji="🔁")
        last = game.stage == len(STAGES) - 1
        skip = ui.Button(label="Reveal" if last else f"Skip (+{STAGES[game.stage + 1][0] - secs}s)",
                         emoji="🏳️" if last else "⏭️", style=discord.ButtonStyle.primary)
        replay.callback = cog.make_button(game, "replay")
        skip.callback = cog.make_button(game, "skip")
        box.add_item(ui.ActionRow(replay, skip))
        self.add_item(box)


def scoreboard(game: Game, n: int = 10) -> str:
    rows = sorted(game.scores.items(), key=lambda kv: -kv[1])[:n]
    return "\n".join(f"{MEDALS[i] if i < 3 else f'{i + 1}.'} <@{uid}> · **{pts}**" for i, (uid, pts) in enumerate(rows)) \
        or "*No points yet.*"


class Quiz(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.games: dict[int, Game] = {}

    async def cog_unload(self) -> None:
        for g in list(self.games.values()):
            await self._end(g, announce=False)

    @property
    def music(self):
        return self.bot.get_cog("Music")

    # ---- loading songs -------------------------------------------------------
    def _history_songs(self, guild_id: int) -> list[Track]:
        rows = self.bot.stats.query(
            "SELECT url, MAX(title) title, MAX(artist) artist, MAX(thumbnail) thumbnail, COUNT(*) n FROM plays "
            "WHERE guild_id = ? AND played >= ? GROUP BY url ORDER BY n DESC LIMIT 200", guild_id, MIN_PLAY)
        tracks = []
        for r in rows:
            t = Track(url=r["url"], title=r["title"], thumbnail=r["thumbnail"], uploader=r["artist"])
            if not re.search(r"youtube\.com|youtu\.be|soundcloud\.com", r["url"]):
                t.search_query = r["title"]  # Spotify/Deezer: find it on YouTube like /play does
            tracks.append(t)
        return tracks

    async def _load(self, guild_id: int, source: str | None) -> tuple[list[Song], str]:
        music = self.music
        if not source:
            tracks, what = self._history_songs(guild_id), "this server's most played songs"
        elif is_meta_url(source):
            tracks, what = await music.meta.fetch(source)
        else:
            tracks, what = await music.extractor.search(source)
        songs, seen = [], set()
        for t in tracks:
            artist = clean_artist(t.uploader)
            title = t.title
            if t.search_query and artist and title.startswith(f"{artist} - "):
                title = title[len(artist) + 3:]
            name = clean_title(title, artist)
            if norm(name) and norm(name) not in seen:
                seen.add(norm(name))
                songs.append(Song(t, name, artist))
        return songs, what or "playlist"

    # ---- audio ------------------------------------------------------------------
    async def _prepare(self, game: Game, idx: int) -> bool:
        """Resolve a song's stream (or find it in the audio cache). False if it can't be played."""
        music, song = self.music, game.songs[idx]
        music.extractor.apply_cached_match(song.track)
        if music.cache.lookup(song.track):
            return True
        try:
            await music.extractor.resolve(song.track)
        except ExtractError as e:
            log.info("quiz: skipping %s: %s", song.track.title, e)
            return False
        return True

    def _play_clip(self, game: Game) -> None:
        if not game.vc.is_connected():
            return
        if game.vc.is_playing():
            game.vc.stop()
        secs = STAGES[game.stage][0]
        track = game.songs[game.answer].track
        if game.local:
            src, before, opus = str(game.local), f"-nostdin -ss {game.start:.1f}", True
        else:
            before = RECONNECT + f" -ss {game.start:.1f}"
            if track.headers:
                before += " -headers " + shlex.quote("".join(f"{k}: {v}\r\n" for k, v in track.headers.items()))
            src, opus = track.stream_url, track.is_opus
        game.vc.play(discord.FFmpegOpusAudio(src, executable=self.bot.ffmpeg, before_options=before,
                                             options=f"-vn -t {secs}", codec="copy" if opus else None, bitrate=96))

    # ---- game loop -------------------------------------------------------------------
    async def _run(self, game: Game) -> None:
        queue, planned, game.order = list(game.order), len(game.order), []
        used = set(queue)
        prep = asyncio.create_task(self._prepare(game, queue[0]))
        try:
            while queue and len(game.order) < planned:
                idx = queue.pop(0)
                ok = await prep
                if not ok:  # unplayable: swap in an unused song
                    spare = [i for i in range(len(game.songs)) if i not in used]
                    if spare:
                        used.add(pick := random.choice(spare))
                        queue.insert(0, pick)
                if queue:
                    prep = asyncio.create_task(self._prepare(game, queue[0]))  # prefetch during this round
                if not ok:
                    continue
                game.order.append(idx)
                await self._round(game, idx, planned)
                if game.over:
                    return
                await asyncio.sleep(5)
            await self._end(game)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("quiz crashed")
            await self._end(game)
        finally:
            prep.cancel()

    async def _round(self, game: Game, idx: int, planned: int) -> None:
        song = game.songs[idx]
        game.round, game.answer, game.winner, game.wrong = len(game.order), idx, None, []
        game.local = self.music.cache.lookup(song.track)
        await song.track.wait_available()
        dur = song.track.duration or 0
        game.start = random.uniform(0.15, 0.4) * dur if dur > 60 else 0.0
        for stage in range(len(STAGES)):
            if not game.vc.is_connected():  # kicked from voice
                await self._end(game)
                return
            game.stage, game.guessed = stage, set()
            game.advance.clear()
            await self._show(game, planned)
            self._play_clip(game)
            try:
                await asyncio.wait_for(game.advance.wait(), STAGE_TIME + STAGES[stage][0])
            except asyncio.TimeoutError:
                pass
            if game.winner or game.over:
                break
        if game.over:
            return
        if game.vc.is_playing():
            game.vc.stop()
        await self._reveal(game, song, planned)

    async def _show(self, game: Game, planned: int) -> None:
        view = RoundView(self, game)
        try:
            if game.message and game.message.id and game.stage:
                await game.message.edit(view=view)
            else:
                game.message = await game.channel.send(view=view)
        except discord.HTTPException as e:
            log.warning("quiz: can't post round: %s", e)

    async def _reveal(self, game: Game, song: Song, planned: int) -> None:
        t = song.track
        if game.winner:
            secs, name, pts, colour = STAGES[game.stage]
            result = f"🎉 <@{game.winner}> got it on **{name}** ({secs} s) · **+{pts}**"
        else:
            colour, result = 0x95A5A6, "😅 Nobody got it."
        box = ui.Container(accent_colour=discord.Colour(colour))
        head = [f"### ✅ Round {game.round}/{planned}: it was",
                f"## [{esc(short(song.name, 80))}]({t.play_url or t.url})" + (f"\nby **{esc(song.artist)}**" if song.artist else "")]
        box.add_item(ui.Section(*head, accessory=ui.Thumbnail(t.thumbnail)) if t.thumbnail
                     else ui.TextDisplay("\n".join(head)))
        box.add_item(ui.TextDisplay(result))
        box.add_item(ui.Separator())
        box.add_item(ui.TextDisplay("**Scores**\n" + scoreboard(game, 5)))
        view = ui.LayoutView(timeout=None)
        view.add_item(box)
        try:
            if game.message:
                await game.message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
        game.message = None

    async def _end(self, game: Game, announce: bool = True) -> None:
        if self.games.get(game.guild_id) is not game:
            return
        game.over = True
        game.advance.set()
        self.games.pop(game.guild_id, None)
        if game.task and game.task is not asyncio.current_task():
            game.task.cancel()
        try:
            if game.vc.is_playing():
                game.vc.stop()
            await game.vc.disconnect(force=True)
        except Exception:
            pass
        if not announce:
            return
        box = ui.Container(accent_colour=discord.Colour(0xF1C40F))
        winner = max(game.scores.items(), key=lambda kv: kv[1], default=None)
        box.add_item(ui.TextDisplay(f"## 🏁 Quiz over!\n" + (f"👑 <@{winner[0]}> wins with **{winner[1]}** points!"
                                                            if winner else "Nobody scored. Tough crowd 😅")))
        box.add_item(ui.Separator())
        box.add_item(ui.TextDisplay("**Final scores**\n" + scoreboard(game)))
        view = ui.LayoutView(timeout=None)
        view.add_item(box)
        try:
            await game.channel.send(view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    # ---- buttons ---------------------------------------------------------------------
    def make_button(self, game: Game, action: str):
        async def cb(interaction: discord.Interaction):
            if self.games.get(game.guild_id) is not game or game.winner or not game.message \
                    or interaction.message.id != game.message.id:
                return await interaction.response.send_message("This round is over.", ephemeral=True)
            voice = getattr(interaction.user, "voice", None)
            if not voice or voice.channel != game.vc.channel:
                return await interaction.response.send_message("Join the quiz voice channel to play.", ephemeral=True)
            await interaction.response.defer()
            if action == "replay":
                self._play_clip(game)
            else:
                game.advance.set()
        return cb

    # ---- commands --------------------------------------------------------------------
    quiz = app_commands.Group(name="quiz", description="Guess-the-song game in voice", guild_only=True)

    @quiz.command(name="start", description="Start a guess-the-song game in your voice channel")
    @app_commands.describe(
        playlist="YouTube / Spotify / Deezer / SoundCloud playlist link (default: this server's most played songs)",
        rounds="Number of songs (default 10)",
    )
    async def quiz_start(self, interaction: discord.Interaction, playlist: str | None = None,
                         rounds: app_commands.Range[int, 1, 30] = 10):
        guild, music = interaction.guild, self.music
        voice = interaction.user.voice
        if not voice or not voice.channel:
            return await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
        if guild.id in self.games:
            return await interaction.response.send_message("A quiz is already running here.", ephemeral=True)
        if music.players.get(guild.id) or guild.voice_client:
            return await interaction.response.send_message("I'm busy playing music. Use `/stop` first.", ephemeral=True)
        perms = voice.channel.permissions_for(guild.me)
        if not (perms.connect and perms.speak):
            return await interaction.response.send_message(f"I need Connect and Speak in {voice.channel.mention}.",
                                                           ephemeral=True)
        await interaction.response.defer(thinking=True)
        try:
            songs, what = await self._load(guild.id, playlist)
        except ExtractError as e:
            return await interaction.followup.send(f"❌ Couldn't load that: {e}")
        if len(songs) < MIN_SONGS:
            hint = "Play some music first, or give a playlist link." if not playlist else "Use a playlist link."
            return await interaction.followup.send(f"❌ Need at least {MIN_SONGS} songs, found {len(songs)}. {hint}")
        if guild.id in self.games or guild.voice_client:
            return await interaction.followup.send("❌ Something else started meanwhile.")
        try:
            vc = await voice.channel.connect(self_deaf=True, timeout=20)
        except (RuntimeError, asyncio.TimeoutError, discord.ClientException) as e:
            log.error("quiz voice connect failed: %s", e)
            return await interaction.followup.send("❌ Couldn't join voice.")
        order = random.sample(range(len(songs)), min(rounds, len(songs)))
        game = Game(guild.id, interaction.channel, vc, interaction.user.id, songs, order)
        self.games[guild.id] = game
        game.task = asyncio.create_task(self._run(game), name=f"quiz-{guild.id}")
        await interaction.followup.send(
            f"🎮 **Music quiz!** {len(order)} rounds from {esc(short(what, 80))} ({len(songs)} songs).\n"
            f"Each round starts with a **1 s** clip. ⏭️ Skip for a longer one: "
            + " → ".join(f"{s}s {n} ({p} pts)" for s, n, p, _c in STAGES)
            + ".\nAnswer with `/guess`, it searches the song list as you type. First round in a few seconds…")

    @quiz.command(name="stop", description="Stop the music quiz")
    async def quiz_stop(self, interaction: discord.Interaction):
        game = self.games.get(interaction.guild_id)
        if not game:
            return await interaction.response.send_message("No quiz is running.", ephemeral=True)
        is_mod = interaction.user.guild_permissions.manage_guild
        if interaction.user.id != game.host_id and not is_mod:
            return await interaction.response.send_message("Only the host or a mod can stop the quiz.", ephemeral=True)
        await interaction.response.send_message("🛑 Quiz stopped.")
        await self._end(game)

    @app_commands.command(name="guess", description="Guess the song in the music quiz")
    @app_commands.describe(song="Start typing, then pick the song from the list")
    @app_commands.guild_only()
    async def guess(self, interaction: discord.Interaction, song: str):
        game = self.games.get(interaction.guild_id)
        if not game or game.answer < 0 or game.winner or not game.message:
            return await interaction.response.send_message("No round is running right now.", ephemeral=True)
        uid = interaction.user.id
        if uid in game.guessed:
            return await interaction.response.send_message(
                "You already guessed at this stage. Wait for the next clip (or ⏭️ Skip).", ephemeral=True)
        game.guessed.add(uid)
        m = re.fullmatch(r"#q(\d+)", song)
        picked = int(m.group(1)) if m else None
        answer = game.songs[game.answer]
        correct = picked == game.answer if picked is not None else norm(song) in (norm(answer.name), norm(answer.label))
        if correct:
            pts = STAGES[game.stage][2]
            game.scores[uid] = game.scores.get(uid, 0) + pts
            game.winner = uid
            game.advance.set()
            return await interaction.response.send_message(f"✅ Correct! **+{pts}**", ephemeral=True)
        shown = game.songs[picked].name if picked is not None and picked < len(game.songs) else song
        game.wrong.append(esc(short(shown, 40)))
        await interaction.response.send_message(f"❌ Not **{esc(short(shown, 60))}**.", ephemeral=True)
        try:
            await game.message.edit(view=RoundView(self, game))
        except discord.HTTPException:
            pass

    @guess.autocomplete("song")
    async def guess_autocomplete(self, interaction: discord.Interaction, current: str):
        game = self.games.get(interaction.guild_id)
        if not game:
            return []
        q = norm(current)
        hits = [(i, s) for i, s in enumerate(game.songs) if not q or q in norm(s.label)]
        hits.sort(key=lambda h: (not norm(h[1].name).startswith(q), h[1].label.lower()))
        return [app_commands.Choice(name=s.label, value=f"#q{i}") for i, s in hits[:25]]


async def setup(bot):
    await bot.add_cog(Quiz(bot))
