"""Persistent, searchable index of media files on configured NAS mounts.

The index stores filesystem metadata, not media contents. A background scan builds
and reconciles the SQLite catalogue; Discord searches never walk the NAS volume.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
import uuid
from typing import Iterable

from ..utils.media_utils import MediaUtils

MEDIA_EXTENSIONS = frozenset({
    ".mp4", ".mkv", ".avi", ".mov", ".m4v", ".wmv", ".flv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".webm",
    ".mp3", ".flac", ".aac", ".wav", ".ogg", ".m4a", ".opus", ".wma",
    ".aiff", ".aif", ".ape", ".alac", ".ac3", ".dts", ".mka",
    ".ogv", ".3gp", ".3g2", ".vob", ".asf", ".divx", ".mxf", ".m2v",
})
AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".flac", ".aac", ".wav", ".ogg", ".m4a", ".opus", ".wma",
    ".aiff", ".aif", ".ape", ".alac", ".ac3", ".dts", ".mka",
})
_GENERIC_DIRS = frozenset({
    "media", "movies", "movie", "tv", "television", "shows", "series",
    "video", "videos", "library", "downloads",
})
_SEASON_DIR = re.compile(r"^(?:season|series|s)[ ._-]*(\d+)$", re.IGNORECASE)


def _normalise_roots(roots: Iterable[str]) -> list[str]:
    ordered = []
    seen = set()
    for root in roots:
        if not root:
            continue
        path = os.path.normpath(os.path.abspath(os.path.expanduser(root)))
        key = os.path.normcase(path)
        if key not in seen:
            seen.add(key)
            ordered.append(path)
    return ordered


def _infer_series_from_parent(path: str, root: str) -> str | None:
    """Infer a show name from a conventional Show/Season N directory layout."""
    current = os.path.dirname(path)
    root_real = os.path.realpath(root)
    while current:
        name = os.path.basename(current).strip()
        if name and not _SEASON_DIR.match(name) and name.casefold() not in _GENERIC_DIRS:
            return re.sub(r"[._]+", " ", name).strip()
        if os.path.normcase(os.path.realpath(current)) == os.path.normcase(root_real):
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _episode_details(path: str, root: str) -> tuple[str | None, int | None, int | None]:
    """Parse SxxEyy/1x02 names and common Season N/01 Episode layouts."""
    series, season, episode, _year = MediaUtils.parse_tv_filename(path)
    if season is None or episode is None:
        parent_name = os.path.basename(os.path.dirname(path))
        season_match = _SEASON_DIR.match(parent_name)
        if season_match:
            season = int(season_match.group(1))
            series = _infer_series_from_parent(path, root)
            stem = os.path.splitext(os.path.basename(path))[0]
            episode_match = re.match(r"^\s*(\d{1,3})(?:[ ._-]+|$)", stem)
            if episode_match:
                episode = int(episode_match.group(1))
    if season is not None and episode is not None and not series:
        series = _infer_series_from_parent(path, root)
    return series, season, episode


class MediaLibraryService:
    """SQLite-backed media catalogue with background full-volume reconciliation."""

    def __init__(
        self,
        roots: Iterable[str],
        db_path: str = "data/media_library.sqlite3",
        rescan_interval: int = 21600,
    ):
        self.roots = _normalise_roots(roots)
        self.db_path = os.path.abspath(os.path.expanduser(db_path or "data/media_library.sqlite3"))
        self.rescan_interval = max(60, int(rescan_interval))
        self.logger = logging.getLogger(__name__)
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._rescan_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._indexing = False
        self._last_scan_at: float | None = None
        self._last_scan_error: str | None = None
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._initialise_database()

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def _database(self):
        db = self._connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _initialise_database(self) -> None:
        with self._database() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS media_files (
                    path TEXT PRIMARY KEY,
                    root TEXT NOT NULL,
                    name TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    series_title TEXT,
                    season INTEGER,
                    episode INTEGER,
                    scan_id TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_media_season ON media_files(series_title, season, episode)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_media_root ON media_files(root)")
            try:
                db.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS media_files_fts
                    USING fts5(name, path, series_title, content='media_files', content_rowid='rowid')
                """)
                db.executescript("""
                    CREATE TRIGGER IF NOT EXISTS media_files_ai AFTER INSERT ON media_files BEGIN
                        INSERT INTO media_files_fts(rowid, name, path, series_title)
                        VALUES (new.rowid, new.name, new.path, coalesce(new.series_title, ''));
                    END;
                    CREATE TRIGGER IF NOT EXISTS media_files_ad AFTER DELETE ON media_files BEGIN
                        INSERT INTO media_files_fts(media_files_fts, rowid, name, path, series_title)
                        VALUES ('delete', old.rowid, old.name, old.path, coalesce(old.series_title, ''));
                    END;
                    CREATE TRIGGER IF NOT EXISTS media_files_au AFTER UPDATE ON media_files
                    WHEN old.name IS NOT new.name
                      OR old.path IS NOT new.path
                      OR old.series_title IS NOT new.series_title
                    BEGIN
                        INSERT INTO media_files_fts(media_files_fts, rowid, name, path, series_title)
                        VALUES ('delete', old.rowid, old.name, old.path, coalesce(old.series_title, ''));
                        INSERT INTO media_files_fts(rowid, name, path, series_title)
                        VALUES (new.rowid, new.name, new.path, coalesce(new.series_title, ''));
                    END;
                """)
                self._fts_enabled = True
            except sqlite3.OperationalError as error:
                self._fts_enabled = False
                self.logger.warning("SQLite FTS5 unavailable; using fallback media search: %s", error)

    def set_roots(self, roots: Iterable[str]) -> None:
        updated = _normalise_roots(roots)
        with self._lock:
            if updated == self.roots:
                return
            self.roots = updated
            self._rescan_event.set()

    def start(self) -> bool:
        if not self.roots:
            self.logger.info("Media library indexing disabled: no MEDIA_LIBRARY_ROOTS configured")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="MediaLibraryIndexer",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        self._rescan_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def request_rescan(self) -> None:
        self._rescan_event.set()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                result = self.refresh()
                self.logger.info(
                    "Media library scan complete: %s indexed files, %s bytes, %s errors",
                    result["indexed"], result["bytes"], result["errors"],
                )
            except Exception:
                self.logger.exception("Media library scan failed")
            self._rescan_event.wait(self.rescan_interval)
            self._rescan_event.clear()

    def _iter_files(self, root: str, errors: list[str]):
        def on_error(error):
            errors.append(str(error))
            self.logger.warning("Media library traversal error: %s", error)

        for current, directories, filenames in os.walk(root, topdown=True, followlinks=False, onerror=on_error):
            directories[:] = [
                name for name in directories
                if not name.startswith(".") and not os.path.islink(os.path.join(current, name))
            ]
            for filename in filenames:
                if filename.startswith("."):
                    continue
                path = os.path.join(current, filename)
                if os.path.splitext(filename)[1].lower() not in MEDIA_EXTENSIONS:
                    continue
                # Do not allow a symlinked file to escape its configured mount root.
                try:
                    real_path = os.path.realpath(path)
                    if os.path.commonpath([os.path.realpath(root), real_path]) != os.path.realpath(root):
                        continue
                except (OSError, ValueError):
                    continue
                yield path

    def refresh(self) -> dict:
        """Index all configured roots; prune stale rows only after a complete scan."""
        with self._lock:
            roots = list(self.roots)
            if not roots:
                return {"indexed": 0, "bytes": 0, "errors": 0, "complete": False}
            self._indexing = True
            scan_id = uuid.uuid4().hex
            errors: list[str] = []
            indexed = 0
            total_bytes = 0
            try:
                with self._database() as db:
                    db.execute("BEGIN IMMEDIATE")
                    for root in roots:
                        if self._stop_event.is_set():
                            errors.append("Media library scan cancelled")
                            break
                        if not os.path.isdir(root):
                            errors.append(f"Media root is unavailable: {root}")
                            continue
                        for path in self._iter_files(root, errors):
                            if self._stop_event.is_set():
                                errors.append("Media library scan cancelled")
                                break
                            try:
                                stat = os.stat(path)
                            except OSError as error:
                                errors.append(str(error))
                                continue
                            name = os.path.basename(path)
                            extension = os.path.splitext(name)[1].lower()
                            series, season, episode = _episode_details(path, root)
                            media_type = (
                                "episode" if season is not None and episode is not None
                                else "audio" if extension in AUDIO_EXTENSIONS
                                else "video"
                            )
                            db.execute("""
                                INSERT INTO media_files (
                                    path, root, name, extension, size, mtime_ns,
                                    media_type, series_title, season, episode, scan_id
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                ON CONFLICT(path) DO UPDATE SET
                                    root=excluded.root, name=excluded.name,
                                    extension=excluded.extension, size=excluded.size,
                                    mtime_ns=excluded.mtime_ns, media_type=excluded.media_type,
                                    series_title=excluded.series_title, season=excluded.season,
                                    episode=excluded.episode, scan_id=excluded.scan_id
                            """, (
                                os.path.abspath(path), root, name, extension, stat.st_size,
                                stat.st_mtime_ns, media_type, series, season, episode, scan_id,
                            ))
                            indexed += 1
                            total_bytes += stat.st_size
                            # Commit bounded batches so the catalogue becomes searchable
                            # during a long first scan and the WAL stays manageable.
                            if indexed % 1000 == 0:
                                db.commit()
                                db.execute("BEGIN IMMEDIATE")
                        if self._stop_event.is_set():
                            break
                    complete = not errors
                    if complete:
                        for root in roots:
                            db.execute(
                                "DELETE FROM media_files WHERE root = ? AND scan_id != ?",
                                (root, scan_id),
                            )
                        placeholders = ",".join("?" for _ in roots)
                        db.execute(
                            f"DELETE FROM media_files WHERE root NOT IN ({placeholders})",
                            tuple(roots),
                        )
                    db.commit()
                self._last_scan_at = time.time()
                self._last_scan_error = "; ".join(errors[:5]) if errors else None
                return {
                    "indexed": indexed,
                    "bytes": total_bytes,
                    "errors": len(errors),
                    "complete": complete,
                }
            finally:
                self._indexing = False

    @staticmethod
    def _tokens(query: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", (query or "").casefold())

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "path": row["path"],
            "name": row["name"],
            "extension": row["extension"],
            "size": row["size"],
            "media_type": row["media_type"],
            "series_title": row["series_title"],
            "season": row["season"],
            "episode": row["episode"],
        }

    def search(self, query: str, limit: int = 25) -> list[dict]:
        """Search the indexed catalogue, never the live filesystem."""
        tokens = self._tokens(query)
        if not tokens:
            return []
        limit = max(1, min(int(limit), 100))
        with self._database() as db:
            if self._fts_enabled:
                match = " AND ".join(f'"{token}"*' for token in tokens)
                rows = db.execute("""
                    SELECT m.* FROM media_files_fts
                    JOIN media_files m ON m.rowid = media_files_fts.rowid
                    WHERE media_files_fts MATCH ?
                    ORDER BY bm25(media_files_fts), m.name COLLATE NOCASE
                    LIMIT ?
                """, (match, limit)).fetchall()
            else:
                clauses = " AND ".join("(lower(name) LIKE ? OR lower(path) LIKE ?)" for _ in tokens)
                params = []
                for token in tokens:
                    params.extend([f"%{token}%", f"%{token}%"])
                rows = db.execute(
                    f"SELECT * FROM media_files WHERE {clauses} ORDER BY name COLLATE NOCASE LIMIT ?",
                    (*params, limit),
                ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def find_season(self, series_query: str, season: int, limit: int = 500) -> list[dict]:
        """Return episodes for a series/season in stable episode order."""
        tokens = self._tokens(series_query)
        if not tokens or int(season) < 0:
            return []
        limit = max(1, min(int(limit), 2000))
        with self._database() as db:
            if self._fts_enabled:
                match = "series_title : (" + " AND ".join(f'"{token}"' for token in tokens) + ")"
                rows = db.execute("""
                    SELECT m.* FROM media_files_fts
                    JOIN media_files m ON m.rowid = media_files_fts.rowid
                    WHERE media_files_fts MATCH ? AND m.season = ?
                    ORDER BY m.episode, m.name COLLATE NOCASE, m.path
                    LIMIT ?
                """, (match, int(season), limit)).fetchall()
            else:
                clauses = " AND ".join("lower(series_title) LIKE ?" for _ in tokens)
                rows = db.execute(
                    f"SELECT * FROM media_files WHERE season = ? AND {clauses} ORDER BY episode, name COLLATE NOCASE, path LIMIT ?",
                    (int(season), *(f"%{token}%" for token in tokens), limit),
                ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def is_available(self, path: str) -> bool:
        """Revalidate an indexed path before queueing; reject missing/out-of-root targets."""
        candidate = os.path.realpath(path)
        if not os.path.isfile(candidate):
            return False
        for root in self.roots:
            real_root = os.path.realpath(root)
            try:
                if os.path.commonpath([real_root, candidate]) == real_root:
                    return True
            except (OSError, ValueError):
                continue
        return False

    def status(self) -> dict:
        with self._database() as db:
            count = db.execute("SELECT count(*) FROM media_files").fetchone()[0]
            roots = db.execute("SELECT count(DISTINCT root) FROM media_files").fetchone()[0]
        return {
            "roots": list(self.roots),
            "indexed": count,
            "indexed_roots": roots,
            "indexing": self._indexing,
            "last_scan_at": self._last_scan_at,
            "last_scan_error": self._last_scan_error,
        }
