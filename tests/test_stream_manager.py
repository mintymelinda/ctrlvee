import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.cogs.stream_manager import (
    StreamManagerCommands,
    member_has_allowed_role,
    member_is_sharing_in_channel,
    playback_is_active,
)


class StreamManagerHelperTests(unittest.TestCase):
    def test_playback_is_active_only_for_playing_state(self):
        self.assertTrue(playback_is_active(SimpleNamespace(findtext=lambda key: "playing")))
        self.assertFalse(playback_is_active(SimpleNamespace(findtext=lambda key: "paused")))
        self.assertFalse(playback_is_active(None))

    def test_requires_share_in_the_expected_voice_channel(self):
        channel = SimpleNamespace(id=10)
        member = SimpleNamespace(voice=SimpleNamespace(channel=channel, self_stream=True))
        self.assertTrue(member_is_sharing_in_channel(member, channel))
        self.assertFalse(member_is_sharing_in_channel(member, SimpleNamespace(id=11)))
        member.voice.self_stream = False
        self.assertFalse(member_is_sharing_in_channel(member, channel))

    def test_role_authorization_accepts_configured_id_and_name(self):
        member = SimpleNamespace(roles=[SimpleNamespace(id=123, name="Theater Host")])
        self.assertTrue(member_has_allowed_role(member, [123]))
        self.assertTrue(member_has_allowed_role(member, ["Theater Host"]))
        self.assertFalse(member_has_allowed_role(member, ["Other Role"]))


class StreamManagerPersistenceTests(unittest.TestCase):
    def test_assignment_survives_service_recreation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stream_managers.json"
            cog = StreamManagerCommands(SimpleNamespace(), SimpleNamespace(), path)
            cog.assignments["456"] = {"user_id": 789, "host_user_id": 790, "channel_id": 321}
            cog._save_assignments()

            restored = StreamManagerCommands(SimpleNamespace(), SimpleNamespace(), path)

            self.assertEqual(
                restored.assignments,
                {"456": {"user_id": 789, "host_user_id": 790, "channel_id": 321}},
            )
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"456": {"user_id": 789, "channel_id": 321}},
            )

    def test_legacy_assignment_defaults_host_to_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            path.write_text(
                json.dumps({"456": {"user_id": 789, "channel_id": 321}}),
                encoding="utf-8",
            )
            restored = StreamManagerCommands(SimpleNamespace(), SimpleNamespace(), path)
            self.assertEqual(
                restored.assignments["456"],
                {"user_id": 789, "host_user_id": 789, "channel_id": 321},
            )

    def test_missing_registry_starts_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            cog = StreamManagerCommands(SimpleNamespace(), SimpleNamespace(), path)
            self.assertEqual(cog.assignments, {})


if __name__ == "__main__":
    unittest.main()
