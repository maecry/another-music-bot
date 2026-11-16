import copy
import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

import discord
from discord.ext import commands

from musicbot import config as cfg
from musicbot.ffmpeg import check_deno, ffmpeg_exe, prepare_path
from musicbot.settings import SettingsStore
from musicbot.stats import PlayStats
from musicbot.ui import ControlButton, QueueSelect

log = logging.getLogger("musicbot")


def setup_logging() -> None:
    Path("logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    file = logging.handlers.RotatingFileHandler("logs/bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    console = logging.StreamHandler()
    for h in (file, console):
        h.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file)
    root.addHandler(console)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("discord.app_commands.tree").addFilter(_drop_stale_autocomplete)


def _drop_stale_autocomplete(record: logging.LogRecord) -> bool:
    """A slow autocomplete answer after the user typed on / hit enter gets 40060 from Discord. Harmless."""
    exc = record.exc_info and record.exc_info[1]
    return not (isinstance(exc, discord.HTTPException) and exc.code in (40060, 10062))


class MusicBot(commands.Bot):
    def __init__(self, config: cfg.Config):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.guild_messages = True  # ghost-ping tracker: mentions + deletes (no message content intent)
        members = discord.MemberCacheFlags.none()
        members.voice = True  # only cache people in voice (needed for "alone" detection)
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            member_cache_flags=members,
            chunk_guilds_at_startup=False,
            max_messages=None,
            help_command=None,
            # <@user> in panels/quiz/wrapped is a label, not a ping; lunar + ghost-ping pass their own
            allowed_mentions=discord.AllowedMentions.none(),
            activity=discord.Activity(type=discord.ActivityType.listening, name="/play"),
        )
        self.config = config
        self.ffmpeg = ffmpeg_exe()
        self.command_ids: dict[str, int] = {}
        self.settings = SettingsStore(cfg.ROOT / "data" / "settings.json")  # per-server, shared by all cogs
        self.stats = PlayStats(cfg.ROOT / "data" / "history.db")         # play history for /wrapped

    async def setup_hook(self) -> None:
        self.add_dynamic_items(ControlButton, QueueSelect)
        for ext in ("musicbot.cogs.music", "musicbot.cogs.admin", "musicbot.cogs.general",
                    "musicbot.cogs.community", "musicbot.cogs.wrapped", "musicbot.cogs.quiz"):
            await self.load_extension(ext)
        self.add_aliases()
        try:
            n = await self.sync_commands()
            log.info("synced %d slash commands", n)
        except discord.Forbidden:
            log.error(
                "Can't sync slash commands to dev_guild_id=%s (Missing Access). Check that the ID is the "
                "server ID (right-click server icon -> Copy Server ID) and re-invite the bot with BOTH the "
                "`bot` and `applications.commands` scopes. Restart the bot after fixing.",
                self.config.dev_guild_id,
            )

    def add_aliases(self) -> None:
        """Register each alias from config as an extra slash command sharing the original's code."""
        for name, aliases in self.config.aliases.items():
            cmd = self.tree.get_command(name)
            if not isinstance(cmd, discord.app_commands.Command):
                log.warning("alias target /%s doesn't exist, skipped", name)
                continue
            for alias in aliases if isinstance(aliases, list) else [aliases]:
                alias = str(alias).lower().strip()
                if not re.fullmatch(r"[-_a-z0-9]{1,32}", alias) or self.tree.get_command(alias):
                    log.warning("alias /%s for /%s is invalid or taken, skipped", alias, name)
                    continue
                clone = copy.copy(cmd)
                clone.name = alias
                clone.description = f"{cmd.description} (alias of /{name})"[:100]
                self.tree.add_command(clone)

    async def sync_commands(self) -> int:
        if self.config.dev_guild_id:
            guild = discord.Object(self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        self.command_ids = {c.name: c.id for c in synced}  # for clickable </cmd:id> mentions in /help
        return len(synced)

    async def is_owner(self, user) -> bool:
        return user.id in self.config.owner_ids or await super().is_owner(user)

    async def close(self) -> None:
        await super().close()
        self.stats.close()

    async def on_ready(self) -> None:
        log.info("logged in as %s in %d servers", self.user, len(self.guilds))


def main() -> None:
    setup_logging()
    prepare_path()
    config = cfg.load()
    log.info("ffmpeg: %s", ffmpeg_exe())
    try:
        import davey  # discord.py needs it for encrypted voice
        from importlib.metadata import version
        if version("davey").startswith("0.2"):
            log.error("davey %s is installed: discord.py 2.7.1 plays SILENCE with it. Run: pip install 'davey<0.2'",
                      version("davey"))
    except ImportError as e:
        log.error("davey failed to load, so the bot can't join voice: %s. On Windows, Smart App Control "
                  "can block it (Windows Security > App & browser control). On Linux: pip install davey.", e)
    deno = check_deno()
    if deno:
        log.info("deno: %s", deno)
    else:
        log.warning("deno not found: YouTube playback will likely fail (pip install deno)")
    bot = MusicBot(config)
    bot.run(config.token, log_handler=None)
    if getattr(bot, "restart", False):  # set by /update: start fresh in the same process slot
        log.info("restarting after /update")
        os.execv(sys.executable, [sys.executable, *sys.argv])


if __name__ == "__main__":
    main()
