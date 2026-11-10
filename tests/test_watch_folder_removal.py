import os
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

from src.config import path_is_under
from src.services.vlc_controller import VLCController
from src.services.watch_folder_service import WatchFolderService
from src.cogs import watch
from src.services import watch_folder_service as watch_module


class WatchFolderRemovalTests(unittest.TestCase):
    def test_path_containment_does_not_match_sibling_prefix(self):
        self.assertTrue(path_is_under("/media/tv/a.mkv", "/media/tv"))
        self.assertTrue(path_is_under("/media/tv", "/media/tv/"))
        self.assertFalse(path_is_under("/media/tv2/a.mkv", "/media/tv"))

    def test_explicit_sync_forgets_seen_and_pending_files_under_removed_folder(self):
        service = WatchFolderService(
            vlc_controller=None,
            folders=["/media/tv", "/media/movies"],
        )
        service._seen = {"/media/tv/a.mkv", "/media/movies/b.mkv"}
        service._pending = {
            "/media/tv/c.mkv": (1, 0),
            "/media/movies/d.mkv": (1, 0),
        }

        removed = service.sync_folders(["/media/movies"], forget_removed=True)

        self.assertEqual(removed, [os.path.normpath("/media/tv")])
        self.assertEqual(service.folders, [os.path.normpath("/media/movies")])
        self.assertEqual(service._seen, {"/media/movies/b.mkv"})
        self.assertEqual(list(service._pending), ["/media/movies/d.mkv"])

    def test_background_sync_does_not_forget_seen_files(self):
        service = WatchFolderService(
            vlc_controller=None,
            folders=["/media/tv", "/media/movies"],
        )
        service._seen = {"/media/tv/a.mkv"}
        service.sync_folders(["/media/movies"])
        self.assertEqual(service._seen, {"/media/tv/a.mkv"})

    def test_playlist_purge_preserves_current_item_and_sibling_paths(self):
        controller = VLCController.__new__(VLCController)
        controller.logger = __import__("logging").getLogger("test")
        controller.get_playlist = lambda: ET.fromstring(
            '<node>'
            '<leaf id="1" name="old" uri="file:///media/tv/a.mkv"/>'
            '<leaf id="2" name="current" uri="file:///media/tv/b.mkv" current="current"/>'
            '<leaf id="3" name="sibling" uri="file:///media/tv2/c.mkv"/>'
            '</node>'
        )
        deleted = []
        controller.delete_playlist_item = lambda item_id: deleted.append(item_id) or True

        result = controller.remove_playlist_items_under("/media/tv")

        self.assertEqual(deleted, ["1"])
        self.assertEqual(result["removed"], 1)
        self.assertTrue(result["skipped_current"])

    def test_scan_and_removal_cannot_apply_a_stale_folder_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            folder_a = os.path.join(root, "a")
            folder_b = os.path.join(root, "b")
            os.makedirs(folder_a)
            os.makedirs(folder_b)
            configured = [folder_a, folder_b]
            scan_started = threading.Event()
            continue_scan = threading.Event()

            def stale_snapshot():
                snapshot = list(configured)
                scan_started.set()
                self.assertTrue(continue_scan.wait(5))
                return snapshot

            service = WatchFolderService(
                vlc_controller=None,
                folders=[folder_a, folder_b],
            )
            service._first_scan_done = True

            with patch.object(watch_module, "get_watch_folders_from_env", side_effect=stale_snapshot), patch.object(
                watch, "get_watch_folders_from_env", side_effect=lambda: list(configured)
            ), patch.object(
                watch, "_write_env_watch_folders",
                side_effect=lambda new: (configured.__setitem__(slice(None), new), True)[1],
            ):
                scan = threading.Thread(target=service._scan_all, args=(False,))
                scan.start()
                self.assertTrue(scan_started.wait(5))

                removed = []
                removal = threading.Thread(
                    target=lambda: removed.append(
                        watch.remove_watch_folder(folder_a, service, None, False)
                    )
                )
                removal.start()
                continue_scan.set()
                scan.join(5)
                removal.join(5)

            self.assertFalse(scan.is_alive())
            self.assertFalse(removal.is_alive())
            self.assertEqual(service.folders, [os.path.normpath(folder_b)])
            self.assertEqual(configured, [os.path.normpath(folder_b)])
            self.assertEqual(removed[0]["folder"], os.path.normpath(folder_a))


if __name__ == "__main__":
    unittest.main()
