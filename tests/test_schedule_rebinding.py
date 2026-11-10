import unittest
import xml.etree.ElementTree as ET

from src.cogs.scheduler import _resolve_schedule_index


def leaf(name, folder="/movies"):
    return ET.Element("leaf", {
        "name": name,
        "uri": f"file://{folder}/{name.replace(' ', '%20')}",
        "id": name,
    })


class ScheduleRebindingTests(unittest.TestCase):
    def setUp(self):
        self.original = leaf("Heat (1995) 720p.mkv")
        self.other = leaf("Alien (1979).mkv")
        self.entry = {"number": 2, "title": "Heat", "uri": self.original.get("uri")}

    def test_exact_file_follows_playlist_index_shifts(self):
        self.assertEqual(_resolve_schedule_index(self.entry, [self.original, self.other]), 0)

    def test_replaced_file_rebinds_to_same_title_and_year_upgrade(self):
        upgrade = leaf("Heat.1995.2160p.UHD.BluRay.x265.mkv")
        remake = leaf("Heat (2030).mkv")
        self.assertEqual(_resolve_schedule_index(self.entry, [self.other, remake, upgrade]), 2)

    def test_missing_file_without_replacement_does_not_play_old_index(self):
        self.assertIsNone(_resolve_schedule_index(self.entry, [self.other, leaf("Heat (2030).mkv")]))

    def test_missing_on_disk_exact_item_prefers_existing_upgrade(self):
        upgrade = leaf("Heat.1995.2160p.UHD.BluRay.x265.mkv")
        gone = {self.original.get("uri")}
        self.assertEqual(
            _resolve_schedule_index(self.entry, [self.original, self.other, upgrade], lambda uri: uri not in gone),
            2,
        )

    def test_missing_exact_item_is_retained_if_no_replacement_exists(self):
        gone = {self.original.get("uri")}
        self.assertEqual(
            _resolve_schedule_index(self.entry, [self.other, self.original], lambda uri: uri not in gone),
            1,
        )

    def test_missing_upgrade_is_not_selected(self):
        upgrade = leaf("Heat.1995.2160p.UHD.BluRay.x265.mkv")
        gone = {self.original.get("uri"), upgrade.get("uri")}
        self.assertEqual(
            _resolve_schedule_index(self.entry, [self.original, upgrade], lambda uri: uri not in gone),
            0,
        )

    def test_legacy_entry_without_uri_uses_saved_number(self):
        self.assertEqual(_resolve_schedule_index({"number": 2}, [self.original, self.other]), 1)
        self.assertIsNone(_resolve_schedule_index({"number": 5}, [self.original, self.other]))


if __name__ == "__main__":
    unittest.main()
