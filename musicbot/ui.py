"""Components V2 UI: now-playing panel (LayoutView) + persistent controls + queue pages."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import ui

from .extractor import fmt_time

if TYPE_CHECKING:
    from .extractor import Track
    from .player import GuildPlayer

LOOP_LABEL = {"off": "Loop", "track": "Loop: Track", "queue": "Loop: Queue"}
SOURCE_ICON = {"youtube": "🔴", "youtu.be": "🔴", "spotify": "🟢", "soundcloud": "🟠", "deezer": "🟣", "bandcamp": "🔵"}


def esc(text: str) -> str:
    return discord.utils.escape_markdown(text)


def short(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def source_icon(track: Track) -> str:
    return next((icon for key, icon in SOURCE_ICON.items() if key in track.url), "🎵")


# ---- persistent controls ---------------------------------------------------
# DynamicItem = matched by custom_id regex, so buttons keep working after a bot restart.
class ControlButton(ui.DynamicItem[ui.Button], template=r"music:(?P<action>[a-z]+)"):
    def __init__(self, action: str, label: str, emoji: str, style=discord.ButtonStyle.secondary):
        super().__init__(ui.Button(label=label, emoji=emoji, style=style, custom_id=f"music:{action}"))
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Button, match):
        return cls(match["action"], item.label or "", str(item.emoji or "❔"))

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_control(interaction, self.action)


class QueueSelect(ui.DynamicItem[ui.Select], template=r"music-jump"):
    def __init__(self, tracks: list[Track] | None = None):
        options = [
            discord.SelectOption(
                label=short(f"{i}. {t.title}", 100),
                description=short(f"{t.uploader + ' · ' if t.uploader else ''}{fmt_time(t.duration)}", 100),
                value=str(id(t)),
            )
            for i, t in enumerate((tracks or [])[:25], 1)
        ] or [discord.SelectOption(label="Queue is empty", value="none")]
        super().__init__(ui.Select(
            custom_id="music-jump",
            placeholder="Queue - Select a track to play" if tracks else "Queue is empty",
            options=options,
            disabled=not tracks,
        ))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Select, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_control(interaction, "jump", (interaction.data.get("values") or [""])[0])


class PanelView(ui.LayoutView):
    def __init__(self, player: GuildPlayer, ended: bool = False):
        super().__init__(timeout=None)
        box = ui.Container(accent_colour=discord.Colour(player.config.accent_color))
        t = player.current
        if ended or not t:
            box.add_item(ui.TextDisplay("### ⏹️ Stopped" if ended else "### 💤 Queue finished"))
            box.add_item(ui.TextDisplay("-# Use `/play` to add more."))
            self.add_item(box)
            return

        header = [
            f"### {source_icon(t)} Now Playing\n**[{esc(short(t.title, 60))}]({t.url})**",
            f"👤 {esc(short(t.uploader, 40)) + ' · ' if t.uploader else ''}🕒 `{fmt_time(t.duration)}`",
            f"👥 Requested by <@{t.requester_id}>",
        ]
        if t.thumbnail:
            box.add_item(ui.Section(*header, accessory=ui.Thumbnail(t.thumbnail)))
        else:
            box.add_item(ui.TextDisplay("\n".join(header)))
        box.add_item(ui.Separator())
        status = [f"🎶 {len(player.queue)} songs in queue", f"🔁 Loop: {player.loop.title()}"]
        if player.filter != "off":
            from .player import FILTERS
            status.append(f"🎛️ {FILTERS[player.filter][0]}")
        if player.paused:
            status.insert(0, "⏸️ Paused")
        box.add_item(ui.TextDisplay("-# " + " · ".join(status)))
        box.add_item(ui.ActionRow(QueueSelect(list(player.queue))))
        self.add_item(box)

        paused = player.paused
        back = ControlButton("previous", "Previous", "⏮️")
        back.item.disabled = not player.history
        self.add_item(ui.ActionRow(
            back,
            ControlButton("pause", "Resume" if paused else "Pause", "▶️" if paused else "⏸️",
                          discord.ButtonStyle.primary if paused else discord.ButtonStyle.secondary),
            ControlButton("skip", "Skip", "⏭️"),
        ))
        self.add_item(ui.ActionRow(
            ControlButton("loop", LOOP_LABEL[player.loop], "🔁",
                          discord.ButtonStyle.primary if player.loop != "off" else discord.ButtonStyle.secondary),
            ControlButton("shuffle", "Shuffle", "🔀"),
            ControlButton("stop", "Stop", "⏹️", discord.ButtonStyle.danger),
        ))


# ---- queue pages -------------------------------------------------------------
class QueueView(ui.LayoutView):
    PER_PAGE = 10

    def __init__(self, player: GuildPlayer, page: int = 0):
        super().__init__(timeout=120)
        self.player = player
        self.page = page
        self.build()

    def build(self) -> None:
        self.clear_items()
        q = list(self.player.queue)
        pages = max(1, -(-len(q) // self.PER_PAGE))
        self.page = max(0, min(self.page, pages - 1))
        start = self.page * self.PER_PAGE
        lines = [
            f"`{i}.` {esc(short(t.title, 70))} · {fmt_time(t.duration)}"
            for i, t in enumerate(q[start:start + self.PER_PAGE], start + 1)
        ]
        cur = self.player.current
        total = sum(t.duration or 0 for t in q)
        box = ui.Container(accent_colour=discord.Colour(self.player.config.accent_color))
        box.add_item(ui.TextDisplay(f"### 📜 Queue · {len(q)} tracks · {fmt_time(total)}"))
        if cur:
            box.add_item(ui.TextDisplay(f"**Now:** {esc(short(cur.title, 90))}"))
        box.add_item(ui.Separator())
        box.add_item(ui.TextDisplay("\n".join(lines) or "*Queue is empty.*"))
        box.add_item(ui.TextDisplay(f"-# Page {self.page + 1}/{pages}"))
        if pages > 1:
            prev = ui.Button(emoji="◀️", disabled=self.page == 0)
            nxt = ui.Button(emoji="▶️", disabled=self.page >= pages - 1)
            prev.callback = self._turn(-1)
            nxt.callback = self._turn(1)
            box.add_item(ui.ActionRow(prev, nxt))
        self.add_item(box)

    def _turn(self, step: int):
        async def cb(interaction: discord.Interaction):
            self.page += step
            self.build()
            await interaction.response.edit_message(view=self)
        return cb


# ---- control handler -----------------------------------------------------------
async def handle_control(interaction: discord.Interaction, action: str, value: str = "") -> None:
    cog = interaction.client.get_cog("Music")
    player = cog.players.get(interaction.guild_id) if cog and interaction.guild_id else None
    if not player or not player.current:
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    if action == "queue":
        return await interaction.response.send_message(view=QueueView(player), ephemeral=True)
    voice = getattr(interaction.user, "voice", None)
    if not voice or voice.channel != player.vc.channel:
        return await interaction.response.send_message("Join my voice channel to use the controls.", ephemeral=True)

    if action == "pause":
        player.toggle_pause()
    elif action == "loop":
        player.cycle_loop()
    elif action == "shuffle":
        player.shuffle()
    elif action == "jump":
        track = next((t for t in player.queue if str(id(t)) == value), None)
        if not track:
            return await interaction.response.send_message("That track isn't in the queue anymore.", ephemeral=True)
        await interaction.response.defer()
        return player.jump(track)  # the new track posts a fresh panel
    elif action == "previous":
        if not player.history:
            return await interaction.response.send_message("No previous track.", ephemeral=True)
        await interaction.response.defer()
        return player.previous()  # the new track posts a fresh panel
    elif action == "skip":
        await interaction.response.defer()
        return player.skip()
    elif action == "stop":
        await interaction.response.defer()
        return await player.destroy()
    if interaction.message and player.panel and interaction.message.id == player.panel.id:
        await interaction.response.edit_message(view=PanelView(player))
    else:  # control used on an old panel
        await interaction.response.send_message("Done.", ephemeral=True, delete_after=5)
        await player.refresh_panel()
