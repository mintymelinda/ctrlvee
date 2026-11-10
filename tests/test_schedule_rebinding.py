import unittest
import xml.etree.ElementTree as ET

from src.cogs.scheduler import _resolve_schedule_index


def leaf(name, folder="/movies"):
    return ET.Element("leaf", {
        "name": name,
        "uri": f"file://{folder}/{name.replace(' ', '%20')}",
        "id": name,
    })


class ScheduleRebindingTests(unittest.TestCase):
    def setUp(self):
        self.original = leaf("Heat (1995) 720p.mkv")
        self.other = leaf("Alien (1979).mkv")
        self.entry = {"number": 2, "title": "Heat", "uri": self.original.get("uri")}

    def test_exact_file_follows_playlist_index_shifts(self):
        self.assertEqual(_resolve_schedule_index(self.entry, [self.original, self.other]), 0)

    def test_replaced_file_rebinds_to_same_title_and_year_upgrade(self):
        upgrade = leaf("Heat.1995.2160p.UHD.BluRay.x265.mkv")
        remake = leaf("Heat (2030).mkv")
        self.assertEqual(_resolve_schedule_index(self.entry, [self.other, remake, upgrade]), 2)

    def test_missing_file_without_replacement_does_not_play_old_index(self):
        self.assertIsNone(_resolve_schedule_index(self.entry, [self.other, leaf("Heat (2030).mkv")]))

    def test_missing_on_disk_exact_item_prefers_existing_upgrade(self):
        upgrade = leaf("Heat.1995.2160p.UHD.BluRay.x265.mkv")
        gone = {self.original.get("uri")}
        self.assertEqual(
            _resolve_schedule_index(self.entry, [self.original, self.other, upgrade], lambda uri: uri not in gone),
            2,
        )

    def test_missing_exact_item_is_retained_if_no_replacement_exists(self):
        gone = {self.original.get("uri")}
        self.assertEqual(
            _resolve_schedule_index(self.entry, [self.other, self.original], lambda uri: uri not in gone),
            1,
        )

    def test_missing_upgrade_is_not_selected(self):
        upgrade = leaf("Heat.1995.2160p.UHD.BluRay.x265.mkv")
        gone = {self.original.get("uri"), upgrade.get("uri")}
        self.assertEqual(
            _resolve_schedule_index(self.entry, [self.original, upgrade], lambda uri: uri not in gone),
            0,
        )

    def test_legacy_entry_without_uri_uses_saved_number(self):
        self.assertEqual(_resolve_schedule_index({"number": 2}, [self.original, self.other]), 1)
        self.assertIsNone(_resolve_schedule_index({"number": 5}, [self.original, self.other]))



import asyncio
import json
import tempfile
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from src.cogs.scheduler import Scheduler, PH_TZ


class ScheduledPlaybackLifecycleTests(unittest.TestCase):
    def make_scheduler(self, schedule, vlc):
        scheduler = Scheduler.__new__(Scheduler)
        scheduler.bot = Mock()
        scheduler.vlc = vlc
        scheduler.tmdb = Mock()
        scheduler.scheduled = [schedule]
        scheduler.pre_announce_sent = set()
        scheduler._schedule_lock = asyncio.Lock()
        scheduler._save_schedule_backup = Mock()
        return scheduler

    def make_schedule(self, **updates):
        schedule = {
            "number": 1,
            "title": "Heat (1995)",
            "uri": "file:///movies/Heat%20(1995).mkv",
            "dt": datetime.now(PH_TZ) - timedelta(minutes=1),
            "channel": 123,
            "status": "pending",
            "attempt_count": 0,
        }
        schedule.update(updates)
        return schedule

    def test_successful_playback_is_removed_only_after_vlc_accepts(self):
        schedule = self.make_schedule()
        vlc = Mock()
        vlc.play_item.return_value = object()
        scheduler = self.make_scheduler(schedule, vlc)

        outcome = scheduler._execute_due_schedule(schedule, leaf("Heat (1995).mkv"), datetime.now(PH_TZ))

        self.assertEqual(outcome, "started")
        self.assertEqual(scheduler.scheduled, [])
        self.assertEqual(schedule["status"], "executing")
        self.assertEqual(vlc.play_item.call_count, 1)
        self.assertEqual(scheduler._save_schedule_backup.call_count, 2)

    def test_failed_playback_retains_job_and_schedules_retry(self):
        schedule = self.make_schedule()
        vlc = Mock()
        vlc.play_item.return_value = None
        scheduler = self.make_scheduler(schedule, vlc)
        now = datetime.now(PH_TZ)

        outcome = scheduler._execute_due_schedule(schedule, leaf("Heat (1995).mkv"), now)

        self.assertEqual(outcome, "retry")
        self.assertEqual(scheduler.scheduled, [schedule])
        self.assertEqual(schedule["status"], "pending")
        self.assertEqual(schedule["attempt_count"], 1)
        self.assertGreater(schedule["next_attempt_at"], now.timestamp())
        self.assertEqual(vlc.play_item.call_count, 1)

    def test_retry_backoff_prevents_duplicate_attempt_before_due(self):
        schedule = self.make_schedule(next_attempt_at=(datetime.now(PH_TZ) + timedelta(minutes=2)).timestamp())
        vlc = Mock()
        scheduler = self.make_scheduler(schedule, vlc)

        outcome = scheduler._execute_due_schedule(schedule, leaf("Heat (1995).mkv"), datetime.now(PH_TZ))

        self.assertEqual(outcome, "deferred")
        vlc.play_item.assert_not_called()

    def test_restart_recovers_schedule_already_playing_without_replaying(self):
        schedule = self.make_schedule(status="executing", attempt_count=1)
        vlc = Mock()
        scheduler = self.make_scheduler(schedule, vlc)
        current_leaf = leaf("Heat (1995).mkv")
        current_leaf.set("current", "current")

        outcome = scheduler._execute_due_schedule(schedule, current_leaf, datetime.now(PH_TZ))

        self.assertEqual(outcome, "recovered")
        self.assertEqual(scheduler.scheduled, [])
        vlc.play_item.assert_not_called()

    def test_restart_retries_interrupted_attempt_when_target_is_not_current(self):
        schedule = self.make_schedule(status="executing", attempt_count=1)
        vlc = Mock()
        vlc.play_item.return_value = object()
        scheduler = self.make_scheduler(schedule, vlc)

        outcome = scheduler._execute_due_schedule(schedule, leaf("Heat (1995).mkv"), datetime.now(PH_TZ))

        self.assertEqual(outcome, "started")
        vlc.play_item.assert_called_once_with("Heat (1995).mkv")
        self.assertEqual(scheduler.scheduled, [])

    def test_retry_state_survives_backup_reload(self):
        schedule = self.make_schedule(
            status="pending",
            attempt_count=2,
            next_attempt_at=(datetime.now(PH_TZ) + timedelta(minutes=1)).timestamp(),
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as backup:
            json.dump([{**schedule, "dt": schedule["dt"].isoformat()}], backup)
            backup_path = backup.name
        try:
            with patch("src.cogs.scheduler.SCHEDULE_BACKUP_FILE", backup_path):
                scheduler = Scheduler.__new__(Scheduler)
                loaded = scheduler._load_schedule_backup()
            self.assertEqual(loaded[0]["attempt_count"], 2)
            self.assertEqual(loaded[0]["status"], "pending")
            self.assertEqual(loaded[0]["dt"], schedule["dt"])
            self.assertAlmostEqual(loaded[0]["next_attempt_at"], schedule["next_attempt_at"])
        finally:
            import os
            os.unlink(backup_path)


if __name__ == "__main__":
    unittest.main()
