from __future__ import annotations

import mimetypes
import os
import re
import threading
import time
import tempfile
import shutil
from pathlib import Path
from typing import Any, Optional

from app.domain.exceptions import DownloadPaused
from app.engine.file_types import FILE_TYPE_VIDEO, classify_mime
from app.schemas.settings import AppSettings
from app.engine.jobs import jobs_registry
from app.services.interfaces import IDownloadEngine, IProbeEngine
from app.utils.logger import get_logger

logger = get_logger(__name__)

_MAGNET_RE = re.compile(r"^magnet:\?", re.IGNORECASE)

# Shared libtorrent session to avoid binding the DHT listen port for every
# torrent and to reuse the DHT routing table across jobs.
_shared_lt_session: Any | None = None
_shared_lt_lock = threading.Lock()


class TorrentProber(IProbeEngine):
    """Resolve magnet metadata without downloading payload data."""

    def probe(
        self,
        job_id: str,
        url: str,
        settings: AppSettings,
        referer: Optional[str] = None,
        page_title: Optional[str] = None,
        mime_hint: Optional[str] = None,
    ) -> dict[str, Any]:
        return probe_magnet(url)


class TorrentDownloader(IDownloadEngine):
    """Download a magnet link using libtorrent."""

    def download(self, job_id: str, url: str, output_dir: Path, **kwargs: Any) -> str:
        loop = kwargs.get("loop")
        event_queue = kwargs.get("event_queue")
        selected_files = kwargs.get("selected_files")
        settings = kwargs.get("settings")
        if settings is None:
            raise RuntimeError("TorrentDownloader requires 'settings'")
        return download_torrent(
            job_id=job_id,
            url=url,
            output_dir=str(output_dir),
            loop=loop,
            event_queue=event_queue,
            selected_files=selected_files,
            settings=settings,
        )


def is_magnet_url(url: str) -> bool:
    return bool(_MAGNET_RE.match(url.strip()))


def _import_libtorrent() -> Any:
    try:
        import libtorrent as lt
    except ImportError as error:
        raise RuntimeError(
            "Torrent support requires the libtorrent Python bindings."
        ) from error
    return lt


def _build_session_params(lt: Any) -> Any:
    """Build a high-performance libtorrent 2.0 session parameter pack.

    Uses the high_performance_seed preset as a base and overrides the settings
    that matter for a downloader: active queue limits, socket buffers, request
    queues, and peer discovery.
    """
    try:
        base = lt.high_performance_seed()
    except AttributeError:
        base = {}

    settings: dict[str, Any] = dict(base)
    settings.update(
        {
            "user_agent": "DownloadAnything (libtorrent)",
            "listen_interfaces": "0.0.0.0:6881,[::]:6881",
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
            "dht_bootstrap_nodes": "dht.libtorrent.org:25401,router.bittorrent.com:6881,router.utorrent.com:6881,dht.transmissionbt.com:6881",
            "alert_mask": int(
                lt.alert.category_t.error_notification
                | lt.alert.category_t.status_notification
                | lt.alert.category_t.storage_notification
                | lt.alert.category_t.progress_notification
            ),
            # Let the router.py concurrency limits control queueing.
            "active_downloads": -1,
            "active_seeds": -1,
            "active_limit": 20000,
            # Socket / buffer tuning for high-throughput links.
            "recv_socket_buffer_size": 2 * 1024 * 1024,
            "send_socket_buffer_size": 2 * 1024 * 1024,
            "send_buffer_watermark": 5 * 1024 * 1024,
            "send_buffer_low_watermark": 1024 * 1024,
            "send_buffer_watermark_factor": 200,
            "max_out_request_queue": 5000,
            "max_allowed_in_request_queue": 5000,
            "file_pool_size": 200,
            "aio_threads": max(4, (os.cpu_count() or 4) * 2),
            "mixed_mode_algorithm": int(lt.bandwidth_mixed_algo_t.prefer_tcp),
        }
    )

    params = lt.session_params()
    params.settings.update(settings)
    return params


def _get_shared_session() -> Any:
    global _shared_lt_session
    with _shared_lt_lock:
        if _shared_lt_session is None:
            lt = _import_libtorrent()
            _shared_lt_session = lt.session(_build_session_params(lt))
        return _shared_lt_session


def _torrent_files(
    info: Any, selected_indices: set[int] | None = None
) -> list[dict[str, Any]]:
    file_storage = info.files()
    files: list[dict[str, Any]] = []
    for index in range(file_storage.num_files()):
        if selected_indices is not None and index not in selected_indices:
            continue
        files.append(
            {
                "index": index,
                "path": str(file_storage.file_path(index)),
                "size": int(file_storage.file_size(index)),
                "priority": 4,
            }
        )
    return files


def _torrent_metadata(
    handle: Any, selected_indices: set[int] | None = None
) -> dict[str, Any]:
    info = handle.torrent_file()
    files = _torrent_files(info, selected_indices)
    status = handle.status()
    info_hash = str(status.info_hash)
    return {
        "name": info.name(),
        "files": files,
        "totalSize": sum(file["size"] for file in files),
        "pieceLength": int(info.piece_length()),
        "pieceCount": int(info.num_pieces()),
        "infoHash": info_hash,
    }


def _safe_torrent_output_path(output_root: Path, torrent_name: str) -> Path:
    root = output_root.resolve()
    candidate = (root / torrent_name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise RuntimeError("Torrent metadata points outside the selected output directory") from error
    return candidate


def probe_magnet(url: str, timeout: float = 30.0) -> dict[str, Any]:
    """Resolve magnet metadata without downloading payload data."""
    if not is_magnet_url(url):
        raise ValueError("Not a magnet URI")

    lt = _import_libtorrent()
    session = _get_shared_session()
    params = lt.parse_magnet_uri(url)
    temp_dir = tempfile.mkdtemp()
    params.save_path = temp_dir
    params.flags |= lt.torrent_flags.duplicate_is_error
    params.flags |= lt.torrent_flags.default_dont_download
    handle = session.add_torrent(params)
    deadline = time.monotonic() + timeout

    try:
        while not handle.has_metadata():
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out while resolving torrent metadata")
            for alert in session.pop_alerts():
                if isinstance(alert, lt.torrent_error_alert):
                    raise RuntimeError(alert.message())
            time.sleep(0.05)
        return _torrent_metadata(handle)
    finally:
        try:
            session.remove_torrent(handle, option=lt.options_t.delete_files)
        except Exception as exc:
            logger.warning(f"Failed to remove torrent handle during magnet probe cleanup: {exc}")
        try:
            shutil.rmtree(temp_dir)
        except Exception as exc:
            logger.warning(f"Failed to remove temp directory {temp_dir} during magnet probe cleanup: {exc}")


_VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".mkv",
        ".avi",
        ".mov",
        ".wmv",
        ".flv",
        ".webm",
        ".m4v",
        ".mpg",
        ".mpeg",
        ".ts",
        ".m2ts",
        ".m3u8",
    }
)


def _is_video_path(path: str) -> bool:
    """Return True when a torrent file path looks like a video container."""
    lower_path = path.lower()
    if any(lower_path.endswith(ext) for ext in _VIDEO_EXTENSIONS):
        return True
    mime, _ = mimetypes.guess_type(path)
    return classify_mime(mime) == FILE_TYPE_VIDEO


def _torrent_primary_video_index(
    file_storage: Any, selected_indices: set[int]
) -> int | None:
    """Find the largest selected video file and return its index."""
    primary_index: int | None = None
    primary_size = 0
    for index in selected_indices:
        if index < 0 or index >= file_storage.num_files():
            continue
        path = str(file_storage.file_path(index))
        size = int(file_storage.file_size(index))
        if size > primary_size and _is_video_path(path):
            primary_index = index
            primary_size = size
    return primary_index


def _apply_streaming_priorities(
    handle: Any, file_storage: Any, file_index: int, num_pieces: int
) -> None:
    """Enable sequential download and prioritize header pieces for streaming.

    Sets libtorrent sequential mode and bumps the first few pieces plus the
    last piece of the target file to maximum priority with tight deadlines.
    """
    try:
        handle.set_sequential_download(True)
    except Exception as exc:
        logger.warning(f"Failed to enable sequential download: {exc}")
        return

    info = handle.torrent_file()
    if info is None:
        return

    file_size = int(file_storage.file_size(file_index))
    try:
        first_piece = info.map_file(file_index, 0, 1).piece
        last_piece = info.map_file(file_index, max(0, file_size - 1), 1).piece
    except Exception as exc:
        logger.warning(f"Failed to map file pieces for streaming: {exc}")
        return

    header_range = range(first_piece, min(first_piece + 5, num_pieces))
    for piece in header_range:
        try:
            handle.piece_priority(piece, 7)
            handle.set_piece_deadline(piece, 1000)
        except Exception as exc:
            logger.warning(f"Failed to set priority/deadline for piece {piece}: {exc}")

    if 0 <= last_piece < num_pieces and last_piece not in header_range:
        try:
            handle.piece_priority(last_piece, 7)
            handle.set_piece_deadline(last_piece, 1000)
        except Exception as exc:
            logger.warning(
                f"Failed to set priority/deadline for last piece {last_piece}: {exc}"
            )


def _apply_torrent_limits(session: Any, handle: Any, settings: Any) -> None:
    """Apply per-torrent rate and peer limits.

    libtorrent's session rate limit is global and would be thrashed by multiple
    concurrent jobs. Per-handle limits keep each torrent isolated.
    """

    def rate_limit(value: int) -> int:
        # Per-torrent handle limit: 0 means unlimited.
        return 0 if value <= 0 else value * 1024

    try:
        handle.set_download_limit(rate_limit(settings.torrentDownloadLimit))
        handle.set_upload_limit(rate_limit(settings.torrentUploadLimit))
    except Exception as exc:
        # Fall back to session-level limits for very old libtorrent bindings.
        logger.warning(
            f"Failed to set per-torrent rate limits; falling back to session-level limits: {exc}"
        )
        session.apply_settings(
            {
                "download_rate_limit": rate_limit(settings.torrentDownloadLimit),
                "upload_rate_limit": rate_limit(settings.torrentUploadLimit),
            }
        )
    handle.set_max_connections(max(1, settings.torrentPeerLimit))
    handle.set_max_uploads(max(1, settings.torrentUploadPeerLimit))


def _raise_if_paused(job_id: str) -> None:
    """Abort the torrent thread if the job has been paused/removed."""
    if jobs_registry.is_paused(job_id) or jobs_registry.get_job(job_id) is None:
        logger.debug(f"Torrent job {job_id} is paused or removed; aborting early.")
        raise DownloadPaused()


def download_torrent(
    job_id: str,
    url: str,
    output_dir: str,
    loop: Any,
    event_queue: Any,
    settings: AppSettings,
    selected_files: list[int] | None = None,
) -> str:
    """Download a magnet synchronously; call from a worker thread."""
    if not is_magnet_url(url):
        raise ValueError("Torrent downloads require a magnet URI")

    _raise_if_paused(job_id)

    lt = _import_libtorrent()
    session = _get_shared_session()
    params = lt.parse_magnet_uri(url)
    params.save_path = str(Path(output_dir).expanduser().resolve())
    params.flags |= lt.torrent_flags.duplicate_is_error
    params.flags |= lt.torrent_flags.default_dont_download
    handle = session.add_torrent(params)
    logger.info(f"Torrent {job_id} added to session")
    metadata: dict[str, Any] | None = None

    try:
        Path(params.save_path).mkdir(parents=True, exist_ok=True)
        # Apply user limits immediately so the torrent starts with the right caps.
        _apply_torrent_limits(session, handle, settings)
        while not handle.has_metadata():
            if jobs_registry.is_paused(job_id):
                handle.pause()
                jobs_registry.update_job(job_id, status="paused")
                while jobs_registry.is_paused(job_id):
                    if jobs_registry.get_job(job_id) is None:
                        raise DownloadPaused()
                    time.sleep(0.5)
                handle.resume()
                _apply_torrent_limits(session, handle, settings)
            if jobs_registry.get_job(job_id) is None:
                raise DownloadPaused()
            for alert in session.pop_alerts():
                if isinstance(alert, lt.torrent_error_alert):
                    raise RuntimeError(alert.message())
            time.sleep(0.05)

        info = handle.torrent_file()
        if info is None:
            raise RuntimeError("Failed to retrieve torrent metadata")
        logger.info(f"Torrent {job_id} metadata resolved: {info.name()}")

        file_storage = info.files()
        num_files = file_storage.num_files()
        selected_set = set(selected_files) if selected_files else set(range(num_files))
        selected_set = {i for i in selected_set if 0 <= i < num_files}
        if not selected_set:
            selected_set = set(range(num_files))

        priorities = [4 if i in selected_set else 0 for i in range(num_files)]
        try:
            handle.prioritize_files(priorities)
        except Exception as exc:
            logger.warning(
                f"handle.prioritize_files failed ({exc}); falling back to per-file priorities"
            )
            try:
                for index, priority in enumerate(priorities):
                    handle.file_priority(index, priority)
            except Exception as inner:
                raise RuntimeError("Could not set torrent file priorities") from inner

        video_index = _torrent_primary_video_index(file_storage, selected_set)
        if video_index is not None:
            logger.info(
                f"Torrent {job_id} selected video file index {video_index} for streaming"
            )
            _apply_streaming_priorities(
                handle, file_storage, video_index, info.num_pieces()
            )

        metadata = _torrent_metadata(handle, selected_set)
        torrent_output_path = _safe_torrent_output_path(
            Path(params.save_path), str(metadata["name"])
        )
        jobs_registry.update_job(
            job_id,
            title=metadata["name"],
            file_path=str(torrent_output_path),
            total_bytes=metadata["totalSize"],
            combined_total_bytes=metadata["totalSize"],
            torrent_files=metadata["files"],
            torrent_info_hash=metadata["infoHash"],
            torrent_piece_length=metadata["pieceLength"],
            torrent_piece_count=metadata["pieceCount"],
        )

        seeding_start_time = None

        while True:
            _apply_torrent_limits(session, handle, settings)
            if jobs_registry.is_paused(job_id):
                handle.pause()
                jobs_registry.update_job(job_id, status="paused")
                while jobs_registry.is_paused(job_id):
                    if jobs_registry.get_job(job_id) is None:
                        raise DownloadPaused()
                    time.sleep(0.5)
                handle.resume()
            if jobs_registry.get_job(job_id) is None:
                raise DownloadPaused()

            for alert in session.pop_alerts():
                if isinstance(alert, lt.torrent_error_alert):
                    raise RuntimeError(alert.message())
                if isinstance(alert, lt.torrent_finished_alert):
                    if alert.handle.info_hash() == handle.info_hash():
                        logger.debug(f"Torrent {job_id} finished alert received")

            status = handle.status(
                lt.torrent_handle.query_accurate_download_counters
                | lt.torrent_handle.query_pieces
            )
            downloaded = int(status.total_wanted_done)
            total = int(status.total_wanted or metadata["totalSize"])

            # A torrent is complete when all wanted payload bytes are
            # downloaded. Counters are authoritative even if libtorrent's
            # is_finished flag is lagging during end-game or piece checking.
            total_wanted = int(status.total_wanted or 0)
            total_wanted_done = int(status.total_wanted_done or 0)
            is_download_complete = total_wanted > 0 and total_wanted_done >= total_wanted

            # If the payload is finished and seeding is disabled, mark completed
            # and exit immediately so the job does not stay stuck.
            if is_download_complete and settings.torrentSeedRatio <= 0:
                logger.info(f"Torrent {job_id} download completed; seeding disabled.")
                break

            if is_download_complete:
                if seeding_start_time is None:
                    seeding_start_time = time.monotonic()
                    logger.info(f"Torrent {job_id} download completed. Starting seeding phase.")

                uploaded = status.all_time_upload
                actual_downloaded = status.all_time_download or total or 1
                current_ratio = uploaded / actual_downloaded

                ratio_limit = settings.torrentSeedRatio
                ratio_limit_met = ratio_limit > 0 and current_ratio >= ratio_limit

                if ratio_limit_met or ratio_limit <= 0:
                    logger.info(f"Torrent {job_id} seeding limits reached. Stopping.")
                    break

                job_status = "seeding"
                speed = int(status.upload_payload_rate)
                progress = 100.0
            else:
                job_status = "downloading"
                speed = int(status.download_payload_rate)
                progress = (downloaded / total * 100) if total else 0.0

            pieces = list(status.pieces or [])
            completed_pieces = sum(1 for piece in pieces if piece)

            if job_status == "seeding":
                eta = 0
            else:
                eta = int((total - downloaded) / speed) if speed > 0 and total > downloaded else 0

            torrent_state = {
                "peers": int(status.num_peers),
                "seeds": int(status.num_seeds),
                "availability": float(status.distributed_copies),
                "completedPieces": completed_pieces,
                "pieceCount": int(metadata["pieceCount"]),
                "ratio": float(status.all_time_upload / (status.all_time_download or total or 1)),
                "seeding": job_status == "seeding",
            }
            jobs_registry.update_job(
                job_id,
                persist=False,
                progress=progress,
                downloaded_bytes=downloaded,
                total_bytes=total,
                combined_downloaded_bytes=downloaded,
                combined_total_bytes=total,
                speed=speed,
                eta=eta,
                status=job_status,
                torrent_peers=torrent_state["peers"],
                torrent_seeds=torrent_state["seeds"],
                torrent_availability=torrent_state["availability"],
                torrent_completed_pieces=completed_pieces,
            )
            loop.call_soon_threadsafe(
                event_queue.put_nowait,
                {
                    "type": "download_progress",
                    "jobId": job_id,
                    "status": job_status,
                    "downloadedBytes": downloaded,
                    "totalBytes": total,
                    "combinedDownloadedBytes": downloaded,
                    "combinedTotalBytes": total,
                    "streamPhase": "single",
                    "speed": speed,
                    "eta": eta,
                    "torrent": torrent_state,
                },
            )
            time.sleep(0.1)

        file_path = str(
            _safe_torrent_output_path(Path(params.save_path), str(metadata["name"]))
        )
        jobs_registry.update_job(
            job_id,
            status="completed",
            progress=100.0,
            file_path=file_path,
            downloaded_bytes=metadata["totalSize"],
            total_bytes=metadata["totalSize"],
            combined_downloaded_bytes=metadata["totalSize"],
            combined_total_bytes=metadata["totalSize"],
        )
        return file_path
    except DownloadPaused:
        logger.info(f"Torrent job {job_id} was successfully paused.")
        jobs_registry.update_job(job_id, status="paused")
        raise
    except Exception as error:
        logger.error(f"Torrent job {job_id} failed: {error}")
        jobs_registry.update_job(job_id, status="failed", error=str(error))
        raise
    finally:
        try:
            session.remove_torrent(handle)
        except Exception as exc:
            logger.warning(f"Failed to remove torrent handle for job {job_id} during cleanup: {exc}")
