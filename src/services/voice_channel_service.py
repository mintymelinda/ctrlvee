import asyncio

import discord


class VoiceChannelService:
    """Move CtrlVee into the voice channel of a playback-command requester."""

    def __init__(self, bot, connect_timeout: float = 20.0):
        self.bot = bot
        self.connect_timeout = connect_timeout
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._target_channel_ids: dict[int, int] = {}

    def get_target_channel(self, guild):
        """Return the last command-selected channel for reconnect handling."""
        channel_id = self._target_channel_ids.get(getattr(guild, "id", None))
        if channel_id is None:
            return None
        return guild.get_channel(channel_id)

    async def join_member_channel(self, member) -> tuple[bool, str]:
        """Join the invoking member's current voice channel."""
        guild = getattr(member, "guild", None)
        voice_state = getattr(member, "voice", None)
        channel = getattr(voice_state, "channel", None)
        if guild is None or channel is None:
            return False, "Join a voice channel first, then try the playback command."

        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return False, "I can only join a server voice channel."

        return await self.join_channel(channel)

    async def join_channel(self, channel) -> tuple[bool, str]:
        """Connect or move the guild's existing voice client to channel."""
        guild = getattr(channel, "guild", None)
        if guild is None:
            return False, "That voice channel is unavailable."

        guild_id = guild.id
        lock = self._guild_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            bot_member = getattr(guild, "me", None)
            if bot_member is None:
                return False, "I could not verify my permissions in that voice channel."

            try:
                permissions = channel.permissions_for(bot_member)
            except Exception:
                return False, "I could not verify my permissions in that voice channel."

            if not getattr(permissions, "view_channel", True):
                return False, "I need permission to view that voice channel."
            if not getattr(permissions, "connect", False):
                return False, "I need the Connect permission in that voice channel."

            existing = getattr(guild, "voice_client", None)
            if existing is None:
                existing = discord.utils.get(self.bot.voice_clients, guild=guild)

            try:
                if existing is not None and existing.is_connected():
                    current_channel = getattr(existing, "channel", None)
                    if current_channel is None or current_channel.id != channel.id:
                        await existing.move_to(channel)
                else:
                    await channel.connect(
                        timeout=self.connect_timeout,
                        self_mute=True,
                        self_deaf=True,
                    )
            except discord.Forbidden:
                return False, "Discord denied my request to join that voice channel."
            except discord.ClientException as exc:
                return False, f"I could not join that voice channel: {exc}"
            except Exception:
                return False, "I could not connect to that voice channel. Please try again."

            connected = getattr(guild, "voice_client", None)
            if connected is None:
                connected = discord.utils.get(self.bot.voice_clients, guild=guild)
            if connected is None or not connected.is_connected():
                return False, "Discord did not confirm the voice connection."

            connected_channel = getattr(connected, "channel", None)
            if connected_channel is None or connected_channel.id != channel.id:
                return False, "I connected, but Discord did not place me in the requested channel."

            self._target_channel_ids[guild_id] = channel.id
            return True, "joined"
