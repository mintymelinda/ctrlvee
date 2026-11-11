import unittest

import discord
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from src.config import Config
from src.cogs.playback import (
    PlaybackCommands,
    PlaybackControlView,
    PlaybackAdvancedView,
    PlaybackAudioTrackSelect,
    PlaybackSubtitleTrackSelect,
    PlaybackSpeedSelect,
    QueueClearConfirmView,
    QueuePlaylistSelect,
    QueueManageView,
    _decide_playback_allow,
    _member_has_allowed_role,
)


class PlaybackOwnershipTests(unittest.IsolatedAsyncioTestCase):
    def test_interaction_role_check_accepts_configured_role_name_or_id(self):
        member = SimpleNamespace(roles=[
            SimpleNamespace(id=42, name="DJ"),
            SimpleNamespace(id=43, name="Moderator"),
        ])
        self.assertTrue(_member_has_allowed_role(member, ["DJ"]))
        self.assertTrue(_member_has_allowed_role(member, [43]))
        self.assertTrue(_member_has_allowed_role(member, ["42"]))
        self.assertFalse(_member_has_allowed_role(member, ["Admin"]))

    def test_playback_panel_exposes_queue_and_shuffle_buttons(self):
        view = PlaybackControlView(Mock())
        labels = {button.label for button in view.children}
        self.assertTrue({"Previous", "Play", "Pause", "Stop", "Next", "Restart"}.issubset(labels))
        self.assertTrue({"Toggle Shuffle", "View Queue"}.issubset(labels))

    def test_playback_panel_exposes_seek_buttons(self):
        view = PlaybackControlView(Mock())
        labels = {button.label for button in view.children}
        self.assertTrue({"Rewind 10s", "Forward 10s", "Rewind 30s", "Forward 30s"}.issubset(labels))

    async def test_seek_buttons_dispatch_fixed_ten_second_offsets(self):
        cog = Mock()
        view = PlaybackControlView(cog)
        view._dispatch = AsyncMock()
        interaction = Mock()

        buttons = {button.label: button for button in view.children}
        for label in ("Rewind 10s", "Forward 10s", "Rewind 30s", "Forward 30s"):
            await buttons[label].callback(interaction)

        view._dispatch.assert_has_awaits([
            call(interaction, "rewind", 10),
            call(interaction, "forward", 10),
            call(interaction, "rewind", 30),
            call(interaction, "forward", 30),
        ])

    def test_playback_panel_exposes_add_to_queue_button(self):
        view = PlaybackControlView(Mock())
        self.assertIn("Add to Queue", {button.label for button in view.children})

    def test_playback_panel_exposes_manage_queue_button(self):
        view = PlaybackControlView(Mock())
        self.assertIn("Manage Queue", {button.label for button in view.children})

    async def test_playback_panel_reuses_video_status_embed_and_adds_queue_overview(self):
        embed = Mock()
        embed.title = "Now Playing: Example Movie"
        cog = SimpleNamespace(
            get_status_embed=AsyncMock(return_value=embed),
            vlc=SimpleNamespace(
                run_async=AsyncMock(
                    return_value=(
                        {"queued_items": {}, "shuffle_currently_on": True},
                        None,
                    )
                )
            ),
        )

        result = await PlaybackCommands._playback_panel_embed(cog)

        self.assertIs(result, embed)
        cog.get_status_embed.assert_awaited_once_with()
        cog.vlc.run_async.assert_awaited_once_with("get_queue_snapshot")
        embed.add_field.assert_any_call(name="Shuffle", value="Enabled", inline=True)
        embed.add_field.assert_any_call(
            name="Soft Queue (0)",
            value="No queued items",
            inline=False,
        )
        embed.set_footer.assert_called_once()

    def test_queue_manager_exposes_clear_queue_confirmation_action(self):
        queue_status = {"queued_items": {"track": {"queue_order": 1, "item_name": "Track"}}}
        view = QueueManageView(
            Mock(),
            queue_status,
            ET.fromstring('<playlist><leaf id="track" name="Track"/></playlist>'),
        )
        self.assertIn("Clear Queue", {child.label for child in view.children if hasattr(child, "label")})

        confirm_view = QueueClearConfirmView(Mock())
        self.assertEqual(
            {child.label for child in confirm_view.children},
            {"Confirm Clear", "Cancel"},
        )

    async def test_clear_queue_confirmation_rechecks_playback_role(self):
        view = QueueClearConfirmView(Mock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(roles=[]),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        with patch.object(Config, "ALLOWED_ROLES", ["DJ"]):
            allowed = await view.interaction_check(interaction)
        self.assertFalse(allowed)
        interaction.response.send_message.assert_awaited_once_with(
            "You don't have a role permitted to control playback.",
            ephemeral=True,
        )

    async def test_confirm_clear_queue_checks_voice_room_before_mutating(self):
        cog = Mock()
        cog._playback_guard = AsyncMock(return_value=(False, "Join the listening room"))
        cog.vlc = SimpleNamespace(run_async=AsyncMock())
        view = QueueClearConfirmView(cog)
        interaction = SimpleNamespace(
            user=Mock(),
            response=SimpleNamespace(
                send_message=AsyncMock(),
                defer=AsyncMock(),
            ),
            edit_original_response=AsyncMock(),
        )

        await view.confirm_clear(interaction)

        cog._playback_guard.assert_awaited_once_with(
            interaction.user,
            presence_only=True,
        )
        interaction.response.send_message.assert_awaited_once_with(
            "Join the listening room",
            ephemeral=True,
        )
        interaction.response.defer.assert_not_awaited()
        cog.vlc.run_async.assert_not_awaited()

    async def test_confirm_clear_queue_uses_serialized_tracking_clear(self):
        cog = Mock()
        cog._playback_guard = AsyncMock(return_value=(True, ""))
        cog.vlc = SimpleNamespace(run_async=AsyncMock())
        cog.logger = Mock()
        view = QueueClearConfirmView(cog)
        interaction = SimpleNamespace(
            user=Mock(),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        await view.confirm_clear(interaction)

        cog.vlc.run_async.assert_awaited_once_with("clear_queue_tracking")
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.edit_original_response.assert_awaited_once()
        self.assertIn("have been cleared", interaction.edit_original_response.await_args.kwargs["content"])
        self.assertIsNone(interaction.edit_original_response.await_args.kwargs["view"])

    def test_queue_manager_preserves_queue_order_and_exposes_mutations(self):
        queue_status = {
            "queued_items": {
                "third": {"queue_order": 3, "item_name": "Third"},
                "first": {"queue_order": 1, "item_name": "First"},
                "second": {"queue_order": 2, "item_name": "Second"},
            }
        }
        playlist = ET.fromstring(
            '<playlist><leaf id="first" name="First Track"/>'
            '<leaf id="second" name="Second Track"/>'
            '<leaf id="third" name="Third Track"/></playlist>'
        )

        view = QueueManageView(Mock(), queue_status, playlist)
        select = next(child for child in view.children if hasattr(child, "options"))
        labels = {child.label for child in view.children if hasattr(child, "label")}

        self.assertEqual([option.value for option in select.options], ["first", "second", "third"])
        self.assertIn("First Track", select.options[0].label)
        self.assertIn("Second Track", select.options[1].label)
        self.assertIn("Third Track", select.options[2].label)
        self.assertTrue({"Remove", "Move Earlier", "Move Later"}.issubset(labels))
        self.assertTrue(view.remove_selected_button.disabled)
        self.assertTrue(view.move_earlier_button.disabled)
        self.assertTrue(view.move_later_button.disabled)

    async def test_queue_manager_enforces_voice_room_guard_before_mutating(self):
        cog = Mock()
        cog._playback_guard = AsyncMock(return_value=(False, "Join the listening room"))
        cog.vlc = SimpleNamespace(run_async=AsyncMock())
        view = QueueManageView(
            cog,
            {"queued_items": {"track": {"queue_order": 1, "item_name": "Track"}}},
            ET.fromstring('<playlist><leaf id="track" name="Track"/></playlist>'),
        )
        view.selected_queue_item_id = "track"
        interaction = SimpleNamespace(
            user=Mock(),
            response=SimpleNamespace(
                send_message=AsyncMock(),
                defer=AsyncMock(),
            ),
            message=SimpleNamespace(edit=AsyncMock()),
        )

        await view._apply_queue_change(interaction, "remove")

        cog._playback_guard.assert_awaited_once_with(
            interaction.user,
            presence_only=True,
        )
        interaction.response.send_message.assert_awaited_once_with(
            "Join the listening room",
            ephemeral=True,
        )
        interaction.response.defer.assert_not_awaited()
        cog.vlc.run_async.assert_not_awaited()

    def test_queue_picker_preserves_playlist_positions_and_limits_options(self):
        playlist = [
            ET.Element("leaf", {"name": f"Track {position}"})
            for position in range(1, 31)
        ]
        view = PlaybackControlView(Mock())
        picker = QueuePlaylistSelect(view, playlist)

        self.assertEqual(len(picker.options), 25)
        self.assertEqual(picker.options[0].label, "Track 1")
        self.assertEqual(picker.options[0].value, "1")
        self.assertEqual(picker.options[-1].label, "Track 25")
        self.assertEqual(picker.options[-1].value, "25")

    async def test_playback_panel_embed_summarizes_soft_queue_and_shuffle(self):
        cog = PlaybackCommands.__new__(PlaybackCommands)
        status_embed = discord.Embed(title="Now Playing: Current")
        status_embed.add_field(name="State", value="Playing")
        status_embed.add_field(name="Now Playing", value="Current")
        cog.get_status_embed = AsyncMock(return_value=status_embed)
        queue = {
            "queued_items": {
                "2": {"queue_order": 1, "item_name": "Queued Track"},
            },
            "shuffle_currently_on": True,
        }
        playlist = ET.fromstring("<playlist />")
        cog.vlc = SimpleNamespace(
            run_async=AsyncMock(return_value=(queue, playlist))
        )

        embed = await PlaybackCommands._playback_panel_embed(cog)
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["State"], "Playing")
        self.assertEqual(fields["Now Playing"], "Current")
        self.assertEqual(fields["Shuffle"], "Enabled")
        self.assertEqual(fields["Soft Queue (1)"], "**#1** Queued Track")
        cog.get_status_embed.assert_awaited_once_with()
        cog.vlc.run_async.assert_awaited_once_with("get_queue_snapshot")

    def test_advanced_playback_menu_only_shows_reported_tracks(self):
        playback_view = PlaybackControlView(Mock())
        view = PlaybackAdvancedView(
            playback_view,
            [{"index": 1, "name": "English", "language": "eng"}],
            [{"index": 2, "name": "French", "language": "fra"}],
        )
        self.assertEqual(
            [type(item) for item in view.children],
            [PlaybackAudioTrackSelect, PlaybackSubtitleTrackSelect, PlaybackSpeedSelect],
        )
        audio = view.children[0]
        subtitles = view.children[1]
        self.assertEqual([(option.label, option.value) for option in audio.options], [("1. English", "1")])
        subtitle_values = {option.value for option in subtitles.options}
        self.assertEqual(subtitle_values, {"2", "off"})

    def test_advanced_menu_adds_subtitle_navigation_for_multiple_tracks(self):
        playback_view = PlaybackControlView(Mock())
        view = PlaybackAdvancedView(
            playback_view,
            [],
            [
                {"index": 1, "name": "English"},
                {"index": 2, "name": "French"},
            ],
        )
        subtitle_select = next(
            item for item in view.children
            if isinstance(item, PlaybackSubtitleTrackSelect)
        )
        self.assertEqual(
            {option.value for option in subtitle_select.options},
            {"1", "2", "__next__", "__previous__", "off"},
        )

    def test_advanced_menu_always_exposes_speed_reset_and_presets(self):
        view = PlaybackAdvancedView(PlaybackControlView(Mock()), [], [])
        self.assertEqual(len(view.children), 1)
        speed_select = view.children[0]
        self.assertIsInstance(speed_select, PlaybackSpeedSelect)
        self.assertEqual(
            [option.value for option in speed_select.options],
            ["0.5", "0.75", "1.0", "1.25", "1.5", "1.75", "2.0"],
        )
        self.assertTrue(next(option for option in speed_select.options if option.value == "1.0").default)

    async def test_advanced_selects_dispatch_existing_guarded_commands(self):
        cases = (
            (PlaybackAudioTrackSelect, [{"index": 3, "name": "English"}], "3", "audio_set", ("3",)),
            (PlaybackSubtitleTrackSelect, [{"index": 2, "name": "French"}], "2", "subtitle_set", ("2",)),
            (PlaybackSubtitleTrackSelect, [{"index": 2, "name": "French"}], "off", "subtitle_set", ("off",)),
            (PlaybackSubtitleTrackSelect, [{"index": 1}, {"index": 2}], "__next__", "subtitle_next", ()),
            (PlaybackSubtitleTrackSelect, [{"index": 1}, {"index": 2}], "__previous__", "subtitle_prev", ()),
            (PlaybackSpeedSelect, [], "1.5", "speed", ("1.5",)),
        )
        for select_type, tracks, value, command, args in cases:
            with self.subTest(command=command, value=value):
                playback_view = PlaybackControlView(Mock())
                playback_view._dispatch = AsyncMock()
                select = (
                    select_type(playback_view)
                    if select_type is PlaybackSpeedSelect
                    else select_type(playback_view, tracks)
                )
                select._values = [value]
                interaction = Mock()
                await select.callback(interaction)
                playback_view._dispatch.assert_awaited_once_with(
                    interaction,
                    command,
                    *args,
                    refresh_panel=False,
                )

    async def test_advanced_menu_rejects_users_without_control_role(self):
        playback_view = PlaybackControlView(Mock())
        view = PlaybackAdvancedView(playback_view, [], [])
        interaction = Mock()
        interaction.user = SimpleNamespace(roles=[])
        interaction.response.send_message = AsyncMock()
        self.assertFalse(await view.interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once_with(
            "You don't have a role permitted to control playback.",
            ephemeral=True,
        )

    async def test_advanced_track_commands_require_voice_room_presence(self):
        for command, args in (
            (PlaybackCommands.audio_set, ("1",)),
            (PlaybackCommands.subtitle_set, ("1",)),
            (PlaybackCommands.subtitle_next, ()),
            (PlaybackCommands.subtitle_prev, ()),
        ):
            with self.subTest(command=command.name):
                cog = PlaybackCommands.__new__(PlaybackCommands)
                cog._check_room = AsyncMock(return_value=False)
                ctx = SimpleNamespace(send=AsyncMock())
                await command.callback(cog, ctx, *args)
                cog._check_room.assert_awaited_once_with(ctx)
                ctx.send.assert_not_awaited()

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
