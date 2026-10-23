"""Persistent task store (ported from the retired Rust backend's TaskDb).

Same schema and lifecycle as the Rust backend: unfinished tasks survive
restarts and are restored as 'paused' on load. The legacy schema
(req/result/postprocessed columns) is migrated in place — columns are
renamed/added, legacy ones left in place harmlessly.
"""

import orjson
import structlog
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    gid TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    thumbnail TEXT,
    filename_hint TEXT,
    req_json TEXT NOT NULL,
    status TEXT NOT NULL,
    total_length INTEGER NOT NULL DEFAULT 0,
    completed_length INTEGER NOT NULL DEFAULT 0,
    download_speed INTEGER NOT NULL DEFAULT 0,
    file_path TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC);
CREATE TABLE IF NOT EXISTS categories (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL
);
"""

# One settings row (id=1) — a typed column per AppSettings field, bools
# as 0/1 (sqlite has no bool). AppSettings.load coerces them back.
# Kept as a separate statement: _migrate re-runs it when rebuilding a
# stale settings table.
_SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    api_port INTEGER NOT NULL DEFAULT 16801,
    api_token TEXT NOT NULL DEFAULT '',
    max_concurrent_downloads INTEGER NOT NULL DEFAULT 3,
    extractor_engine TEXT NOT NULL DEFAULT 'auto',
    ytdlp_generic INTEGER NOT NULL DEFAULT 0,
    downloader_engine TEXT NOT NULL DEFAULT 'aria2_next',
    parallel_segments INTEGER NOT NULL DEFAULT 16,
    player_client TEXT NOT NULL DEFAULT 'default',
    ffmpeg_path TEXT NOT NULL DEFAULT '',
    aria2_next_path TEXT NOT NULL DEFAULT '',
    aria2_rpc_port INTEGER NOT NULL DEFAULT 16802,
    aria2_split INTEGER NOT NULL DEFAULT 16,
    aria2_max_connection_per_server INTEGER NOT NULL DEFAULT 16,
    aria2_min_split_size TEXT NOT NULL DEFAULT '1M',
    aria2_file_allocation TEXT NOT NULL DEFAULT 'none',
    aria2_min_tls_version TEXT NOT NULL DEFAULT 'TLSv1.2',
    aria2_max_overall_download_limit TEXT NOT NULL DEFAULT '0',
    aria2_max_download_limit TEXT NOT NULL DEFAULT '0',
    aria2_lowest_speed_limit TEXT NOT NULL DEFAULT '0',
    aria2_retry_wait INTEGER NOT NULL DEFAULT 0,
    aria2_state_save_interval INTEGER NOT NULL DEFAULT 10,
    bt_seed_ratio REAL NOT NULL DEFAULT 1.0,
    bt_seed_time INTEGER NOT NULL DEFAULT 0,
    bt_max_peers INTEGER NOT NULL DEFAULT 100,
    bt_listen_port INTEGER NOT NULL DEFAULT 6881,
    bt_enable_dht INTEGER NOT NULL DEFAULT 1,
    bt_enable_pex INTEGER NOT NULL DEFAULT 1,
    bt_enable_lpd INTEGER NOT NULL DEFAULT 1,
    bt_sequential INTEGER NOT NULL DEFAULT 0,
    bt_trackers TEXT NOT NULL DEFAULT '',
    media_format TEXT NOT NULL DEFAULT '',
    embed_thumbnail INTEGER NOT NULL DEFAULT 1,
    embed_subtitles INTEGER NOT NULL DEFAULT 0,
    embed_chapters INTEGER NOT NULL DEFAULT 1,
    embed_metadata INTEGER NOT NULL DEFAULT 0,
    check_certificate INTEGER NOT NULL DEFAULT 1,
    cookies_file TEXT NOT NULL DEFAULT '',
    cookies_browser TEXT NOT NULL DEFAULT 'off',
    timeout_secs INTEGER NOT NULL DEFAULT 30,
    retries INTEGER NOT NULL DEFAULT 3
);
"""

# Must equal the settings DDL's column set — a mismatch marks the table stale.
_SETTINGS_COLUMNS = frozenset({
    "id", "api_port", "api_token", "max_concurrent_downloads",
    "extractor_engine", "ytdlp_generic", "downloader_engine",
    "parallel_segments", "player_client",
    "ffmpeg_path", "aria2_next_path", "aria2_rpc_port", "aria2_split",
    "aria2_max_connection_per_server", "aria2_min_split_size",
    "aria2_file_allocation", "aria2_min_tls_version",
    "aria2_max_overall_download_limit", "aria2_max_download_limit",
    "aria2_lowest_speed_limit", "aria2_retry_wait", "aria2_state_save_interval",
    "bt_seed_ratio", "bt_seed_time", "bt_max_peers", "bt_listen_port",
    "bt_enable_dht", "bt_enable_pex", "bt_enable_lpd", "bt_sequential",
    "bt_trackers",
    "media_format", "embed_thumbnail", "embed_subtitles",
    "embed_chapters", "embed_metadata", "check_certificate",
    "cookies_file", "cookies_browser", "timeout_secs", "retries",
})

# Column name → its DDL tail ("INTEGER NOT NULL DEFAULT 3") — the additive
# settings migration ALTERs missing columns in with these declarations.
_SETTINGS_COL_DDL: dict[str, str] = {}
for _line in _SETTINGS_SCHEMA.splitlines():
    _line = _line.strip().rstrip(",")
    if " " in _line and _line[0].isalpha() and not _line.startswith(("CREATE", ")")):
        _name, _, _decl = _line.partition(" ")
        _SETTINGS_COL_DDL[_name] = _decl
del _line, _name, _decl

_SCHEMA += _SETTINGS_SCHEMA

# Columns added to pre-TaskDb databases (old schema: req/result/postprocessed).
_MIGRATIONS = (
    ("filename_hint", "TEXT"),
    ("total_length", "INTEGER NOT NULL DEFAULT 0"),
    ("completed_length", "INTEGER NOT NULL DEFAULT 0"),
    ("download_speed", "INTEGER NOT NULL DEFAULT 0"),
    ("file_path", "TEXT"),
    ("error_code", "TEXT"),
    ("error_message", "TEXT"),
    ("updated_at", "INTEGER NOT NULL DEFAULT 0"),
)


def _now_ms() -> int:
    return int(time.time() * 1000)


class TaskDb:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._migrate()
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def _migrate(self) -> None:
        """Legacy table → TaskDb schema: rename req→req_json, add the
        new columns. Unknown legacy columns (result, postprocessed) stay."""
        cols = {
            r[1]
            for r in self._conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        # Missing settings columns are added and stale ones dropped in
        # place — rebuilding the table would reset every setting to
        # defaults. A table without `id` is a foreign shape (the ancient
        # kv store): rebuild it, carrying the api token over so a paired
        # extension keeps working.
        s_cols = {
            r[1]
            for r in self._conn.execute("PRAGMA table_info(settings)").fetchall()
        }
        if s_cols and "id" not in s_cols:
            token = None
            if "api_token" in s_cols:
                row = self._conn.execute(
                    "SELECT api_token FROM settings WHERE id = 1"
                ).fetchone()
                token = row[0] if row else None
            self._conn.execute("DROP TABLE settings")
            self._conn.execute(_SETTINGS_SCHEMA)
            if token:
                self._conn.execute(
                    "INSERT INTO settings (id, api_token) VALUES (1, ?)",
                    (token,),
                )
        elif s_cols:
            for col in sorted(_SETTINGS_COLUMNS - s_cols - {"id"}):
                self._conn.execute(
                    f"ALTER TABLE settings ADD COLUMN {col} {_SETTINGS_COL_DDL[col]}"
                )
            for col in sorted(s_cols - _SETTINGS_COLUMNS - {"id"}):
                self._conn.execute(f"ALTER TABLE settings DROP COLUMN {col}")
        if not cols:
            return  # fresh db — _SCHEMA creates everything
        if "req" in cols and "req_json" not in cols:
            self._conn.execute("ALTER TABLE tasks RENAME COLUMN req TO req_json")
            cols.discard("req")
            cols.add("req_json")
        for name, decl in _MIGRATIONS:
            if name not in cols:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")
        # Rows restored before updated_at existed (0) or written by the
        # seconds-era code get created_at — the column is ms, so scale up.
        # Idempotent: converted values land above the threshold.
        self._conn.execute(
            "UPDATE tasks SET updated_at = created_at * 1000 "
            "WHERE updated_at < 100000000000"
        )

    @classmethod
    def open(cls, path: Path) -> "TaskDb":
        return cls(path)

    def load_settings(self) -> dict[str, Any] | None:
        """The settings row as a snake_case field dict, categories included."""
        try:
            with self._lock:
                cur = self._conn.execute("SELECT * FROM settings WHERE id = 1")
                row = cur.fetchone()
                cols = [d[0] for d in cur.description] if row else []
                cats = self._conn.execute(
                    "SELECT name, path FROM categories ORDER BY rowid"
                ).fetchall()
        except sqlite3.Error as e:
            log.warning("settings load failed", error=e)
            return None
        if not row:
            return None
        out = dict(zip(cols, row))
        out.pop("id", None)
        out["categories"] = [{"name": n, "path": p} for n, p in cats]
        return out

    def save_settings(
        self, values: dict[str, Any], categories: list[dict[str, str]]
    ) -> None:
        """Raises on sqlite error — a silently lost settings row mints a new
        token next boot and breaks the paired extension."""
        cols = ", ".join(values)
        ph = ", ".join("?" for _ in values)
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO settings (id, {cols}) VALUES (1, {ph})",
                tuple(values.values()),
            )
            self._conn.execute("DELETE FROM categories")
            self._conn.executemany(
                "INSERT INTO categories (name, path) VALUES (?, ?)",
                [(c.get("name", ""), c.get("path", "")) for c in categories],
            )
            self._conn.commit()

    def insert_task(
        self,
        gid: str,
        req: dict[str, Any],
        status: str,
        total_length: int,
        created_at: int,
    ) -> None:
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO tasks (
                        gid, url, thumbnail, filename_hint, req_json, status,
                        total_length, completed_length, download_speed,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
                    """,
                    (
                        gid,
                        req.get("url", ""),
                        req.get("thumbnail"),
                        req.get("filenameHint"),
                        orjson.dumps(req).decode(),
                        status,
                        int(total_length),
                        created_at,
                        # updated_at is ms everywhere — created_at stays
                        # seconds (DownloadInfo createdAt parity).
                        _now_ms(),
                    ),
                )
                self._conn.commit()
        except sqlite3.Error as e:
            log.warning("task insert failed", gid=gid, error=e)

    def update_progress(
        self, gid: str, status: str, done: int, total: int, speed: int
    ) -> None:
        now = _now_ms()
        try:
            with self._lock:
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, completed_length = ?,
                        total_length = CASE WHEN ? > 0 THEN ? ELSE total_length END,
                        download_speed = ?, updated_at = ?
                    WHERE gid = ?
                    """,
                    (status, int(done), int(total), int(total), int(speed), now, gid),
                )
                self._conn.commit()
        except sqlite3.Error as e:
            log.warning("progress update failed", gid=gid, error=e)

    def mark_complete(self, gid: str, file_path: str, size: int) -> None:
        now = _now_ms()
        try:
            with self._lock:
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'complete', file_path = ?, total_length = ?,
                        completed_length = ?, download_speed = 0,
                        error_code = NULL, error_message = NULL, updated_at = ?
                    WHERE gid = ?
                    """,
                    (file_path, int(size), int(size), now, gid),
                )
                self._conn.commit()
        except sqlite3.Error as e:
            log.warning("mark complete failed", gid=gid, error=e)

    def mark_error(self, gid: str, error_code: str, error_message: str) -> None:
        now = _now_ms()
        try:
            with self._lock:
                self._conn.execute(
                    """
                    UPDATE tasks SET status = 'error', error_code = ?,
                        error_message = ?, download_speed = 0, updated_at = ?
                    WHERE gid = ?
                    """,
                    (error_code, error_message, now, gid),
                )
                self._conn.commit()
        except sqlite3.Error as e:
            log.warning("mark error failed", gid=gid, error=e)

    def update_status(self, gid: str, status: str) -> None:
        now = _now_ms()
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE tasks SET status = ?, download_speed = 0, updated_at = ? WHERE gid = ?",
                    (status, now, gid),
                )
                self._conn.commit()
        except sqlite3.Error as e:
            log.warning("status update failed", gid=gid, error=e)

    def delete_task(self, gid: str) -> None:
        try:
            with self._lock:
                self._conn.execute("DELETE FROM tasks WHERE gid = ?", (gid,))
                self._conn.commit()
        except sqlite3.Error as e:
            log.warning("task delete failed", gid=gid, error=e)

    def purge_finished(self) -> int:
        try:
            with self._lock:
                n = self._conn.execute(
                    "DELETE FROM tasks WHERE status = 'complete'"
                ).rowcount
                self._conn.commit()
                return n
        except sqlite3.Error as e:
            log.warning("purge failed", error=e)
            return 0

    def pause_all_active(self) -> int:
        now = _now_ms()
        try:
            with self._lock:
                n = self._conn.execute(
                    "UPDATE tasks SET status = 'paused', download_speed = 0, updated_at = ?"
                    " WHERE status IN ('active', 'downloading', 'waiting', 'postprocessing')",
                    (now,),
                ).rowcount
                self._conn.commit()
                return n
        except sqlite3.Error as e:
            log.warning("pause all failed", error=e)
            return 0

    def load_all_tasks(self, default_download_dir: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """(DownloadInfo-shaped dict, req dict) for every non-removed task,
        newest first — the startup restore set. Tasks that were live when
        the backend shut down come back as 'paused'."""
        try:
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT gid, url, thumbnail, filename_hint, req_json, status,
                           total_length, completed_length, file_path,
                           error_code, error_message, created_at
                    FROM tasks
                    WHERE status != 'removed'
                    ORDER BY created_at DESC
                    LIMIT 1000
                    """
                ).fetchall()
        except sqlite3.Error as e:
            log.warning("task load failed", error=e)
            return []

        out: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for (
            gid, url, thumbnail, filename_hint, req_json, raw_status,
            total, completed, file_path, error_code, error_message, created_at,
        ) in rows:
            try:
                req = orjson.loads(req_json) if req_json else {}
            except orjson.JSONDecodeError:
                req = {"url": url, "createdAt": created_at}
            if not isinstance(req, dict):
                req = {"url": url, "createdAt": created_at}
            status = (
                "paused"
                if raw_status in ("active", "downloading", "waiting", "postprocessing")
                else raw_status
            )
            files = None
            path_val = file_path or filename_hint
            if path_val:
                files = [{
                    "path": path_val,
                    "length": str(total),
                    "completedLength": str(completed),
                }]
            info: dict[str, Any] = {
                "gid": gid,
                "status": status,
                "totalLength": str(total),
                "completedLength": str(completed),
                "downloadSpeed": "0",
                "dir": default_download_dir,
                "files": files,
                "errorCode": error_code,
                "errorMessage": error_message,
                "createdAt": created_at,
                "dax": {
                    "url": url,
                    "filenameHint": filename_hint,
                    "thumbnail": thumbnail,
                    "referer": req.get("referer"),
                    "media": req.get("media"),
                    "createdAt": created_at,
                },
            }
            out.append((info, req))
        return out
