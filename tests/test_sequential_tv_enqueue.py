import unittest

from src.services.watch_folder_service import _episode_sort_key


class SequentialTVEnqueueTests(unittest.TestCase):
    def test_episodes_sort_by_season_then_episode(self):
        paths = [
            "/media/Futurama/Futurama.S02E03.mkv",
            "/media/Futurama/Futurama.S01E10.mkv",
            "/media/Futurama/Futurama.S01E02.mkv",
            "/media/Futurama/Futurama.S02E01.mkv",
        ]
        self.assertEqual(
            sorted(paths, key=_episode_sort_key),
            [
                "/media/Futurama/Futurama.S01E02.mkv",
                "/media/Futurama/Futurama.S01E10.mkv",
                "/media/Futurama/Futurama.S02E01.mkv",
                "/media/Futurama/Futurama.S02E03.mkv",
            ],
        )

    def test_non_episode_media_follows_episodes_in_stable_name_order(self):
        paths = [
            "/media/Movie Z.mkv",
            "/media/Show.S01E01.mkv",
            "/media/Movie A.mkv",
            "/media/Show.S01E02.mkv",
        ]
        self.assertEqual(
            sorted(paths, key=_episode_sort_key),
            [
                "/media/Show.S01E01.mkv",
                "/media/Show.S01E02.mkv",
                "/media/Movie A.mkv",
                "/media/Movie Z.mkv",
            ],
        )

    def test_episode_sorting_handles_compact_season_episode_names(self):
        paths = [
            "/media/Show/Show.2x03.mkv",
            "/media/Show/Show.1x10.mkv",
            "/media/Show/Show.1x02.mkv",
        ]
        self.assertEqual(
            [path.rsplit("/", 1)[-1] for path in sorted(paths, key=_episode_sort_key)],
            ["Show.1x02.mkv", "Show.1x10.mkv", "Show.2x03.mkv"],
        )


if __name__ == "__main__":
    unittest.main()
