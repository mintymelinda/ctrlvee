import os
import socket
import subprocess
import tempfile
import time
import unittest
import wave
from pathlib import Path

import requests

from src.services.vlc_controller import VLCController


class RealVLCIntegrationTests(unittest.TestCase):
    """Run CtrlVee's controller against an actual headless VLC HTTP server."""

    @classmethod
    def setUpClass(cls):
        if not shutil_which("cvlc"):
            raise unittest.SkipTest("cvlc is not installed; use Dockerfile.integration")

        cls.temp_dir = tempfile.TemporaryDirectory(prefix="ctrlvee-vlc-")
        cls.temp_path = Path(cls.temp_dir.name)
        cls.password = "ctrlvee-integration-password"

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]

        cls.media_path = cls.temp_path / "integration-tone.wav"
        sample_rate = 8000
        duration_seconds = 45
        with wave.open(str(cls.media_path), "wb") as media:
            media.setnchannels(1)
            media.setsampwidth(2)
            media.setframerate(sample_rate)
            media.writeframes(b"\x00\x00" * sample_rate * duration_seconds)

        cls.log_path = cls.temp_path / "vlc.log"
        cls.log_file = cls.log_path.open("w+")
        environment = os.environ.copy()
        environment["HOME"] = str(cls.temp_path)
        environment["XDG_CACHE_HOME"] = str(cls.temp_path / "cache")

        cls.process = subprocess.Popen(
            [
                "cvlc",
                "--intf", "http",
                "--http-host", "127.0.0.1",
                "--http-port", str(cls.port),
                "--http-password", cls.password,
                "--no-video",
                "--aout", "dummy",
                "--no-one-instance",
                "--no-media-library",
                "--ignore-config",
                "--quiet",
            ],
            stdin=subprocess.DEVNULL,
            stdout=cls.log_file,
            stderr=subprocess.STDOUT,
            env=environment,
        )

        base_url = f"http://127.0.0.1:{cls.port}"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                cls.log_file.flush()
                cls.log_file.seek(0)
                raise RuntimeError(f"VLC exited during startup:\n{cls.log_file.read()}")
            try:
                response = requests.get(
                    f"{base_url}/requests/status.xml",
                    auth=("", cls.password),
                    timeout=0.5,
                )
                if response.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.2)
        else:
            cls.log_file.flush()
            cls.log_file.seek(0)
            raise RuntimeError(f"VLC HTTP interface did not become ready:\n{cls.log_file.read()}")

        cls.controller = VLCController(
            host="127.0.0.1",
            port=cls.port,
            password=cls.password,
            queue_backup_file=str(cls.temp_path / "queue.json"),
        )

    @classmethod
    def tearDownClass(cls):
        process = getattr(cls, "process", None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log_file = getattr(cls, "log_file", None)
        if log_file is not None:
            log_file.close()
        temp_dir = getattr(cls, "temp_dir", None)
        if temp_dir is not None:
            temp_dir.cleanup()

    def wait_for(self, predicate, description, timeout=12):
        deadline = time.monotonic() + timeout
        last_value = None
        while time.monotonic() < deadline:
            last_value = predicate()
            if last_value:
                return last_value
            time.sleep(0.25)
        self.fail(f"Timed out waiting for {description}; last value: {last_value!r}")

    def test_real_vlc_enqueue_play_pause_seek_resume_and_stop(self):
        initial_status = self.controller.get_status()
        self.assertIsNotNone(initial_status, "Controller could not read VLC status")
        self.assertTrue(self.controller.enqueue_path(str(self.media_path)))

        playlist = self.wait_for(
            lambda: self.controller.get_playlist()
            if self.controller.get_playlist() is not None
            and self.controller.get_playlist().find(".//leaf") is not None
            else None,
            "media to appear in VLC playlist",
        )
        item = playlist.find(".//leaf")
        item_id = item.get("id")
        self.assertTrue(item_id, "VLC playlist item did not expose a stable ID")

        self.assertIsNotNone(self.controller.play_item(item_id))
        playing = self.wait_for(
            lambda: status
            if (status := self.controller.get_status()) is not None
            and status.findtext("state") == "playing"
            else None,
            "VLC playback to start",
        )
        self.assertEqual(playing.findtext("state"), "playing")

        self.assertIsNotNone(self.controller.pause())
        paused = self.wait_for(
            lambda: status
            if (status := self.controller.get_status()) is not None
            and status.findtext("state") == "paused"
            else None,
            "VLC playback to pause",
        )
        self.assertEqual(paused.findtext("state"), "paused")

        self.assertIsNotNone(self.controller.play())
        self.wait_for(
            lambda: status
            if (status := self.controller.get_status()) is not None
            and status.findtext("state") == "playing"
            else None,
            "VLC playback to resume",
        )

        self.assertIsNotNone(self.controller.seek(5))
        seeked = self.wait_for(
            lambda: status
            if (status := self.controller.get_status()) is not None
            and int(status.findtext("time", "0")) >= 4
            else None,
            "VLC playback position to advance after seek",
        )
        self.assertGreaterEqual(int(seeked.findtext("time", "0")), 4)

        self.assertIsNotNone(self.controller.stop())
        stopped = self.wait_for(
            lambda: status
            if (status := self.controller.get_status()) is not None
            and status.findtext("state") == "stopped"
            else None,
            "VLC playback to stop",
        )
        self.assertEqual(stopped.findtext("state"), "stopped")


def shutil_which(command):
    from shutil import which
    return which(command)


if __name__ == "__main__":
    unittest.main()
