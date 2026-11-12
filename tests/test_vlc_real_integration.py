import asyncio
import os
import socket
import subprocess
import tempfile
import time
import unittest
import wave
from pathlib import Path

from src.services.vlc_controller import VLCController


class RealVLCIntegrationTests(unittest.TestCase):
    """Run CtrlVee's controller against an actual headless VLC HTTP server."""

    @classmethod
    def setUpClass(cls):
        if not shutil_which("cvlc"):
            raise unittest.SkipTest("cvlc is not installed; use Dockerfile.integration")

        cls.temp_dir = tempfile.TemporaryDirectory(prefix="ctrlvee-vlc-")
        cls.temp_path = Path(cls.temp_dir.name)
        sample_rate = 8000
        duration_seconds = 90
        cls.media_paths = []
        for index in range(3):
            media_path = cls.temp_path / f"integration-tone-{index + 1}.wav"
            with wave.open(str(media_path), "wb") as media:
                media.setnchannels(1)
                media.setsampwidth(2)
                media.setframerate(sample_rate)
                media.writeframes(b"\x00\x00" * sample_rate * duration_seconds)
            cls.media_paths.append(media_path)
        cls.media_path = cls.media_paths[0]

    @classmethod
    def tearDownClass(cls):
        temp_dir = getattr(cls, "temp_dir", None)
        if temp_dir is not None:
            temp_dir.cleanup()

    def setUp(self):
        self.password = "ctrlvee-integration-password"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]

        self.log_path = self.temp_path / f"{self._testMethodName}.vlc.log"
        self.log_file = self.log_path.open("w+")
        environment = os.environ.copy()
        test_home = self.temp_path / self._testMethodName
        cache_home = test_home / "cache"
        cache_home.mkdir(parents=True, exist_ok=True)
        environment["HOME"] = str(test_home)
        environment["XDG_CACHE_HOME"] = str(cache_home)

        self.process = subprocess.Popen(
            [
                "cvlc",
                "--intf", "dummy",
                "--extraintf", "http",
                "--http-host", "127.0.0.1",
                "--http-port", str(self.port),
                "--http-password", self.password,
                "--no-video",
                "--aout", "dummy",
                "--no-one-instance",
                "--no-media-library",
                "--ignore-config",
            ],
            stdin=subprocess.DEVNULL,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            env=environment,
        )

        self.queue_backup_file = self.temp_path / f"{self._testMethodName}.queue.json"
        self.controller = VLCController(
            host="127.0.0.1",
            port=self.port,
            password=self.password,
            queue_backup_file=str(self.queue_backup_file),
        )

        deadline = time.monotonic() + 20
        consecutive_successes = 0
        last_status = None
        last_playlist = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break

            last_status = self.controller.get_status()
            last_playlist = self.controller.get_playlist()
            if last_status is not None and last_playlist is not None:
                consecutive_successes += 1
                if consecutive_successes >= 3:
                    break
            else:
                consecutive_successes = 0
            time.sleep(0.2)

        if consecutive_successes < 3:
            self.log_file.flush()
            self.log_file.seek(0)
            diagnostics = self.log_file.read()
            self._stop_vlc()
            self.fail(
                "VLC HTTP interface did not become stable "
                f"(status parsed: {last_status is not None}, "
                f"playlist parsed: {last_playlist is not None}, "
                f"process exit: {self.process.poll()}):\\n{diagnostics}"
            )

    def tearDown(self):
        self._stop_vlc()

    def _stop_vlc(self):
        process = getattr(self, "process", None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log_file = getattr(self, "log_file", None)
        if log_file is not None and not log_file.closed:
            log_file.close()

    def vlc_diagnostics(self):
        """Include server liveness and recent VLC output when HTTP calls fail."""
        self.log_file.flush()
        self.log_file.seek(0, os.SEEK_END)
        size = self.log_file.tell()
        self.log_file.seek(max(0, size - 16000))
        return (
            f"VLC process exit: {self.process.poll()}\\n"
            f"Recent VLC output:\\n{self.log_file.read()}"
        )

    def wait_for(self, predicate, description, timeout=12):
        deadline = time.monotonic() + timeout
        last_value = None
        while time.monotonic() < deadline:
            last_value = predicate()
            if last_value:
                return last_value
            time.sleep(0.25)
        self.fail(f"Timed out waiting for {description}; last value: {last_value!r}\\n{self.vlc_diagnostics()}")

    def clear_playlist(self):
        """Stop playback completely before mutating the live VLC playlist."""
        self.assertIsNone(self.process.poll(), self.vlc_diagnostics())
        self.controller.stop()
        self.wait_for(
            lambda: status
            if (status := self.controller.get_status()) is not None
            and status.findtext("state") == "stopped"
            else None,
            "VLC playback to stop before clearing the playlist",
        )
        self.assertIsNone(self.process.poll(), self.vlc_diagnostics())

        self.controller.send_command("pl_empty")
        self.wait_for(
            lambda: playlist
            if (playlist := self.controller.get_playlist()) is not None
            and not playlist.findall(".//leaf")
            else None,
            "VLC playlist to become empty",
        )
        self.assertIsNone(self.process.poll(), self.vlc_diagnostics())
        self.controller.clear_queue_tracking()

    def load_playlist(self):
        """Reset VLC and enqueue fixtures, waiting for each playlist mutation."""
        self.clear_playlist()

        item_ids = []
        for index, media_path in enumerate(self.media_paths, start=1):
            # VLC's HTTP interface can return an empty/ambiguous command response;
            # the playlist itself is the authoritative acknowledgement.
            self.controller.enqueue_path(str(media_path))
            playlist, items = self.wait_for(
                lambda: (playlist, items)
                if (playlist := self.controller.get_playlist()) is not None
                and len(items := playlist.findall(".//leaf")) >= index
                else None,
                f"fixture {index} to appear in VLC playlist",
            )
            item_ids.append(items[index - 1].get("id"))

        self.assertTrue(all(item_ids), f"VLC returned invalid playlist IDs: {item_ids!r}")
        return item_ids

    def wait_for_current_item(self, item_id):
        return self.wait_for(
            lambda: playlist
            if (playlist := self.controller.get_playlist()) is not None
            and any(
                item.get("id") == str(item_id) and item.get("current")
                for item in playlist.findall(".//leaf")
            )
            else None,
            f"VLC to select playlist item {item_id}",
        )

    def wait_for_current_uri(self, media_uri):
        return self.wait_for(
            lambda: item.get("id")
            if (playlist := self.controller.get_playlist()) is not None
            and (item := next(
                (leaf for leaf in playlist.findall(".//leaf")
                 if leaf.get("current") and leaf.get("uri") == media_uri),
                None,
            )) is not None
            else None,
            f"VLC to play media URI {media_uri}",
        )

    def test_real_vlc_playlist_navigation_and_repeat_modes(self):
        first_id, second_id, third_id = self.load_playlist()
        self.controller.play_item(first_id)
        self.wait_for_current_item(first_id)

        # VLC may execute navigation while returning an empty HTTP body. The
        # selected playlist item, not the response XML, is the acknowledgement.
        self.controller.next()
        self.wait_for_current_item(second_id)
        self.controller.previous()
        self.wait_for_current_item(first_id)

        # VLC exposes repeat-current and repeat-playlist as independent toggles.
        self.assertEqual(self.controller.get_repeat_state(), "none")
        self.controller.send_command("pl_loop")
        self.wait_for(
            lambda: self.controller.get_repeat_state() == "all",
            "repeat-playlist mode to enable",
        )
        self.assertIsNotNone(self.controller.send_command("pl_loop"))
        self.wait_for(
            lambda: self.controller.get_repeat_state() == "none",
            "repeat-playlist mode to disable",
        )
        self.controller.send_command("pl_repeat")
        self.wait_for(
            lambda: self.controller.get_repeat_state() == "one",
            "repeat-current mode to enable",
        )
        self.assertIsNotNone(self.controller.send_command("pl_repeat"))
        self.wait_for(
            lambda: self.controller.get_repeat_state() == "none",
            "repeat-current mode to disable",
        )

        # Verify the actual selected item; VLC command responses are not a
        # reliable acknowledgement for every HTTP-interface command.
        self.controller.play_item(third_id)
        self.wait_for_current_item(third_id)

    def test_real_vlc_shuffle_state_changes_are_observable(self):
        first_id, _, _ = self.load_playlist()
        self.assertIsNotNone(self.controller.play_item(first_id))
        self.wait_for_current_item(first_id)

        initial = self.controller.get_shuffle_state()
        self.assertIsInstance(initial, bool)
        try:
            self.controller.set_shuffle_state(not initial)
            self.wait_for(
                lambda: self.controller.get_shuffle_state() is not initial,
                "shuffle state to change",
            )
            result = self.controller.set_shuffle_state(not initial)
            self.assertTrue(result["success"])
            self.assertFalse(result["changed"])
            self.assertEqual(self.controller.get_shuffle_state(), not initial)
        finally:
            # This VLC process is shared by every integration test in the class.
            # Restore the global playlist mode so the next test starts from the
            # same baseline rather than clearing a playing shuffled playlist.
            if self.process.poll() is None:
                restored = self.controller.set_shuffle_state(initial)
                self.assertTrue(restored["success"], restored)
                self.wait_for(
                    lambda: self.controller.get_shuffle_state() is initial,
                    "shuffle state to return to its original value",
                )

    def test_real_vlc_soft_queue_orders_removes_and_plays_items(self):
        first_id, second_id, third_id = self.load_playlist()
        self.assertIsNotNone(self.controller.play_item(first_id))
        self.wait_for_current_item(first_id)

        first = self.controller.queue_item_next(second_id)
        second = self.controller.queue_item_next(third_id)
        self.assertTrue(first["success"], first)
        self.assertTrue(second["success"], second)
        self.assertNotEqual(first["queue_id"], second_id)
        self.assertNotEqual(second["queue_id"], third_id)

        snapshot = self.controller.get_queue_status()
        ordered = sorted(
            snapshot["queued_items"].items(),
            key=lambda entry: entry[1]["queue_order"],
        )
        self.assertEqual(
            [queue_id for queue_id, _ in ordered],
            [first["queue_id"], second["queue_id"]],
        )
        self.assertEqual([info["queue_order"] for _, info in ordered], [1, 2])

        removed = self.controller.remove_from_queue_by_id(first["queue_id"])
        self.assertTrue(removed["success"], removed)
        remaining = self.controller.get_queue_status()["queued_items"]
        self.assertEqual(list(remaining), [second["queue_id"]])
        self.assertEqual(remaining[second["queue_id"]]["queue_order"], 1)

        queued_play = self.controller.play_next_queued_item()
        self.assertTrue(queued_play["success"], queued_play)
        current = self.wait_for_current_uri(self.media_paths[2].as_uri())
        self.assertEqual(queued_play["item_id"], current)
        self.assertEqual(queued_play["queue_id"], second["queue_id"])
        self.assertEqual(len(self.controller.get_playlist().findall(".//leaf")), 4)

    def test_real_vlc_bot_owned_queue_survives_controller_restart_and_playlist_reset(self):
        first_id, second_id, third_id = self.load_playlist()
        self.assertIsNotNone(self.controller.play_item(first_id))
        self.wait_for_current_item(first_id)

        first_queued = self.controller.queue_item_next(second_id)
        second_queued = self.controller.queue_item_next(third_id)
        self.assertTrue(first_queued["success"], first_queued)
        self.assertTrue(second_queued["success"], second_queued)
        first_queue_id = first_queued["queue_id"]
        second_queue_id = second_queued["queue_id"]

        restarted = VLCController(
            host="127.0.0.1",
            port=self.port,
            password=self.password,
            queue_backup_file=str(self.queue_backup_file),
        )
        restored = restarted.reconcile_queue_with_playlist()
        self.assertTrue(restored["success"], restored)
        self.assertEqual(restored["queued_item_ids"], [first_queue_id, second_queue_id])

        # VLC 3.0 can crash if its active playlist is emptied while an
        # item is playing. Stop playback and wait for the server to report the
        # stopped state before simulating loss of the execution playlist.
        restarted.stop()
        self.wait_for(
            lambda: status
            if (status := restarted.get_status()) is not None
            and status.findtext("state") == "stopped"
            else None,
            "VLC playback to stop before simulating playlist loss",
        )
        self.assertIsNone(self.process.poll(), self.vlc_diagnostics())

        # The bot queue must survive VLC losing every playlist ID. VLC is only
        # an execution target; queued URIs and order live in CtrlVee's backup.
        restarted.send_command("pl_empty")
        self.assertIsNone(self.process.poll(), self.vlc_diagnostics())
        self.wait_for(
            lambda: playlist
            if (playlist := restarted.get_playlist()) is not None
            and not playlist.findall(".//leaf")
            else None,
            "VLC execution playlist to clear",
        )
        reconciled = restarted.reconcile_queue_with_playlist()
        self.assertTrue(reconciled["success"], reconciled)
        self.assertEqual(reconciled["removed_queue_ids"], [])
        self.assertEqual(reconciled["queued_item_ids"], [first_queue_id, second_queue_id])

        final_controller = VLCController(
            host="127.0.0.1",
            port=self.port,
            password=self.password,
            queue_backup_file=str(self.temp_path / "queue.json"),
        )
        final_result = final_controller.reconcile_queue_with_playlist()
        self.assertTrue(final_result["success"], final_result)
        self.assertEqual(final_result["queued_item_ids"], [first_queue_id, second_queue_id])
        self.assertEqual(final_controller._queued_items[first_queue_id]["media_uri"], self.media_paths[1].as_uri())

        played = final_controller.play_next_queued_item()
        self.assertTrue(played["success"], played)
        self.wait_for_current_uri(self.media_paths[1].as_uri())
        self.assertEqual(len(final_controller.get_playlist().findall(".//leaf")), 1)

    def test_real_vlc_playback_recovers_after_stop(self):
        first_id, second_id, _ = self.load_playlist()
        self.assertIsNotNone(self.controller.play_item(first_id))
        self.wait_for_current_item(first_id)
        self.wait_for(
            lambda: status
            if (status := self.controller.get_status()) is not None
            and status.findtext("state") == "playing"
            else None,
            "VLC playback to enter the playing state before stop",
        )
        self.assertIsNone(self.process.poll(), self.vlc_diagnostics())

        self.controller.stop()
        self.wait_for(
            lambda: status
            if (status := self.controller.get_status()) is not None
            and status.findtext("state") == "stopped"
            else None,
            "VLC to stop before recovery",
        )
        self.assertIsNone(self.process.poll(), self.vlc_diagnostics())

        recovery = self.controller.play_item_and_capture(second_id)
        self.assertTrue(recovery["success"], recovery)
        self.wait_for_current_item(second_id)
        self.wait_for(
            lambda: status
            if (status := self.controller.get_status()) is not None
            and status.findtext("state") == "playing"
            else None,
            "VLC playback to recover on the next item",
        )

    def test_real_vlc_concurrent_commands_use_serialized_controller_path(self):
        first_id, second_id, _ = self.load_playlist()
        self.assertIsNotNone(self.controller.play_item(first_id))
        self.wait_for_current_item(first_id)

        async def issue_commands():
            return await asyncio.gather(
                self.controller.run_async("seek", 3),
                self.controller.run_async("pause"),
                self.controller.run_async("play"),
                self.controller.run_async("play_item", second_id),
                self.controller.run_async("pause"),
            )

        results = asyncio.run(issue_commands())
        # Command response XML is not a reliable acknowledgement for every
        # VLC action. Verify the final selected item and playback state below.
        self.wait_for_current_item(second_id)
        final_status = self.wait_for(
            lambda: status
            if (status := self.controller.get_status()) is not None
            and status.findtext("state") == "paused"
            else None,
            "last serialized concurrent command to leave VLC paused",
        )
        self.assertEqual(final_status.findtext("state"), "paused")
        self.assertIsNotNone(self.controller.get_playlist())

    def test_real_vlc_enqueue_play_pause_seek_resume_and_stop(self):
        # The VLC server is shared for this test class. Previous tests may have
        # left media loaded or paused, so this test must establish an empty
        # playlist before asserting the single-item enqueue/play lifecycle.
        self.clear_playlist()
        initial_status = self.controller.get_status()
        self.assertIsNotNone(
            initial_status,
            f"Controller could not read VLC status\n{self.vlc_diagnostics()}",
        )
        self.assertTrue(self.controller.enqueue_path(str(self.media_path)))

        def playlist_with_item():
            playlist_snapshot = self.controller.get_playlist()
            if playlist_snapshot is not None and playlist_snapshot.find(".//leaf") is not None:
                return playlist_snapshot
            return None

        playlist = self.wait_for(
            playlist_with_item,
            "media to appear in VLC playlist",
        )
        item = playlist.find(".//leaf")
        item_id = item.get("id")
        self.assertTrue(item_id, "VLC playlist item did not expose a stable ID")

        # VLC may execute a command while returning an empty/non-XML response;
        # verify the selected playlist item instead of treating the HTTP body as
        # the playback acknowledgement.
        self.controller.play_item(item_id)
        self.wait_for_current_item(item_id)
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
