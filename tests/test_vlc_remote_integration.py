import os
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import quote

from src.services.vlc_controller import VLCController


SUPPORTED_MEDIA_EXTENSIONS = {
    ".3gp",
    ".aac",
    ".ac3",
    ".aiff",
    ".ape",
    ".avi",
    ".flac",
    ".flv",
    ".m4a",
    ".m4v",
    ".m2ts",
    ".m2v",
    ".mka",
    ".mkv",
    ".mov",
    ".mp2",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ogg",
    ".opus",
    ".ts",
    ".mts",
    ".wav",
    ".webm",
    ".wma",
    ".wmv",
}


class RemoteVLCIntegrationTests(unittest.TestCase):
    """Exercise real files from the dedicated media folder without modifying them."""

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
        if not os.access(cls.media_dir, os.R_OK | os.X_OK):
            raise RuntimeError(f"Remote VLC media directory is not readable: {cls.media_dir}")

        cls.controller = VLCController(
            host=cls.host,
            port=cls.port,
            password=cls.password,
            queue_backup_file=os.path.join(
                tempfile.gettempdir(), "ctrlvee-remote-vlc-queue.json"
            ),
        )
        cls.wait_until(
            lambda: cls.controller.get_status() is not None
            and cls.controller.get_playlist() is not None,
            "VLC status and playlist endpoints",
        )

    @classmethod
    def wait_until(cls, predicate, description, timeout=30):
        deadline = time.monotonic() + timeout
        last_value = None
        while time.monotonic() < deadline:
            last_value = predicate()
            if last_value:
                return last_value
            time.sleep(0.25)
        raise AssertionError(
            f"Timed out waiting for {description}; last value: {last_value!r}"
        )

    @classmethod
    def media_fixtures(cls):
        candidates = []
        for path in cls.media_dir.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            relative_path = path.relative_to(cls.media_dir)
            if any(part.startswith(".") for part in relative_path.parts):
                continue
            if path.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
                continue
            if path.stat().st_size == 0:
                continue
            candidates.append(path)

        return sorted(candidates, key=lambda path: str(path).casefold())

    def exercise_media_file(self, fixture_path):
        relative_path = fixture_path.relative_to(self.media_dir)
        fixture_name = relative_path.as_posix()
        remote_uri = f"file:///media/{quote(fixture_name, safe='/')}"
        owned_item_id = None

        try:
            owned_item_id = self.controller.play_media_uri(
                remote_uri,
                item_name=fixture_name,
            )
            self.assertIsNotNone(
                owned_item_id,
                f"VLC did not enqueue and select media fixture {fixture_name!r}",
            )

            def playing_status():
                status = self.controller.get_status()
                if (
                    status is not None
                    and status.findtext("state") == "playing"
                    and status.findtext("currentplid") == str(owned_item_id)
                ):
                    return status
                return None

            status = self.wait_until(
                playing_status,
                f"VLC to play {fixture_name!r}",
            )
            print(f"VLC is playing fixture: {fixture_name}")

            self.controller.pause()
            self.wait_until(
                lambda: status
                if (status := self.controller.get_status()) is not None
                and status.findtext("currentplid") == str(owned_item_id)
                and status.findtext("state") == "paused"
                else None,
                f"VLC to pause {fixture_name!r}",
            )

            self.controller.pause()
            status = self.wait_until(
                playing_status,
                f"VLC to resume {fixture_name!r}",
            )

            # VLC can report length=-1 immediately after playback starts. Give
            # its demuxer time to discover the duration before deciding to skip.
            self.seek_coverage[fixture_name] = "duration unavailable"
            try:
                status = self.wait_until(
                    lambda: known_status
                    if (known_status := self.controller.get_status()) is not None
                    and known_status.findtext("currentplid") == str(owned_item_id)
                    and known_status.findtext("state") == "playing"
                    and (known_duration := int(known_status.findtext("length") or "-1")) >= 0
                    else None,
                    f"VLC to report the duration of {fixture_name!r}",
                    timeout=10,
                )
                duration = int(status.findtext("length") or "-1")
            except AssertionError:
                status = self.controller.get_status()
                duration = -1

            if duration >= 35:
                seek_seconds = min(30, duration - 5)
                self.seek_coverage[fixture_name] = "seek attempted"
                self.controller.seek(seek_seconds)
                self.wait_until(
                    lambda: status
                    if (status := self.controller.get_status()) is not None
                    and status.findtext("currentplid") == str(owned_item_id)
                    and int(status.findtext("time", "0")) >= seek_seconds - 5
                    else None,
                    f"VLC to seek within {fixture_name!r}",
                )
                self.seek_coverage[fixture_name] = "seek passed"
            elif duration >= 0:
                self.seek_coverage[fixture_name] = f"skipped: short ({duration}s)"
            else:
                self.seek_coverage[fixture_name] = "skipped: duration unavailable"


            self.controller.stop()
            self.wait_until(
                lambda: status
                if (status := self.controller.get_status()) is not None
                and status.findtext("state") == "stopped"
                else None,
                f"VLC to stop {fixture_name!r}",
            )
        finally:
            if owned_item_id is not None:
                status = self.controller.get_status()
                if (
                    status is not None
                    and status.findtext("currentplid") == str(owned_item_id)
                ):
                    self.controller.stop()
                self.controller.send_command(
                    "pl_delete", {"id": str(owned_item_id)}
                )
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

    def test_remote_playback_lifecycle_with_media_folder_fixtures(self):
        """Play and control every discovered user-supplied file; never alter their bytes."""
        fixtures = self.media_fixtures()
        self.assertTrue(
            fixtures,
            (
                f"No supported media files found in {self.media_dir}. "
                f"Add at least one of: {', '.join(sorted(SUPPORTED_MEDIA_EXTENSIONS))}"
            ),
        )

        self.seek_coverage = {}
        print(
            f"Testing {len(fixtures)} media fixture(s): "
            + ", ".join(path.relative_to(self.media_dir).as_posix() for path in fixtures)
        )
        try:
            for fixture_path in fixtures:
                fixture_name = fixture_path.relative_to(self.media_dir).as_posix()
                self.seek_coverage[fixture_name] = "not reached seek phase"
                with self.subTest(media=fixture_name):
                    self.exercise_media_file(fixture_path)
        finally:
            passed = [
                name for name, result in self.seek_coverage.items()
                if result == "seek passed"
            ]
            attempted = [
                name for name, result in self.seek_coverage.items()
                if result in {"seek passed", "seek attempted"}
            ]
            skipped = {
                name: result for name, result in self.seek_coverage.items()
                if result.startswith("skipped:")
            }
            print(
                "VLC SEEK COVERAGE: "
                f"{len(passed)}/{len(fixtures)} passed; "
                f"{len(attempted)}/{len(fixtures)} attempted; "
                f"{len(skipped)} skipped"
            )
            for name, result in self.seek_coverage.items():
                print(f"  SEEK {result}: {name}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
