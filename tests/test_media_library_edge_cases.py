import tempfile
import unittest
from pathlib import Path

from src.services.media_library_service import MediaLibraryService


class MediaLibraryEdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "library"
        self.root.mkdir()
        self.db_path = Path(self.temp_dir.name) / "data" / "library.sqlite3"
        self.library = MediaLibraryService([str(self.root)], str(self.db_path))

    def write(self, relative, payload=b"media"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def test_search_fallback_without_fts5_matches_service_contract(self):
        movie = self.write("Movies/Arrival (2016).mkv")
        self.write("TV/Futurama/Season 3/Futurama.S03E01.mkv")
        self.library.refresh()
        self.library._fts_enabled = False
        self.assertEqual(self.library.search("ARRIVAL 2016")[0]["path"], str(movie))
        self.assertEqual(len(self.library.find_season("Futurama", 3)), 1)
        self.assertEqual(self.library.find_seasons("Futurama")[0]["episode_count"], 1)

    def test_reconciliation_updates_moved_files_and_fts_entries(self):
        original = self.write("Movies/Old Title.mkv")
        self.library.refresh()
        renamed = self.root / "Movies" / "New Title.mkv"
        original.rename(renamed)
        result = self.library.refresh()
        self.assertTrue(result["complete"], result)
        self.assertEqual(self.library.search("old title"), [])
        self.assertEqual(self.library.search("new title")[0]["path"], str(renamed))

    def test_symlinked_files_cannot_escape_a_configured_root(self):
        outside = Path(self.temp_dir.name) / "outside.mkv"
        outside.write_bytes(b"outside")
        link = self.root / "Movies" / "Outside.mkv"
        link.parent.mkdir()
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("This platform does not permit creating symlinks")
        result = self.library.refresh()
        self.assertTrue(result["complete"], result)
        self.assertEqual(self.library.search("outside"), [])
        self.assertFalse(self.library.is_available(str(link)))

    def test_is_available_rejects_missing_files_and_paths_outside_all_roots(self):
        inside = self.write("Movies/Inside.mkv")
        outside = Path(self.temp_dir.name) / "outside.mkv"
        outside.write_bytes(b"outside")
        self.assertTrue(self.library.is_available(str(inside)))
        self.assertFalse(self.library.is_available(str(outside)))
        self.assertFalse(self.library.is_available(str(self.root / "missing.mkv")))

    def test_multiple_roots_are_indexed_and_reported_independently(self):
        second = Path(self.temp_dir.name) / "second"
        second.mkdir()
        first_file = self.write("Movies/First.mkv")
        second_file = second / "Second.flac"
        second_file.write_bytes(b"audio")
        library = MediaLibraryService([str(self.root), str(second), str(self.root)], str(self.db_path))
        result = library.refresh()
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["indexed"], 2)
        self.assertEqual(library.search("first")[0]["path"], str(first_file))
        self.assertEqual(library.search("second")[0]["path"], str(second_file))
        self.assertEqual(library.status()["indexed_roots"], 2)

    def test_search_limit_is_bounded_even_for_large_catalogues(self):
        for index in range(105):
            self.write(f"Movies/Shared Title {index:03d}.mkv")
        self.library.refresh()
        self.assertEqual(len(self.library.search("shared title", limit=10000)), 100)


if __name__ == "__main__":
    unittest.main()
