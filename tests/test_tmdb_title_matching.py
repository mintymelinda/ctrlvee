import unittest

from src.services.tmdb_service import TMDBService
from src.utils.media_utils import MediaUtils


class TMDBTitleMatchingTests(unittest.TestCase):
    def setUp(self):
        self.service = TMDBService.__new__(TMDBService)

    def score(self, search, candidate, year=2023, candidate_year=2023):
        return self.service._compute_title_score(
            search, candidate, "", year, candidate_year, 0.0, 0
        )

    def test_ampersand_and_word_and_are_equivalent(self):
        self.assertEqual(self.score("Love and Death", "Love & Death"), 150.0)
        self.assertEqual(self.score("Love & Death", "Love & Death"), 150.0)

    def test_ampersand_does_not_create_false_substring_match(self):
        exact = self.score("Love & Death", "Love & Death")
        unrelated = self.score("Love & Death", "Love Death Dog")
        self.assertGreater(exact, unrelated + 50)

    def test_short_title_inside_long_title_is_length_weighted(self):
        exact = self.score("Love and Death", "Love & Death")
        buried_same_year = self.score(
            "Love and Death", "10 Stories About Love and Death"
        )
        buried_other_year = self.score(
            "Love and Death",
            "10 Stories About Love and Death",
            candidate_year=2026,
        )
        self.assertLess(buried_same_year, 100)
        self.assertLess(buried_other_year, exact)

    def test_near_exact_punctuation_variant_scores_as_exact(self):
        self.assertEqual(self.score("Dune Part Two", "Dune: Part Two"), 150.0)

    def test_bracketed_movie_year_is_preserved(self):
        self.assertEqual(
            MediaUtils.parse_movie_filename("Red.Sorghum.[1988].mkv"),
            ("Red Sorghum", 1988),
        )

    def test_bracketed_tv_year_and_episode_are_preserved(self):
        self.assertEqual(
            MediaUtils.parse_tv_filename("Show.[2023].S01E02.[eztv].mkv"),
            ("Show", 1, 2, 2023),
        )

    def test_non_latin_alternate_title_is_removed_when_latin_title_exists(self):
        self.assertEqual(
            MediaUtils.parse_movie_filename("Red Sorghum (紅高粱) [1988].mkv"),
            ("Red Sorghum", 1988),
        )
        self.assertEqual(
            MediaUtils.parse_tv_filename("Red Sorghum (紅高粱) [1988].mkv"),
            ("Red Sorghum", None, None, 1988),
        )

    def test_non_latin_only_title_is_preserved(self):
        self.assertEqual(
            MediaUtils.parse_movie_filename("紅高粱 (1988).mkv"),
            ("紅高粱", 1988),
        )

    def test_latin_parenthetical_is_preserved(self):
        self.assertEqual(
            MediaUtils.parse_tv_filename("The Office (US) S02E01.mkv")[0],
            "The Office (US)",
        )

    def test_non_year_bracket_tags_are_removed(self):
        self.assertEqual(
            MediaUtils.parse_tv_filename("Show [eztv] [2023] S01E02.mkv"),
            ("Show", 1, 2, 2023),
        )


if __name__ == "__main__":
    unittest.main()
