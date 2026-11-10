import json
import tempfile
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

    def test_queue_order_is_compacted_after_removal_and_insertion(self):
        self.queue_item("A")
        self.queue_item("B")
        self.controller._remove_from_queue("A")
        self.queue_item("C")

        orders = sorted(item["queue_order"] for item in self.controller._queued_items.values())
        self.assertEqual(orders, [1, 2])

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

if __name__ == "__main__":
    unittest.main()
