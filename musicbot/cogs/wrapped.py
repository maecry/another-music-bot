"""/wrapped: Spotify-Wrapped-style listening recap as a swipeable Components V2 story (text only, no images)."""
from __future__ import annotations

from datetime import datetime

import discord
from discord import app_commands, ui
from discord.ext import commands

from ..lunar import VN
from ..stats import MIN_PLAY
from ..ui import esc, short

OFFSET = 7 * 3600  # hours/days are counted in Vietnam time (UTC+7)
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
COLORS = [0x1DB954, 0xF573A0, 0x7C3AED, 0xFF6B35, 0x2D46B9, 0xF7DC4A, 0x1DB954]
MEDALS = ["🥇", "🥈", "🥉", "4.", "5."]


def period_start(period: str) -> tuple[int, str]:
    now = datetime.now(VN)
    if period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp()), f"{now:%B %Y}"
    if period == "year":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp()), str(now.year)
    return 0, "All time"


def mins(seconds: float) -> str:
    return f"{int(seconds // 60):,}"


def song_line(r, i: int) -> str:
    artist = f" · {esc(short(r['artist'], 30))}" if r["artist"] else ""
    return f"{MEDALS[i]} **{esc(short(r['title'], 55))}**{artist} · {r['n']}×"


def longest_streak(days: list[int]) -> int:
    best = cur = 0
    prev = None
    for d in sorted(days):
        cur = cur + 1 if prev is not None and d == prev + 1 else 1
        best, prev = max(best, cur), d
    return best


def personality(top_hour: int | None, variety: float) -> tuple[str, str]:
    if top_hour is None:
        when = "Listener"
    elif top_hour >= 22 or top_hour < 4:
        when = "Night Owl 🦉"
    elif top_hour < 9:
        when = "Early Bird 🐦"
    elif top_hour < 17:
        when = "Daytime Groover ☀️"
    else:
        when = "Evening Viber 🌆"
    if variety >= 0.7:
        return f"The Explorer · {when}", "You're always hunting for something new. Repeats? Never heard of them."
    if variety <= 0.35:
        return f"The Loyalist · {when}", "When you love a song, you REALLY love it. Replay is a lifestyle."
    return f"The Balancer · {when}", "Old favourites and new finds, perfectly mixed."


class WrappedView(ui.LayoutView):
    def __init__(self, slides: list[tuple[list, str | None]], owner_id: int):
        super().__init__(timeout=900)
        self.slides = slides  # [(text blocks, thumbnail url)]
        self.owner_id = owner_id
        self.page = 0
        self.build()

    def build(self) -> None:
        self.clear_items()
        texts, thumb = self.slides[self.page]
        box = ui.Container(accent_colour=discord.Colour(COLORS[self.page % len(COLORS)]))
        head, *rest = texts
        if thumb:
            box.add_item(ui.Section(ui.TextDisplay(head), accessory=ui.Thumbnail(thumb)))
        else:
            box.add_item(ui.TextDisplay(head))
        for block in rest:
            box.add_item(ui.Separator() if block == "---" else ui.TextDisplay(block))
        dots = " ".join("●" if i == self.page else "○" for i in range(len(self.slides)))
        box.add_item(ui.TextDisplay(f"-# {dots}"))
        back = ui.Button(emoji="◀️", disabled=self.page == 0)
        nxt = ui.Button(label="Next" if self.page < len(self.slides) - 1 else "Replay",
                        emoji="▶️" if self.page < len(self.slides) - 1 else "🔁", style=discord.ButtonStyle.success)
        back.callback, nxt.callback = self._turn(-1), self._turn(1)
        box.add_item(ui.ActionRow(back, nxt))
        self.add_item(box)

    def _turn(self, step: int):
        async def cb(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id:
                return await interaction.response.send_message("Run `/wrapped` to get your own!", ephemeral=True)
            self.page = (self.page + step) % len(self.slides)
            self.build()
            await interaction.response.edit_message(view=self)
        return cb


class Wrapped(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.stats = bot.stats

    def _q(self, sql: str, *args):
        return self.stats.query(sql, *args)

    # ---- per user ------------------------------------------------------------
    def user_slides(self, guild: discord.Guild, member: discord.abc.User, since: int, label: str):
        base = ("FROM plays p JOIN listeners l ON l.play_id = p.id "
                "WHERE p.guild_id = ? AND l.user_id = ? AND p.started_at >= ? AND p.played >= ?")
        args = (guild.id, member.id, since, MIN_PLAY)
        tot = self._q(f"SELECT COUNT(*) n, COALESCE(SUM(p.played), 0) secs, COUNT(DISTINCT p.url) songs, "
                      f"COUNT(DISTINCT p.artist) artists {base}", *args)[0]
        if not tot["n"]:
            return None
        songs = self._q(f"SELECT p.title, p.artist, MAX(p.thumbnail) thumbnail, COUNT(*) n {base} "
                        f"GROUP BY p.url ORDER BY n DESC, SUM(p.played) DESC LIMIT 5", *args)
        artists = self._q(f"SELECT p.artist, COUNT(*) n, SUM(p.played) secs {base} AND p.artist IS NOT NULL "
                          f"GROUP BY p.artist ORDER BY secs DESC LIMIT 5", *args)
        hours = self._q(f"SELECT ((p.started_at + {OFFSET}) % 86400) / 3600 h, COUNT(*) n {base} "
                        f"GROUP BY h ORDER BY n DESC LIMIT 1", *args)
        wdays = self._q(f"SELECT ((p.started_at + {OFFSET}) / 86400 + 3) % 7 d, COUNT(*) n {base} "
                        f"GROUP BY d ORDER BY n DESC LIMIT 1", *args)
        days = [r["day"] for r in self._q(f"SELECT DISTINCT (p.started_at + {OFFSET}) / 86400 day {base}", *args)]
        first = self._q(f"SELECT p.title, p.artist, p.started_at {base} ORDER BY p.started_at LIMIT 1", *args)[0]
        buddy = self._q(
            "SELECT l2.user_id, COUNT(*) n FROM plays p JOIN listeners l ON l.play_id = p.id "
            "JOIN listeners l2 ON l2.play_id = p.id AND l2.user_id != l.user_id "
            "WHERE p.guild_id = ? AND l.user_id = ? AND p.started_at >= ? AND p.played >= ? "
            "GROUP BY l2.user_id ORDER BY n DESC LIMIT 1", *args)
        requested = self._q("SELECT COUNT(*) n FROM plays WHERE guild_id = ? AND requester_id = ? "
                            "AND started_at >= ? AND played >= ?", *args)[0]["n"]
        ranking = self._q("SELECT l.user_id, SUM(p.played) secs FROM plays p JOIN listeners l ON l.play_id = p.id "
                          "WHERE p.guild_id = ? AND p.started_at >= ? AND p.played >= ? "
                          "GROUP BY l.user_id ORDER BY secs DESC", guild.id, since, MIN_PLAY)
        rank = next(i for i, r in enumerate(ranking, 1) if r["user_id"] == member.id)

        name = esc(member.display_name)
        top_hour = hours[0]["h"] if hours else None
        title, blurb = personality(top_hour, tot["songs"] / tot["n"])
        top = songs[0]
        hrs = tot["secs"] / 3600
        if rank == 1:
            rank_line = "👑 You're the **#1** listener in the server!"
        else:
            rank_line = (f"🏆 **#{rank}** listener in the server, ahead of "
                         f"{(len(ranking) - rank) / (len(ranking) - 1) * 100:.0f}% of everyone.")

        slides = [
            ([f"# 🎧 {name}'s Wrapped\n## {label}",
              f"You've been busy in **{esc(guild.name)}**.\nLet's see what your ears have been up to… 👀",
              "-# Tap Next ▶"], member.display_avatar.url),
            ([f"### You listened for\n# {mins(tot['secs'])} minutes",
              f"That's **{hrs:.1f} hours** of music, across **{tot['n']}** plays and **{tot['songs']}** different songs.",
              rank_line],
             None),
            ([f"### Your #1 song\n# {esc(short(top['title'], 60))}",
              f"{'by **' + esc(top['artist']) + '** · ' if top['artist'] else ''}played **{top['n']}** times",
              "---", "**Your top songs**\n" + "\n".join(song_line(r, i) for i, r in enumerate(songs))],
             top["thumbnail"]),
        ]
        if artists:
            a = artists[0]
            slides.append(([f"### Your top artist\n# {esc(short(a['artist'], 50))}",
                            f"**{mins(a['secs'])} minutes** together, {a['n']} plays.",
                            "---", "**Your top artists**\n" + "\n".join(
                                f"{MEDALS[i]} **{esc(short(r['artist'], 40))}** · {mins(r['secs'])} min"
                                for i, r in enumerate(artists))], None))
        habits = []
        if top_hour is not None:
            habits.append(f"🕐 Peak hour: **{top_hour:02d}:00–{(top_hour + 1) % 24:02d}:00**")
        if wdays:
            habits.append(f"📅 Favourite day: **{DAYS[wdays[0]['d']]}**")
        habits.append(f"🔥 Longest streak: **{longest_streak(days)} day{'s' if longest_streak(days) != 1 else ''}** in a row "
                      f"({len(days)} days with music)")
        habits.append(f"🎛️ You queued **{requested}** song{'s' if requested != 1 else ''} yourself")
        if buddy:
            habits.append(f"🤝 Listening buddy: <@{buddy[0]['user_id']}> ({buddy[0]['n']} songs together)")
        when = datetime.fromtimestamp(first["started_at"], VN)
        habits.append(f"🌱 It all started with **{esc(short(first['title'], 50))}** on {when:%d %b}")
        slides.append((["### Your listening habits", "\n".join(habits)], None))
        slides.append(([f"### Your listening personality\n# {title}", blurb], None))
        slides.append(([
            f"## 🎧 {name} · Wrapped {label}",
            f"**{mins(tot['secs'])}** minutes · **{tot['songs']}** songs · **{tot['artists']}** artists · #{rank} listener",
            "---",
            "**Top songs**\n" + "\n".join(f"{i}. {esc(short(r['title'], 45))}" for i, r in enumerate(songs, 1)),
            "**Top artists**\n" + "\n".join(f"{i}. {esc(short(r['artist'], 45))}" for i, r in enumerate(artists, 1))
            if artists else "",
            f"**Personality:** {title}",
        ], member.display_avatar.url))
        return [(list(filter(None, t)), th) for t, th in slides]

    # ---- whole server ----------------------------------------------------------
    def server_slides(self, guild: discord.Guild, since: int, label: str):
        base = "FROM plays p WHERE p.guild_id = ? AND p.started_at >= ? AND p.played >= ?"
        args = (guild.id, since, MIN_PLAY)
        tot = self._q(f"SELECT COUNT(*) n, COALESCE(SUM(p.played), 0) secs, COUNT(DISTINCT p.url) songs, "
                      f"COUNT(DISTINCT p.artist) artists {base}", *args)[0]
        if not tot["n"]:
            return None
        songs = self._q(f"SELECT p.title, p.artist, MAX(p.thumbnail) thumbnail, COUNT(*) n {base} "
                        f"GROUP BY p.url ORDER BY n DESC, SUM(p.played) DESC LIMIT 5", *args)
        artists = self._q(f"SELECT p.artist, COUNT(*) n, SUM(p.played) secs {base} AND p.artist IS NOT NULL "
                          f"GROUP BY p.artist ORDER BY secs DESC LIMIT 5", *args)
        listeners = self._q("SELECT l.user_id, SUM(p.played) secs FROM plays p JOIN listeners l ON l.play_id = p.id "
                            "WHERE p.guild_id = ? AND p.started_at >= ? AND p.played >= ? "
                            "GROUP BY l.user_id ORDER BY secs DESC LIMIT 5", *args)
        djs = self._q(f"SELECT p.requester_id, COUNT(*) n {base} AND p.requester_id != 0 "
                      f"GROUP BY p.requester_id ORDER BY n DESC LIMIT 5", *args)
        hours = self._q(f"SELECT ((p.started_at + {OFFSET}) % 86400) / 3600 h, COUNT(*) n {base} "
                        f"GROUP BY h ORDER BY n DESC LIMIT 1", *args)
        top = songs[0]
        icon = guild.icon.url if guild.icon else None
        slides = [
            ([f"# 🎧 {esc(guild.name)} Wrapped\n## {label}",
              "The whole server's year in music. Let's go 🔊", "-# Tap Next ▶"], icon),
            ([f"### Together you listened for\n# {mins(tot['secs'])} minutes",
              f"**{tot['n']}** plays · **{tot['songs']}** different songs · **{tot['artists']}** artists"
              + (f"\nRush hour: **{hours[0]['h']:02d}:00** 🕐" if hours else "")], None),
            ([f"### The server's #1 song\n# {esc(short(top['title'], 60))}",
              f"{'by **' + esc(top['artist']) + '** · ' if top['artist'] else ''}played **{top['n']}** times",
              "---", "**Top songs**\n" + "\n".join(song_line(r, i) for i, r in enumerate(songs))], top["thumbnail"]),
        ]
        if artists:
            slides.append((["### Top artists", "\n".join(
                f"{MEDALS[i]} **{esc(short(r['artist'], 40))}** · {mins(r['secs'])} min" for i, r in enumerate(artists))],
                None))
        people = ["### The people behind the music"]
        if listeners:
            people.append("🎧 **Top listeners**\n" + "\n".join(
                f"{MEDALS[i]} <@{r['user_id']}> · {mins(r['secs'])} min" for i, r in enumerate(listeners)))
        if djs:
            people.append("🎛️ **Top DJs** (most songs queued)\n" + "\n".join(
                f"{MEDALS[i]} <@{r['requester_id']}> · {r['n']} songs" for i, r in enumerate(djs)))
        slides.append((people, None))
        return slides

    @app_commands.command(description="Your Spotify-Wrapped-style music recap (or the whole server's)")
    @app_commands.describe(user="Whose Wrapped to show (default: you)", period="Time range (default: this year)",
                           server="Show the whole server's Wrapped instead")
    @app_commands.choices(period=[
        app_commands.Choice(name="This month", value="month"),
        app_commands.Choice(name="This year", value="year"),
        app_commands.Choice(name="All time", value="all"),
    ])
    @app_commands.guild_only()
    async def wrapped(self, interaction: discord.Interaction, user: discord.Member | None = None,
                      period: app_commands.Choice[str] | None = None, server: bool = False):
        since, label = period_start(period.value if period else "year")
        if server:
            slides = self.server_slides(interaction.guild, since, label)
        else:
            slides = self.user_slides(interaction.guild, user or interaction.user, since, label)
        if not slides:
            who = "this server" if server else ("you" if not user or user == interaction.user else user.display_name)
            return await interaction.response.send_message(
                f"No listening found for {esc(who)} ({label}). Songs count after {MIN_PLAY} s of playing "
                f"while you're in the voice channel.", ephemeral=True)
        await interaction.response.send_message(view=WrappedView(slides, interaction.user.id),
                                                allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Wrapped(bot))
