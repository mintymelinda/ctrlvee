import unittest
import xml.etree.ElementTree as ET

from src.cogs.playlist import PlaySearchView, PlaylistCommands


class SearchPickerTests(unittest.TestCase):
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

    def test_search_ties_are_sorted_by_playlist_position(self):
        cog = PlaylistCommands.__new__(PlaylistCommands)
        cog._get_playlist_items = lambda: [
            ET.Element("leaf", {"name": "Futurama S01E01.mkv", "id": "1"}),
            ET.Element("leaf", {"name": "Futurama S01E01.mkv", "id": "2"}),
            ET.Element("leaf", {"name": "Futurama S01E01.mkv", "id": "3"}),
        ]
        results = cog._search_items("Futurama S01E01")
        self.assertEqual([number for number, _ in results], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
