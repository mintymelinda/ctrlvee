import unittest
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from src.config import Config
from src.cogs.playback import PlaybackCommands, _decide_playback_allow


class PlaybackOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def test_pure_guard_allows_when_no_room_is_configured(self):
        self.assertEqual(_decide_playback_allow(False, True, 1, None, False, False), (True, ""))

    def test_requester_must_be_in_configured_room(self):
        self.assertEqual(
            _decide_playback_allow(True, False, 2, None, False, False),
            (False, "not_in_room"),
        )

    def test_current_owner_cannot_be_hijacked_while_present(self):
        self.assertEqual(
            _decide_playback_allow(True, True, 2, 1, True, True),
            (False, "hijack"),
        )

    def test_same_owner_can_control_their_track(self):
        self.assertEqual(
            _decide_playback_allow(True, True, 1, 1, True, True),
            (True, ""),
        )

    def test_track_change_releases_old_owner_claim(self):
        self.assertEqual(
            _decide_playback_allow(True, True, 2, 1, False, True),
            (True, ""),
        )

    def test_owner_leaving_room_releases_old_owner_claim(self):
        self.assertEqual(
            _decide_playback_allow(True, True, 2, 1, True, False),
            (True, ""),
        )

    async def test_guard_requires_room_presence_and_resolves_owner_membership(self):
        room = SimpleNamespace(id=100, name="Listening Room")
        owner = SimpleNamespace(
            id=1,
            display_name="Owner",
            voice=SimpleNamespace(channel=room),
        )
        guild = SimpleNamespace(get_member=lambda user_id: owner if user_id == 1 else None)
        requester = SimpleNamespace(
            id=2,
            guild=guild,
            voice=SimpleNamespace(channel=room),
        )
        cog = PlaybackCommands.__new__(PlaybackCommands)
        cog._playback_owner_id = 1
        cog._playback_owner_item_id = "7"
        cog._resolve_room_channel = lambda _guild: room
        cog._current_stream_item_id = AsyncMock(return_value="7")

        with patch.object(Config, "ENABLE_VOICE_ROOM_RULES", True), patch.object(
            Config, "VOICE_JOIN_CHANNEL_ID", 100
        ):
            allowed, reason = await cog._playback_guard(requester)
        self.assertFalse(allowed)
        self.assertIn("Owner", reason)

    async def test_presence_only_still_requires_room_but_skips_hijack(self):
        room = SimpleNamespace(id=100, name="Listening Room")
        guild = SimpleNamespace(get_member=lambda _user_id: None)
        requester = SimpleNamespace(
            id=2,
            guild=guild,
            voice=SimpleNamespace(channel=room),
        )
        cog = PlaybackCommands.__new__(PlaybackCommands)
        cog._playback_owner_id = 1
        cog._playback_owner_item_id = "7"
        cog._resolve_room_channel = lambda _guild: room
        cog._current_stream_item_id = AsyncMock(return_value="7")

        with patch.object(Config, "ENABLE_VOICE_ROOM_RULES", True), patch.object(
            Config, "VOICE_JOIN_CHANNEL_ID", 100
        ):
            allowed, reason = await cog._playback_guard(requester, presence_only=True)
        self.assertTrue(allowed)
        self.assertEqual(reason, "")


    def test_clear_command_initiated_item_only_clears_matching_selection(self):
        cog = PlaybackCommands.__new__(PlaybackCommands)
        cog._command_initiated_item_id = "expected"

        cog._clear_command_initiated_item("different")
        self.assertEqual(cog._command_initiated_item_id, "expected")

        cog._clear_command_initiated_item("expected")
        self.assertIsNone(cog._command_initiated_item_id)

    async def test_play_num_fails_closed_when_vlc_state_is_unavailable(self):
        cog = PlaybackCommands.__new__(PlaybackCommands)
        cog._playback_guard = AsyncMock(return_value=(True, ""))
        cog._command_initiated_item_id = None
        cog._clear_command_initiated_item = PlaybackCommands._clear_command_initiated_item.__get__(
            cog, PlaybackCommands
        )
        cog.vlc = SimpleNamespace(
            run_async=AsyncMock(side_effect=[
                {"success": True, "item_id": "42", "item": SimpleNamespace(get=lambda _key: "Track")},
                {"success": False, "reason": "status_unavailable", "state": None},
            ])
        )
        cog._seat_playback_owner = Mock()
        cog._announce_now_playing = AsyncMock()
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=7),
            send=AsyncMock(),
        )

        with patch("src.cogs.playback.asyncio.sleep", new=AsyncMock()):
            await PlaybackCommands.play_number.callback(cog, ctx, 3)

        messages = [call.args[0] for call in ctx.send.await_args_list]
        self.assertTrue(any("could not be confirmed" in message for message in messages))
        self.assertFalse(any(message.startswith("Started playing") for message in messages))
        self.assertIsNone(cog._command_initiated_item_id)
        cog._seat_playback_owner.assert_not_called()
        cog._announce_now_playing.assert_not_awaited()

    async def test_monitor_announcement_stops_when_selection_changes_during_presence(self):
        cog = PlaybackCommands.__new__(PlaybackCommands)
        cog.vlc = SimpleNamespace(
            run_async=AsyncMock(return_value={
                "success": False,
                "reason": "selection_changed",
                "current_item_id": "2",
            })
        )
        cog._choose_metadata_source_name = Mock(return_value="Track")
        cog._set_presence = AsyncMock()
        cog._set_voice_channel_status = AsyncMock()
        cog._np_cooldown = 10
        cog._last_now_playing_key = None
        cog._last_now_playing_ts = 0.0
        cog.tmdb = SimpleNamespace()
        cog.logger = Mock()
        item = ET.Element("leaf", {"id": "1", "name": "Track"})

        with (
            patch("src.cogs.playback.MediaUtils.clean_filename_for_display", return_value="Track"),
            patch("src.cogs.playback.MediaUtils.parse_movie_filename", return_value=(None, None)),
            patch(
                "src.cogs.playback.MediaUtils.parse_tv_filename",
                return_value=(None, None, None, None),
            ),
            patch("src.cogs.playback.MediaUtils.extract_edition_tag", return_value=None),
        ):
            await cog._announce_now_playing(
                "monitor", item, 1, expected_state="playing"
            )

        cog.vlc.run_async.assert_awaited_once_with(
            "get_current_item_if_unchanged", "1"
        )
        cog._set_presence.assert_awaited_once()
        cog._set_voice_channel_status.assert_not_awaited()

    async def test_monitor_announcement_stops_when_selection_changes_during_voice_status(self):
        cog = PlaybackCommands.__new__(PlaybackCommands)
        cog.vlc = SimpleNamespace(run_async=AsyncMock())
        cog._choose_metadata_source_name = Mock(return_value="Track")
        cog._set_presence = AsyncMock()
        cog._set_voice_channel_status = AsyncMock()
        cog._monitor_snapshot_is_current = AsyncMock(side_effect=[True, False])
        cog.bot = SimpleNamespace(get_channel=Mock(), fetch_channel=AsyncMock())
        cog._np_cooldown = 10
        cog._last_now_playing_key = None
        cog._last_now_playing_ts = 0.0
        cog.tmdb = SimpleNamespace()
        cog.logger = Mock()
        item = ET.Element("leaf", {"id": "1", "name": "Track"})

        with (
            patch("src.cogs.playback.MediaUtils.clean_filename_for_display", return_value="Track"),
            patch("src.cogs.playback.MediaUtils.parse_movie_filename", return_value=(None, None)),
            patch(
                "src.cogs.playback.MediaUtils.parse_tv_filename",
                return_value=(None, None, None, None),
            ),
            patch("src.cogs.playback.MediaUtils.extract_edition_tag", return_value=None),
        ):
            await cog._announce_now_playing(
                "monitor", item, 1, expected_state="playing"
            )

        cog._set_presence.assert_awaited_once()
        cog._set_voice_channel_status.assert_awaited_once()
        cog._monitor_snapshot_is_current.assert_has_awaits(
            [call("1", "playing"), call("1", "playing")]
        )
        cog.bot.get_channel.assert_not_called()
        cog.bot.fetch_channel.assert_not_awaited()

    async def test_unresolvable_configured_room_fails_closed(self):
        guild = SimpleNamespace(get_member=lambda _user_id: None)
        requester = SimpleNamespace(id=2, guild=guild, voice=None)
        cog = PlaybackCommands.__new__(PlaybackCommands)
        cog._playback_owner_id = None
        cog._playback_owner_item_id = None
        cog._resolve_room_channel = lambda _guild: None
        with patch.object(Config, "ENABLE_VOICE_ROOM_RULES", True), patch.object(
            Config, "VOICE_JOIN_CHANNEL_ID", 100
        ):
            allowed, reason = await cog._playback_guard(requester)
        self.assertFalse(allowed)
        self.assertIn("VOICE_JOIN_CHANNEL_ID", reason)


if __name__ == "__main__":
    unittest.main()
