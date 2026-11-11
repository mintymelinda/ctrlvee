import os
import tempfile
import unittest
from pathlib import Path

from unittest.mock import patch

from src.config import Config, parse_allowed_roles_value, parse_watch_folders_value


class ConfigParsingTests(unittest.TestCase):
    def test_allowed_roles_accept_names_ids_and_mentions_without_duplicates(self):
        roles = parse_allowed_roles_value(
            "Theater Host,123456789012345678,<@&123456789012345678>,theater host"
        )

        self.assertEqual(roles, ["Theater Host", 123456789012345678])

    def test_command_voice_auto_join_requires_voice_join_enabled(self):
        with patch.object(Config, "ENABLE_VOICE_JOIN", False), patch.object(
            Config, "VOICE_AUTO_JOIN_ON_COMMAND", True
        ):
            errors = Config.validate()

        self.assertIn(
            "VOICE_AUTO_JOIN_ON_COMMAND requires ENABLE_VOICE_JOIN=true",
            errors,
        )

    def test_watch_folders_support_quoted_paths_containing_commas(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "TV, Movies"
            second = Path(directory) / "Music"
            raw = f'"{first}";"{second}"'

            folders = parse_watch_folders_value(raw)

        self.assertEqual(folders, [os.path.abspath(first), os.path.abspath(second)])


if __name__ == "__main__":
    unittest.main()
