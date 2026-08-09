"""Persistent settings store for the DownloadAnything yt-dlp backend.

Settings live in the SQLite database (see :mod:`db`). Every mutation goes
through :class:`SettingsStore.update`,
which merges the partial update, persists it and notifies subscribers so the
websocket server can broadcast the new state to all connected clients in
realtime.

Reads are served from an in-memory copy so worker threads can call
:meth:`SettingsStore.get` synchronously; writes are persisted through
aiosqlite on the event loop.
"""

from __future__ import annotations

import asyncio
import copy
import os
import threading
from collections.abc import Callable
from typing import Any, cast

import structlog

try:
    from .db import Database
except ImportError:
    from db import Database

log = structlog.get_logger()

DEFAULT_SETTINGS: dict[str, Any] = {
    # Download destinations. `downloadDir` is the active default;
    # `presetPaths` feed the location selector in the extension modal.
    "downloadDir": os.path.join(os.path.expanduser("~"), "Downloads"),
    "presetPaths": [
        {"name": "Downloads", "path": os.path.join(os.path.expanduser("~"), "Downloads")},
        {"name": "Desktop", "path": os.path.join(os.path.expanduser("~"), "Desktop")},
        {"name": "Movies", "path": os.path.join(os.path.expanduser("~"), "Movies")},
    ],
    # Network / performance.
    "rateLimit": 0,  # bytes/sec, 0 = unlimited
    "concurrentFragments": 16,
    "retries": 20,
    "proxy": "",
    "cookiesFromBrowser": "",  # e.g. "chrome", "firefox"; empty = off
    # Post-processing extras.
    "addMetadata": True,
    "writeThumbnail": True,
    "writeSubs": True,
    "mergeOutputFormat": "mkv",  # default output container
    # Queue behaviour.
    "maxConcurrentDownloads": 5,
    # aria2-next external downloader options.
    "aria2NextConnections": 32,
    "aria2NextMaxConcurrent": 32,
    "aria2NextMinSplitSize": "1M",
    "aria2NextFileAllocation": "none",
    "aria2NextExtraArgs": "",
    # libtorrent session options.
    "torrentListenPort": 6881,
    "torrentEnableDht": True,
    "torrentEnableLsd": True,
    "torrentEnableUpnp": True,
    "torrentEnableNatpmp": True,
    "torrentEnablePex": True,
    "torrentMaxConnections": 500,
    "torrentUploadLimit": 0,
    "torrentMetadataTimeout": 120,
}

# Keys the clients are allowed to set, with light type validation.
_SCHEMA: dict[str, type | tuple[type, ...]] = {
    "downloadDir": str,
    "presetPaths": list,
    "rateLimit": (int, float),
    "concurrentFragments": int,
    "retries": int,
    "proxy": str,
    "cookiesFromBrowser": str,
    "addMetadata": bool,
    "writeThumbnail": bool,
    "writeSubs": bool,
    "mergeOutputFormat": str,
    "maxConcurrentDownloads": int,
    "aria2NextConnections": int,
    "aria2NextMaxConcurrent": int,
    "aria2NextMinSplitSize": str,
    "aria2NextFileAllocation": str,
    "aria2NextExtraArgs": str,
    "torrentListenPort": int,
    "torrentEnableDht": bool,
    "torrentEnableLsd": bool,
    "torrentEnableUpnp": bool,
    "torrentEnableNatpmp": bool,
    "torrentEnablePex": bool,
    "torrentMaxConnections": int,
    "torrentUploadLimit": (int, float),
    "torrentMetadataTimeout": int,
}


class SettingsStore:
    """Thread-safe settings container with SQLite persistence and subscribers."""

    def __init__(self, db: Database, loop: asyncio.AbstractEventLoop) -> None:
        self._db = db
        self._loop = loop
        self._lock = threading.RLock()
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        self._settings = copy.deepcopy(DEFAULT_SETTINGS)

    async def load(self) -> None:
        """Load settings from the database over the built-in defaults."""
        stored = await self._db.load_settings()
        with self._lock:
            self._settings.update(self._validate(stored))
        log.info("Settings loaded", keys=list(stored.keys()) if isinstance(stored, dict) else [])

    def _persist(self, snapshot: dict[str, Any]) -> None:
        def schedule() -> None:
            asyncio.ensure_future(self._db.save_settings(snapshot))

        try:
            self._loop.call_soon_threadsafe(schedule)
        except RuntimeError:
            log.error("Could not persist settings: event loop is closed")

    @staticmethod
    def _clean_preset_paths(value: list[object]) -> list[dict[str, str]]:
        clean: list[dict[str, str]] = []
        for raw in value:
            if not isinstance(raw, dict):
                continue
            preset = cast(dict[str, object], raw)
            path = str(preset.get("path") or "").strip()
            name = str(preset.get("name") or "").strip()
            if not path or not name:
                continue
            clean.append({"name": name, "path": path})
        return clean

    @staticmethod
    def _validate(partial: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for key, value in partial.items():
            expected = _SCHEMA.get(key)
            if expected is None:
                continue
            if not isinstance(value, expected):
                continue
            numeric_types = (int, float)
            if isinstance(value, bool) and (
                expected in numeric_types
                or (isinstance(expected, tuple) and any(t in numeric_types for t in expected))
            ):
                continue
            if key == "presetPaths":
                value = SettingsStore._clean_preset_paths(cast(list[object], value))
            if key == "mergeOutputFormat":
                value = str(value).strip().lower()
                if not value:
                    value = DEFAULT_SETTINGS["mergeOutputFormat"]
            if key == "downloadDir" and not str(value).strip():
                continue
            if key in {
                "rateLimit",
                "concurrentFragments",
                "retries",
                "maxConcurrentDownloads",
                "aria2NextConnections",
                "aria2NextMaxConcurrent",
                "torrentListenPort",
                "torrentMaxConnections",
                "torrentUploadLimit",
                "torrentMetadataTimeout",
            }:
                if not isinstance(value, (int, float)) or value < 0:
                    continue
                if key in {"concurrentFragments", "retries", "maxConcurrentDownloads", "aria2NextConnections", "aria2NextMaxConcurrent", "torrentMaxConnections", "torrentMetadataTimeout"}:
                    if value < 1:
                        continue
                if key == "torrentListenPort" and not 1 <= value <= 65535:
                    continue
            clean[key] = value
        return clean

    def get(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._settings)

    def update(self, partial: dict[str, Any]) -> dict[str, Any]:
        """Merge a validated partial update, persist, and notify subscribers."""
        clean = self._validate(partial)
        if not clean:
            log.warning("Settings update empty after validation", requested_keys=list(partial.keys()) if isinstance(partial, dict) else [])
            return self.get()
        with self._lock:
            self._settings.update(clean)
            snapshot = copy.deepcopy(self._settings)
        log.info("Settings updated", changed_keys=list(clean.keys()))
        self._persist(snapshot)
        for callback in self._subscribers:
            try:
                callback(snapshot)
            except Exception:  # noqa: BLE001 - never let a subscriber kill the store
                log.exception("Settings subscriber failed")
        return snapshot

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._subscribers.append(callback)
