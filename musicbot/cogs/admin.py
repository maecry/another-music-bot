import asyncio
import os
import sys
import time

import discord
from discord import app_commands
from discord.ext import commands

from ..config import ROOT


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

    @app_commands.default_permissions(administrator=True)
    @app_commands.command(description="Pull the latest code from GitHub and restart the bot (owner only)")
    async def update(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        code, out = await run(["git", "pull", "--ff-only"])
        if code:
            return await interaction.followup.send(f"❌ `git pull` failed, not restarting:\n```\n{out[-1800:]}\n```", ephemeral=True)
        if "Already up to date" in out:
            return await interaction.followup.send("✅ Already up to date, nothing to restart.", ephemeral=True)
        if "requirements.txt" in out:  # same install the panel's startup command does
            code, pip_out = await run([sys.executable, "-m", "pip", "install", "-U", "--prefix", ".local", "-r", "requirements.txt"])
            if code:
                return await interaction.followup.send(f"❌ pip install failed, not restarting:\n```\n{pip_out[-1800:]}\n```", ephemeral=True)
        await interaction.followup.send(f"🔄 Updated, restarting...\n```\n{out[-1800:]}\n```", ephemeral=True)
        self.bot.restart = True
        await self.bot.close()  # main() re-execs the process once run() returns


async def run(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=ROOT, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace").strip()


async def setup(bot):
    await bot.add_cog(Admin(bot))
