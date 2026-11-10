import os
import tempfile
import unittest
from pathlib import Path

from src.config import parse_allowed_roles_value, parse_watch_folders_value


class ConfigParsingTests(unittest.TestCase):
    def test_allowed_roles_accept_names_ids_and_mentions_without_duplicates(self):
        roles = parse_allowed_roles_value(
            "Theater Host,123456789012345678,<@&123456789012345678>,theater host"
        )

        self.assertEqual(roles, ["Theater Host", 123456789012345678])

    def test_watch_folders_support_quoted_paths_containing_commas(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "TV, Movies"
            second = Path(directory) / "Music"
            raw = f'"{first}";"{second}"'

            folders = parse_watch_folders_value(raw)

        self.assertEqual(folders, [os.path.abspath(first), os.path.abspath(second)])


if __name__ == "__main__":
    unittest.main()
