import os
import tempfile
import unittest
from pathlib import Path

from src.services.media_library_service import MediaLibraryService


class MediaLibraryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "media"
        self.root.mkdir()
        self.db_path = Path(self.temp_dir.name) / "data" / "library.sqlite3"
        self.library = MediaLibraryService([str(self.root)], str(self.db_path), rescan_interval=3600)

    def _write(self, relative_path, content=b"media"):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_indexes_entire_root_and_searches_without_scanning_live_filesystem(self):
        movie = self._write("Movies/Arrival (2016).mkv")
        episode = self._write("TV/Futurama/Season 03/Futurama.S03E01.The.Honking.mkv")
        self._write("Music/Daft Punk/Discovery.flac")
        self._write("Movies/notes.txt")
        hidden = self.root / ".private"
        hidden.mkdir()
        (hidden / "Secret.mkv").write_bytes(b"hidden")

        result = self.library.refresh()
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["indexed"], 3)
        matches = self.library.search("futurama")
        self.assertEqual([item["path"] for item in matches], [str(episode)])
        self.assertEqual(self.library.search("discovery")[0]["media_type"], "audio")
        self.assertEqual(self.library.search("arrival")[0]["path"], str(movie))

        # Search is catalogue-only: a later file is invisible until a refresh.
        later = self._write("Movies/Later.mkv")
        self.assertEqual(self.library.search("later"), [])
        self.library.refresh()
        self.assertEqual(self.library.search("later")[0]["path"], str(later))

    def test_find_seasons_lists_episode_counts_without_counting_alternate_versions_twice(self):
        self._write("TV/Futurama/Season 03/Futurama.S03E01.720p.mkv")
        self._write("TV/Futurama/Season 03/Futurama.S03E01.1080p.mkv")
        self._write("TV/Futurama/Season 03/Futurama.S03E02.mkv")
        self._write("TV/Futurama/Season 04/Futurama.S04E01.mkv")
        self.library.refresh()
        self.assertEqual(self.library.find_seasons("Futurama"), [
            {"season": 3, "episode_count": 2, "file_count": 3},
            {"season": 4, "episode_count": 1, "file_count": 1},
        ])

    def test_season_lookup_uses_episode_numbers_and_parent_directory_series_name(self):
        expected = [
            self._write("TV/Futurama/Season 03/S03E02 - War Is the H Word.mkv"),
            self._write("TV/Futurama/Season 03/S03E01 - The Honking.mkv"),
            self._write("TV/Futurama/Season 03/S03E10 - Where the Buggalo Roam.mkv"),
        ]
        self.library.refresh()
        results = self.library.find_season("Futurama", 3)
        self.assertEqual([Path(item["path"]).name for item in results], [
            expected[1].name, expected[0].name, expected[2].name,
        ])
        self.assertTrue(all(item["series_title"].casefold() == "futurama" for item in results))

    def test_season_directory_and_numbered_episode_filename_are_recognised(self):
        first = self._write("TV/Futurama/Season 3/01 - The Honking.mkv")
        second = self._write("TV/Futurama/Season 3/02 - War Is the H Word.mkv")
        self.library.refresh()
        results = self.library.find_season("Futurama", 3)
        self.assertEqual([item["path"] for item in results], [str(first), str(second)])
        self.assertTrue(all(item["media_type"] == "episode" for item in results))

    def test_search_matches_multiple_terms_and_is_case_insensitive(self):
        path = self._write("TV/Futurama/Futurama.S03E01.The.Honking.mkv")
        self.library.refresh()
        self.assertEqual(self.library.search("FUTURAMA honking")[0]["path"], str(path))
        self.assertEqual(self.library.search("not present"), [])

    def test_complete_scan_prunes_deleted_files_but_incomplete_scan_does_not(self):
        removed = self._write("Movies/Removed.mkv")
        kept = self._write("Movies/Kept.mkv")
        self.library.refresh()
        removed.unlink()

        self.library.roots.append(str(self.root / "temporarily-unavailable"))
        incomplete = self.library.refresh()
        self.assertFalse(incomplete["complete"])
        self.assertTrue(self.library.search("removed"))

        self.library.roots.remove(str(self.root / "temporarily-unavailable"))
        complete = self.library.refresh()
        self.assertTrue(complete["complete"])
        self.assertEqual(self.library.search("removed"), [])
        self.assertEqual(self.library.search("kept")[0]["path"], str(kept))

    def test_rejects_empty_search_and_caps_result_limit(self):
        self._write("Movies/One.mkv")
        self.library.refresh()
        self.assertEqual(self.library.search("!!!"), [])
        self.assertEqual(len(self.library.search("one", limit=10000)), 1)

    def test_index_is_persistent_across_service_instances(self):
        path = self._write("Movies/Arrival.mkv")
        self.library.refresh()
        reopened = MediaLibraryService([str(self.root)], str(self.db_path))
        self.assertEqual(reopened.search("arrival")[0]["path"], str(path))


if __name__ == "__main__":
    unittest.main()
