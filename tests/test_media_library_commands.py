import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.cogs.playlist import PlaylistCommands


class MediaLibraryCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.cog = PlaylistCommands.__new__(PlaylistCommands)
        self.cog.watch_service = SimpleNamespace(media_library=Mock())
        self.cog.vlc = SimpleNamespace(run_async=AsyncMock(return_value={
            "success": True,
            "added": 2,
            "total_queued": 2,
        }))
        self.ctx = SimpleNamespace(send=AsyncMock())

    async def test_media_search_shows_available_seasons_for_a_series(self):
        library = self.cog.watch_service.media_library
        library.roots = [self.temp_dir.name]
        library.status = Mock(return_value={"indexing": False, "indexed": 1})
        library.search = Mock(return_value=[
            {"path": "/media/Futurama/Futurama.S03E01.mkv", "name": "Futurama.S03E01.mkv", "media_type": "episode"},
        ])
        library.find_seasons = Mock(return_value=[
            {"season": 3, "episode_count": 15, "file_count": 15},
        ])

        await PlaylistCommands.media_search.callback(
            self.cog, self.ctx, query="Futurama"
        )

        library.search.assert_called_once_with("Futurama", 20)
        library.find_seasons.assert_called_once_with("Futurama")
        self.ctx.send.assert_awaited_once()
        embed = self.ctx.send.await_args.kwargs["embed"]
        self.assertIn("Season 3 (15 episodes)", embed.fields[0].value)

    async def test_queue_season_adds_all_episodes_in_catalogue_order_as_direct_uris(self):
        first = Path(self.temp_dir.name) / "Futurama.S03E01.The.Honking.mkv"
        second = Path(self.temp_dir.name) / "Futurama.S03E02.War.Is.the.H.Word.mkv"
        first.touch()
        second.touch()
        library = self.cog.watch_service.media_library
        library.roots = [self.temp_dir.name]
        library.find_season = Mock(return_value=[
            {"path": str(first), "name": first.name, "episode": 1},
            {"path": str(second), "name": second.name, "episode": 2},
        ])

        await PlaylistCommands.queue_season.callback(
            self.cog, self.ctx, 3, series="Futurama"
        )

        library.find_season.assert_called_once_with("Futurama", 3, 2000)
        self.cog.vlc.run_async.assert_awaited_once()
        method, entries = self.cog.vlc.run_async.await_args.args
        self.assertEqual(method, "queue_media_batch")
        self.assertEqual([Path(entry["media_uri"].removeprefix("file://")).name for entry in entries], [
            first.name, second.name,
        ])
        self.assertEqual([entry["item_name"] for entry in entries], [first.name, second.name])
        self.ctx.send.assert_awaited_once()
        self.assertIn("Queued **2 episodes**", self.ctx.send.await_args.args[0])

    async def test_queue_season_refuses_duplicate_episode_versions_without_partial_queue(self):
        first = Path(self.temp_dir.name) / "Futurama.S03E01.1080p.mkv"
        second = Path(self.temp_dir.name) / "Futurama.S03E01.720p.mkv"
        first.touch()
        second.touch()
        library = self.cog.watch_service.media_library
        library.roots = [self.temp_dir.name]
        library.find_season = Mock(return_value=[
            {"path": str(first), "name": first.name, "episode": 1},
            {"path": str(second), "name": second.name, "episode": 1},
        ])

        await PlaylistCommands.queue_season.callback(
            self.cog, self.ctx, 3, series="Futurama"
        )

        self.cog.vlc.run_async.assert_not_awaited()
        self.ctx.send.assert_awaited_once()
        self.assertIn("did not queue an ambiguous season", self.ctx.send.await_args.args[0])

    async def test_queue_media_requires_a_unique_match(self):
        library = self.cog.watch_service.media_library
        library.roots = [self.temp_dir.name]
        library.search = Mock(return_value=[
            {"path": "/media/Futurama.S03E01.1080p.mkv", "name": "Futurama.S03E01.1080p.mkv"},
            {"path": "/media/Futurama.S03E01.720p.mkv", "name": "Futurama.S03E01.720p.mkv"},
        ])

        await PlaylistCommands.queue_media.callback(
            self.cog, self.ctx, query="Futurama S03E01"
        )

        self.cog.vlc.run_async.assert_not_awaited()
        self.assertIn("ambiguous", self.ctx.send.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
