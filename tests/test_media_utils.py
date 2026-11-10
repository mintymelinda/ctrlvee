import unittest
import xml.etree.ElementTree as ET

from src.utils.media_utils import MediaUtils


class MediaUtilsTests(unittest.TestCase):
    def test_movie_filename_extracts_title_and_year(self):
        title, year = MediaUtils.parse_movie_filename(
            "The.Matrix.1999.1080p.BluRay.x265.mkv"
        )

        self.assertEqual(title, "The Matrix")
        self.assertEqual(year, 1999)

    def test_four_digit_title_at_start_is_not_mistaken_for_year(self):
        title, year = MediaUtils.parse_movie_filename("1917.2019.1080p.mkv")

        self.assertEqual(title, "1917")
        self.assertEqual(year, 2019)

    def test_tv_filename_extracts_series_season_and_episode(self):
        series, season, episode, year = MediaUtils.parse_tv_filename(
            "Futurama.1999.S02E03.The.Cryonic.Woman.1080p.WEB-DL.mkv"
        )

        self.assertEqual(series, "Futurama")
        self.assertEqual((season, episode, year), (2, 3, 1999))

    def test_tv_filename_supports_compact_season_episode_pattern(self):
        series, season, episode, year = MediaUtils.parse_tv_filename(
            "Futurama - 2x03 - The Cryonic Woman.mkv"
        )

        self.assertEqual(series, "Futurama")
        self.assertEqual((season, episode, year), (2, 3, None))

    def test_edition_tag_is_normalized(self):
        edition = MediaUtils.extract_edition_tag(
            "/movies/Example.{edition-IMAX.Extended}.mkv"
        )

        self.assertEqual(edition, "IMAX Extended")

    def test_duration_reads_attribute_and_loading_state(self):
        self.assertEqual(
            MediaUtils.get_media_duration(ET.fromstring('<leaf duration="3600" />')),
            3600,
        )
        self.assertEqual(
            MediaUtils.get_media_duration(ET.fromstring('<leaf duration="0" />')),
            "Loading...",
        )


if __name__ == "__main__":
    unittest.main()
