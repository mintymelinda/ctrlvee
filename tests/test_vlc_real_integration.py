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
        cls.password = "ctrlvee-integration-password"

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]

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

        cls.controller = VLCController(
            host="127.0.0.1",
            port=cls.port,
            password=cls.password,
            queue_backup_file=str(cls.temp_path / "queue.json"),
        )

        # A single HTTP 200 from VLC is not sufficient readiness: the server
        # can begin accepting requests before both XML endpoints are stable.
        # Require consecutive successful parses through the same controller
        # path used by the tests before allowing any test to run.
        deadline = time.monotonic() + 20
        consecutive_successes = 0
        last_status = None
        last_playlist = None
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                break

            last_status = cls.controller.get_status()
            last_playlist = cls.controller.get_playlist()
            if last_status is not None and last_playlist is not None:
                consecutive_successes += 1
                if consecutive_successes >= 3:
                    break
            else:
                consecutive_successes = 0
            time.sleep(0.2)
        else:
            consecutive_successes = 0

        if consecutive_successes < 3:
            cls.log_file.flush()
            cls.log_file.seek(0)
            raise RuntimeError(
                "VLC HTTP interface did not become stable "
                f"(status parsed: {last_status is not None}, "
                f"playlist parsed: {last_playlist is not None}, "
                f"process exit: {cls.process.poll()}):\\n{cls.log_file.read()}"
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

    def load_playlist(self):
        """Reset VLC and enqueue fixtures, waiting for each playlist mutation."""
        self.controller.stop()
        self.controller.send_command("pl_empty")
        self.wait_for(
            lambda: playlist
            if (playlist := self.controller.get_playlist()) is not None
            and not playlist.findall(".//leaf")
            else None,
            "VLC playlist to become empty",
        )
        self.controller.clear_queue_tracking()

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

    def test_real_vlc_playlist_navigation_and_repeat_modes(self):
        first_id, second_id, third_id = self.load_playlist()
        self.assertIsNotNone(self.controller.play_item(first_id))
        self.wait_for_current_item(first_id)

        self.assertIsNotNone(self.controller.next())
        self.wait_for_current_item(second_id)
        self.assertIsNotNone(self.controller.previous())
        self.wait_for_current_item(first_id)

        # VLC exposes repeat-current and repeat-playlist as independent toggles.
        self.assertEqual(self.controller.get_repeat_state(), "none")
        self.assertIsNotNone(self.controller.send_command("pl_loop"))
        self.wait_for(
            lambda: self.controller.get_repeat_state() == "all",
            "repeat-playlist mode to enable",
        )
        self.assertIsNotNone(self.controller.send_command("pl_loop"))
        self.wait_for(
            lambda: self.controller.get_repeat_state() == "none",
            "repeat-playlist mode to disable",
        )
        self.assertIsNotNone(self.controller.send_command("pl_repeat"))
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
        self.controller.set_shuffle_state(not initial)
        self.wait_for(
            lambda: self.controller.get_shuffle_state() is not initial,
            "shuffle state to change",
        )
        result = self.controller.set_shuffle_state(not initial)
        self.assertTrue(result["success"])
        self.assertFalse(result["changed"])
        self.assertEqual(self.controller.get_shuffle_state(), not initial)

    def test_real_vlc_soft_queue_orders_removes_and_plays_items(self):
        first_id, second_id, third_id = self.load_playlist()
        self.assertIsNotNone(self.controller.play_item(first_id))
        self.wait_for_current_item(first_id)

        first = self.controller.queue_item_next(second_id)
        second = self.controller.queue_item_next(third_id)
        self.assertTrue(first["success"], first)
        self.assertTrue(second["success"], second)

        snapshot = self.controller.get_queue_status()
        ordered = sorted(
            snapshot["queued_items"].items(),
            key=lambda entry: entry[1]["queue_order"],
        )
        self.assertEqual([item_id for item_id, _ in ordered], [second_id, third_id])
        self.assertEqual([info["queue_order"] for _, info in ordered], [1, 2])

        removed = self.controller.remove_from_queue_by_id(second_id)
        self.assertTrue(removed["success"], removed)
        remaining = self.controller.get_queue_status()["queued_items"]
        self.assertEqual(list(remaining), [third_id])
        self.assertEqual(remaining[third_id]["queue_order"], 1)

        queued_play = self.controller.play_next_queued_item()
        self.assertTrue(queued_play["success"], queued_play)
        self.wait_for_current_item(third_id)
        self.assertEqual(queued_play["item_id"], third_id)

    def test_real_vlc_soft_queue_survives_controller_restart_and_prunes_missing_items(self):
        first_id, second_id, third_id = self.load_playlist()
        self.assertIsNotNone(self.controller.play_item(first_id))
        self.wait_for_current_item(first_id)

        self.assertTrue(self.controller.queue_item_next(second_id)["success"])
        self.assertTrue(self.controller.queue_item_next(third_id)["success"])

        # Recreate the controller as if the bot process had restarted while VLC
        # continued running. The persisted soft queue must retain its order.
        restarted = VLCController(
            host="127.0.0.1",
            port=self.port,
            password=self.password,
            queue_backup_file=str(self.temp_path / "queue.json"),
        )
        restored = restarted.reconcile_queue_with_playlist()
        self.assertTrue(restored["success"], restored)
        self.assertEqual(restored["queued_item_ids"], [second_id, third_id])

        # Remove the first queued media entry from VLC while the bot is offline.
        restarted.send_command("pl_delete", {"id": second_id})
        self.wait_for(
            lambda: playlist
            if (playlist := restarted.get_playlist()) is not None
            and all(item.get("id") != str(second_id) for item in playlist.findall(".//leaf"))
            else None,
            "deleted VLC playlist item to disappear",
        )

        reconciled = restarted.reconcile_queue_with_playlist()
        self.assertTrue(reconciled["success"], reconciled)
        self.assertEqual(reconciled["removed_item_ids"], [second_id])
        self.assertEqual(reconciled["queued_item_ids"], [third_id])
        self.assertEqual(restarted._queued_items[third_id]["queue_order"], 1)

        # A second controller restart must read the pruned, compacted backup.
        final_controller = VLCController(
            host="127.0.0.1",
            port=self.port,
            password=self.password,
            queue_backup_file=str(self.temp_path / "queue.json"),
        )
        final_result = final_controller.reconcile_queue_with_playlist()
        self.assertTrue(final_result["success"], final_result)
        self.assertEqual(final_result["queued_item_ids"], [third_id])

    def test_real_vlc_playback_recovers_after_stop(self):
        first_id, second_id, _ = self.load_playlist()
        self.assertIsNotNone(self.controller.play_item(first_id))
        self.wait_for_current_item(first_id)
        self.assertIsNotNone(self.controller.stop())
        self.wait_for(
            lambda: status
            if (status := self.controller.get_status()) is not None
            and status.findtext("state") == "stopped"
            else None,
            "VLC to stop before recovery",
        )

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
        self.assertTrue(all(result is not None for result in results))
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
        initial_status = self.controller.get_status()
        self.assertIsNotNone(initial_status, "Controller could not read VLC status")
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
