"""Async SQLite persistence for the DownloadAnything backend.

Single aiosqlite connection owned by the event loop. Settings are stored as
JSON values in a key/value table; jobs are stored one row per download with
their full snapshot so the queue survives restarts.

All methods must be awaited on the event loop thread. Worker threads reach
this store by scheduling coroutines with ``loop.call_soon_threadsafe`` (see
:mod:`engine`).
"""

from __future__ import annotations

import os
import platform
import time

import orjson
from pathlib import Path
from typing import Any

import aiosqlite
import structlog

log = structlog.get_logger()


def app_support_dir() -> Path:
    """Return a cross-platform application support directory for DownloadAnything."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library/Application Support/DownloadAnything"
    if system == "Windows":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / "DownloadAnything"
        return Path.home() / "AppData/Local/DownloadAnything"
    return Path.home() / ".local/share/DownloadAnything"


def _database_path() -> Path:
    configured_dir = os.environ.get("DA_DATA_DIR")
    if configured_dir:
        return Path(configured_dir).expanduser() / "downloadanything.db"
    return app_support_dir() / "downloadanything.db"


DB_PATH = _database_path()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    directory TEXT NOT NULL,
    format_selector TEXT,
    custom_filename TEXT,
    selected_files TEXT,
    parent_id TEXT,
    child_ids TEXT,
    is_playlist INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    thumbnail TEXT NOT NULL DEFAULT '',
    engine TEXT NOT NULL DEFAULT 'ytdlp',
    media_type TEXT,
    status TEXT NOT NULL,
    percent REAL NOT NULL DEFAULT 0,
    downloaded INTEGER NOT NULL DEFAULT 0,
    total INTEGER,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
"""


class Database:
    """Thin aiosqlite wrapper exposing settings and job persistence."""

    def __init__(self, path: Path = DB_PATH) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.executescript(_SCHEMA)
        await self._migrate_jobs()
        await self._conn.commit()

    async def _migrate_jobs(self) -> None:
        """Add columns introduced after the initial schema."""
        async with self.conn.execute("PRAGMA table_info(jobs)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "media_type" not in columns:
            await self.conn.execute("ALTER TABLE jobs ADD COLUMN media_type TEXT")
            log.info("Migrated jobs table: added media_type column")
        if "selected_files" not in columns:
            await self.conn.execute("ALTER TABLE jobs ADD COLUMN selected_files TEXT")
            log.info("Migrated jobs table: added selected_files column")
        if "parent_id" not in columns:
            await self.conn.execute("ALTER TABLE jobs ADD COLUMN parent_id TEXT")
            log.info("Migrated jobs table: added parent_id column")
        if "child_ids" not in columns:
            await self.conn.execute("ALTER TABLE jobs ADD COLUMN child_ids TEXT")
            log.info("Migrated jobs table: added child_ids column")
        if "is_playlist" not in columns:
            await self.conn.execute("ALTER TABLE jobs ADD COLUMN is_playlist INTEGER NOT NULL DEFAULT 0")
            log.info("Migrated jobs table: added is_playlist column")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be awaited first")
        return self._conn

    # --------------------------------------------------------------- settings
    async def load_settings(self) -> dict[str, Any]:
        async with self.conn.execute("SELECT key, value FROM settings") as cursor:
            rows = await cursor.fetchall()
        out: dict[str, Any] = {}
        for key, raw in rows:
            try:
                out[key] = orjson.loads(raw)
            except orjson.JSONDecodeError:
                log.warning("Ignoring corrupt settings row: %s", key)
        return out

    async def save_settings(self, settings: dict[str, Any]) -> None:
        await self.conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(key, orjson.dumps(value).decode("utf-8")) for key, value in settings.items()],
        )
        await self.conn.commit()

    # ------------------------------------------------------------------- jobs
    async def upsert_job(self, job: dict[str, Any]) -> None:
        """Persist a job snapshot (idempotent)."""
        await self.conn.execute(
            """
            INSERT INTO jobs (
                id, url, directory, format_selector, custom_filename, selected_files,
                parent_id, child_ids, is_playlist, title,
                thumbnail, engine, media_type, status, percent, downloaded, total, error,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                percent = excluded.percent,
                downloaded = excluded.downloaded,
                total = excluded.total,
                error = excluded.error,
                title = excluded.title,
                thumbnail = excluded.thumbnail,
                custom_filename = excluded.custom_filename,
                selected_files = excluded.selected_files,
                parent_id = excluded.parent_id,
                child_ids = excluded.child_ids,
                is_playlist = excluded.is_playlist,
                media_type = excluded.media_type,
                directory = excluded.directory,
                updated_at = excluded.updated_at
            """,
            (
                job["id"],
                job["url"],
                job["directory"],
                job.get("formatSelector"),
                job.get("customFilename"),
                orjson.dumps(job.get("selectedFiles")).decode("utf-8") if job.get("selectedFiles") is not None else None,
                job.get("parentId") or None,
                orjson.dumps(job.get("childIds") or []).decode("utf-8"),
                1 if job.get("isPlaylist") else 0,
                job.get("title") or "",
                job.get("thumbnail") or "",
                job.get("engine") or "ytdlp",
                job.get("mediaType") or "",
                job["status"],
                float(job.get("percent") or 0),
                int(job.get("downloaded") or 0),
                job.get("total"),
                job.get("error"),
                float(job.get("createdAt") or time.time()),
                time.time(),
            ),
        )
        await self.conn.commit()

    async def list_jobs(self) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
            columns = [col[0] for col in cursor.description]
        return [dict(zip(columns, row)) for row in rows]

    async def delete_job(self, job_id: str) -> None:
        await self.conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        await self.conn.commit()
