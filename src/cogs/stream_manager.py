import json
import logging
import os
from pathlib import Path

import discord
from discord.ext import commands, tasks

from ..config import Config

logger = logging.getLogger(__name__)
POLL_SECONDS = 5


def member_is_sharing_in_channel(member, channel) -> bool:
    voice = getattr(member, "voice", None)
    return bool(voice and voice.channel and channel and voice.channel.id == channel.id
                and getattr(voice, "self_stream", False))


def playback_is_active(status) -> bool:
    return status is not None and status.findtext("state") == "playing"


def member_has_allowed_role(member, allowed_roles) -> bool:
    for role in getattr(member, "roles", ()):
        for allowed in allowed_roles:
            if isinstance(allowed, int) and role.id == allowed:
                return True
            if isinstance(allowed, str) and allowed.strip() in (role.name, str(role.id)):
                return True
    return False


class StreamManagerCommands(commands.Cog):
    """Manage the human VLC/screen-share operator and alert them when needed."""

    def __init__(self, bot, vlc, registry_path="data/stream_managers.json"):
        self.bot = bot
        self.vlc = vlc
        self.registry_path = Path(registry_path)
        self.assignments = self._load_assignments()
        self._playing_guilds = set()
        self._alerted_guilds = set()

    def _load_assignments(self):
        try:
            with self.registry_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            return {
                str(guild_id): {"user_id": int(value["user_id"]), "host_user_id": int(value.get("host_user_id", value["user_id"])), "channel_id": int(value["channel_id"])}
                for guild_id, value in data.items()
                if isinstance(value, dict) and value.get("user_id") and value.get("channel_id")
            } if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, KeyError):
            logger.exception("Could not load stream-manager assignments")
            return {}

    def _save_assignments(self):
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.registry_path.with_suffix(self.registry_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(self.assignments, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, self.registry_path)

    def _authorized(self, member):
        return member_has_allowed_role(member, Config.ALLOWED_ROLES)

    def _assignment(self, guild):
        return self.assignments.get(str(guild.id))

    def _voice_channel(self, guild):
        voice_client = getattr(guild, "voice_client", None)
        channel = getattr(voice_client, "channel", None)
        if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return channel
        channel_id = int(getattr(Config, "VOICE_JOIN_CHANNEL_ID", 0) or 0)
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)) else None

    async def _notification_channel(self, guild, assignment):
        channel_id = assignment.get("channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None and guild.system_channel is not None:
            channel = guild.system_channel
        if channel is None and channel_id:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None
        return channel

    async def _notify(self, guild, reason):
        assignment = self._assignment(guild)
        if not assignment:
            return
        manager = guild.get_member(assignment["user_id"])
        host_id = assignment.get("host_user_id", assignment["user_id"])
        host = guild.get_member(host_id)
        if manager is None:
            try:
                manager = await guild.fetch_member(assignment["user_id"])
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning("Assigned stream manager %s is unavailable in guild %s", assignment["user_id"], guild.id)
        if host is None:
            try:
                host = await guild.fetch_member(host_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.warning("Assigned stream host %s is unavailable in guild %s", host_id, guild.id)
        recipients = [person for person in (manager, host) if person is not None]
        if not recipients:
            self._alerted_guilds.add(guild.id)
            return
        channel = await self._notification_channel(guild, assignment)
        if channel is None:
            logger.warning("No notification channel available for stream manager in guild %s", guild.id)
            return
        mentions = " ".join(dict.fromkeys(person.mention for person in recipients))
        await channel.send(
            f"{mentions} {reason}",
            allowed_mentions=discord.AllowedMentions(users=recipients, roles=False, everyone=False),
        )
        self._alerted_guilds.add(guild.id)

    async def _check_guild(self, guild):
        assignment = self._assignment(guild)
        if not assignment:
            self._alerted_guilds.discard(guild.id)
            self._playing_guilds.discard(guild.id)
            return
        try:
            status = await self.vlc.run_async("get_status")
        except Exception:
            logger.exception("Could not read VLC status for stream-manager monitoring")
            return
        if not playback_is_active(status):
            self._playing_guilds.discard(guild.id)
            self._alerted_guilds.discard(guild.id)
            return

        self._playing_guilds.add(guild.id)
        host_id = assignment.get("host_user_id", assignment["user_id"])
        host = guild.get_member(host_id)
        if host is None:
            try:
                host = await guild.fetch_member(host_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                if guild.id not in self._alerted_guilds:
                    await self._notify(guild, "VLC is playing, but the assigned stream host is unavailable. Please check the stream-host assignment.")
                return

        channel = self._voice_channel(guild)
        sharing = member_is_sharing_in_channel(host, channel)
        if not sharing and guild.id not in self._alerted_guilds:
            await self._notify(
                guild,
                "VLC playback is active and the screen share is not running in the session voice channel. Please join that channel and share your VLC screen.",
            )
        elif sharing:
            self._alerted_guilds.discard(guild.id)

    async def cog_load(self):
        self._monitor.start()

    async def cog_unload(self):
        self._monitor.cancel()

    @tasks.loop(seconds=POLL_SECONDS)
    async def _monitor(self):
        for guild in list(self.bot.guilds):
            await self._check_guild(guild)

    @_monitor.before_loop
    async def _before_monitor(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        assignment = self._assignment(member.guild)
        host_id = assignment.get("host_user_id", assignment["user_id"]) if assignment else None
        if not assignment or member.id != host_id:
            return
        if member.guild.id not in self._playing_guilds:
            return
        channel = self._voice_channel(member.guild)
        was_sharing = bool(
            before.channel and channel and before.channel.id == channel.id
            and getattr(before, "self_stream", False)
        )
        sharing = member_is_sharing_in_channel(member, channel)
        if was_sharing and not sharing and member.guild.id not in self._alerted_guilds:
            await self._notify(
                member.guild,
                "Your screen share stopped while VLC is still playing. Please restart your VLC screen share.",
            )
        elif sharing or after.channel is not None:
            # A returning manager gets a fresh check on the next monitor tick.
            self._alerted_guilds.discard(member.guild.id)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        assignment = self._assignment(member.guild)
        host_id = assignment.get("host_user_id", assignment["user_id"]) if assignment else None
        if assignment and member.id in {assignment["user_id"], host_id}:
            self._alerted_guilds.discard(member.guild.id)


    @commands.command(name="stream_host", aliases=["streamhost"])
    @commands.guild_only()
    async def stream_host(self, ctx, action: str = "status", member: discord.Member = None):
        """Assign the human who runs VLC and shares the screen."""
        if not self._authorized(ctx.author):
            await ctx.send("You do not have permission to manage the stream host assignment.")
            return
        action = action.lower()
        key = str(ctx.guild.id)
        assignment = self.assignments.get(key)
        if action in {"set", "assign"}:
            if member is None:
                await ctx.send(f"Usage: {Config.DISCORD_COMMAND_PREFIX}stream_host set @member")
                return
            if member.bot:
                await ctx.send("The stream host must be a human Discord user.")
                return
            if assignment is None:
                await ctx.send("Assign a stream manager first with !stream_manager set @member.")
                return
            assignment["host_user_id"] = member.id
            self._alerted_guilds.discard(ctx.guild.id)
            try:
                self._save_assignments()
            except OSError:
                logger.exception("Could not persist stream-host assignment")
                await ctx.send("I could not save the stream-host assignment.")
                return
            await ctx.send(
                f"{member.mention} is now the assigned stream host. I'll monitor their screen share.",
                allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
            )
            return
        if action in {"clear", "remove", "unset"}:
            if assignment is not None:
                assignment["host_user_id"] = assignment["user_id"]
            self._alerted_guilds.discard(ctx.guild.id)
            try:
                self._save_assignments()
            except OSError:
                logger.exception("Could not clear stream-host assignment")
                await ctx.send("I could not save the stream-host change.")
                return
            await ctx.send("The separate stream-host assignment has been cleared; the stream manager will be used as the host.")
            return
        if action != "status":
            await ctx.send(f"Usage: {Config.DISCORD_COMMAND_PREFIX}stream_host [set @member|clear|status]")
            return
        if assignment is None:
            await ctx.send("No stream manager is assigned.")
            return
        host_id = assignment.get("host_user_id", assignment["user_id"])
        host = ctx.guild.get_member(host_id)
        label = host.mention if host else f"User ID {host_id} (not currently in this server)"
        sharing = member_is_sharing_in_channel(host, self._voice_channel(ctx.guild)) if host else False
        await ctx.send(
            f"**Stream host:** {label}\n"
            f"**Screen share:** {'Active' if sharing else 'Not sharing in the session voice channel'}"
        )

    @commands.command(name="stream_manager", aliases=["streammanager"])
    @commands.guild_only()
    async def stream_manager(self, ctx, action: str = "status", member: discord.Member = None):
        """Assign, clear, or inspect the server's human screen-share manager."""
        if not self._authorized(ctx.author):
            await ctx.send("You do not have permission to manage the stream manager assignment.")
            return
        action = action.lower()
        key = str(ctx.guild.id)
        if action in {"set", "assign"}:
            if member is None:
                await ctx.send(f"Usage: `{Config.DISCORD_COMMAND_PREFIX}stream_manager set @member`")
                return
            if member.bot:
                await ctx.send("The stream manager must be a human Discord user.")
                return
            existing_host_id = self.assignments.get(key, {}).get("host_user_id", member.id)
            self.assignments[key] = {"user_id": member.id, "host_user_id": existing_host_id, "channel_id": ctx.channel.id}
            self._alerted_guilds.discard(ctx.guild.id)
            try:
                self._save_assignments()
            except OSError:
                logger.exception("Could not persist stream-manager assignment")
                await ctx.send("I could not save that assignment. Check the bot's persistent data directory.")
                return
            await ctx.send(
                f"{member.mention} is now the assigned stream manager. I'll ping them when a screen share is needed or stops during playback.",
                allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False),
            )
            return
        if action in {"clear", "remove", "unset"}:
            self.assignments.pop(key, None)
            self._alerted_guilds.discard(ctx.guild.id)
            self._playing_guilds.discard(ctx.guild.id)
            try:
                self._save_assignments()
            except OSError:
                logger.exception("Could not persist stream-manager removal")
                await ctx.send("I could not save the assignment change.")
                return
            await ctx.send("The stream manager assignment has been cleared.")
            return
        if action != "status":
            await ctx.send(f"Usage: `{Config.DISCORD_COMMAND_PREFIX}stream_manager [set @member|clear|status]`")
            return
        assignment = self._assignment(ctx.guild)
        if not assignment:
            await ctx.send("No stream manager is assigned. Use `!stream_manager set @member`.")
            return
        manager = ctx.guild.get_member(assignment["user_id"])
        host_id = assignment.get("host_user_id", assignment["user_id"])
        host = ctx.guild.get_member(host_id)
        manager_label = manager.mention if manager else f"User ID {assignment['user_id']} (not currently in this server)"
        host_label = host.mention if host else f"User ID {host_id} (not currently in this server)"
        sharing = member_is_sharing_in_channel(host, self._voice_channel(ctx.guild)) if host else False
        await ctx.send(
            f"**Stream manager:** {manager_label}\n"
            f"**Stream host:** {host_label}\n"
            f"**Screen share:** {'Active' if sharing else 'Not sharing in the session voice channel'}"
        )
