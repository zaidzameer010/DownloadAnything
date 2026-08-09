"""libtorrent-backed magnet probing and torrent job management."""

from __future__ import annotations

import asyncio
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from send2trash import send2trash
import orjson
from pathlib import Path
from typing import Any, cast

try:
    import libtorrent as lt
except ImportError:  # Keep the backend importable with a useful runtime error.
    lt = None  # type: ignore[assignment]

import structlog

try:
    from .db import Database, app_support_dir
    from .settings import SettingsStore
except ImportError:
    from db import Database, app_support_dir
    from settings import SettingsStore

from urllib.parse import unquote

log = structlog.get_logger("da.torrent")


def is_magnet_url(url: str) -> bool:
    return url.strip().lower().startswith("magnet:?")


def _clean_torrent_path(target: str) -> str:
    path_str = unquote(target[7:]) if target.startswith("file://") else target
    return path_str.strip()


def is_torrent_input(url: str) -> bool:
    if is_magnet_url(url):
        return True
    path_str = _clean_torrent_path(url)
    return path_str.lower().endswith(".torrent") or (Path(path_str).is_file() and path_str.lower().endswith(".torrent"))


def _require_libtorrent() -> Any:
    if lt is None:
        raise RuntimeError(
            "libtorrent is not installed in the backend environment; install the "
            "Python 2.x bindings in the downloadanything Conda environment"
        )
    return lt


_VIDEO_EXTS = frozenset({
    "mp4", "mkv", "mp3", "avi", "mov", "webm", "m4v", "mpeg",
})


def _is_video_file(path: str) -> bool:
    return Path(path).suffix.lstrip(".").lower() in _VIDEO_EXTS


def _format_hash(value: Any) -> str:
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return ""


def _tracker_url(tracker: Any) -> str:
    if isinstance(tracker, dict):
        tracker_dict = cast(dict[str, Any], tracker)
        return str(tracker_dict.get("url") or "")
    return str(getattr(tracker, "url", tracker) or "")


def _torrent_summary(ti: Any, magnet: str) -> dict[str, Any]:
    files = ti.files()
    file_rows = [
        {
            "path": str(files.file_path(index)),
            "size": int(files.file_size(index)),
        }
        for index in range(files.num_files())
    ]
    trackers = [_tracker_url(tracker) for tracker in ti.trackers()]
    trackers = [tracker for tracker in trackers if tracker]
    return {
        "name": str(ti.name() or "Torrent"),
        "infoHash": _format_hash(ti.info_hash()),
        "totalSize": int(ti.total_size()),
        "fileCount": len(file_rows),
        "files": file_rows,
        "trackers": trackers,
        "magnet": magnet,
    }


class TorrentJob:
    """Mutable state for one libtorrent download."""

    def __init__(
        self,
        url: str,
        directory: str,
        *,
        filename: str | None = None,
        title: str = "",
        torrent_name: str = "",
        selected_files: list[str] | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.url = url
        self.directory = directory
        self.custom_filename = filename
        self.selected_files = set(selected_files) if selected_files is not None else None
        self.handle: Any | None = None
        self.cancel_event = threading.Event()
        self.pause_requested = False
        self.event_revision = 0
        self.data: dict[str, Any] = {
            "id": self.id,
            "url": url,
            "title": title or torrent_name,
            "filename": filename or torrent_name,
            "thumbnail": "",
            "directory": directory,
            "engine": "torrent",
            "mediaType": "torrent",
            "formatSelector": None,
            "customFilename": filename,
            "selectedFiles": sorted(self.selected_files) if self.selected_files is not None else None,
            "torrentName": torrent_name,
            "status": "queued",
            "percent": 0.0,
            "downloaded": 0,
            "total": None,
            "speed": None,
            "eta": None,
            "peers": 0,
            "error": None,
            "createdAt": time.time(),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> TorrentJob:
        selected_files = row.get("selected_files")
        if isinstance(selected_files, str):
            try:
                selected_files = orjson.loads(selected_files)
            except orjson.JSONDecodeError:
                selected_files = None
        if not isinstance(selected_files, list):
            selected_files = None
        job = cls(
            row["url"],
            row["directory"],
            filename=row.get("custom_filename"),
            title=row.get("title") or "",
            selected_files=selected_files,
        )
        job.id = row["id"]
        job.data.update(
            id=job.id,
            status=row["status"],
            percent=float(row.get("percent") or 0),
            downloaded=int(row.get("downloaded") or 0),
            total=row.get("total"),
            error=row.get("error"),
            title=row.get("title") or "",
            filename=row.get("custom_filename") or "",
            createdAt=row.get("created_at") or time.time(),
        )
        return job

    def snapshot(self) -> dict[str, Any]:
        snapshot = dict(self.data)
        snapshot["revision"] = self.event_revision
        return snapshot


class TorrentManager:
    """Own the libtorrent session and expose the backend job-manager contract."""

    def __init__(self, settings_store: SettingsStore, db: Database, loop: asyncio.AbstractEventLoop) -> None:
        module = _require_libtorrent()
        self._settings_store = settings_store
        self._db = db
        self._loop = loop
        self._session = module.session(self._session_settings(settings_store.get()))
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, settings_store.get()["maxConcurrentDownloads"]),
            thread_name_prefix="torrent",
        )
        self._jobs: dict[str, TorrentJob] = {}
        self._lock = threading.RLock()
        self._session_lock = threading.RLock()
        self._listeners: list[Any] = []
        self._worker_futures: dict[str, Any] = {}
        self._db_tasks: dict[str, asyncio.Task[Any]] = {}
        self._last_emit: dict[str, float] = {}
        self._probe_root = app_support_dir() / "torrent-probes"
        self._probe_root.mkdir(parents=True, exist_ok=True)
        settings_store.subscribe(self._on_settings_changed)
        log.info("libtorrent session ready", version=getattr(module, "version", "unknown"))

    @staticmethod
    def _session_settings(settings: dict[str, Any]) -> dict[str, Any]:
        port = int(settings.get("torrentListenPort") or 6881)
        return {
            # Listen on both IPv4 and IPv6 to maximize peer reachability.
            "listen_interfaces": f"0.0.0.0:{port},[::]:{port}",
            "enable_dht": bool(settings.get("torrentEnableDht", True)),
            "enable_lsd": bool(settings.get("torrentEnableLsd", True)),
            "enable_upnp": bool(settings.get("torrentEnableUpnp", True)),
            "enable_natpmp": bool(settings.get("torrentEnableNatpmp", True)),
            "connections_limit": int(settings.get("torrentMaxConnections") or 200),
            "upload_rate_limit": int(settings.get("torrentUploadLimit") or 0),
            # High-performance session tuning (see libtorrent tuning docs):
            # prefer TCP over uTP, connect faster to more peers, keep larger
            # peer/disk buffers, and announce to all trackers/tiers.
            "mixed_mode_algorithm": 0,  # settings_pack::prefer_tcp
            "connection_speed": 200,
            "max_peerlist_size": 10000,
            "file_pool_size": 100,
            "aio_threads": 20,
            "max_out_request_queue": 1000,
            "max_peer_recv_buffer_size": 5 * 1024 * 1024,
            "alert_queue_size": 10000,
            "max_queued_disk_bytes": 200 * 1024 * 1024,
            "peer_timeout": 60,
            "inactivity_timeout": 300,
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
            "dht_announce_interval": 300,
            "max_pex_peers": 100,
            "send_buffer_watermark": 1024 * 1024,
        }

    def _torrent_params(self, magnet: str, save_path: Path) -> Any:
        module = _require_libtorrent()
        clean_path = _clean_torrent_path(magnet)
        if Path(clean_path).is_file():
            info = module.torrent_info(str(clean_path))
            params = module.add_torrent_params()
            params.ti = info
            params.save_path = str(save_path)
        else:
            params = module.parse_magnet_uri(magnet)
            params.save_path = str(save_path)
        if not self._settings_store.get().get("torrentEnablePex", True):
            params.flags |= module.torrent_flags.disable_pex
        return params

    def _on_settings_changed(self, settings: dict[str, Any]) -> None:
        try:
            self._session.apply_settings(self._session_settings(settings))
        except Exception:  # noqa: BLE001
            log.exception("Could not apply libtorrent settings")

    def add_listener(self, callback: Any) -> None:
        self._listeners.append(callback)

    async def _dispatch_listener(self, listener: Any, snapshot: dict[str, Any]) -> None:
        try:
            await listener(snapshot)
        except Exception:  # noqa: BLE001
            log.exception("Torrent job listener failed")

    def _persist(self, job: TorrentJob) -> None:
        snapshot = job.snapshot()

        async def write() -> None:
            if not job.data.get("removed"):
                await self._db.upsert_job(snapshot)

        def schedule() -> None:
            previous = self._db_tasks.get(job.id)

            async def run() -> None:
                if previous is not None:
                    await asyncio.gather(previous, return_exceptions=True)
                await write()

            task = asyncio.ensure_future(run())
            self._db_tasks[job.id] = task
            task.add_done_callback(lambda done: self._db_tasks.pop(job.id, None) if self._db_tasks.get(job.id) is done else None)

        self._loop.call_soon_threadsafe(schedule)

    def _delete_persisted(self, job_id: str) -> None:
        async def delete() -> None:
            await self._db.delete_job(job_id)

        self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(delete()))

    def _emit(self, job: TorrentJob, *, force: bool = False) -> None:
        with self._lock:
            if job.data.get("removed"):
                return
            now = time.monotonic()
            if not force and now - self._last_emit.get(job.id, 0) < 0.1:
                return
            self._last_emit[job.id] = now
            job.event_revision += 1
            snapshot = job.snapshot()
            if force:
                self._persist(job)
            listeners = list(self._listeners)

        def dispatch() -> None:
            for listener in listeners:
                asyncio.ensure_future(self._dispatch_listener(listener, snapshot))

        self._loop.call_soon_threadsafe(dispatch)

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job.snapshot() for job in self._jobs.values()]

    def get(self, job_id: str) -> TorrentJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def restore(self, rows: list[dict[str, Any]]) -> None:
        for row in reversed(rows):
            if row.get("engine") != "torrent":
                continue
            if row["status"] in {"queued", "downloading", "postprocessing"}:
                row["status"] = "paused"
            job = TorrentJob.from_row(row)
            with self._lock:
                self._jobs[job.id] = job
            self._persist(job)
        if any(row.get("engine") == "torrent" for row in rows):
            log.info("Restored torrent jobs")

    def probe(self, magnet: str) -> dict[str, Any]:
        module = _require_libtorrent()
        target = magnet.strip()

        clean_path = _clean_torrent_path(target)
        if Path(clean_path).is_file() or clean_path.lower().endswith(".torrent"):
            file_path = Path(clean_path)
            if not file_path.exists():
                return {"ok": False, "engine": "torrent", "error": f"Torrent file not found: {clean_path}"}
            try:
                info = module.torrent_info(str(file_path))
                summary = _torrent_summary(info, target)
                return {"ok": True, "engine": "torrent", "url": target, "torrent": summary}
            except Exception as exc:  # noqa: BLE001
                log.warning("Torrent file probe failed", file=clean_path, error=str(exc))
                return {"ok": False, "engine": "torrent", "error": str(exc) or "Torrent probe failed"}

        if not is_magnet_url(target):
            return {"ok": False, "engine": "none", "error": "Not a magnet URL or .torrent file"}

        probe_id = uuid.uuid4().hex
        probe_path = self._probe_root / probe_id
        probe_path.mkdir(parents=True, exist_ok=True)
        handle = None
        try:
            params = self._torrent_params(target, probe_path)
            with self._session_lock:
                handle = self._session.add_torrent(params)
            timeout = max(5, int(self._settings_store.get().get("torrentMetadataTimeout") or 90))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if handle.has_metadata():
                    info = handle.get_torrent_info()
                    return {"ok": True, "engine": "torrent", "url": target, "torrent": _torrent_summary(info, target)}
                status = handle.status()
                if getattr(status, "error", None):
                    return {"ok": False, "engine": "torrent", "error": str(status.error)}
                time.sleep(0.25)
            return {"ok": False, "engine": "torrent", "error": "Timed out while fetching torrent metadata"}
        except Exception as exc:  # noqa: BLE001
            log.warning("Torrent metadata probe failed", error=str(exc))
            return {"ok": False, "engine": "torrent", "error": str(exc) or "Torrent probe failed"}
        finally:
            if handle is not None:
                try:
                    with self._session_lock:
                        self._session.remove_torrent(handle)
                except Exception:  # noqa: BLE001
                    log.debug("Could not remove metadata probe torrent", exc_info=True)
            shutil.rmtree(probe_path, ignore_errors=True)

    def resolve_duplicate(self, url: str, directory: str, action: str | None = None) -> dict[str, Any]:
        with self._lock:
            duplicate = next(
                (
                    job for job in self._jobs.values()
                    if job.url == url
                    and job.directory == directory
                    and job.data["status"] in {"queued", "downloading", "paused", "postprocessing"}
                ),
                None,
            )
        if duplicate is None:
            return {"status": "ok", "filename": None}
        if action == "skip":
            return {"status": "ok", "skipped": True}
        if action == "override":
            self.remove(duplicate.id)
            return {"status": "ok", "filename": None}
        if action == "rename":
            return {"status": "ok", "filename": None}
        return {
            "status": "duplicate",
            "type": "job",
            "existing": duplicate.snapshot(),
            "filename": duplicate.data.get("filename") or duplicate.data.get("title") or "Torrent",
            "suggestedName": f"{duplicate.data.get('title') or 'Torrent'} (1)",
        }

    def start(
        self,
        url: str,
        directory: str,
        *,
        filename: str | None = None,
        title: str = "",
        selected_files: list[str] | None = None,
    ) -> TorrentJob:
        job = TorrentJob(url, directory, filename=filename, title=title, selected_files=selected_files)
        with self._lock:
            self._jobs[job.id] = job
        self._emit(job, force=True)
        future = self._executor.submit(self._run, job)
        with self._lock:
            self._worker_futures[job.id] = future
        future.add_done_callback(lambda done: self._worker_futures.pop(job.id, None) if self._worker_futures.get(job.id) is done else None)
        return job

    def _run(self, job: TorrentJob) -> None:
        module = _require_libtorrent()
        handle = None
        try:
            Path(job.directory).expanduser().mkdir(parents=True, exist_ok=True)
            params = self._torrent_params(job.url, Path(job.directory).expanduser())
            with self._session_lock:
                handle = self._session.add_torrent(params)
            job.handle = handle
            with self._lock:
                job.data["status"] = "downloading"
            self._emit(job, force=True)
            priorities_applied = False
            streaming_applied = False

            while True:
                if job.cancel_event.is_set():
                    self._remove_handle(handle, delete_files=False)
                    with self._lock:
                        job.data.update(status="paused" if job.pause_requested else "cancelled", speed=None, eta=None)
                    self._emit(job, force=True)
                    return

                status = handle.status()
                if handle.has_metadata():
                    info = handle.get_torrent_info()
                    if job.selected_files is not None and not priorities_applied:
                        files = info.files()
                        priorities = [1 if files.file_path(index) in job.selected_files else 0 for index in range(files.num_files())]
                        handle.prioritize_files(priorities)
                        priorities_applied = True
                    if not streaming_applied:
                        self._apply_streaming_priorities(handle, info, job)
                        streaming_applied = True
                    if not job.data.get("torrentName"):
                        job.data["torrentName"] = str(info.name() or "Torrent")
                        job.data["title"] = job.data["torrentName"]
                        job.data["filename"] = job.data["torrentName"]
                    total = int(info.total_size())
                else:
                    total = int(getattr(status, "total_wanted", 0) or 0) or None
                downloaded = int(getattr(status, "total_done", 0) or 0)
                rate = int(getattr(status, "download_payload_rate", 0) or 0)
                percent = (downloaded / total * 100) if total else float(getattr(status, "progress", 0) or 0) * 100
                eta = int((total - downloaded) / rate) if total and rate > 0 and downloaded < total else None
                with self._lock:
                    job.data.update(
                        percent=max(0.0, min(100.0, percent)),
                        downloaded=downloaded,
                        total=total,
                        speed=rate or None,
                        eta=eta,
                        peers=int(getattr(status, "num_peers", 0) or 0),
                    )
                self._emit(job)
                if getattr(status, "is_seeding", False) or getattr(status, "is_finished", False):
                    self._remove_handle(handle, delete_files=False)
                    with self._lock:
                        job.data.update(status="completed", percent=100.0, speed=None, eta=None)
                    self._emit(job, force=True)
                    return
                if getattr(status, "error", None):
                    raise RuntimeError(str(status.error))
                time.sleep(0.25)
        except Exception as exc:  # noqa: BLE001
            if handle is not None:
                self._remove_handle(handle, delete_files=False)
            with self._lock:
                if job.cancel_event.is_set():
                    job.data.update(status="paused" if job.pause_requested else "cancelled", speed=None, eta=None)
                else:
                    job.data.update(status="failed", error=str(exc) or "Torrent download failed", speed=None, eta=None)
            self._emit(job, force=True)
        finally:
            job.handle = None

    def _apply_streaming_priorities(self, handle: Any, info: Any, job: TorrentJob) -> None:
        """Enable sequential download and boost first/last pieces of video files."""
        files = info.files()
        selected = job.selected_files
        video_files: list[int] = []
        for index in range(files.num_files()):
            path = str(files.file_path(index))
            if selected is not None and path not in selected:
                continue
            if _is_video_file(path):
                video_files.append(index)
        if not video_files:
            return

        module = _require_libtorrent()
        try:
            handle.set_flags(module.torrent_flags.sequential_download)
        except AttributeError:
            try:
                handle.set_sequential_download(True)
            except Exception:  # noqa: BLE001
                log.debug("Could not enable sequential download")

        for index in video_files:
            size = int(files.file_size(index))
            if size <= 0:
                continue
            first_piece = info.map_file(index, 0, 1).piece
            last_piece = info.map_file(index, size - 1, 1).piece
            for piece in {first_piece, last_piece}:
                try:
                    handle.piece_priority(piece, 7)
                except Exception:  # noqa: BLE001
                    log.debug("Could not set piece priority", piece=piece)

    def _remove_handle(self, handle: Any, *, delete_files: bool) -> None:
        module = _require_libtorrent()
        flags = module.options_t.delete_files if delete_files else 0
        try:
            with self._session_lock:
                self._session.remove_torrent(handle, flags)
        except Exception:  # noqa: BLE001
            log.debug("Could not remove torrent handle", exc_info=True)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.data["status"] not in {"queued", "paused", "downloading", "postprocessing"}:
            return False
        job.cancel_event.set()
        if job.data["status"] in {"queued", "paused"}:
            with self._lock:
                job.data.update(status="cancelled", speed=None, eta=None)
            self._emit(job, force=True)
        return True

    def pause(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.data["status"] not in {"queued", "downloading"}:
            return False
        job.pause_requested = True
        job.cancel_event.set()
        return True

    def resume(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.data["status"] not in {"paused", "failed", "cancelled"}:
            return False
        job.cancel_event.clear()
        job.pause_requested = False
        with self._lock:
            job.data.update(status="queued", error=None, speed=None, eta=None)
        self._emit(job, force=True)
        future = self._executor.submit(self._run, job)
        with self._lock:
            self._worker_futures[job.id] = future
        return True

    def _storage_path(self, job: TorrentJob, handle: Any | None = None) -> Path | None:
        directory = Path(job.directory).expanduser()
        torrent_name = str(job.data.get("torrentName") or job.data.get("filename") or "").strip()
        if handle is not None:
            try:
                directory = Path(handle.save_path()).expanduser()
            except Exception:  # noqa: BLE001
                log.debug("Could not read torrent save path", exc_info=True)
            try:
                torrent_name = str(handle.get_torrent_info().name() or torrent_name).strip()
            except Exception:  # noqa: BLE001
                log.debug("Could not read torrent name before removal", exc_info=True)
        if not torrent_name:
            return None
        return directory / Path(torrent_name).name

    def storage_path(self, job: TorrentJob) -> Path | None:
        """Return the on-disk storage path for a torrent job."""
        return self._storage_path(job, job.handle)

    @staticmethod
    def _trash_storage(path: Path | None) -> None:
        if path is None or not path.exists():
            return
        try:
            send2trash(str(path))
        except OSError:
            log.exception("Could not move incomplete torrent storage to trash", path=str(path))

    def remove(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        status = job.data["status"]
        handle = job.handle
        storage_path = self._storage_path(job, handle) if status != "completed" else None
        job.cancel_event.set()
        if handle is not None:
            self._remove_handle(handle, delete_files=False)
        self._trash_storage(storage_path)
        with self._lock:
            job.data["removed"] = True
            self._jobs.pop(job_id, None)
        self._delete_persisted(job_id)
        return True

    def clear_finished(self) -> list[str]:
        with self._lock:
            ids = [job_id for job_id, job in self._jobs.items() if job.data["status"] == "completed"]
        for job_id in ids:
            self.remove(job_id)
        return ids

    async def shutdown(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.data["status"] in {"queued", "downloading", "postprocessing"}:
                # Treat shutdown as a pause so jobs can resume after restart.
                job.pause_requested = True
                job.cancel_event.set()
        await self._loop.run_in_executor(None, lambda: self._executor.shutdown(wait=True, cancel_futures=True))
        with self._session_lock:
            self._session.pause()
        pending = list(self._db_tasks.values())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
