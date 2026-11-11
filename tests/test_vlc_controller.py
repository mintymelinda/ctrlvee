import asyncio
import json
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from src.services.vlc_controller import VLCController


class VLCControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.backup_path = Path(self.temp_dir.name) / "queue_backup.json"
        self.controller = VLCController(
            host="127.0.0.1",
            port=8080,
            password="test-password",
            queue_backup_file=str(self.backup_path),
        )

    def queue_item(self, item_id):
        with patch.object(self.controller, "get_shuffle_state", return_value=False), patch.object(
            self.controller, "get_current_position", return_value=0
        ), patch.object(
            self.controller, "_parse_playlist_info", return_value=({}, None)
        ):
            result = self.controller.queue_item_next(item_id)
        self.assertTrue(result["success"], result)

    def test_play_if_not_playing_checks_and_starts_under_one_operation(self):
        status = ET.fromstring("<root><state>paused</state></root>")
        with patch.object(self.controller, "get_status", return_value=status), patch.object(
            self.controller, "play", return_value=ET.fromstring("<root><ok/></root>")
        ) as play:
            result = self.controller.play_if_not_playing()
        self.assertTrue(result["available"])
        self.assertTrue(result["command_sent"])
        play.assert_called_once_with()

    def test_pause_if_playing_does_not_pause_a_stopped_player(self):
        status = ET.fromstring("<root><state>stopped</state></root>")
        with patch.object(self.controller, "get_status", return_value=status), patch.object(
            self.controller, "pause"
        ) as pause:
            result = self.controller.pause_if_playing()
        self.assertTrue(result["available"])
        self.assertFalse(result["command_sent"])
        pause.assert_not_called()

    def test_play_playlist_item_at_position_selects_and_plays_atomically(self):
        playlist = ET.fromstring('<playlist><leaf id="1" name="First"/><leaf id="2" name="Second"/></playlist>')
        with patch.object(self.controller, "get_playlist", return_value=playlist), patch.object(
            self.controller, "play_item", return_value=ET.fromstring("<root><ok/></root>")
        ) as play:
            result = self.controller.play_playlist_item_at_position(2)
        self.assertTrue(result["success"])
        self.assertEqual(result["item_id"], "2")
        self.assertEqual(result["item"].get("name"), "Second")
        play.assert_called_once_with("2")

    def test_play_item_and_capture_rejects_item_missing_from_live_playlist(self):
        playlist = ET.fromstring('<playlist><leaf id="newer" name="Newer"/></playlist>')
        with patch.object(self.controller, "get_playlist", return_value=playlist), patch.object(
            self.controller, "play_item"
        ) as play:
            result = self.controller.play_item_and_capture("stale")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "item_not_found")
        play.assert_not_called()

    def test_play_item_and_capture_validates_and_plays_item_under_one_operation(self):
        playlist = ET.fromstring(
            '<playlist><leaf id="first" name="First"/>'
            '<leaf id="selected" name="Selected"/></playlist>'
        )
        with patch.object(self.controller, "get_playlist", return_value=playlist), patch.object(
            self.controller,
            "play_item",
            return_value=ET.fromstring("<root><ok/></root>"),
        ) as play:
            result = self.controller.play_item_and_capture("selected")

        self.assertTrue(result["success"])
        self.assertEqual(result["item_id"], "selected")
        self.assertEqual(result["position"], 2)
        self.assertEqual(result["item"].get("name"), "Selected")
        play.assert_called_once_with("selected")

    def test_scheduled_playback_refuses_to_override_a_manual_selection(self):
        playlist = ET.fromstring(
            '<playlist><leaf id="scheduled"/><leaf id="manual" current="current"/></playlist>'
        )
        with patch.object(self.controller, "get_playlist", return_value=playlist), patch.object(
            self.controller, "play_item"
        ) as play:
            result = self.controller.play_scheduled_item_if_unchanged("scheduled", "previous")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "playback_changed")
        self.assertEqual(result["current_item_id"], "manual")
        play.assert_not_called()

    def test_scheduled_playback_confirms_only_a_playing_target(self):
        before = ET.fromstring(
            '<playlist><leaf id="current" current="current"/><leaf id="scheduled"/></playlist>'
        )
        after = ET.fromstring(
            '<playlist><leaf id="current"/><leaf id="scheduled" current="current"/></playlist>'
        )
        status = ET.fromstring("<root><state>playing</state></root>")
        with patch.object(self.controller, "get_playlist", side_effect=[before, after]), patch.object(
            self.controller, "get_status", return_value=status
        ), patch.object(
            self.controller, "play_item", return_value=ET.fromstring("<root><ok/></root>")
        ) as play:
            result = self.controller.play_scheduled_item_if_unchanged("scheduled", "current")

        self.assertTrue(result["success"])
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["source_item_id"], "current")
        play.assert_called_once_with("scheduled")

    def test_scheduled_playback_does_not_confirm_paused_target(self):
        before = ET.fromstring(
            '<playlist><leaf id="current" current="current"/><leaf id="scheduled"/></playlist>'
        )
        after = ET.fromstring('<playlist><leaf id="scheduled" current="current"/></playlist>')
        status = ET.fromstring("<root><state>paused</state></root>")
        with patch.object(self.controller, "get_playlist", side_effect=[before, after]), patch.object(
            self.controller, "get_status", return_value=status
        ), patch.object(
            self.controller, "play_item", return_value=ET.fromstring("<root><ok/></root>")
        ):
            result = self.controller.play_scheduled_item_if_unchanged("scheduled", "current")

        self.assertTrue(result["success"])
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["reason"], "awaiting_confirmation")

    def test_resume_selected_item_only_targets_the_still_current_item(self):
        playlist = ET.fromstring('<playlist><leaf id="selected" current="current"/></playlist>')
        status = ET.fromstring("<root><state>paused</state></root>")
        with patch.object(self.controller, "get_playlist", return_value=playlist), patch.object(
            self.controller, "get_status", return_value=status
        ), patch.object(self.controller, "play_item", return_value=ET.fromstring("<root><ok/></root>")) as play:
            result = self.controller.resume_selected_item_if_current("selected")

        self.assertTrue(result["success"])
        self.assertEqual(result["reason"], "resume_sent")
        play.assert_called_once_with("selected")

    def test_resume_selected_item_refuses_to_override_a_newer_selection(self):
        playlist = ET.fromstring('<playlist><leaf id="newer" current="current"/></playlist>')
        with patch.object(self.controller, "get_playlist", return_value=playlist), patch.object(
            self.controller, "get_status"
        ) as get_status, patch.object(self.controller, "play_item") as play:
            result = self.controller.resume_selected_item_if_current("older")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "selection_changed")
        get_status.assert_not_called()
        play.assert_not_called()

    def test_resume_selected_item_verification_does_not_issue_another_play(self):
        playlist = ET.fromstring('<playlist><leaf id="selected" current="current"/></playlist>')
        status = ET.fromstring("<root><state>paused</state></root>")
        with patch.object(self.controller, "get_playlist", return_value=playlist), patch.object(
            self.controller, "get_status", return_value=status
        ), patch.object(self.controller, "play_item") as play:
            result = self.controller.resume_selected_item_if_current("selected", resume=False)

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "not_playing")
        play.assert_not_called()

    def test_queue_playlist_item_at_position_selects_and_queues_atomically(self):
        playlist = ET.fromstring('<playlist><leaf id="1" name="First"/><leaf id="2" name="Second"/></playlist>')
        queued = {"success": True, "queue_order": 1, "total_queued": 1}
        with patch.object(self.controller, "get_playlist", return_value=playlist), patch.object(
            self.controller, "queue_item_next", return_value=queued
        ) as queue:
            result = self.controller.queue_playlist_item_at_position(2)
        self.assertTrue(result["success"])
        self.assertEqual(result["item_id"], "2")
        self.assertEqual(result["item_name"], "Second")
        self.assertEqual(result["playlist_position"], 2)
        queue.assert_called_once_with("2")

    def test_play_next_or_advance_prefers_queued_item(self):
        queued = {"success": True, "item_name": "Queued"}
        with patch.object(self.controller, "get_next_queued_item", return_value={"item_id": "1"}), patch.object(
            self.controller, "play_next_queued_item", return_value=queued
        ), patch.object(self.controller, "next") as next_track:
            result = self.controller.play_next_or_advance()
        self.assertIs(result["queued_result"], queued)
        self.assertFalse(result["next_success"])
        self.assertEqual(result["expected_item_id"], "1")
        next_track.assert_not_called()

    def test_set_shuffle_state_only_toggles_when_needed(self):
        with patch.object(self.controller, "get_shuffle_state", return_value=False), patch.object(
            self.controller, "toggle_shuffle", return_value=ET.fromstring("<root><ok/></root>")
        ) as toggle:
            result = self.controller.set_shuffle_state(True)
        self.assertTrue(result["success"])
        self.assertFalse(result["previous"])
        self.assertTrue(result["enabled"])
        toggle.assert_called_once_with()

    def test_monitor_queue_play_revalidates_state_and_current_item_before_playing(self):
        self.controller._queued_items = {
            "queued": {"item_name": "Queued", "queue_order": 1, "restore_shuffle": False}
        }
        status = ET.fromstring("<root><state>playing</state></root>")
        playlist = ET.fromstring('<playlist><leaf id="current" current="current"/></playlist>')
        with patch.object(self.controller, "get_status", return_value=status), patch.object(
            self.controller, "get_playlist", return_value=playlist
        ), patch.object(self.controller, "play_item_by_id", return_value=True) as play, patch.object(
            self.controller, "get_shuffle_state", return_value=False
        ):
            result = self.controller.play_queued_item_if_monitor_state(
                "playing", expected_current_item_id="current"
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["attempted"])
        self.assertEqual(result["reason"], "played")
        play.assert_called_once_with("queued")

    def test_monitor_queue_play_ignores_stale_state_observation(self):
        status = ET.fromstring("<root><state>paused</state></root>")
        with patch.object(self.controller, "get_status", return_value=status), patch.object(
            self.controller, "get_playlist"
        ) as get_playlist, patch.object(self.controller, "play_next_queued_item") as play:
            result = self.controller.play_queued_item_if_monitor_state("playing")

        self.assertFalse(result["attempted"])
        self.assertEqual(result["reason"], "state_changed")
        get_playlist.assert_not_called()
        play.assert_not_called()

    def test_monitor_queue_play_ignores_stale_current_item_observation(self):
        status = ET.fromstring("<root><state>playing</state></root>")
        playlist = ET.fromstring('<playlist><leaf id="new-manual-item" current="current"/></playlist>')
        with patch.object(self.controller, "get_status", return_value=status), patch.object(
            self.controller, "get_playlist", return_value=playlist
        ), patch.object(self.controller, "play_next_queued_item") as play:
            result = self.controller.play_queued_item_if_monitor_state(
                "playing", expected_current_item_id="old-item"
            )

        self.assertFalse(result["attempted"])
        self.assertEqual(result["reason"], "current_item_changed")
        play.assert_not_called()

    def test_monitor_queue_play_requires_paused_item_to_be_at_end(self):
        status = ET.fromstring(
            "<root><state>paused</state><time>40</time><length>120</length></root>"
        )
        with patch.object(self.controller, "get_status", return_value=status), patch.object(
            self.controller, "play_next_queued_item"
        ) as play:
            result = self.controller.play_queued_item_if_monitor_state(
                "paused", require_end=True
            )

        self.assertFalse(result["attempted"])
        self.assertEqual(result["reason"], "not_at_end")
        play.assert_not_called()

    def test_monitor_queue_play_starts_queued_item_when_paused_at_end(self):
        self.controller._queued_items = {
            "queued": {"item_name": "Queued", "queue_order": 1, "restore_shuffle": False}
        }
        status = ET.fromstring(
            "<root><state>paused</state><time>118</time><length>120</length></root>"
        )
        with patch.object(self.controller, "get_status", return_value=status), patch.object(
            self.controller, "play_item_by_id", return_value=True
        ) as play, patch.object(self.controller, "get_shuffle_state", return_value=False):
            result = self.controller.play_queued_item_if_monitor_state(
                "paused", require_end=True
            )

        self.assertTrue(result["success"])
        self.assertTrue(result["attempted"])
        play.assert_called_once_with("queued")

    def test_queue_backup_round_trip_preserves_both_queue_structures(self):
        self.controller._queued_items = {
            "17": {
                "item_id": "17",
                "item_name": "Episode 1",
                "queue_order": 1,
                "restore_shuffle": True,
            }
        }
        self.controller._shuffle_restore_queue = ["17"]

        self.controller._save_queue_backup()
        restored = VLCController(queue_backup_file=str(self.backup_path))

        self.assertEqual(restored._queued_items, self.controller._queued_items)
        self.assertEqual(restored._shuffle_restore_queue, ["17"])

    def test_empty_backup_loads_empty_queue_state(self):
        self.backup_path.write_text("", encoding="utf-8")

        restored = VLCController(queue_backup_file=str(self.backup_path))

        self.assertEqual(restored._queued_items, {})
        self.assertEqual(restored._shuffle_restore_queue, [])

    def test_malformed_backup_fails_closed_to_empty_queue(self):
        self.backup_path.write_text("{not valid json", encoding="utf-8")

        restored = VLCController(queue_backup_file=str(self.backup_path))

        self.assertEqual(restored._queued_items, {})
        self.assertEqual(restored._shuffle_restore_queue, [])

    def test_make_request_parses_successful_xml_response(self):
        response = Mock(status_code=200, content=b"<root><state>playing</state></root>")

        with patch("src.services.vlc_controller.requests.get", return_value=response) as get:
            result = self.controller._make_request("status.xml", {"command": "pl_pause"})

        self.assertEqual(result.findtext("state"), "playing")
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["auth"], ("", "test-password"))
        self.assertEqual(get.call_args.kwargs["timeout"], 5)

    def test_make_request_returns_none_for_authentication_failure(self):
        response = Mock(status_code=401, content=b"")
        with patch("src.services.vlc_controller.requests.get", return_value=response):
            self.assertIsNone(self.controller._make_request("status.xml"))

    def test_make_request_returns_none_when_vlc_is_unreachable(self):
        with patch(
            "src.services.vlc_controller.requests.get",
            side_effect=requests.exceptions.ConnectionError("offline"),
        ):
            self.assertIsNone(self.controller._make_request("status.xml"))

    def test_parse_playlist_info_returns_item_metadata_and_current_id(self):
        import xml.etree.ElementTree as ET

        playlist = ET.fromstring(
            '<playlist><leaf id="4" name="Pilot" /><leaf id="9" name="Second" current="current" /></playlist>'
        )

        playlist_map, current_id = self.controller._parse_playlist_info(playlist)

        self.assertEqual(playlist_map["4"]["name"], "Pilot")
        self.assertEqual(playlist_map["4"]["position"], 1)
        self.assertFalse(playlist_map["4"]["is_current"])
        self.assertEqual(current_id, "9")
        self.assertTrue(playlist_map["9"]["is_current"])

    def test_uri_to_path_decodes_local_file_uri(self):
        path = self.controller._uri_to_path("file:///media/Some%20Show/Episode%201.mkv")

        self.assertEqual(path, "/media/Some Show/Episode 1.mkv")

    def test_move_queued_item_by_id_reorders_and_persists_without_changing_shuffle_restore(self):
        self.controller._queued_items = {
            "A": {"item_name": "Alpha", "queue_order": 1, "restore_shuffle": True},
            "B": {"item_name": "Bravo", "queue_order": 2, "restore_shuffle": False},
            "C": {"item_name": "Charlie", "queue_order": 3, "restore_shuffle": True},
        }
        self.controller._shuffle_restore_queue = ["A", "C"]

        result = self.controller.move_queued_item_by_id("C", "up")

        self.assertTrue(result["success"])
        self.assertEqual(result["item_id"], "C")
        self.assertEqual(result["queue_order"], 2)
        self.assertEqual(self.controller._queued_items["B"]["queue_order"], 3)
        self.assertEqual(self.controller._shuffle_restore_queue, ["A", "C"])

        restored = VLCController(
            host="127.0.0.1",
            port=8080,
            password="test-password",
            queue_backup_file=str(self.backup_path),
        )
        self.assertEqual(restored._queued_items, self.controller._queued_items)
        self.assertEqual(restored._shuffle_restore_queue, ["A", "C"])

    def test_move_queued_item_by_id_rejects_invalid_direction_and_queue_boundaries(self):
        self.controller._queued_items = {
            "A": {"item_name": "Alpha", "queue_order": 1},
            "B": {"item_name": "Bravo", "queue_order": 2},
        }

        invalid_direction = self.controller.move_queued_item_by_id("A", "sideways")
        at_top = self.controller.move_queued_item_by_id("A", "up")
        at_bottom = self.controller.move_queued_item_by_id("B", "down")

        self.assertFalse(invalid_direction["success"])
        self.assertFalse(at_top["success"])
        self.assertFalse(at_bottom["success"])
        self.assertEqual(
            [item["queue_order"] for item in self.controller._queued_items.values()],
            [1, 2],
        )

    def test_remove_from_queue_by_id_uses_stable_identity_and_compacts_order(self):
        self.controller._queued_items = {
            "A": {"item_name": "Alpha", "queue_order": 1, "restore_shuffle": True},
            "B": {"item_name": "Bravo", "queue_order": 2, "restore_shuffle": False},
        }
        self.controller._shuffle_restore_queue = ["A"]

        result = self.controller.remove_from_queue_by_id("A")

        self.assertTrue(result["success"])
        self.assertEqual(result["removed_item_id"], "A")
        self.assertEqual(self.controller._queued_items["B"]["queue_order"], 1)
        self.assertEqual(self.controller._shuffle_restore_queue, ["A"])

    def test_queue_management_by_id_rejects_stale_playlist_identity(self):
        self.controller._queued_items = {
            "current": {"item_name": "Current", "queue_order": 1},
        }

        removed = self.controller.remove_from_queue_by_id("stale")
        moved = self.controller.move_queued_item_by_id("stale", "down")

        self.assertFalse(removed["success"])
        self.assertFalse(moved["success"])
        self.assertEqual(list(self.controller._queued_items), ["current"])

    def test_queue_order_is_compacted_after_removal_and_insertion(self):
        self.queue_item("A")
        self.queue_item("B")
        self.controller._remove_from_queue("A")
        self.queue_item("C")

        orders = sorted(item["queue_order"] for item in self.controller._queued_items.values())
        self.assertEqual(orders, [1, 2])

    def test_monitor_transition_rejects_stale_state_without_mutating_queue(self):
        self.queue_item("queued")
        status = ET.fromstring("<root><state>paused</state></root>")
        playlist = ET.fromstring('<playlist><leaf id="current" current="current"/></playlist>')

        with patch.object(self.controller, "get_status", return_value=status), patch.object(
            self.controller, "get_playlist", return_value=playlist
        ), patch.object(self.controller, "play_next_queued_item") as play:
            result = self.controller.check_and_handle_queue_transition(
                "current", expected_state="playing"
            )

        self.assertTrue(result["stale"])
        self.assertEqual(result["reason"], "state_changed")
        self.assertIn("queued", self.controller._queued_items)
        play.assert_not_called()

    def test_monitor_transition_rejects_stale_item_without_mutating_queue(self):
        self.queue_item("queued")
        status = ET.fromstring("<root><state>playing</state></root>")
        playlist = ET.fromstring('<playlist><leaf id="newer" current="current"/></playlist>')

        with patch.object(self.controller, "get_status", return_value=status), patch.object(
            self.controller, "get_playlist", return_value=playlist
        ), patch.object(self.controller, "play_next_queued_item") as play:
            result = self.controller.check_and_handle_queue_transition(
                "old", expected_state="playing"
            )

        self.assertTrue(result["stale"])
        self.assertEqual(result["reason"], "current_item_changed")
        self.assertIn("queued", self.controller._queued_items)
        play.assert_not_called()

    def test_monitor_transition_bookkeeping_never_starts_queue_playback(self):
        self.queue_item("queued")
        status = ET.fromstring("<root><state>playing</state></root>")
        playlist = ET.fromstring('<playlist><leaf id="natural" current="current"/></playlist>')

        with patch.object(self.controller, "get_status", return_value=status), patch.object(
            self.controller, "get_playlist", return_value=playlist
        ), patch.object(self.controller, "play_next_queued_item") as play:
            result = self.controller.check_and_handle_queue_transition(
                "natural", expected_state="playing"
            )

        self.assertFalse(result["stale"])
        self.assertEqual(result["transitions"], [])
        self.assertIn("queued", self.controller._queued_items)
        play.assert_not_called()

    def test_transition_does_not_advance_twice_after_queued_item_starts(self):
        self.queue_item("A")
        self.queue_item("B")

        with patch.object(self.controller, "play_item_by_id", return_value=True) as play, patch.object(
            self.controller, "get_shuffle_state", return_value=False
        ):
            self.controller.play_next_queued_item()
            self.controller.check_and_handle_queue_transition("A")

        self.assertEqual(play.call_count, 1)


    def test_pending_queue_item_is_retained_until_vlc_confirms_transition(self):
        self.queue_item("A")
        self.queue_item("B")

        with patch.object(self.controller, "play_item_by_id", return_value=True) as play, patch.object(
            self.controller, "get_shuffle_state", return_value=False
        ):
            result = self.controller.play_next_queued_item()

        self.assertTrue(result["success"])
        self.assertIn("A", self.controller._queued_items)
        self.assertEqual(self.controller.get_next_queued_item()["item_id"], "A")
        play.assert_called_once_with("A")

        stale_result = self.controller.check_and_handle_queue_transition("previous")
        self.assertEqual(stale_result["transitions"], [])
        self.assertEqual(play.call_count, 1)

        confirmed_result = self.controller.check_and_handle_queue_transition("A")
        self.assertEqual(
            [transition["action"] for transition in confirmed_result["transitions"]],
            ["queued_item_started"],
        )
        self.assertEqual(self.controller.get_next_queued_item()["item_id"], "B")

    def test_pending_queue_item_blocks_duplicate_play_commands(self):
        self.queue_item("A")

        with patch.object(self.controller, "play_item_by_id", return_value=True) as play, patch.object(
            self.controller, "get_shuffle_state", return_value=False
        ):
            first = self.controller.play_next_queued_item()
            second = self.controller.play_next_queued_item()

        self.assertTrue(first["success"])
        self.assertFalse(second["success"])
        self.assertIn("already starting", second["error"])
        play.assert_called_once_with("A")

    def test_play_next_or_advance_returns_queue_item_identity(self):
        queued = {"success": True, "item_id": "queued-7", "item_name": "Queued", "queue_order": 1}
        with patch.object(self.controller, "get_next_queued_item", return_value={"item_id": "queued-7"}), patch.object(
            self.controller, "play_next_queued_item", return_value=queued
        ), patch.object(self.controller, "next") as next_track:
            result = self.controller.play_next_or_advance()

        self.assertEqual(result["expected_item_id"], "queued-7")
        self.assertFalse(result["next_success"])
        next_track.assert_not_called()

    def test_play_next_or_advance_captures_current_item_after_fallback(self):
        playlist = ET.fromstring('<playlist><leaf id="old"/><leaf id="new" current="current"/></playlist>')
        with patch.object(self.controller, "get_next_queued_item", return_value=None), patch.object(
            self.controller, "next", return_value=True
        ), patch.object(self.controller, "get_playlist", return_value=playlist):
            result = self.controller.play_next_or_advance()

        self.assertTrue(result["next_success"])
        self.assertEqual(result["expected_item_id"], "new")
        self.assertEqual(result["position"], 2)

    def test_previous_and_capture_returns_the_selected_item_identity(self):
        playlist = ET.fromstring('<playlist><leaf id="previous" current="current"/></playlist>')
        with patch.object(self.controller, "previous", return_value=True), patch.object(
            self.controller, "get_playlist", return_value=playlist
        ):
            result = self.controller.previous_and_capture()

        self.assertTrue(result["success"])
        self.assertEqual(result["expected_item_id"], "previous")
        self.assertEqual(result["position"], 1)

    def test_current_item_confirmation_rejects_a_newer_selection(self):
        status = ET.fromstring("<root><state>playing</state></root>")
        playlist = ET.fromstring('<playlist><leaf id="newer" current="current"/></playlist>')
        with patch.object(self.controller, "get_status", return_value=status), patch.object(
            self.controller, "get_playlist", return_value=playlist
        ):
            result = self.controller.get_current_item_if_unchanged("stale")

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "selection_changed")
        self.assertEqual(result["current_item_id"], "newer")

    def test_current_item_confirmation_returns_fresh_matching_snapshot(self):
        status = ET.fromstring("<root><state>playing</state></root>")
        playlist = ET.fromstring('<playlist><leaf id="current" current="current" name="Current"/></playlist>')
        with patch.object(self.controller, "get_status", return_value=status), patch.object(
            self.controller, "get_playlist", return_value=playlist
        ):
            result = self.controller.get_current_item_if_unchanged("current")

        self.assertTrue(result["success"])
        self.assertEqual(result["item_id"], "current")
        self.assertEqual(result["item"].get("name"), "Current")
        self.assertEqual(result["state"], "playing")


if __name__ == "__main__":
    unittest.main()

class VLCControllerAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_async_serializes_complete_controller_operations(self):
        controller = VLCController(queue_backup_file="unused-async-test.json")
        state_lock = threading.Lock()
        active = 0
        max_active = 0

        def operation(value):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03)
            with state_lock:
                active -= 1
            return value

        controller.test_operation = operation
        results = await asyncio.gather(
            controller.run_async("test_operation", 1),
            controller.run_async("test_operation", 2),
        )

        self.assertEqual(sorted(results), [1, 2])
        self.assertEqual(max_active, 1)

    async def test_run_sync_shares_lock_with_async_operations(self):
        controller = VLCController(queue_backup_file="unused-sync-test.json")
        entered = threading.Event()
        release = threading.Event()
        state_lock = threading.Lock()
        active = 0
        max_active = 0

        def operation(value, block=False):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            if block:
                entered.set()
                release.wait(timeout=2)
            else:
                time.sleep(0.02)
            with state_lock:
                active -= 1
            return value

        controller.test_operation = operation
        async_operation = asyncio.create_task(
            controller.run_async("test_operation", "async", True)
        )
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        sync_operation = asyncio.create_task(
            asyncio.to_thread(controller.run_sync, "test_operation", "sync")
        )
        await asyncio.sleep(0.03)
        self.assertFalse(sync_operation.done())
        release.set()

        self.assertEqual(await async_operation, "async")
        self.assertEqual(await sync_operation, "sync")
        self.assertEqual(max_active, 1)

    async def test_run_async_does_not_block_the_event_loop(self):
        controller = VLCController(queue_backup_file="unused-async-test.json")
        entered = threading.Event()
        release = threading.Event()

        def blocked_operation():
            entered.set()
            release.wait(timeout=2)
            return "complete"

        controller.test_blocked_operation = blocked_operation
        operation = asyncio.create_task(controller.run_async("test_blocked_operation"))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        await asyncio.sleep(0)
        self.assertFalse(operation.done())
        release.set()
        self.assertEqual(await operation, "complete")
