import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.cogs.playlist import PlaylistCommands


class MediaLibraryCommandEdgeCaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.cog = PlaylistCommands.__new__(PlaylistCommands)
        self.library = Mock()
        self.library.roots = [str(self.root)]
        self.library.is_available.return_value = True
        self.cog.watch_service = SimpleNamespace(media_library=self.library)
        self.cog.vlc = SimpleNamespace(run_async=AsyncMock(return_value={
            "success": True, "added": 1, "total_queued": 1,
        }))
        self.ctx = SimpleNamespace(send=AsyncMock())

    async def test_queue_media_prefers_exact_full_path_over_fuzzy_matches(self):
        selected = self.root / "Futurama.S03E01.1080p.mkv"
        other = self.root / "Futurama.S03E01.720p.mkv"
        selected.touch()
        other.touch()
        self.library.search.return_value = [
            {"path": str(selected), "name": selected.name},
            {"path": str(other), "name": other.name},
        ]
        await PlaylistCommands.queue_media.callback(self.cog, self.ctx, query=str(selected))
        self.library.is_available.assert_called_once_with(str(selected))
        self.cog.vlc.run_async.assert_awaited_once()
        method, entries = self.cog.vlc.run_async.await_args.args
        self.assertEqual(method, "queue_media_batch")
        self.assertEqual(entries[0]["media_uri"], selected.as_uri())

    async def test_queue_media_revalidates_indexed_path_before_queueing(self):
        stale = self.root / "Deleted.mkv"
        self.library.search.return_value = [{"path": str(stale), "name": stale.name}]
        self.library.is_available.return_value = False
        await PlaylistCommands.queue_media.callback(self.cog, self.ctx, query="Deleted")
        self.cog.vlc.run_async.assert_not_awaited()
        self.ctx.send.assert_awaited_once()
        self.assertIn("missing_or_outside_the_library", self.ctx.send.await_args.args[0])

    async def test_queue_season_rejects_negative_season_without_querying_library(self):
        await PlaylistCommands.queue_season.callback(self.cog, self.ctx, -1, series="Futurama")
        self.library.find_season.assert_not_called()
        self.cog.vlc.run_async.assert_not_awaited()
        self.assertIn("zero or greater", self.ctx.send.await_args.args[0])

    async def test_queue_season_refuses_safety_limit_result_without_queueing(self):
        self.library.find_season.return_value = [
            {"path": str(self.root / f"episode-{index}.mkv"), "name": f"episode-{index}.mkv", "episode": index}
            for index in range(2000)
        ]
        await PlaylistCommands.queue_season.callback(self.cog, self.ctx, 3, series="Futurama")
        self.library.find_season.assert_called_once_with("Futurama", 3, 2000)
        self.cog.vlc.run_async.assert_not_awaited()
        self.assertIn("safety limit of 2,000", self.ctx.send.await_args.args[0])

    async def test_media_search_does_not_query_empty_index_while_initial_scan_runs(self):
        self.library.status.return_value = {"indexing": True, "indexed": 0}
        await PlaylistCommands.media_search.callback(self.cog, self.ctx, query="Futurama")
        self.library.search.assert_not_called()
        self.library.find_seasons.assert_not_called()
        self.assertIn("still building its initial index", self.ctx.send.await_args.args[0])

    async def test_commands_explain_when_media_library_is_not_configured(self):
        self.library.roots = []
        await PlaylistCommands.queue_media.callback(self.cog, self.ctx, query="Futurama")
        self.library.search.assert_not_called()
        self.cog.vlc.run_async.assert_not_awaited()
        self.assertIn("not configured", self.ctx.send.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
