"""libtorrent-backed magnet probing and torrent job management."""

from __future__ import annotations

import asyncio
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from send2trash import send2trash
import orjson
from pathlib import Path
from collections.abc import Sequence
from typing import Any, cast

try:
    import resource
except ImportError:
    resource = None

try:
    import libtorrent as lt
except ImportError:  # Keep the backend importable with a useful runtime error.
    lt = None  # type: ignore[assignment]

import structlog
import urllib.parse

try:
    from .db import Database, app_support_dir
    from .jobbase import JobManager
    from .settings import SettingsStore
except ImportError:
    from db import Database, app_support_dir
    from jobbase import JobManager
    from settings import SettingsStore


def unquote(value: str) -> str:
    """Decode a percent-encoded string with UTF-8 replacement decoding."""
    return urllib.parse.unquote(value, encoding="utf-8", errors="replace")

log = structlog.get_logger("da.torrent")

_DEFAULT_FILE_POOL_SIZE = 100
_DESCRIPTOR_LIMIT_THRESHOLD = 1024
_DESCRIPTOR_RESERVE = 96
_MAX_CONSTRAINED_FILE_POOL_SIZE = 64
# libtorrent's default piece priority; used to restore defaults when the
# sequential playback window is disabled.
_DEFAULT_PIECE_PRIORITY = 4


def _descriptor_budgets(
    settings: dict[str, Any],
    *,
    soft_limit: int | None = None,
) -> tuple[int, int]:
    requested_connections = max(1, int(settings.get("torrentMaxConnections") or 200))
    if soft_limit is None:
        try:
            soft_limit = int(resource.getrlimit(resource.RLIMIT_NOFILE)[0]) if resource else 0
        except (OSError, ValueError):
            soft_limit = 0
    if soft_limit <= 0 or soft_limit >= _DESCRIPTOR_LIMIT_THRESHOLD:
        return requested_connections, _DEFAULT_FILE_POOL_SIZE

    reserve = min(_DESCRIPTOR_RESERVE, max(16, soft_limit // 2))
    available = max(2, soft_limit - reserve)
    file_pool_size = max(1, min(_MAX_CONSTRAINED_FILE_POOL_SIZE, available // 3))
    connection_limit = max(1, min(requested_connections, available - file_pool_size))
    return connection_limit, file_pool_size


def is_magnet_url(url: str) -> bool:
    """Check if a target string is a valid BitTorrent magnet URI."""
    clean = url.strip().strip("'\"")
    return clean.lower().startswith("magnet:?")


def _clean_torrent_path(target: str) -> str:
    """Normalize file URLs or quoted string paths pointing to local torrent files."""
    clean = target.strip().strip("'\"")
    path_str = unquote(clean[7:]) if clean.startswith("file://") else clean
    return path_str.strip().strip("'\"")


def is_torrent_input(url: str) -> bool:
    """Check if an input string is either a magnet URI or a valid local .torrent file path."""
    clean = url.strip().strip("'\"")
    if is_magnet_url(clean):
        return True
    path_str = _clean_torrent_path(clean)
    return path_str.lower().endswith(".torrent")


def _require_libtorrent() -> Any:
    if lt is None:
        raise RuntimeError(
            "libtorrent is not installed in the backend environment; install the "
            "Python 2.x bindings in the downloadanything Conda environment"
        )
    return lt


_VIDEO_EXTS = frozenset({
    "mp4", "mkv", "avi", "mov", "webm", "m4v", "mpeg", "mpg",
    "ts", "m2ts", "mts", "wmv", "flv", "3gp", "ogv", "vob",
})
_AUDIO_EXTS = frozenset({
    "mp3", "m4a", "flac", "ogg", "wav", "aac", "opus", "wma",
})
# Containers that often keep the index / moov / cues at the end of the file.
_FOOTER_INDEX_EXTS = frozenset({
    "mp4", "mov", "m4v", "m4a", "3gp", "avi", "mkv", "webm",
})
_PLAYBACK_HEADER_BYTES = 4 * 1024 * 1024
_PLAYBACK_FOOTER_BYTES = 2 * 1024 * 1024
_PLAYBACK_WINDOW_BYTES = 16 * 1024 * 1024
_PLAYBACK_DEADLINE_STEP_MS = 50
_PLAYBACK_MAX_WINDOW_PIECES = 48
_PLAYBACK_FILE_DEADLINE_GAP_MS = 15_000


def _file_extension(path: str) -> str:
    return Path(path).suffix.lstrip(".").lower()


def _is_video_file(path: str) -> bool:
    return _file_extension(path) in _VIDEO_EXTS


def _is_playable_file(path: str) -> bool:
    return _is_video_file(path) or _file_extension(path) in _AUDIO_EXTS


def _needs_footer_index(path: str) -> bool:
    return _file_extension(path) in _FOOTER_INDEX_EXTS


def _playable_sort_rank(path: str) -> int:
    ext = _file_extension(path)
    if ext in _VIDEO_EXTS:
        return 0
    if ext in _AUDIO_EXTS:
        return 1
    return 2


def _pieces_for_bytes(byte_count: int, piece_length: int) -> int:
    length = max(1, piece_length)
    return max(1, (max(1, byte_count) + length - 1) // length)


def _piece_is_missing(have_pieces: Sequence[bool], piece: int) -> bool:
    if piece < 0 or piece >= len(have_pieces):
        return True
    return not have_pieces[piece]


def _status_error(status: Any) -> str:
    """Return the torrent error message, preferring the non-deprecated errc."""
    errc = getattr(status, "errc", None)
    if errc is not None and errc.value() != 0:
        return errc.message()
    return str(getattr(status, "error", "") or "")


def _torrent_file_status(
    index: int,
    size: int,
    downloaded: int,
    active_files: set[int],
) -> str:
    if downloaded >= size:
        return "completed"
    if index in active_files:
        return "downloading"
    return "queued"


def _playback_deadline_plan(
    first_piece: int,
    last_piece: int,
    have_pieces: Sequence[bool],
    *,
    header_pieces: int,
    footer_pieces: int,
    window_pieces: int,
    deadline_step_ms: int,
    include_footer: bool,
    deadline_offset_ms: int = 0,
) -> list[tuple[int, int]]:
    """Build time-critical deadlines for a playable prefix, then a moving window.

    libtorrent's sequential_download flag only affects request order and is a
    poor fit for streaming. Deadlines make the time-critical picker try to
    *complete* the header, end-index, and upcoming playback window first.
    """
    if last_piece < first_piece:
        return []

    planned: list[tuple[int, int]] = []
    seen: set[int] = set()
    next_deadline = 0

    def add(piece: int) -> None:
        nonlocal next_deadline
        if (
            piece in seen
            or piece < first_piece
            or piece > last_piece
            or not _piece_is_missing(have_pieces, piece)
        ):
            return
        seen.add(piece)
        planned.append((piece, deadline_offset_ms + next_deadline))
        next_deadline += deadline_step_ms

    header_count = max(1, header_pieces)
    header_end = min(last_piece, first_piece + header_count - 1)
    for piece in range(first_piece, header_end + 1):
        add(piece)

    footer_start = last_piece + 1
    if include_footer and last_piece > header_end:
        footer_count = max(1, footer_pieces)
        footer_start = max(header_end + 1, last_piece - footer_count + 1)
        for piece in range(last_piece, footer_start - 1, -1):
            add(piece)

    cursor = first_piece
    while cursor <= last_piece and cursor < footer_start and not _piece_is_missing(have_pieces, cursor):
        cursor += 1
    cursor = max(cursor, header_end + 1)
    window_end = min(last_piece, cursor + max(1, window_pieces) - 1, footer_start - 1)
    if cursor <= window_end:
        for piece in range(cursor, window_end + 1):
            add(piece)
    return planned


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
        "infoHash": str(ti.info_hash()),
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


@lru_cache(maxsize=64)
def _load_torrent_info(path: str, mtime: float) -> Any:
    """Parse a .torrent file, memoized per (path, mtime)."""
    return _require_libtorrent().torrent_info(path)


class TorrentManager(JobManager):
    """Own the libtorrent session and expose the backend job-manager contract."""

    def __init__(
        self,
        settings_store: SettingsStore,
        db: Database,
        loop: asyncio.AbstractEventLoop,
        work_slots: Any,
        max_workers: int,
    ) -> None:
        module = _require_libtorrent()
        self._settings_store = settings_store
        self._db = db
        self._loop = loop
        self._work_slots = work_slots
        self._session = module.session(self._session_settings(settings_store.get()))
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="torrent",
        )
        self._init_job_state()
        self._session_lock = threading.RLock()
        self._probe_root = app_support_dir() / "torrent-probes"
        self._probe_root.mkdir(parents=True, exist_ok=True)
        settings_store.subscribe(self._on_settings_changed)
        log.info("libtorrent session ready", version=getattr(module, "version", "unknown"))

    @staticmethod
    def _session_settings(settings: dict[str, Any]) -> dict[str, Any]:
        port = int(settings.get("torrentListenPort") or 6881)
        connection_limit, file_pool_size = _descriptor_budgets(settings)
        sess_opts: dict[str, Any] = {
            # Listen on both IPv4 and IPv6 to maximize peer reachability.
            "listen_interfaces": f"0.0.0.0:{port},[::]:{port}",
            "enable_dht": bool(settings.get("torrentEnableDht", True)),
            "enable_lsd": bool(settings.get("torrentEnableLsd", True)),
            "enable_upnp": bool(settings.get("torrentEnableUpnp", True)),
            "enable_natpmp": bool(settings.get("torrentEnableNatpmp", True)),
            "connections_limit": connection_limit,
            "upload_rate_limit": int(settings.get("torrentUploadLimit") or 0),
            "download_rate_limit": int(settings.get("torrentDownloadLimit") or 0),
            # High-performance session tuning (see libtorrent tuning docs):
            # prefer TCP over uTP, connect faster to more peers, keep larger
            # peer/disk buffers, and announce to all trackers/tiers.
            "mixed_mode_algorithm": 0,  # settings_pack::prefer_tcp
            "connection_speed": 500,
            "listen_queue_size": 128,
            "max_peerlist_size": 10000,
            "file_pool_size": file_pool_size,
            "aio_threads": 24,
            "max_out_request_queue": 2000,
            "request_queue_time": 3,
            "recv_socket_buffer_size": 2 * 1024 * 1024,
            "send_socket_buffer_size": 1024 * 1024,
            "max_peer_recv_buffer_size": 10 * 1024 * 1024,
            "alert_queue_size": 10000,
            "max_queued_disk_bytes": 256 * 1024 * 1024,
            "use_disk_read_ahead": True,
            "contiguous_recv_buffer": True,
            "close_redundant_connections": True,
            "allow_multiple_connections_per_ip": True,
            "unchoke_slots_limit": 64,
            "num_optimistic_unchoke_slots": 8,
            "active_downloads": 20,
            "active_limit": 50,
            "active_tracker_limit": 50,
            "active_dht_limit": 50,
            "peer_timeout": 60,
            "inactivity_timeout": 300,
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
            "dht_announce_interval": 120,
            "dht_bootstrap_nodes": (
                "dht.libtorrent.org:25401,router.bittorrent.com:6881,"
                "dht.transmissionbt.com:6881,router.utorrent.com:6881,dht.aelitis.com:6881"
            ),
            "max_pex_peers": 100,
            "send_buffer_watermark": 1024 * 1024,
        }
        if settings.get("torrentForceEncryption"):
            # Force PE encryption (pe_forced = 0)
            sess_opts["out_enc_policy"] = 0
            sess_opts["in_enc_policy"] = 0
        return sess_opts

    def _torrent_params(self, magnet: str, save_path: Path) -> Any:
        module = _require_libtorrent()
        clean_path = _clean_torrent_path(magnet)
        if not is_magnet_url(clean_path) and Path(clean_path).is_file():
            file_path = Path(clean_path)
            info = _load_torrent_info(str(file_path), file_path.stat().st_mtime)
            params = module.add_torrent_params()
            params.ti = info
            params.save_path = str(save_path)
        else:
            params = module.parse_magnet_uri(magnet)
            params.save_path = str(save_path)
        current_settings = self._settings_store.get()
        if not current_settings.get("torrentEnablePex", True):
            params.flags |= module.torrent_flags.disable_pex
        # Sequential-download is a torrent-wide request-order hint and is a
        # poor fit for playable video. Playback uses piece deadlines instead.
        return params

    def _on_settings_changed(self, settings: dict[str, Any]) -> None:
        # SettingsStore invokes subscribers on the event loop; apply_settings is
        # a libtorrent call that can block and contend with worker threads, so
        # it must run off the loop.
        def apply() -> None:
            try:
                with self._session_lock:
                    self._session.apply_settings(self._session_settings(settings))
            except Exception:  # noqa: BLE001
                log.exception("Could not apply libtorrent settings")

        try:
            self._loop.run_in_executor(None, apply)
        except RuntimeError:
            log.debug("Could not apply libtorrent settings: event loop is closed")

    def _emit(self, job: TorrentJob, *, force: bool = False) -> None:
        with self._lock:
            self._emit_core(job, force=force)

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

    def probe(self, magnet: str, cancel_event: threading.Event | None = None) -> dict[str, Any]:
        module = _require_libtorrent()
        target = magnet.strip()

        clean_path = _clean_torrent_path(target)
        if not is_magnet_url(clean_path) and (Path(clean_path).is_file() or clean_path.lower().endswith(".torrent")):
            file_path = Path(clean_path)
            if not file_path.exists():
                return {"ok": False, "engine": "torrent", "error": f"Torrent file not found: {clean_path}"}
            try:
                info = _load_torrent_info(str(file_path), file_path.stat().st_mtime)
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
            # Metadata-only probe: never upload payload while waiting for metadata.
            params.flags |= module.torrent_flags.upload_mode
            with self._session_lock:
                handle = self._session.add_torrent(params)
            timeout = max(5, int(self._settings_store.get().get("torrentMetadataTimeout") or 90))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if cancel_event and cancel_event.is_set():
                    return {"ok": False, "engine": "torrent", "error": "Probe cancelled"}
                if handle.has_metadata():
                    info = handle.get_torrent_info()
                    return {"ok": True, "engine": "torrent", "url": target, "torrent": _torrent_summary(info, target)}
                status = handle.status()
                if _status_error(status):
                    return {"ok": False, "engine": "torrent", "error": _status_error(status)}
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

    @staticmethod
    def _existing_storage_path(directory: str, torrent_name: str | None) -> Path | None:
        name = str(torrent_name or "").strip()
        if not name:
            return None
        path = Path(directory).expanduser() / Path(name).name
        return path if path.exists() else None

    def resolve_duplicate(
        self,
        url: str,
        directory: str,
        action: str | None = None,
        torrent_name: str | None = None,
    ) -> dict[str, Any]:
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

        if duplicate is not None:
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

        existing_storage = self._existing_storage_path(directory, torrent_name)
        if existing_storage is None:
            return {"status": "ok", "filename": None}
        if action == "skip":
            return {"status": "ok", "skipped": True}
        if action == "resume":
            return {"status": "ok", "filename": None}
        if action == "override":
            self._trash_storage(existing_storage)
            return {"status": "ok", "filename": None}
        return {
            "status": "duplicate",
            "type": "torrent",
            "existing": str(existing_storage),
            "filename": str(existing_storage.name),
            "suggestedName": "",
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
        log.info("Torrent download started", job_id=job.id, url=job.url, directory=directory)
        self._submit(job)
        return job

    def _run(self, job: TorrentJob) -> None:
        if not self._acquire_work_slot(job):
            with self._lock:
                if job.pause_requested and job.data["status"] == "queued":
                    job.data.update(status="paused", speed=None, eta=None)
                    self._emit(job, force=True)
            return
        try:
            self._run_with_slot(job)
        finally:
            self._work_slots.release()

    def _run_with_slot(self, job: TorrentJob) -> None:
        handle = None
        try:
            save_path = Path(job.directory).expanduser()
            save_path.mkdir(parents=True, exist_ok=True)
            params = self._torrent_params(job.url, save_path)
            with self._session_lock:
                handle = self._session.add_torrent(params)
            job.handle = handle
            with self._lock:
                job.data["status"] = "downloading"
            self._emit(job, force=True)
            last_piece_priorities: list[int] | None = None
            last_deadline_plan: list[tuple[int, int]] = []
            ordered_cache: tuple[Any, list[int]] | None = None
            last_active_files: list[int] | None = None
            files: Any = None  # file_storage is immutable once metadata arrives

            while True:
                if job.cancel_event.is_set():
                    self._remove_handle(handle)
                    with self._lock:
                        job.data.update(status="paused" if job.pause_requested else "cancelled", speed=None, eta=None)
                    self._emit(job, force=True)
                    return
                status = handle.status()
                files_list: list[dict[str, Any]] = []
                all_selected_complete = False
                if handle.has_metadata():
                    info = handle.get_torrent_info()
                    if files is None:
                        files = info.files()
                    settings = self._settings_store.get()
                    sequential_enabled = bool(settings.get("torrentSequentialDownload", True))
                    schedule_key = sequential_enabled
                    if ordered_cache is None or ordered_cache[0] != schedule_key:
                        ordered_cache = (
                            schedule_key,
                            self._ordered_file_indices(
                                info,
                                job,
                                prioritize_playable=sequential_enabled,
                            ),
                        )
                    ordered_files = ordered_cache[1]
                    if job.selected_files is not None and not ordered_files:
                        raise ValueError("None of the selected torrent files exist")
                    file_prog = handle.file_progress()
                    active_files, last_piece_priorities = self._apply_file_schedule(
                        handle,
                        info,
                        files,
                        file_prog,
                        ordered_files,
                        last_piece_priorities,
                        last_active_files,
                        sequential=sequential_enabled,
                        max_concurrent=max(1, int(settings.get("maxConcurrentDownloads") or 1)),
                    )
                    last_active_files = active_files
                    if sequential_enabled:
                        last_deadline_plan = self._apply_playback_deadlines(
                            handle,
                            info,
                            files,
                            active_files,
                            getattr(status, "pieces", None),
                            last_deadline_plan,
                        )
                    elif last_deadline_plan:
                        self._clear_playback_deadlines(handle)
                        last_deadline_plan = []
                    if not job.data.get("torrentName"):
                        with self._lock:
                            job.data["torrentName"] = str(info.name() or "Torrent")
                            # Preserve a user-supplied custom name; only fall
                            # back to the torrent's own name otherwise.
                            if not job.custom_filename and not job.data.get("title"):
                                job.data["title"] = job.data["torrentName"]
                                job.data["filename"] = job.data["torrentName"]
                    active_file_set = set(active_files)
                    total = 0
                    downloaded = 0
                    all_selected_complete = bool(ordered_files)
                    for i in ordered_files:
                        f_path = str(files.file_path(i))
                        f_size = int(files.file_size(i))
                        f_done = min(f_size, int(file_prog[i]) if i < len(file_prog) else 0)
                        total += f_size
                        downloaded += f_done
                        if f_done < f_size:
                            all_selected_complete = False
                        f_pct = (f_done / f_size * 100.0) if f_size > 0 else 100.0
                        files_list.append({
                            "path": f_path,
                            "filename": Path(f_path).name,
                            "size": f_size,
                            "downloaded": f_done,
                            "percent": max(0.0, min(100.0, f_pct)),
                            "status": _torrent_file_status(i, f_size, f_done, active_file_set),
                        })
                    total = total or None
                    # An all-zero-byte selection must not flip the job to
                    # completed before libtorrent has actually finished it.
                    if not total:
                        all_selected_complete = False
                else:
                    total = int(getattr(status, "total_wanted", 0) or 0) or None
                    downloaded = int(getattr(status, "total_wanted_done", 0) or 0)
                rate = int(getattr(status, "download_payload_rate", 0) or 0)
                percent = (downloaded / total * 100) if total else float(getattr(status, "progress", 0) or 0) * 100
                eta = int((total - downloaded) / rate) if total and rate > 0 and downloaded < total else None
                with self._lock:
                    job_update: dict[str, Any] = dict(
                        percent=max(0.0, min(100.0, percent)),
                        downloaded=downloaded,
                        total=total,
                        speed=rate or None,
                        eta=eta,
                        peers=int(getattr(status, "num_peers", 0) or 0),
                    )
                    if files_list:
                        job_update["files"] = files_list
                    job.data.update(job_update)
                self._emit(job)
                status_error = _status_error(status)
                if status_error:
                    raise RuntimeError(status_error)
                if all_selected_complete:
                    self._remove_handle(handle)
                    with self._lock:
                        job.data.update(status="completed", percent=100.0, speed=None, eta=None)
                    self._emit(job, force=True)
                    log.info("Torrent download completed", job_id=job.id, name=job.data.get("torrentName"))
                    return
                time.sleep(0.25)
        except Exception as exc:  # noqa: BLE001
            if handle is not None:
                self._remove_handle(handle)
            with self._lock:
                if job.cancel_event.is_set():
                    job.data.update(status="paused" if job.pause_requested else "cancelled", speed=None, eta=None)
                else:
                    msg = str(exc) or "Torrent download failed"
                    log.exception("Torrent download failed", job_id=job.id, error=msg)
                    job.data.update(status="failed", error=msg, speed=None, eta=None)
            self._emit(job, force=True)
        finally:
            job.handle = None

    @staticmethod
    def _ordered_file_indices(
        info: Any,
        job: TorrentJob,
        *,
        prioritize_playable: bool = False,
    ) -> list[int]:
        files = info.files()
        selected = job.selected_files
        ordered_files = [
            index
            for index in range(files.num_files())
            if selected is None or str(files.file_path(index)) in selected
        ]
        if prioritize_playable:
            ordered_files.sort(
                key=lambda index: (
                    _playable_sort_rank(str(files.file_path(index))),
                    index,
                )
            )
        return ordered_files

    def _apply_file_schedule(
        self,
        handle: Any,
        info: Any,
        files: Any,
        progress: Sequence[int],
        ordered_files: list[int],
        previous: list[int] | None,
        previous_active: list[int] | None = None,
        *,
        sequential: bool = True,
        max_concurrent: int = 1,
    ) -> tuple[list[int] | None, list[int] | None]:
        """Prioritize pieces for the sequential playback window.

        With ``sequential`` disabled the piece priorities are restored to the
        libtorrent default so every selected file downloads in parallel instead
        of being gated by the active-file window.
        """
        # Deliberately reuses the global maxConcurrentDownloads setting as the
        # per-torrent active-file window size; the cross-engine queue slot
        # itself is shared via the work-slot limiter.
        pending_files = [
            index
            for index in ordered_files
            if int(files.file_size(index)) > int(progress[index] if index < len(progress) else 0)
        ]
        active_files = pending_files[:max_concurrent]
        # Piece priorities depend only on the active file set; skip the O(pieces)
        # rebuild and the libtorrent call while the window is unchanged.
        if previous is not None and active_files == previous_active:
            return active_files, previous
        if not sequential:
            # Sequential mode is off: restore default piece priorities so all
            # selected files download in parallel instead of one window at a time.
            if previous is not None:
                handle.prioritize_pieces([_DEFAULT_PIECE_PRIORITY] * int(info.num_pieces()))
            return active_files, None
        priorities = [0] * int(info.num_pieces())
        for rank, index in enumerate(active_files):
            size = int(files.file_size(index))
            if size <= 0:
                continue
            priority = max(1, 7 - rank)
            first_piece = int(info.map_file(index, 0, 1).piece)
            last_piece = int(info.map_file(index, size - 1, 1).piece)
            for piece in range(max(0, first_piece), min(len(priorities) - 1, last_piece) + 1):
                priorities[piece] = max(priorities[piece], priority)
        if previous != priorities:
            handle.prioritize_pieces(priorities)
        return active_files, priorities

    @staticmethod
    def _clear_playback_deadlines(handle: Any) -> None:
        try:
            handle.clear_piece_deadlines()
        except Exception:  # noqa: BLE001
            log.debug("Could not clear playback piece deadlines")

    def _apply_playback_deadlines(
        self,
        handle: Any,
        info: Any,
        files: Any,
        active_files: list[int],
        have_pieces: Sequence[bool] | None,
        previous: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        """Keep a time-critical header, footer, and moving playback window."""
        piece_length = int(info.piece_length() or 0) or 1
        header_pieces = min(
            _PLAYBACK_MAX_WINDOW_PIECES,
            _pieces_for_bytes(_PLAYBACK_HEADER_BYTES, piece_length),
        )
        footer_pieces = min(
            _PLAYBACK_MAX_WINDOW_PIECES,
            _pieces_for_bytes(_PLAYBACK_FOOTER_BYTES, piece_length),
        )
        window_pieces = min(
            _PLAYBACK_MAX_WINDOW_PIECES,
            max(4, _pieces_for_bytes(_PLAYBACK_WINDOW_BYTES, piece_length)),
        )
        known_pieces: Sequence[bool] = have_pieces or ()
        planned: list[tuple[int, int]] = []
        claimed: set[int] = set()
        for rank, index in enumerate(active_files):
            path = str(files.file_path(index))
            if not _is_playable_file(path):
                continue
            size = int(files.file_size(index))
            if size <= 0:
                continue
            try:
                first_piece = int(info.map_file(index, 0, 1).piece)
                last_piece = int(info.map_file(index, size - 1, 1).piece)
            except (TypeError, ValueError, AttributeError):
                continue
            for piece, deadline in _playback_deadline_plan(
                first_piece,
                last_piece,
                known_pieces,
                header_pieces=header_pieces,
                footer_pieces=footer_pieces,
                window_pieces=window_pieces,
                deadline_step_ms=_PLAYBACK_DEADLINE_STEP_MS,
                include_footer=_needs_footer_index(path),
                deadline_offset_ms=rank * _PLAYBACK_FILE_DEADLINE_GAP_MS,
            ):
                if piece in claimed:
                    continue
                claimed.add(piece)
                planned.append((piece, deadline))
        if planned == previous:
            return previous
        previous_pieces = {piece for piece, _deadline in previous}
        current_pieces = {piece for piece, _deadline in planned}
        stale = previous_pieces - current_pieces
        if len(stale) > 16:
            self._clear_playback_deadlines(handle)
        else:
            for piece in stale:
                try:
                    handle.reset_piece_deadline(piece)
                except Exception:  # noqa: BLE001
                    log.debug("Could not reset playback piece deadline", piece=piece)
        for piece, deadline in planned:
            try:
                handle.set_piece_deadline(piece, deadline)
            except Exception:  # noqa: BLE001
                log.debug("Could not set playback piece deadline", piece=piece)
        return planned

    def _remove_handle(self, handle: Any) -> None:
        module = _require_libtorrent()
        try:
            with self._session_lock:
                self._session.remove_torrent(handle, 0)
        except Exception:  # noqa: BLE001
            log.debug("Could not remove torrent handle", exc_info=True)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.data["status"] not in {"queued", "paused", "downloading", "postprocessing"}:
            return False
        log.info("Cancelling torrent", job_id=job_id)
        job.cancel_event.set()
        self._cancel_queued_worker(job_id)
        with self._lock:
            job.data.update(status="cancelled", speed=None, eta=None)
        self._emit(job, force=True)
        return True

    def pause(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.data["status"] not in {"queued", "downloading"}:
            return False
        status = job.data["status"]
        log.info("Pausing torrent", job_id=job_id, status=status)
        job.pause_requested = True
        job.cancel_event.set()
        if status == "queued":
            self._cancel_queued_worker(job_id)
            with self._lock:
                job.data.update(status="paused", speed=None, eta=None)
            self._emit(job, force=True)
        return True

    def resume(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.data["status"] not in {"paused", "failed", "cancelled"}:
            return False
        log.info("Resuming torrent", job_id=job_id)
        with self._lock:
            existing = self._worker_futures.get(job.id)
        if existing is not None and not existing.done():
            # The old worker is still winding down after a pause and may emit
            # "paused" over the resumed state; wait for it, then restart. Only
            # clear the cancel flags once it has actually exited, or it would
            # keep running alongside the new worker.
            try:
                existing.result(timeout=10)
            except TimeoutError:
                log.warning("Old torrent worker did not stop in time; not restarting", job_id=job_id)
                return True
            except Exception:  # noqa: BLE001
                log.debug("Old torrent worker failed during wind-down", job_id=job_id, exc_info=True)
        if job.data.get("removed"):
            return True
        job.cancel_event.clear()
        job.pause_requested = False
        with self._lock:
            job.data.update(status="queued", error=None, speed=None, eta=None)
        self._emit(job, force=True)
        self._submit(job)
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
        except Exception:  # noqa: BLE001
            log.exception("Could not move incomplete torrent storage to trash", path=str(path))

    def remove(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job:
            return False
        log.info("Removing torrent", job_id=job_id)
        # Mark removed and drop from the in-memory index before cancelling so the
        # worker's final "cancelled" emit is suppressed and the UI does not flicker.
        with self._lock:
            job.data["removed"] = True
            self._jobs.pop(job_id, None)
            # Tests may build the manager without __init__; tolerate missing maps.
            getattr(self, "_last_emit", {}).pop(job_id, None)
            # Keep _db_tasks: _delete_persisted must chain after any in-flight
            # persist, otherwise a late upsert can resurrect the deleted row.
            # Capture state under the lock: the worker may be mutating it.
            status = job.data["status"]
            handle = job.handle
        # Without metadata the real storage name is unknown; trashing a path
        # derived from the user-supplied filename could hit an unrelated file.
        # torrentName is only set once metadata has been seen.
        storage_path = (
            self._storage_path(job, handle)
            if status != "completed" and job.data.get("torrentName")
            else None
        )
        job.cancel_event.set()
        if handle is not None:
            self._remove_handle(handle)
        self._trash_storage(storage_path)
        self._delete_persisted(job_id)
        return True

    def clear_finished(self) -> list[str]:
        with self._lock:
            ids = [job_id for job_id, job in self._jobs.items() if job.data["status"] == "completed"]
        for job_id in ids:
            self.remove(job_id)
        return ids

    async def shutdown(self) -> None:
        def _shutdown_sync() -> None:
            with self._lock:
                jobs = list(self._jobs.values())
            for job in jobs:
                if job.data["status"] in {"queued", "downloading", "postprocessing"}:
                    # Treat shutdown as a pause so jobs can resume after restart.
                    job.pause_requested = True
                    job.cancel_event.set()
            self._executor.shutdown(wait=True, cancel_futures=True)
            with self._session_lock:
                self._session.pause()

        # Lock acquisition and session.pause() can block; keep them off the loop.
        try:
            await asyncio.wait_for(
                self._loop.run_in_executor(None, _shutdown_sync),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            log.warning("Torrent executor shutdown timed out; continuing")
        pending = [*self._listener_tasks, *self._db_tasks.values()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
