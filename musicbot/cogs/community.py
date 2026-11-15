"""Non-music server features: lunar-day reminders (/setlunardayremind, /lunar) and the ghost-ping tracker."""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from datetime import datetime, time as dtime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..lunar import VN, describe, to_lunar

log = logging.getLogger(__name__)

WEEKDAYS = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]
GHOST_WINDOW = 600     # only report mentions removed within 10 minutes of sending
GHOST_CACHE_MAX = 5000  # messages with mentions kept in memory (a few hundred KB at most)


class Ghost:
    __slots__ = ("channel_id", "author_id", "users", "roles", "everyone", "at")

    def __init__(self, msg: discord.Message, users: set[int]):
        self.channel_id = msg.channel.id
        self.author_id = msg.author.id
        self.users = users
        self.roles = {r.id for r in msg.role_mentions}
        self.everyone = msg.mention_everyone
        self.at = time.time()


def mention_targets(msg: discord.Message) -> set[int]:
    return {u.id for u in msg.mentions if not u.bot and u.id != msg.author.id}


class Community(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.settings = bot.settings
        self.ghosts: OrderedDict[int, Ghost] = OrderedDict()  # message id -> who it pinged

    async def cog_load(self) -> None:
        self.lunar_loop.start()

    async def cog_unload(self) -> None:
        self.lunar_loop.cancel()

    # ---- lunar calendar ------------------------------------------------------
    @staticmethod
    def _tomorrow_text(today) -> str | None:
        """Reminder text if tomorrow is the 1st or 15th of a lunar month, else None."""
        tomorrow = today + timedelta(days=1)
        day, month, year, leap = to_lunar(tomorrow)
        if day not in (1, 15):
            return None
        icon = "🌕" if day == 15 else "🌑"
        return (
            f"{icon} **Ngày mai là {describe(day, month, leap)}**\n"
            f"-# {day}/{month}{' nhuận' if leap else ''} âm lịch · "
            f"{WEEKDAYS[tomorrow.weekday()]}, {tomorrow:%d/%m/%Y} dương lịch"
        )

    @staticmethod
    def _next_reminder(hour: int) -> datetime:
        now = datetime.now(VN)
        for i in range(62):
            day = now.date() + timedelta(days=i)
            at = datetime.combine(day, dtime(hour), VN)
            if at > now and to_lunar(day + timedelta(days=1))[0] in (1, 15):
                return at
        raise RuntimeError("no lunar 1st/15th within 62 days")  # can't happen

    @tasks.loop(time=[dtime(h, tzinfo=VN) for h in range(24)])
    async def lunar_loop(self) -> None:
        now = datetime.now(VN)
        text = self._tomorrow_text(now.date())
        if not text:
            return
        for gid, s in self.settings.items():
            if not s.lunar_channel_id or s.lunar_hour != now.hour:
                continue
            channel = self.bot.get_channel(s.lunar_channel_id)
            if channel is None:
                log.warning("lunar reminder: channel %s in guild %s is gone", s.lunar_channel_id, gid)
                continue
            ping = ""
            if s.lunar_role_id:
                ping = "@everyone " if s.lunar_role_id == gid else f"<@&{s.lunar_role_id}> "
            try:
                await channel.send(ping + text, allowed_mentions=discord.AllowedMentions(everyone=True, roles=True))
            except discord.HTTPException as e:
                log.warning("lunar reminder in %s failed: %s", channel.id, e)

    @lunar_loop.error
    async def lunar_loop_error(self, error: BaseException) -> None:
        log.exception("lunar reminder loop crashed", exc_info=error)

    @app_commands.command(name="setlunardayremind",
                          description="Remind this server the day before every lunar 1st and 15th (mùng 1 / Rằm)")
    @app_commands.describe(
        channel="Where to post (default: this channel)",
        hour="Hour to post, Vietnam time (0-23, default 20 = 8 PM)",
        ping="Role to ping, e.g. @everyone (default: no ping)",
        off="Turn the reminder off",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def setlunardayremind(
        self, interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        hour: app_commands.Range[int, 0, 23] | None = None,
        ping: discord.Role | None = None,
        off: bool = False,
    ):
        s = self.settings.get(interaction.guild_id)
        if off:
            s.lunar_channel_id = s.lunar_role_id = 0
            self.settings.save()
            return await interaction.response.send_message("🔕 Lunar-day reminders are off.", ephemeral=True)

        channel = channel or interaction.channel
        me = interaction.guild.me
        if not isinstance(channel, (discord.TextChannel, discord.Thread)) or not channel.permissions_for(me).send_messages:
            return await interaction.response.send_message(f"❌ I can't send messages in {channel.mention}.", ephemeral=True)
        s.lunar_channel_id = channel.id
        if hour is not None:
            s.lunar_hour = hour
        if ping is not None:
            s.lunar_role_id = ping.id
        self.settings.save()

        role_id = s.lunar_role_id
        role = interaction.guild.get_role(role_id) if role_id else None
        warn = ""
        if role and (role.is_default() or not role.mentionable) and not channel.permissions_for(me).mention_everyone:
            warn = f"\n⚠️ I need **Mention @everyone, @here and All Roles** in {channel.mention} to ping {role.mention}."
        nxt = self._next_reminder(s.lunar_hour)
        await interaction.response.send_message(
            f"🌙 Lunar-day reminders on in {channel.mention} at **{s.lunar_hour:02d}:00** (giờ Việt Nam), "
            f"the day before every mùng 1 and Rằm"
            f"{f', pinging {role.mention}' if role else ''}.\n"
            f"Next one: <t:{int(nxt.timestamp())}:F> (<t:{int(nxt.timestamp())}:R>).{warn}",
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="lunar", description="Today's date in the Vietnamese lunar calendar (âm lịch)")
    async def lunar(self, interaction: discord.Interaction):
        today = datetime.now(VN).date()
        day, month, year, leap = to_lunar(today)
        d = today
        while to_lunar(d)[0] not in (1, 15) or d == today:
            d += timedelta(days=1)
        nd, nm, _, nl = to_lunar(d)
        await interaction.response.send_message(
            f"📅 Hôm nay: **{describe(day, month, leap)}** năm {year} "
            f"({WEEKDAYS[today.weekday()]}, {today:%d/%m/%Y})\n"
            f"-# Tiếp theo: {describe(nd, nm, nl)} · {d:%d/%m} ({(d - today).days} ngày nữa)"
        )

    # ---- ghost-ping tracker --------------------------------------------------
    def _ghost_on(self, guild_id: int | None) -> bool:
        s = self.settings.peek(guild_id) if guild_id else None
        return bool(s and s.ghostping)

    @commands.Cog.listener()
    async def on_message(self, msg: discord.Message) -> None:
        if msg.author.bot or not self._ghost_on(msg.guild and msg.guild.id):
            return
        users = mention_targets(msg)
        if not (users or msg.role_mentions or msg.mention_everyone):
            return
        self.ghosts[msg.id] = Ghost(msg, users)
        cutoff = time.time() - GHOST_WINDOW
        while self.ghosts and (len(self.ghosts) > GHOST_CACHE_MAX or next(iter(self.ghosts.values())).at < cutoff):
            self.ghosts.popitem(last=False)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        g = self.ghosts.pop(payload.message_id, None)
        if g and payload.guild_id:
            await self._report(payload.guild_id, g, g.users, g.roles, g.everyone, "deleted the message")

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        g = self.ghosts.get(payload.message_id)
        if not g or not payload.guild_id:
            return
        msg = payload.message
        users = mention_targets(msg)
        roles = {r.id for r in msg.role_mentions}
        gone_users, gone_roles = g.users - users, g.roles - roles
        gone_everyone = g.everyone and not msg.mention_everyone
        g.users, g.roles, g.everyone = users, roles, msg.mention_everyone
        if gone_users or gone_roles or gone_everyone:
            await self._report(payload.guild_id, g, gone_users, gone_roles, gone_everyone, "edited the ping out")

    async def _report(self, guild_id: int, g: Ghost, users: set[int], roles: set[int], everyone: bool, how: str) -> None:
        s = self.settings.peek(guild_id)
        if not s or not s.ghostping or time.time() - g.at > GHOST_WINDOW:
            return
        guild = self.bot.get_guild(guild_id)
        targets = [f"<@{u}>" for u in users]
        for rid in roles:
            role = guild and guild.get_role(rid)
            targets.append(f"**@{discord.utils.escape_markdown(role.name)}**" if role else "a role")
        if everyone:
            targets.append("**@everyone**")
        if not targets:
            return
        channel = self.bot.get_channel(s.ghostping_channel_id or g.channel_id)
        if channel is None:
            return
        where = f" in <#{g.channel_id}>" if channel.id != g.channel_id else ""
        try:
            await channel.send(
                f"👻 **Ghost ping!** <@{g.author_id}> pinged {', '.join(targets)}{where} and then {how} "
                f"(<t:{int(g.at)}:R>).",
                # notify the people who were pinged, but not the pinger, roles or @everyone
                allowed_mentions=discord.AllowedMentions(users=[discord.Object(u) for u in users],
                                                         roles=False, everyone=False),
            )
        except discord.HTTPException as e:
            log.debug("ghost ping report failed in %s: %s", channel.id, e)

    @app_commands.command(name="setghostping",
                          description="Call out people who ping someone and then delete or edit the message")
    @app_commands.describe(enabled="Turn the tracker on or off",
                           channel="Post reports here (default: the channel where it happened)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def setghostping(self, interaction: discord.Interaction, enabled: bool,
                           channel: discord.TextChannel | None = None):
        s = self.settings.get(interaction.guild_id)
        s.ghostping = enabled
        if not enabled:
            self.settings.save()
            return await interaction.response.send_message("👻 Ghost-ping tracker is off.", ephemeral=True)
        if channel and not channel.permissions_for(interaction.guild.me).send_messages:
            return await interaction.response.send_message(f"❌ I can't send messages in {channel.mention}.", ephemeral=True)
        s.ghostping_channel_id = channel.id if channel else 0
        self.settings.save()
        where = channel.mention if channel else "the channel where it happens"
        await interaction.response.send_message(
            f"👻 Ghost-ping tracker is on. Pings deleted or edited out within {GHOST_WINDOW // 60} minutes "
            f"get reported in {where}.\n-# I can't see message text, only who was pinged.",
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(Community(bot))
