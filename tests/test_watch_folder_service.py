import tempfile
import unittest
from pathlib import Path

from src.services.watch_folder_service import WatchFolderService


class WatchFolderServiceTests(unittest.TestCase):
    def test_recursive_scan_finds_supported_media_and_skips_hidden_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Movies").mkdir()
            (root / "Movies" / "Example.mkv").write_bytes(b"media")
            (root / "Movies" / "notes.txt").write_text("not media", encoding="utf-8")
            (root / ".hidden").mkdir()
            (root / ".hidden" / "Secret.mp4").write_bytes(b"hidden")
            (root / "Movies" / ".ignored.mp3").write_bytes(b"hidden")

            service = WatchFolderService(vlc_controller=None, folders=[directory])
            found = {Path(path).relative_to(root).as_posix() for path in service._iter_media_files(directory)}

        self.assertEqual(found, {"Movies/Example.mkv"})


if __name__ == "__main__":
    unittest.main()
