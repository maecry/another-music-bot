import discord
from discord import app_commands, ui
from discord.ext import commands

# Commands not listed here still show up, under "Other", so new features never go missing from /help.
CATEGORIES = [
    ("🎵 Playback", ["play", "playnext", "pause", "resume", "skip", "previous", "seek", "nowplaying", "join", "leave", "stop"]),
    ("📜 Queue", ["queue", "shuffle", "loop", "remove", "move", "clear", "dedupe"]),
    ("🎛️ Sound", ["filter"]),
    ("🎮 Fun", ["quiz", "guess", "wrapped"]),
    ("🇻🇳 Community", ["lunar", "setlunardayremind", "setghostping"]),
    ("⚙️ Server (Manage Server)", ["settings"]),
    ("🔧 Owner only", ["stats", "sync", "update"]),
    ("❔ Other", []),
]


class General(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _mention(self, name: str) -> str:
        cid = getattr(self.bot, "command_ids", {}).get(name.split()[0])
        return f"</{name}:{cid}>" if cid else f"`/{name}`"  # clickable when we know the command ID

    def _line(self, cmd, aliases: dict[str, list[str]]) -> str:
        if isinstance(cmd, app_commands.Group):
            subs = ", ".join(self._mention(f"{cmd.name} {c.name}") for c in cmd.commands)
            return f"{subs} · {cmd.description}"
        extra = aliases.get(cmd.name)
        also = f" (also {', '.join('/' + a for a in extra)})" if extra else ""
        return f"{self._mention(cmd.name)} · {cmd.description}{also}"

    @app_commands.command(description="List all commands")
    async def help(self, interaction: discord.Interaction):
        aliases = {name: (a if isinstance(a, list) else [a]) for name, a in self.bot.config.aliases.items()}
        alias_names = {x for a in aliases.values() for x in a}
        cmds = {c.name: c for c in self.bot.tree.get_commands() if c.name not in alias_names}
        known = {n for _, names in CATEGORIES for n in names} | {"help"}

        box = ui.Container(accent_colour=discord.Colour(self.bot.config.accent_color))
        box.add_item(ui.TextDisplay(f"## {self.bot.user.name} · Commands"))
        for title, names in CATEGORIES:
            names = names or sorted(n for n in cmds if n not in known)
            lines = [self._line(cmds[n], aliases) for n in names if n in cmds]
            if lines:
                box.add_item(ui.Separator())
                box.add_item(ui.TextDisplay(f"**{title}**\n" + "\n".join(lines)))
        box.add_item(ui.Separator())
        box.add_item(ui.TextDisplay(
            "-# The now-playing panel has buttons too: ⏮️ Previous · ⏸️ Pause · ⏭️ Skip · 🔁 Loop · 🔀 Shuffle · ⏹️ Stop"))
        view = ui.LayoutView()
        view.add_item(box)
        await interaction.response.send_message(view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(General(bot))
