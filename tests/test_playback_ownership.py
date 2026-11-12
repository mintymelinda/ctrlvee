import unittest
import json
import tempfile

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
    QueuePlaylistView,
    QueueManageSelect,
    QueueManageView,
    _decide_playback_allow,
    _member_has_allowed_role,
    _format_progress_bar,
)


class PlaybackOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_command_uses_requester_auto_join_guard_when_enabled(self):
        vlc = SimpleNamespace(run_async=AsyncMock())
        cog = PlaybackCommands(SimpleNamespace(), vlc, Mock(), Mock())
        cog._check_room = AsyncMock(return_value=False)
        ctx = SimpleNamespace()

        with patch.object(Config, "ENABLE_VOICE_JOIN", True), patch.object(
            Config, "VOICE_AUTO_JOIN_ON_COMMAND", True
        ):
            await cog.pause.callback(cog, ctx)

        cog._check_room.assert_awaited_once_with(ctx)
        vlc.run_async.assert_not_awaited()

    async def test_playback_guard_joins_requesters_voice_channel_when_enabled(self):
        channel = SimpleNamespace(id=321)
        join_member_voice_channel = AsyncMock(return_value=(True, "joined"))
        bot = SimpleNamespace(join_member_voice_channel=join_member_voice_channel)
        cog = PlaybackCommands(bot, Mock(), Mock(), Mock())
        member = SimpleNamespace(
            id=123,
            guild=SimpleNamespace(get_member=Mock(return_value=None)),
            voice=SimpleNamespace(channel=channel),
        )

        with patch.object(Config, "ENABLE_VOICE_JOIN", True), patch.object(
            Config, "VOICE_AUTO_JOIN_ON_COMMAND", True
        ):
            allowed, reason = await cog._playback_guard(member, presence_only=True)

        self.assertTrue(allowed)
        self.assertEqual(reason, "")
        join_member_voice_channel.assert_awaited_once_with(member)

    async def test_playback_guard_rejects_requester_outside_voice_when_auto_join_enabled(self):
        join_member_voice_channel = AsyncMock()
        cog = PlaybackCommands(
            SimpleNamespace(join_member_voice_channel=join_member_voice_channel),
            Mock(),
            Mock(),
            Mock(),
        )
        member = SimpleNamespace(
            id=123,
            guild=SimpleNamespace(get_member=Mock(return_value=None)),
            voice=SimpleNamespace(channel=None),
        )

        with patch.object(Config, "ENABLE_VOICE_JOIN", True), patch.object(
            Config, "VOICE_AUTO_JOIN_ON_COMMAND", True
        ):
            allowed, reason = await cog._playback_guard(member, presence_only=True)

        self.assertFalse(allowed)
        self.assertIn("Join a voice channel", reason)
        join_member_voice_channel.assert_not_awaited()

    async def test_playback_guard_stops_when_voice_join_fails(self):
        join_member_voice_channel = AsyncMock(return_value=(False, "Missing Connect permission"))
        cog = PlaybackCommands(
            SimpleNamespace(join_member_voice_channel=join_member_voice_channel),
            Mock(),
            Mock(),
            Mock(),
        )
        member = SimpleNamespace(
            id=123,
            guild=SimpleNamespace(get_member=Mock(return_value=None)),
            voice=SimpleNamespace(channel=SimpleNamespace(id=321)),
        )

        with patch.object(Config, "ENABLE_VOICE_JOIN", True), patch.object(
            Config, "VOICE_AUTO_JOIN_ON_COMMAND", True
        ):
            allowed, reason = await cog._playback_guard(member, presence_only=True)

        self.assertFalse(allowed)
        self.assertEqual(reason, "Missing Connect permission")
        join_member_voice_channel.assert_awaited_once_with(member)

    def test_interaction_role_check_accepts_configured_role_name_or_id(self):
        member = SimpleNamespace(roles=[
            SimpleNamespace(id=42, name="DJ"),
            SimpleNamespace(id=43, name="Moderator"),
        ])
        self.assertTrue(_member_has_allowed_role(member, ["DJ"]))
        self.assertTrue(_member_has_allowed_role(member, [43]))
        self.assertTrue(_member_has_allowed_role(member, ["42"]))
        self.assertFalse(_member_has_allowed_role(member, ["Admin"]))

    def test_playback_panel_is_persistent_across_timeouts_and_restarts(self):
        view = PlaybackControlView(Mock())
        self.assertIsNone(view.timeout)
        buttons = [child for child in view.children if isinstance(child, discord.ui.Button)]
        self.assertTrue(buttons)
        self.assertTrue(all(button.custom_id for button in buttons))
        self.assertEqual(len({button.custom_id for button in buttons}), len(buttons))

    def test_playback_panel_exposes_queue_and_shuffle_buttons(self):
        view = PlaybackControlView(Mock())
        labels = {
            button.label for button in view.children
            if isinstance(button, discord.ui.Button)
        }
        self.assertTrue({"Previous", "Play", "Pause", "Stop", "Next"}.issubset(labels))
        self.assertNotIn("Restart", labels)
        menu = next(child for child in view.children if isinstance(child, discord.ui.Select))
        self.assertTrue({"shuffle", "view_queue"}.issubset({option.value for option in menu.options}))

    def test_playback_panel_exposes_seek_buttons(self):
        view = PlaybackControlView(Mock())
        labels = {
            button.label for button in view.children
            if isinstance(button, discord.ui.Button)
        }
        self.assertTrue({"Rewind 10s", "Forward 10s", "Rewind 30s", "Forward 30s"}.issubset(labels))

    async def test_seek_buttons_dispatch_fixed_ten_second_offsets(self):
        cog = Mock()
        view = PlaybackControlView(cog)
        view._dispatch = AsyncMock()
        interaction = Mock()

        buttons = {
            button.label: button for button in view.children
            if isinstance(button, discord.ui.Button)
        }
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
        menu = next(child for child in view.children if isinstance(child, discord.ui.Select))
        self.assertIn("add_to_queue", {option.value for option in menu.options})

    def test_playback_panel_exposes_manage_queue_button(self):
        view = PlaybackControlView(Mock())
        menu = next(child for child in view.children if isinstance(child, discord.ui.Select))
        self.assertIn("manage_queue", {option.value for option in menu.options})

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
        cog.vlc = SimpleNamespace(run_async=AsyncMock(return_value={
            "success": True,
            "removed_item_ids": [],
            "preserved_item_ids": [],
            "failed_item_ids": [],
        }))
        cog._scheduled_media_protection = Mock(return_value={
            "protected_item_ids": set(),
            "protected_uris": set(),
        })
        cog.logger = Mock()
        view = QueueClearConfirmView(cog)
        interaction = SimpleNamespace(
            user=Mock(),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        await view.confirm_clear(interaction)

        cog.vlc.run_async.assert_awaited_once_with(
            "clear_queue",
            protected_item_ids=set(),
            protected_uris=set(),
        )
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.edit_original_response.assert_awaited_once()
        self.assertIn("has been cleared", interaction.edit_original_response.await_args.kwargs["content"])
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

    def test_queue_picker_preserves_stable_ids_and_playlist_positions_across_pages(self):
        playlist = [
            ET.Element("leaf", {"id": str(position * 10), "name": f"Track {position}"})
            for position in range(1, 31)
        ]
        picker_view = QueuePlaylistView(PlaybackControlView(Mock()), playlist)
        picker = next(child for child in picker_view.children if isinstance(child, QueuePlaylistSelect))

        self.assertEqual(len(picker.options), 25)
        self.assertEqual(picker.options[0].label, "Track 1")
        self.assertEqual(picker.options[0].value, "10")
        self.assertEqual(picker.options[0].description, "Playlist item #1")
        self.assertEqual(picker.options[-1].label, "Track 25")
        self.assertEqual(picker.options[-1].value, "250")

        picker_view.page = 1
        next_page = QueuePlaylistSelect(picker_view)
        self.assertEqual([option.value for option in next_page.options], ["260", "270", "280", "290", "300"])
        self.assertEqual(next_page.options[0].description, "Playlist item #26")

    def test_queue_picker_omits_playlist_entries_without_stable_ids(self):
        playlist = [
            ET.Element("leaf", {"id": "stable", "name": "Stable"}),
            ET.Element("leaf", {"name": "No ID"}),
            ET.Element("leaf", {"id": "later", "name": "Later"}),
        ]
        view = QueuePlaylistView(PlaybackControlView(Mock()), playlist)
        picker = next(child for child in view.children if isinstance(child, QueuePlaylistSelect))

        self.assertEqual([option.value for option in picker.options], ["stable", "later"])
        self.assertEqual([option.description for option in picker.options], [
            "Playlist item #1",
            "Playlist item #3",
        ])

    async def test_queue_by_stable_id_checks_voice_guard_and_uses_serialized_controller(self):
        cog = SimpleNamespace(
            _playback_guard=AsyncMock(return_value=(True, "")),
            vlc=SimpleNamespace(run_async=AsyncMock(return_value={
                "success": True, "item_name": "Selected", "queue_order": 2,
            })),
        )
        context = SimpleNamespace(author=Mock(), send=AsyncMock())

        await PlaybackCommands.queue_playlist_item_by_id(cog, context, "vlc-42")

        cog._playback_guard.assert_awaited_once_with(context.author)
        cog.vlc.run_async.assert_awaited_once_with("queue_playlist_item_by_id", "vlc-42")
        context.send.assert_awaited_once_with("Queued **Selected** as queue item **#2**.")

    async def test_queue_by_stable_id_reports_removed_playlist_item(self):
        cog = SimpleNamespace(
            _playback_guard=AsyncMock(return_value=(True, "")),
            vlc=SimpleNamespace(run_async=AsyncMock(return_value={
                "success": False, "error": "item_not_found",
            })),
        )
        context = SimpleNamespace(author=Mock(), send=AsyncMock())

        await PlaybackCommands.queue_playlist_item_by_id(cog, context, "removed")

        context.send.assert_awaited_once_with(
            "That playlist item is no longer available. Reopen Add to Queue and choose it again.",
        )

    async def test_queue_picker_dispatches_stable_item_id(self):
        view = PlaybackControlView(Mock())
        view._dispatch = AsyncMock()
        picker_view = QueuePlaylistView(view, [ET.Element("leaf", {"id": "vlc-42", "name": "Selected"})])
        picker = next(child for child in picker_view.children if isinstance(child, QueuePlaylistSelect))
        picker._values = ["vlc-42"]
        interaction = SimpleNamespace(
            user=SimpleNamespace(roles=[SimpleNamespace(id=1, name="DJ")]),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        with patch.object(Config, "ALLOWED_ROLES", ["DJ"]):
            await picker.callback(interaction)

        view._dispatch.assert_awaited_once_with(
            interaction,
            "queue_playlist_item_by_id",
            "vlc-42",
        )

    async def test_playback_panel_registry_restores_messages_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_path = f"{directory}/playback_panels.json"
            channel = SimpleNamespace(id=321)
            message = SimpleNamespace(id=654, channel=channel)
            writer = PlaybackCommands.__new__(PlaybackCommands)
            writer.logger = Mock()
            writer._playback_panel_messages = {654: message}
            with patch.object(Config, "PLAYBACK_PANEL_REGISTRY_PATH", registry_path):
                writer._save_playback_panel_registry()
                with open(registry_path, "r", encoding="utf-8") as registry_file:
                    self.assertEqual(
                        json.load(registry_file),
                        [{"channel_id": 321, "message_id": 654}],
                    )

                panel_embed = discord.Embed(title="Now Playing: Persisted")
                panel_embed.set_footer(
                    text="Video playback controls and queue overview. Playback actions are restricted to permitted roles."
                )
                restored_message = SimpleNamespace(
                    id=654,
                    channel=channel,
                    author=SimpleNamespace(id=999),
                    embeds=[panel_embed],
                    edit=AsyncMock(),
                )
                channel.fetch_message = AsyncMock(return_value=restored_message)
                bot = SimpleNamespace(
                    user=SimpleNamespace(id=999),
                    get_channel=Mock(return_value=channel),
                    fetch_channel=AsyncMock(),
                )
                reader = PlaybackCommands.__new__(PlaybackCommands)
                reader.bot = bot
                reader.logger = Mock()
                reader._playback_panel_messages = {}
                reader._playback_panel_registry_restored = False

                await reader._restore_playback_panel_registry()

                self.assertIs(reader._playback_panel_messages[654], restored_message)
                channel.fetch_message.assert_awaited_once_with(654)
                bot.fetch_channel.assert_not_awaited()

    async def test_refresh_playback_panels_updates_registered_message(self):
        cog = PlaybackCommands.__new__(PlaybackCommands)
        cog.logger = Mock()
        message = SimpleNamespace(id=123, edit=AsyncMock())
        cog._playback_panel_messages = {123: message}
        embed = discord.Embed(title="Now Playing: Updated")
        cog._playback_panel_embed = AsyncMock(return_value=embed)

        await PlaybackCommands._refresh_playback_panels(cog)

        cog._playback_panel_embed.assert_awaited_once_with()
        message.edit.assert_awaited_once()
        self.assertEqual(message.edit.await_args.kwargs["embed"], embed)
        self.assertIsInstance(
            message.edit.await_args.kwargs["view"],
            PlaybackControlView,
        )

    async def test_refresh_playback_panels_skips_status_work_without_panels(self):
        cog = PlaybackCommands.__new__(PlaybackCommands)
        cog._playback_panel_messages = {}
        cog._playback_panel_embed = AsyncMock()

        await PlaybackCommands._refresh_playback_panels(cog)

        cog._playback_panel_embed.assert_not_awaited()

    async def test_playback_panel_registers_message_for_live_refresh(self):
        cog = PlaybackCommands.__new__(PlaybackCommands)
        cog._playback_panel_messages = {}
        cog._playback_panel_embed = AsyncMock(
            return_value=discord.Embed(title="Now Playing: Current")
        )
        message = SimpleNamespace(id=456)
        context = SimpleNamespace(send=AsyncMock(return_value=message))

        await PlaybackCommands.playback_panel.callback(cog, context)

        self.assertIs(cog._playback_panel_messages[456], message)
        context.send.assert_awaited_once()

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

    def test_progress_bar_clamps_position_and_formats_fraction(self):
        self.assertEqual(_format_progress_bar(0, 100), "[░░░░░░░░░░░░░░░░]")
        self.assertEqual(_format_progress_bar(50, 100), "[████████░░░░░░░░]")
        self.assertEqual(_format_progress_bar(150, 100), "[████████████████]")
        self.assertEqual(_format_progress_bar(-10, 100), "[░░░░░░░░░░░░░░░░]")
        self.assertEqual(_format_progress_bar(10, 0), "")

    async def test_status_embed_handles_missing_state_and_adds_visual_progress(self):
        status = ET.fromstring(
            "<root><time>45</time><length>180</length></root>"
        )
        playlist = ET.fromstring(
            '<playlist><leaf id="1" name="Example.Movie.2025" uri="/media/example.mkv" current="true"/></playlist>'
        )
        cog = PlaybackCommands.__new__(PlaybackCommands)
        cog.vlc = SimpleNamespace(
            run_async=AsyncMock(side_effect=[status, playlist])
        )
        cog.tmdb = SimpleNamespace(
            get_movie_metadata=Mock(return_value=None),
            get_tv_metadata=Mock(return_value=None),
            _last_match_score=0.0,
        )
        cog.logger = Mock()

        with (
            patch("src.cogs.playback.MediaUtils.parse_movie_filename", return_value=("Example Movie", 2025)),
            patch("src.cogs.playback.MediaUtils.parse_tv_filename", return_value=(None, None, None, None)),
            patch("src.cogs.playback.MediaUtils.extract_edition_tag", return_value=None),
            patch("src.cogs.playback.MediaUtils.clean_filename_for_display", return_value="Example Movie"),
            patch("src.cogs.playback.MediaUtils.format_time", side_effect=lambda value: f"{value}s"),
        ):
            embed = await PlaybackCommands.get_status_embed(cog)

        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["State"], "⚠️ Unavailable")
        self.assertIn("[████░░░░░░░░░░░░]", fields["Progress"])
        self.assertIn("45s / 180s", fields["Progress"])

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


    def test_playback_panel_has_three_rows_with_compact_management_menu(self):
        view = PlaybackControlView(Mock())
        buttons_by_row = {
            row: [
                child for child in view.children
                if isinstance(child, discord.ui.Button) and child.row == row
            ]
            for row in range(3)
        }
        management = [
            child for child in view.children
            if isinstance(child, discord.ui.Select)
        ]

        self.assertEqual(len(buttons_by_row[0]), 5)
        self.assertEqual(
            {button.label for button in buttons_by_row[0]},
            {"Previous", "Play", "Pause", "Stop", "Next"},
        )
        self.assertEqual(len(buttons_by_row[1]), 4)
        self.assertEqual(
            {button.label for button in buttons_by_row[1]},
            {"Rewind 10s", "Forward 10s", "Rewind 30s", "Forward 30s"},
        )
        self.assertEqual(len(buttons_by_row[2]), 0)
        self.assertEqual(len(management), 1)
        self.assertEqual(
            {option.value for option in management[0].options},
            {"view_queue", "manage_queue", "add_to_queue", "shuffle", "advanced"},
        )
        self.assertEqual(management[0].custom_id, "ctrlvee:panel:management")
        self.assertTrue(view.is_persistent())

    async def test_management_menu_dispatches_selected_action(self):
        view = PlaybackControlView(Mock())
        select = next(child for child in view.children if isinstance(child, discord.ui.Select))
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock())
        )
        view._dispatch = AsyncMock()

        for action, command in (
            ("view_queue", "queue_status"),
            ("shuffle", "shuffle_toggle"),
        ):
            view._dispatch.reset_mock()
            select._values = [action]
            await select.callback(interaction)
            view._dispatch.assert_awaited_once_with(interaction, command)
    async def test_management_menu_routes_queue_tools_to_existing_handlers(self):
        view = PlaybackControlView(Mock())
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock())
        )
        view.manage_queue_button = AsyncMock()
        view.add_to_queue_button = AsyncMock()
        view.advanced_options_button = AsyncMock()

        await view._handle_management_action(interaction, "manage_queue")
        await view._handle_management_action(interaction, "add_to_queue")
        await view._handle_management_action(interaction, "advanced")

        view.manage_queue_button.assert_awaited_once_with(interaction, None)
        view.add_to_queue_button.assert_awaited_once_with(interaction, None)
        view.advanced_options_button.assert_awaited_once_with(interaction, None)

    def test_playlist_picker_pages_every_playlist_item(self):
        playlist_items = [
            {"id": str(index), "name": f"Video {index}"}
            for index in range(1, 61)
        ]
        view = QueuePlaylistView(PlaybackControlView(Mock()), playlist_items)

        select = next(child for child in view.children if isinstance(child, QueuePlaylistSelect))
        self.assertEqual(view.page_count, 3)
        self.assertEqual([option.value for option in select.options], [str(i) for i in range(1, 26)])
        self.assertTrue(view.previous_button.disabled)
        self.assertFalse(view.next_button.disabled)

        view.page = 2
        select = QueuePlaylistSelect(view)
        self.assertEqual([option.value for option in select.options], [str(i) for i in range(51, 61)])

    def test_queue_manager_pages_every_queued_item(self):
        queued_items = {
            str(index): {"queue_order": index, "item_name": f"Queued {index}"}
            for index in range(1, 61)
        }
        view = QueueManageView(
            Mock(),
            {"queued_items": queued_items},
            ET.fromstring("<playlist />"),
        )

        select = next(child for child in view.children if isinstance(child, discord.ui.Select))
        self.assertEqual(view.page_count, 3)
        self.assertEqual([option.value for option in select.options], [str(i) for i in range(1, 26)])

        view.page = 2
        select = QueueManageSelect(view)
        self.assertEqual([option.value for option in select.options], [str(i) for i in range(51, 61)])

    async def test_picker_page_navigation_keeps_navigation_controls(self):
        playlist_items = [{"id": str(index), "name": f"Video {index}"} for index in range(1, 31)]
        view = QueuePlaylistView(PlaybackControlView(Mock()), playlist_items)
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock())
        )

        await view._change_page(interaction, 1)

        self.assertEqual(view.page, 1)
        self.assertEqual(
            {child.label for child in view.children if isinstance(child, discord.ui.Button)},
            {"Previous", "Page 2/2", "Next", "Search"},
        )
        self.assertTrue(view.next_button.disabled)
        interaction.response.edit_message.assert_awaited_once()

    async def test_queue_manager_page_navigation_keeps_action_controls(self):
        queued_items = {
            str(index): {"queue_order": index, "item_name": f"Queued {index}"}
            for index in range(1, 31)
        }
        view = QueueManageView(Mock(), {"queued_items": queued_items}, ET.fromstring("<playlist />"))
        interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock())
        )

        await view._change_page(interaction, 1)

        labels = {child.label for child in view.children if isinstance(child, discord.ui.Button)}
        self.assertEqual(view.page, 1)
        self.assertTrue({"Previous", "Page 2/2", "Next", "Remove", "Move Earlier", "Move Later", "Clear Queue"}.issubset(labels))
        self.assertTrue(view.next_button.disabled)
        self.assertTrue(view.remove_selected_button.disabled)
        interaction.response.edit_message.assert_awaited_once()


    async def test_playback_panel_lifecycle_persists_restores_and_refreshes(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_path = f"{directory}/playback_panels.json"
            channel = SimpleNamespace(id=321)
            original_message = SimpleNamespace(id=654, channel=channel)
            writer = PlaybackCommands.__new__(PlaybackCommands)
            writer.logger = Mock()
            writer._playback_panel_messages = {654: original_message}

            with patch.object(Config, "PLAYBACK_PANEL_REGISTRY_PATH", registry_path):
                writer._save_playback_panel_registry()

                panel_embed = discord.Embed(title="Video Playback")
                panel_embed.set_footer(
                    text="Video playback controls and queue overview. Playback actions are restricted to permitted roles."
                )
                restored_message = SimpleNamespace(
                    id=654,
                    channel=channel,
                    author=SimpleNamespace(id=999),
                    embeds=[panel_embed],
                    edit=AsyncMock(),
                )
                channel.fetch_message = AsyncMock(return_value=restored_message)
                bot = SimpleNamespace(
                    user=SimpleNamespace(id=999),
                    get_channel=Mock(return_value=channel),
                    fetch_channel=AsyncMock(),
                )
                reader = PlaybackCommands.__new__(PlaybackCommands)
                reader.bot = bot
                reader.logger = Mock()
                reader._playback_panel_messages = {}
                reader._playback_panel_registry_restored = False
                reader._playback_panel_embed = AsyncMock(
                    return_value=discord.Embed(title="Restored panel")
                )

                await reader._restore_playback_panel_registry()
                await reader._refresh_playback_panels()

                self.assertTrue(reader._playback_panel_registry_restored)
                self.assertIs(reader._playback_panel_messages[654], restored_message)
                restored_message.edit.assert_awaited_once()
                self.assertEqual(
                    restored_message.edit.await_args.kwargs["embed"].title,
                    "Restored panel",
                )
                self.assertIsInstance(
                    restored_message.edit.await_args.kwargs["view"],
                    PlaybackControlView,
                )

    async def test_registry_retries_transient_discord_errors_on_next_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_path = f"{directory}/playback_panels.json"
            with open(registry_path, "w", encoding="utf-8") as registry_file:
                json.dump([{"channel_id": 321, "message_id": 654}], registry_file)

            channel = SimpleNamespace(id=321)
            panel_embed = discord.Embed(title="Video Playback")
            panel_embed.set_footer(
                text="Video playback controls and queue overview. Playback actions are restricted to permitted roles."
            )
            restored_message = SimpleNamespace(
                id=654,
                channel=channel,
                author=SimpleNamespace(id=999),
                embeds=[panel_embed],
                edit=AsyncMock(),
            )
            transient_error = discord.HTTPException(
                SimpleNamespace(status=503, reason="temporarily unavailable"),
                "temporary failure",
            )
            channel.fetch_message = AsyncMock(
                side_effect=[transient_error, restored_message]
            )
            bot = SimpleNamespace(
                user=SimpleNamespace(id=999),
                get_channel=Mock(return_value=channel),
                fetch_channel=AsyncMock(),
            )
            cog = PlaybackCommands.__new__(PlaybackCommands)
            cog.bot = bot
            cog.logger = Mock()
            cog._playback_panel_messages = {}
            cog._playback_panel_registry_restored = False

            with patch.object(Config, "PLAYBACK_PANEL_REGISTRY_PATH", registry_path):
                await cog._restore_playback_panel_registry()
                self.assertFalse(cog._playback_panel_registry_restored)
                self.assertNotIn(654, cog._playback_panel_messages)

                await cog._restore_playback_panel_registry()

            self.assertTrue(cog._playback_panel_registry_restored)
            self.assertIs(cog._playback_panel_messages[654], restored_message)
            self.assertEqual(channel.fetch_message.await_count, 2)

    async def test_registry_discards_deleted_or_non_panel_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_path = f"{directory}/playback_panels.json"
            with open(registry_path, "w", encoding="utf-8") as registry_file:
                json.dump(
                    [
                        {"channel_id": 321, "message_id": 654},
                        {"channel_id": 321, "message_id": 655},
                    ],
                    registry_file,
                )

            channel = SimpleNamespace(id=321)
            not_found = discord.NotFound(
                SimpleNamespace(status=404, reason="not found"),
                "deleted",
            )
            ordinary_message = SimpleNamespace(
                id=655,
                author=SimpleNamespace(id=999),
                embeds=[discord.Embed(title="Not a playback panel")],
            )
            channel.fetch_message = AsyncMock(side_effect=[not_found, ordinary_message])
            bot = SimpleNamespace(
                user=SimpleNamespace(id=999),
                get_channel=Mock(return_value=channel),
                fetch_channel=AsyncMock(),
            )
            cog = PlaybackCommands.__new__(PlaybackCommands)
            cog.bot = bot
            cog.logger = Mock()
            cog._playback_panel_messages = {}
            cog._playback_panel_registry_restored = False

            with patch.object(Config, "PLAYBACK_PANEL_REGISTRY_PATH", registry_path):
                await cog._restore_playback_panel_registry()
                with open(registry_path, "r", encoding="utf-8") as registry_file:
                    self.assertEqual(json.load(registry_file), [])

            self.assertTrue(cog._playback_panel_registry_restored)
            self.assertEqual(cog._playback_panel_messages, {})

    async def test_refresh_removes_missing_panel_without_blocking_other_panels(self):
        cog = PlaybackCommands.__new__(PlaybackCommands)
        cog.logger = Mock()
        deleted_message = SimpleNamespace(
            id=1,
            edit=AsyncMock(side_effect=discord.NotFound(
                SimpleNamespace(status=404, reason="not found"), "deleted"
            )),
        )
        healthy_message = SimpleNamespace(id=2, edit=AsyncMock())
        cog._playback_panel_messages = {1: deleted_message, 2: healthy_message}
        cog._playback_panel_embed = AsyncMock(return_value=discord.Embed(title="Current"))
        cog._save_playback_panel_registry = Mock()

        await PlaybackCommands._refresh_playback_panels(cog)

        self.assertNotIn(1, cog._playback_panel_messages)
        self.assertIn(2, cog._playback_panel_messages)
        healthy_message.edit.assert_awaited_once()
        cog._save_playback_panel_registry.assert_called_once_with()

    async def test_primary_panel_buttons_dispatch_existing_commands(self):
        view = PlaybackControlView(Mock())
        view._dispatch = AsyncMock()
        interaction = Mock()
        buttons = {
            button.label: button
            for button in view.children
            if isinstance(button, discord.ui.Button)
        }

        for label, command, args in (
            ("Previous", "previous_track", ()),
            ("Play", "play", ()),
            ("Pause", "pause", ()),
            ("Stop", "stop", ()),
            ("Next", "next_track", ()),
            ("Rewind 10s", "rewind", (10,)),
            ("Forward 10s", "forward", (10,)),
            ("Rewind 30s", "rewind", (30,)),
            ("Forward 30s", "forward", (30,)),
        ):
            view._dispatch.reset_mock()
            await buttons[label].callback(interaction)
            view._dispatch.assert_awaited_once_with(interaction, command, *args)

    async def test_unauthorized_panel_interaction_never_dispatches_playback(self):
        view = PlaybackControlView(Mock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(roles=[SimpleNamespace(id=10, name="Viewer")]),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        view._dispatch = AsyncMock()

        with patch.object(Config, "ALLOWED_ROLES", ["DJ"]):
            allowed = await view.interaction_check(interaction)

        self.assertFalse(allowed)
        view._dispatch.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once_with(
            "You don't have a role permitted to control playback.",
            ephemeral=True,
        )

    async def test_registry_ignores_malformed_entries_and_uses_channel_fetch_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            registry_path = f"{directory}/playback_panels.json"
            with open(registry_path, "w", encoding="utf-8") as registry_file:
                json.dump(
                    [
                        {"channel_id": 321},
                        {"channel_id": "not-an-id", "message_id": 700},
                        {"channel_id": 321, "message_id": 654},
                    ],
                    registry_file,
                )

            channel = SimpleNamespace(id=321)
            panel_embed = discord.Embed(title="Video Playback")
            panel_embed.set_footer(
                text="Video playback controls and queue overview. Playback actions are restricted to permitted roles."
            )
            message = SimpleNamespace(
                id=654,
                channel=channel,
                author=SimpleNamespace(id=999),
                embeds=[panel_embed],
            )
            channel.fetch_message = AsyncMock(return_value=message)
            bot = SimpleNamespace(
                user=SimpleNamespace(id=999),
                get_channel=Mock(return_value=None),
                fetch_channel=AsyncMock(return_value=channel),
            )
            cog = PlaybackCommands.__new__(PlaybackCommands)
            cog.bot = bot
            cog.logger = Mock()
            cog._playback_panel_messages = {}
            cog._playback_panel_registry_restored = False

            with patch.object(Config, "PLAYBACK_PANEL_REGISTRY_PATH", registry_path):
                await cog._restore_playback_panel_registry()
                with open(registry_path, "r", encoding="utf-8") as registry_file:
                    self.assertEqual(json.load(registry_file), [
                        {"channel_id": 321, "message_id": 654}
                    ])

            bot.fetch_channel.assert_awaited_once_with(321)
            channel.fetch_message.assert_awaited_once_with(654)
            self.assertIs(cog._playback_panel_messages[654], message)
            self.assertTrue(cog._playback_panel_registry_restored)


if __name__ == "__main__":
    unittest.main()


class PlaybackPanelDispatchTests(unittest.IsolatedAsyncioTestCase):
    def make_interaction(self, message=None):
        return SimpleNamespace(
            user=SimpleNamespace(id=7),
            guild=SimpleNamespace(id=8),
            channel=SimpleNamespace(id=9),
            message=message,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_dispatch_invokes_command_callback_with_interaction_context(self):
        observed = {}
        cog = SimpleNamespace(logger=Mock(), _playback_panel_embed=AsyncMock())
        async def callback(received_cog, ctx, *args):
            observed["cog"] = received_cog
            observed["ctx"] = ctx
            observed["args"] = args
            await ctx.send("private result", ephemeral=False)

        cog.seek = SimpleNamespace(callback=AsyncMock(side_effect=callback))
        view = PlaybackControlView(cog)
        interaction = self.make_interaction()

        await view._dispatch(interaction, "seek", 30, "forward", refresh_panel=False)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertIs(observed["cog"], cog)
        self.assertEqual(observed["args"], (30, "forward"))
        ctx = observed["ctx"]
        self.assertIs(ctx.author, interaction.user)
        self.assertIs(ctx.guild, interaction.guild)
        self.assertIs(ctx.channel, interaction.channel)
        self.assertIs(ctx.message, interaction.message)
        interaction.followup.send.assert_awaited_once_with(
            content="private result",
            embed=None,
            ephemeral=True,
        )
        cog.seek.callback.assert_awaited_once()

    async def test_dispatch_refreshes_panel_after_successful_command(self):
        message = SimpleNamespace(edit=AsyncMock())
        cog = SimpleNamespace(
            logger=Mock(),
            play=SimpleNamespace(callback=AsyncMock()),
            _playback_panel_embed=AsyncMock(return_value=discord.Embed(title="Updated")),
        )
        view = PlaybackControlView(cog)
        interaction = self.make_interaction(message)

        await view._dispatch(interaction, "play")

        cog.play.callback.assert_awaited_once()
        cog._playback_panel_embed.assert_awaited_once_with()
        message.edit.assert_awaited_once()
        self.assertEqual(message.edit.await_args.kwargs["embed"].title, "Updated")
        self.assertIs(message.edit.await_args.kwargs["view"], view)

    async def test_dispatch_reports_command_failure_without_refreshing_panel(self):
        message = SimpleNamespace(edit=AsyncMock())
        cog = SimpleNamespace(
            logger=Mock(),
            stop=SimpleNamespace(callback=AsyncMock(side_effect=RuntimeError("VLC failed"))),
            _playback_panel_embed=AsyncMock(),
        )
        view = PlaybackControlView(cog)
        interaction = self.make_interaction(message)

        await view._dispatch(interaction, "stop")

        interaction.followup.send.assert_awaited_once_with(
            "That playback action failed. Check the bot logs for details.",
            ephemeral=True,
        )
        cog.logger.exception.assert_called_once()
        cog._playback_panel_embed.assert_not_awaited()
        message.edit.assert_not_awaited()

    async def test_dispatch_keeps_command_result_when_panel_refresh_fails(self):
        message = SimpleNamespace(edit=AsyncMock(side_effect=discord.HTTPException(
            SimpleNamespace(status=503, reason="unavailable"),
            "temporary failure",
        )))
        cog = SimpleNamespace(
            logger=Mock(),
            pause=SimpleNamespace(callback=AsyncMock()),
            _playback_panel_embed=AsyncMock(return_value=discord.Embed(title="Updated")),
        )
        view = PlaybackControlView(cog)
        interaction = self.make_interaction(message)

        await view._dispatch(interaction, "pause")

        cog.pause.callback.assert_awaited_once()
        message.edit.assert_awaited_once()
        cog.logger.debug.assert_called_once()
        interaction.followup.send.assert_not_awaited()

    async def test_advanced_options_loads_only_vlc_reported_tracks(self):
        audio = [{"index": 1, "name": "Stereo"}]
        subtitles = [{"index": 3, "name": "English"}]
        cog = SimpleNamespace(
            vlc=SimpleNamespace(run_async=AsyncMock(side_effect=[audio, subtitles])),
            logger=Mock(),
        )
        view = PlaybackControlView(cog)
        interaction = self.make_interaction()

        await view.advanced_options_button(interaction, None)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        cog.vlc.run_async.assert_has_awaits([
            call("get_audio_tracks"),
            call("get_subtitle_tracks"),
        ])
        message = interaction.followup.send.await_args
        advanced_view = message.kwargs["view"]
        self.assertIsInstance(advanced_view, PlaybackAdvancedView)
        self.assertTrue(any(isinstance(item, PlaybackAudioTrackSelect) for item in advanced_view.children))
        self.assertTrue(any(isinstance(item, PlaybackSubtitleTrackSelect) for item in advanced_view.children))
        self.assertIn("Audio tracks are available", message.args[0])
        self.assertIn("Subtitle tracks and navigation are available", message.args[0])
        self.assertTrue(message.kwargs["ephemeral"])

    async def test_advanced_options_reports_empty_tracks_but_keeps_speed_control(self):
        cog = SimpleNamespace(
            vlc=SimpleNamespace(run_async=AsyncMock(return_value=[])),
            logger=Mock(),
        )
        view = PlaybackControlView(cog)
        interaction = self.make_interaction()

        await view.advanced_options_button(interaction, None)

        message = interaction.followup.send.await_args
        self.assertIn("did not report any audio tracks", message.args[0])
        self.assertIn("did not report any subtitle tracks", message.args[0])
        advanced_view = message.kwargs["view"]
        self.assertEqual([type(item) for item in advanced_view.children], [PlaybackSpeedSelect])

    async def test_advanced_options_reports_vlc_errors_ephemerally(self):
        cog = SimpleNamespace(
            vlc=SimpleNamespace(run_async=AsyncMock(side_effect=RuntimeError("VLC unavailable"))),
            logger=Mock(),
        )
        view = PlaybackControlView(cog)
        interaction = self.make_interaction()

        await view.advanced_options_button(interaction, None)

        interaction.followup.send.assert_awaited_once_with(
            "Advanced playback options are currently unavailable.",
            ephemeral=True,
        )
        cog.logger.exception.assert_called_once()

    async def test_queue_manager_reports_empty_queue_without_opening_controls(self):
        playlist = ET.fromstring("<playlist />")
        cog = SimpleNamespace(
            vlc=SimpleNamespace(run_async=AsyncMock(return_value=({"queued_items": {}}, playlist))),
            logger=Mock(),
        )
        view = PlaybackControlView(cog)
        interaction = self.make_interaction()

        await view.manage_queue_button(interaction, None)

        cog.vlc.run_async.assert_awaited_once_with("get_queue_snapshot")
        interaction.followup.send.assert_awaited_once_with(
            "There are no queued tracks to manage.",
            ephemeral=True,
        )

    async def test_queue_manager_reports_unavailable_snapshot(self):
        cog = SimpleNamespace(
            vlc=SimpleNamespace(run_async=AsyncMock(side_effect=RuntimeError("VLC unavailable"))),
            logger=Mock(),
        )
        view = PlaybackControlView(cog)
        interaction = self.make_interaction()

        await view.manage_queue_button(interaction, None)

        interaction.followup.send.assert_awaited_once_with(
            "The soft queue is currently unavailable.",
            ephemeral=True,
        )
        cog.logger.exception.assert_called_once()
