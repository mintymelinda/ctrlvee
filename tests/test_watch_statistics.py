import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

from src.cogs.stats import build_stats_embed, stats_title_key
from src.services.stats_service import StatsService


class WatchStatisticsTests(unittest.TestCase):
    def test_empty_stats_embed_is_helpful(self):
        stats = StatsService(path="/missing/stats.json")
        self.assertEqual(build_stats_embed(stats).description, "No watch time recorded yet.")

    def test_watch_time_accumulates_and_replays_do_not_double_count_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            stats = StatsService(os.path.join(directory, "stats.json"))
            viewers = {1: "Ann"}
            for _ in range(20):
                stats.record(viewers, "heat|1995", 30, 600, 30)
            self.assertEqual(stats.users["1"]["seconds"], 600)
            self.assertEqual(stats.users["1"]["finished"], [])
            self.assertEqual(stats.users["1"]["progress"]["heat|1995"], [0])

    def test_progress_persists_and_finished_threshold_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "stats.json")
            stats = StatsService(path)
            stats.record({1: "Ann"}, "heat|1995", 30, 600, 30)
            for minute in range(1, 5):
                stats.record({1: "Ann"}, "heat|1995", minute * 60, 600, 30)
            stats.save()
            stats = StatsService(path)
            for minute in range(5, 8):
                stats.record({1: "Ann"}, "heat|1995", minute * 60, 600, 30)
            self.assertEqual(stats.users["1"]["finished"], [])
            stats.record({1: "Ann"}, "heat|1995", 8 * 60, 600, 30)
            self.assertEqual(stats.users["1"]["finished"], ["heat|1995"])
            self.assertNotIn("heat|1995", stats.users["1"]["progress"])

    def test_short_skips_do_not_finish_a_title(self):
        stats = StatsService(path="/missing/stats.json")
        stats.record({2: "Bob"}, "heat|1995", 590, 600, 30)
        self.assertEqual(stats.users["2"]["finished"], [])

    def test_exclusions_hide_users_without_deleting_their_history(self):
        stats = StatsService(path="/missing/stats.json", excluded=[1])
        stats.record({1: "Ann", 2: "Bob"}, None, 0, 0, 30)
        hours, _ = stats.ladders()
        self.assertEqual([name for name, _ in hours], ["Bob"])
        self.assertIn("1", stats.users)

    def test_corrupt_stats_are_preserved_aside(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "stats.json")
            with open(path, "w", encoding="utf-8") as file:
                file.write("{broken")
            stats = StatsService(path)
            self.assertEqual(stats.users, {})
            self.assertTrue(os.path.exists(path + ".corrupt"))

    def test_upgraded_movie_encodes_share_a_title_key(self):
        original = ET.Element("leaf", {
            "name": "Heat (1995) 720p.mkv",
            "uri": "file:///movies/Heat%20(1995)%20720p.mkv",
        })
        upgrade = ET.Element("leaf", {
            "name": "Heat.1995.2160p.UHD.BluRay.x265.mkv",
            "uri": "file:///movies/Heat.1995.2160p.UHD.BluRay.x265.mkv",
        })
        self.assertEqual(stats_title_key(original), stats_title_key(upgrade))

    def test_tv_episodes_have_distinct_title_keys(self):
        first = ET.Element("leaf", {"name": "Show.S01E01.mkv"})
        second = ET.Element("leaf", {"name": "Show.S01E02.mkv"})
        self.assertNotEqual(stats_title_key(first), stats_title_key(second))

    def test_ladder_embed_contains_both_rankings(self):
        stats = StatsService(path="/missing/stats.json")
        stats.record({1: "Ann"}, None, 0, 0, 3600)
        stats.record({1: "Ann"}, "heat|1995", 0, 60, 30)
        embed = build_stats_embed(stats)
        self.assertIn("Ann", embed.fields[0].value)
        self.assertIn("Ann", embed.fields[1].value)


if __name__ == "__main__":
    unittest.main()
