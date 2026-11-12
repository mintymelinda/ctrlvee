import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.services.vlc_controller import VLCController


class DirectMediaQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.controller = VLCController(
            host="127.0.0.1",
            port=8080,
            password="test-password",
            queue_backup_file=str(Path(self.temp_dir.name) / "queue.json"),
        )

    def test_bulk_queue_accepts_media_not_loaded_in_vlc_playlist(self):
        items = [
            {"media_uri": "file:///nas/TV/Futurama/S03E01.mkv", "item_name": "S03E01.mkv"},
            {"media_uri": "file:///nas/TV/Futurama/S03E02.mkv", "item_name": "S03E02.mkv"},
        ]
        with patch.object(self.controller, "get_shuffle_state", return_value=False), patch.object(
            self.controller, "_save_queue_backup"
        ) as save, patch.object(self.controller, "get_playlist") as get_playlist:
            result = self.controller.queue_media_batch(items)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["total_queued"], 2)
        queued = sorted(self.controller._queued_items.values(), key=lambda entry: entry["queue_order"])
        self.assertEqual([entry["media_uri"] for entry in queued], [item["media_uri"] for item in items])
        self.assertEqual([entry["item_name"] for entry in queued], [item["item_name"] for item in items])
        self.assertTrue(all(entry.get("playlist_item_id") is None for entry in queued))
        get_playlist.assert_not_called()
        save.assert_called_once_with()

    def test_bulk_queue_validates_every_item_before_mutating(self):
        before = dict(self.controller._queued_items)
        result = self.controller.queue_media_batch([
            {"media_uri": "file:///nas/one.mkv", "item_name": "one.mkv"},
            {"media_uri": "", "item_name": "invalid.mkv"},
        ])
        self.assertFalse(result["success"])
        self.assertEqual(self.controller._queued_items, before)

    def test_repeated_media_can_be_queued_as_separate_entries(self):
        item = {"media_uri": "file:///nas/movie.mkv", "item_name": "movie.mkv"}
        with patch.object(self.controller, "get_shuffle_state", return_value=False), patch.object(
            self.controller, "_save_queue_backup"
        ):
            result = self.controller.queue_media_batch([item, item])
        self.assertTrue(result["success"], result)
        self.assertEqual(result["added"], 2)
        self.assertEqual(len(self.controller._queued_items), 2)

    def test_bulk_queue_restores_shuffle_after_the_last_added_entry(self):
        with patch.object(self.controller, "get_shuffle_state", return_value=True), patch.object(
            self.controller, "toggle_shuffle", return_value=True
        ) as toggle, patch.object(self.controller, "_save_queue_backup"):
            result = self.controller.queue_media_batch([
                {"media_uri": "file:///nas/one.mkv", "item_name": "one.mkv"},
                {"media_uri": "file:///nas/two.mkv", "item_name": "two.mkv"},
            ])
        self.assertTrue(result["success"], result)
        toggle.assert_called_once_with()
        ordered = sorted(self.controller._queued_items.items(), key=lambda pair: pair[1]["queue_order"])
        self.assertFalse(ordered[0][1]["restore_shuffle"])
        self.assertTrue(ordered[-1][1]["restore_shuffle"])
        self.assertEqual(self.controller._shuffle_restore_queue, [ordered[-1][0]])


if __name__ == "__main__":
    unittest.main()
