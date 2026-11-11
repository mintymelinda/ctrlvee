import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from src.services.voice_channel_service import VoiceChannelService


class VoiceChannelServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(voice_clients=[])
        self.service = VoiceChannelService(self.bot, connect_timeout=7)
        self.guild = SimpleNamespace(id=100, me=object(), voice_client=None, get_channel=Mock())
        self.channel = SimpleNamespace(
            id=200,
            guild=self.guild,
            permissions_for=Mock(return_value=SimpleNamespace(view_channel=True, connect=True)),
            connect=AsyncMock(),
        )
        self.guild.get_channel.return_value = self.channel
        self.bot.guilds = [self.guild]

    async def test_join_channel_connects_and_records_target(self):
        voice_client = Mock()
        voice_client.is_connected.return_value = True
        voice_client.channel = self.channel

        async def connect(**kwargs):
            self.assertEqual(kwargs, {
                "timeout": 7,
                "self_mute": True,
                "self_deaf": True,
            })
            self.guild.voice_client = voice_client
            return voice_client

        self.channel.connect.side_effect = connect

        success, reason = await self.service.join_channel(self.channel)

        self.assertTrue(success)
        self.assertEqual(reason, "joined")
        self.channel.connect.assert_awaited_once()
        self.assertEqual(self.service.get_target_channel(self.guild), self.channel)
        self.assertIs(self.service.get_any_target_channel(), self.channel)

    async def test_join_channel_moves_existing_connection(self):
        old_channel = SimpleNamespace(id=199)
        voice_client = Mock()
        voice_client.is_connected.return_value = True
        voice_client.channel = old_channel
        async def move_to(channel):
            voice_client.channel = channel

        voice_client.move_to = AsyncMock(side_effect=move_to)

        self.guild.voice_client = voice_client

        success, reason = await self.service.join_channel(self.channel)

        self.assertTrue(success)
        self.assertEqual(reason, "joined")
        voice_client.move_to.assert_awaited_once_with(self.channel)
        self.channel.connect.assert_not_awaited()
        self.assertEqual(self.service.get_target_channel(self.guild), self.channel)

    async def test_join_channel_keeps_existing_connection_when_already_in_target(self):
        voice_client = Mock()
        voice_client.is_connected.return_value = True
        voice_client.channel = self.channel
        voice_client.move_to = AsyncMock()
        self.guild.voice_client = voice_client

        success, reason = await self.service.join_channel(self.channel)

        self.assertTrue(success)
        self.assertEqual(reason, "joined")
        voice_client.move_to.assert_not_awaited()
        self.channel.connect.assert_not_awaited()

    async def test_join_channel_rejects_missing_connect_permission(self):
        self.channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True,
            connect=False,
        )

        success, reason = await self.service.join_channel(self.channel)

        self.assertFalse(success)
        self.assertIn("Connect permission", reason)
        self.channel.connect.assert_not_awaited()

    async def test_join_channel_rejects_missing_view_permission(self):
        self.channel.permissions_for.return_value = SimpleNamespace(
            view_channel=False,
            connect=True,
        )

        success, reason = await self.service.join_channel(self.channel)

        self.assertFalse(success)
        self.assertIn("view", reason.lower())
        self.channel.connect.assert_not_awaited()

    async def test_join_channel_reports_discord_connection_failure(self):
        self.channel.connect.side_effect = discord.Forbidden(
            response=Mock(status=403, reason="Forbidden"),
            message="missing permission",
        )

        success, reason = await self.service.join_channel(self.channel)

        self.assertFalse(success)
        self.assertIn("denied", reason.lower())
        self.assertIsNone(self.service.get_target_channel(self.guild))

    async def test_concurrent_requests_for_same_guild_share_one_connection(self):
        voice_client = Mock()
        voice_client.is_connected.return_value = True
        voice_client.channel = self.channel

        async def connect(**kwargs):
            await asyncio.sleep(0)
            self.guild.voice_client = voice_client
            return voice_client

        self.channel.connect.side_effect = connect

        results = await asyncio.gather(
            self.service.join_channel(self.channel),
            self.service.join_channel(self.channel),
        )

        self.assertEqual(results, [(True, "joined"), (True, "joined")])
        self.channel.connect.assert_awaited_once()

    async def test_join_member_channel_rejects_user_not_in_voice(self):
        member = SimpleNamespace(guild=self.guild, voice=SimpleNamespace(channel=None))

        success, reason = await self.service.join_member_channel(member)

        self.assertFalse(success)
        self.assertIn("Join a voice channel", reason)
        self.channel.connect.assert_not_awaited()

    async def test_join_member_channel_uses_requesters_current_channel(self):
        member = SimpleNamespace(
            guild=self.guild,
            voice=SimpleNamespace(channel=self.channel),
        )
        voice_client = Mock()
        voice_client.is_connected.return_value = True
        voice_client.channel = self.channel

        async def connect(**kwargs):
            self.guild.voice_client = voice_client
            return voice_client

        self.channel.connect.side_effect = connect

        with patch(
            "src.services.voice_channel_service.discord.VoiceChannel",
            type(self.channel),
        ), patch(
            "src.services.voice_channel_service.discord.StageChannel",
            type(self.channel),
        ):
            success, reason = await self.service.join_member_channel(member)

        self.assertTrue(success)
        self.assertEqual(reason, "joined")
        self.channel.connect.assert_awaited_once()
