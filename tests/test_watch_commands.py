import unittest
from unittest.mock import Mock, patch

from src.cogs.watch import remove_watch_folder


class WatchFolderRemovalTests(unittest.TestCase):
    @patch("src.cogs.watch._write_env_watch_folders", return_value=True)
    @patch("src.cogs.watch.get_watch_folders_from_env", return_value=["/media/Movies"])
    def test_playlist_purge_uses_shared_vlc_controller_lock(self, _folders, _write):
        vlc = Mock()

        result = remove_watch_folder("/media/Movies", None, vlc, purge_playlist=True)

        self.assertEqual(result["folder"], "/media/Movies")
        self.assertEqual(result["purge"], vlc.run_sync.return_value)
        vlc.run_sync.assert_called_once_with("remove_playlist_items_under", "/media/Movies")
        vlc.remove_playlist_items_under.assert_not_called()

    @patch("src.cogs.watch._write_env_watch_folders", return_value=True)
    @patch("src.cogs.watch.get_watch_folders_from_env", return_value=["/media/Movies"])
    def test_keep_playlist_option_does_not_call_vlc(self, _folders, _write):
        vlc = Mock()

        result = remove_watch_folder("/media/Movies", None, vlc, purge_playlist=False)

        self.assertIsNone(result["purge"])
        vlc.run_sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
