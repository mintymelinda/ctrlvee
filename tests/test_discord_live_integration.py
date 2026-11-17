"""Optional live Discord smoke test for a dedicated CtrlVee test server.

This test is intentionally read-only. It verifies gateway authentication,
guild visibility, and (when configured) channel permissions without sending
messages or changing server state.
"""

import asyncio
import os
import sys

import discord


async def run_live_check():
    token = os.getenv("DISCORD_TEST_BOT_TOKEN", "").strip()
    guild_id = os.getenv("DISCORD_TEST_GUILD_ID", "").strip()
    if not token or not guild_id:
        print(
            "SKIP: set DISCORD_TEST_BOT_TOKEN and DISCORD_TEST_GUILD_ID "
            "to enable the live Discord integration check."
        )
        return 0

    try:
        expected_guild_id = int(guild_id)
    except ValueError:
        print("DISCORD_TEST_GUILD_ID must be an integer.", file=sys.stderr)
        return 2

    intents = discord.Intents.none()
    intents.guilds = True
    client = discord.Client(intents=intents)
    startup = asyncio.create_task(client.start(token))

    try:
        await asyncio.wait_for(client.wait_until_ready(), timeout=45)
        if client.user is None:
            raise RuntimeError("Discord gateway connected without identifying the bot user.")

        guild = client.get_guild(expected_guild_id)
        if guild is None:
            raise RuntimeError(
                f"Bot {client.user} connected but cannot see test guild {expected_guild_id}. "
                "Check the guild ID and bot installation."
            )

        print(f"Discord gateway authenticated as {client.user} ({client.user.id}).")
        print(f"Test guild is visible: {guild.name} ({guild.id}).")

        text_channel_id = os.getenv("DISCORD_TEST_TEXT_CHANNEL_ID", "").strip()
        if text_channel_id:
            channel = await client.fetch_channel(int(text_channel_id))
            if channel.guild.id != expected_guild_id:
                raise RuntimeError("Configured test text channel belongs to a different guild.")
            permissions = channel.permissions_for(guild.me)
            required = ("view_channel", "send_messages", "embed_links")
            missing = [name for name in required if not getattr(permissions, name)]
            if missing:
                raise RuntimeError(
                    f"Bot lacks required permissions in #{channel.name}: {', '.join(missing)}"
                )
            print(f"Text-channel permissions verified: #{channel.name}.")

        voice_channel_id = os.getenv("DISCORD_TEST_VOICE_CHANNEL_ID", "").strip()
        if voice_channel_id:
            channel = await client.fetch_channel(int(voice_channel_id))
            if channel.guild.id != expected_guild_id:
                raise RuntimeError("Configured test voice channel belongs to a different guild.")
            if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                raise RuntimeError("DISCORD_TEST_VOICE_CHANNEL_ID is not a voice or stage channel.")
            permissions = channel.permissions_for(guild.me)
            required = ("view_channel", "connect", "speak")
            missing = [name for name in required if not getattr(permissions, name)]
            if missing:
                raise RuntimeError(
                    f"Bot lacks required permissions in {channel.name}: {', '.join(missing)}"
                )
            print(f"Voice-channel permissions verified: {channel.name}.")

        return 0
    finally:
        await client.close()
        if not startup.done():
            startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run_live_check()))
    except Exception as error:
        print(f"Discord integration check failed: {error}", file=sys.stderr)
        raise SystemExit(1)
