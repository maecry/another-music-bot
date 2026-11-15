import os
import time

import discord
from discord import app_commands
from discord.ext import commands


def rss_mb() -> str:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return f"{int(line.split()[1]) / 1024:.0f} MB"
    except OSError:
        pass
    return "n/a"


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.started = time.monotonic()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("Owner only.", ephemeral=True)
            return False
        return True

    @app_commands.default_permissions(administrator=True)
    @app_commands.command(description="Bot stats (owner only)")
    async def stats(self, interaction: discord.Interaction):
        music = self.bot.get_cog("Music")
        players = len(music.players) if music else 0
        n, size = music.cache.stats() if music and music.cache.enabled else (0, 0)
        up = int(time.monotonic() - self.started)
        await interaction.response.send_message(
            f"**RAM:** {rss_mb()} · **Servers:** {len(self.bot.guilds)} · **Players:** {players} · **Cache:** {n} songs, {size / 1024**3:.2f} GB\n"
            f"**Uptime:** {up // 3600}h {up // 60 % 60}m · **Latency:** {self.bot.latency * 1000:.0f} ms · "
            f"**discord.py** {discord.__version__} · **pid** {os.getpid()}",
            ephemeral=True,
        )

    @app_commands.default_permissions(administrator=True)
    @app_commands.command(description="Re-sync slash commands (owner only)")
    async def sync(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        synced = await self.bot.sync_commands()
        await interaction.followup.send(f"Synced {synced} commands.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Admin(bot))
