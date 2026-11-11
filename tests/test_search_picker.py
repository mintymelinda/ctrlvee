import unittest
from unittest.mock import AsyncMock
import xml.etree.ElementTree as ET

from src.cogs.playlist import PlaySearchView, PlaylistCommands


class SearchPickerTests(unittest.IsolatedAsyncioTestCase):
    def _results(self, count):
        return [
            (index + 1, ET.Element("leaf", {
                "id": str(index + 1),
                "name": f"Futurama S01E{index + 1:02d}.mkv",
            }))
            for index in range(count)
        ]

    def test_picker_pages_five_results_at_a_time(self):
        view = PlaySearchView(object(), 123, self._results(12))
        self.assertEqual(view.page_count, 3)
        self.assertEqual(len(view._page_results(0)), 5)
        self.assertEqual(len(view._page_results(1)), 5)
        self.assertEqual(len(view._page_results(2)), 2)

    def test_picker_caps_result_count_to_discord_safe_limit(self):
        view = PlaySearchView(object(), 123, self._results(40))
        self.assertEqual(len(view.results), 25)
        self.assertEqual(view.page_count, 5)
        self.assertEqual(view.total_matches, 40)

    def test_picker_includes_navigation_and_cancel_controls(self):
        view = PlaySearchView(object(), 123, self._results(8))
        labels = [child.label for child in view.children]
        self.assertEqual(len(labels), 8)
        self.assertIn("Previous", labels)
        self.assertIn("Next", labels)
        self.assertIn("Cancel", labels)

    async def test_search_ties_are_sorted_by_playlist_position(self):
        cog = PlaylistCommands.__new__(PlaylistCommands)
        cog._get_playlist_items = AsyncMock(return_value=[
            ET.Element("leaf", {"name": "Futurama S01E01.mkv", "id": "1"}),
            ET.Element("leaf", {"name": "Futurama S01E01.mkv", "id": "2"}),
            ET.Element("leaf", {"name": "Futurama S01E01.mkv", "id": "3"}),
        ])
        results = await cog._search_items("Futurama S01E01")
        self.assertEqual([number for number, _ in results], [1, 2, 3])


class QueuePickerSearchTests(unittest.TestCase):
    def _items(self):
        return [
            (1, {"id": "11", "name": "Futurama S01E01.mkv"}),
            (2, {"id": "12", "name": "The Simpsons S02E03.mkv"}),
            (3, {"id": "13", "name": "Futurama S01E02.mkv", "uri": "file:///media/Comedy/"}),
            (4, {"id": "14", "name": "Movie.mkv", "uri": "file:///media/Drama/Arrival.mkv"}),
        ]

    def test_search_filters_titles_without_changing_playlist_positions(self):
        from src.cogs.playback import _filter_queue_playlist_items

        results = _filter_queue_playlist_items(self._items(), "futurama")
        self.assertEqual([position for position, _ in results], [1, 3])
        self.assertEqual([item["id"] for _, item in results], ["11", "13"])

    def test_search_matches_filename_path_and_multiple_terms(self):
        from src.cogs.playback import _filter_queue_playlist_items

        results = _filter_queue_playlist_items(self._items(), "media arrival")
        self.assertEqual([position for position, _ in results], [4])

    def test_empty_search_restores_all_playlist_items(self):
        from src.cogs.playback import _filter_queue_playlist_items

        self.assertEqual(
            _filter_queue_playlist_items(self._items(), ""),
            self._items(),
        )

    def test_no_match_returns_empty_results(self):
        from src.cogs.playback import _filter_queue_playlist_items

        self.assertEqual(
            _filter_queue_playlist_items(self._items(), "nonexistent title"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
