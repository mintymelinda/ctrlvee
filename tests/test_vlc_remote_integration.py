import os
import tempfile
import time
import unittest
import wave
from pathlib import Path

from src.services.vlc_controller import VLCController


class RemoteVLCIntegrationTests(unittest.TestCase):
    """Exercise one dedicated LAN VLC endpoint without touching unrelated entries."""

    @classmethod
    def setUpClass(cls):
        required = ("REMOTE_VLC_HOST", "REMOTE_VLC_PASSWORD", "REMOTE_VLC_MEDIA_DIR")
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise unittest.SkipTest(f"Remote VLC configuration missing: {', '.join(missing)}")

        cls.host = os.environ["REMOTE_VLC_HOST"]
        cls.port = int(os.getenv("REMOTE_VLC_PORT", "18080"))
        cls.password = os.environ["REMOTE_VLC_PASSWORD"]
        cls.media_dir = Path(os.environ["REMOTE_VLC_MEDIA_DIR"]).resolve()
        if not cls.media_dir.is_dir():
            raise RuntimeError(f"Remote VLC media directory does not exist: {cls.media_dir}")
        if not os.access(cls.media_dir, os.W_OK):
            raise RuntimeError(f"Remote VLC media directory is not writable: {cls.media_dir}")

        cls.controller = VLCController(
            host=cls.host,
            port=cls.port,
            password=cls.password,
            queue_backup_file=os.path.join(tempfile.gettempdir(), "ctrlvee-remote-vlc-queue.json"),
        )
        cls.wait_until(
            lambda: cls.controller.get_status() is not None
            and cls.controller.get_playlist() is not None,
            "VLC status and playlist endpoints",
        )

    @classmethod
    def wait_until(cls, predicate, description, timeout=20):
        deadline = time.monotonic() + timeout
        last_value = None
        while time.monotonic() < deadline:
            last_value = predicate()
            if last_value:
                return last_value
            time.sleep(0.25)
        raise AssertionError(f"Timed out waiting for {description}; last value: {last_value!r}")

    def test_remote_playback_lifecycle_and_owned_item_cleanup(self):
        """Add/play/control/delete only the uniquely named item created by this test."""
        fixture_name = f"ctrlvee-remote-{os.getpid()}-{time.time_ns()}.wav"
        fixture_path = self.media_dir / fixture_name
        remote_uri = f"file:///media/{fixture_name}"
        owned_item_id = None

        try:
            with wave.open(str(fixture_path), "wb") as media:
                media.setnchannels(1)
                media.setsampwidth(2)
                media.setframerate(8000)
                media.writeframes(b"\\x00\\x00" * 8000 * 120)

            owned_item_id = self.controller.play_media_uri(
                remote_uri,
                item_name=fixture_name,
            )
            self.assertIsNotNone(owned_item_id, "VLC did not enqueue and select the test fixture")

            def playing_status():
                status = self.controller.get_status()
                if (
                    status is not None
                    and status.findtext("state") == "playing"
                    and status.findtext("currentplid") == str(owned_item_id)
                ):
                    return status
                return None

            self.wait_until(playing_status, "VLC to play the test-owned fixture")

            self.controller.pause()
            self.wait_until(
                lambda: status
                if (status := self.controller.get_status()) is not None
                and status.findtext("currentplid") == str(owned_item_id)
                and status.findtext("state") == "paused"
                else None,
                "VLC to pause the test-owned fixture",
            )

            self.controller.pause()
            self.wait_until(
                playing_status,
                "VLC to resume the test-owned fixture",
            )

            self.controller.seek(30)
            self.wait_until(
                lambda: status
                if (status := self.controller.get_status()) is not None
                and status.findtext("currentplid") == str(owned_item_id)
                and int(status.findtext("time", "0")) >= 25
                else None,
                "VLC to seek within the test-owned fixture",
            )

            self.controller.stop()
            self.wait_until(
                lambda: status
                if (status := self.controller.get_status()) is not None
                and status.findtext("state") == "stopped"
                else None,
                "VLC to stop playback",
            )
        finally:
            if owned_item_id is not None:
                # Never clear the playlist: remove only the ID created above.
                status = self.controller.get_status()
                if status is not None and status.findtext("currentplid") == str(owned_item_id):
                    self.controller.stop()
                self.controller.send_command("pl_delete", {"id": str(owned_item_id)})
                self.wait_until(
                    lambda: playlist
                    if (playlist := self.controller.get_playlist()) is not None
                    and not any(
                        item.get("id") == str(owned_item_id)
                        for item in playlist.findall(".//leaf")
                    )
                    else None,
                    f"VLC to remove test-owned playlist item {owned_item_id}",
                )
            fixture_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
