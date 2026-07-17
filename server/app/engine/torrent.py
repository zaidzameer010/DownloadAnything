from __future__ import annotations

import re
import time
import tempfile
import shutil
from pathlib import Path
from typing import Any

from app.api.settings import load_settings
from app.engine.jobs import DownloadPaused, jobs_registry
from app.utils.logger import logger

_MAGNET_RE = re.compile(r"^magnet:\?", re.IGNORECASE)


def is_magnet_url(url: str) -> bool:
    return bool(_MAGNET_RE.match(url.strip()))


def _libtorrent() -> Any:
    try:
        import libtorrent as lt
    except ImportError as error:
        raise RuntimeError(
            "Torrent support requires the libtorrent Python bindings."
        ) from error
    return lt


def _torrent_files(info: Any) -> list[dict[str, Any]]:
    file_storage = info.files()
    return [
        {
            "index": index,
            "path": str(file_storage.file_path(index)),
            "size": int(file_storage.file_size(index)),
            "priority": 4,
        }
        for index in range(file_storage.num_files())
    ]


def _torrent_metadata(handle: Any) -> dict[str, Any]:
    info = handle.torrent_file()
    files = _torrent_files(info)
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

    lt = _libtorrent()
    session = lt.session()
    params = lt.parse_magnet_uri(url)
    temp_dir = tempfile.mkdtemp()
    params.save_path = temp_dir
    params.flags |= lt.torrent_flags.duplicate_is_error
    handle = session.add_torrent(params)
    deadline = time.monotonic() + timeout

    try:
        while not handle.has_metadata():
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out while resolving torrent metadata")
            for alert in session.pop_alerts():
                if isinstance(alert, lt.torrent_error_alert):
                    raise RuntimeError(alert.message())
            time.sleep(0.2)
        return _torrent_metadata(handle)
    finally:
        try:
            session.remove_torrent(handle)
        except Exception:
            pass
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass


def _apply_limits(session: Any, handle: Any, settings: Any) -> None:
    def rate_limit(value: int) -> int:
        return -1 if value <= 0 else value * 1024

    session.set_download_rate_limit(rate_limit(settings.torrentDownloadLimit))
    session.set_upload_rate_limit(rate_limit(settings.torrentUploadLimit))
    session.set_max_connections(max(1, settings.torrentPeerLimit))
    handle.set_max_connections(max(1, settings.torrentPeerLimit))
    handle.set_max_uploads(max(1, settings.torrentUploadPeerLimit))
    if settings.torrentSeedRatio > 0:
        handle.set_ratio(float(settings.torrentSeedRatio))


def download_torrent(
    job_id: str,
    url: str,
    output_dir: str,
    loop: Any,
    event_queue: Any,
) -> str:
    """Download a magnet synchronously; call from a worker thread."""
    if not is_magnet_url(url):
        raise ValueError("Torrent downloads require a magnet URI")

    lt = _libtorrent()
    session = lt.session()
    params = lt.parse_magnet_uri(url)
    params.save_path = str(Path(output_dir).expanduser().resolve())
    params.flags |= lt.torrent_flags.duplicate_is_error
    handle = session.add_torrent(params)
    metadata: dict[str, Any] | None = None

    try:
        Path(params.save_path).mkdir(parents=True, exist_ok=True)
        while not handle.has_metadata():
            settings = load_settings()
            if jobs_registry.is_paused(job_id):
                handle.pause()
                jobs_registry.update_job(job_id, status="paused")
                while jobs_registry.is_paused(job_id):
                    if jobs_registry.get_job(job_id) is None:
                        raise DownloadPaused()
                    time.sleep(0.5)
                handle.resume()
                _apply_limits(session, handle, settings)
            if jobs_registry.get_job(job_id) is None:
                raise DownloadPaused()
            for alert in session.pop_alerts():
                if isinstance(alert, lt.torrent_error_alert):
                    raise RuntimeError(alert.message())
            time.sleep(0.2)

        metadata = _torrent_metadata(handle)
        torrent_output_path = _safe_torrent_output_path(
            Path(params.save_path), str(metadata["name"])
        )
        handle.set_sequential_download(True)
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
            settings = load_settings()
            _apply_limits(session, handle, settings)
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

            status = handle.status()
            downloaded = int(status.total_wanted_done)
            total = int(status.total_wanted or metadata["totalSize"])

            is_download_complete = status.is_finished

            if is_download_complete:
                if seeding_start_time is None:
                    seeding_start_time = time.monotonic()
                    logger.info(f"Torrent {job_id} download completed. Starting seeding phase.")

                uploaded = status.all_time_upload
                actual_downloaded = status.all_time_download or total or 1
                current_ratio = uploaded / actual_downloaded

                ratio_limit = settings.torrentSeedRatio
                time_limit_mins = settings.torrentSeedTimeMinutes

                ratio_limit_met = ratio_limit > 0 and current_ratio >= ratio_limit

                seeding_duration_mins = (time.monotonic() - seeding_start_time) / 60.0
                time_limit_met = time_limit_mins > 0 and seeding_duration_mins >= time_limit_mins

                if ratio_limit_met or time_limit_met or (ratio_limit <= 0 and time_limit_mins <= 0):
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
                if time_limit_mins > 0:
                    remaining_mins = max(0.0, time_limit_mins - seeding_duration_mins)
                    eta = int(remaining_mins * 60)
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
            time.sleep(0.5)

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
        jobs_registry.update_job(job_id, status="paused")
        raise
    except Exception as error:
        logger.error(f"Torrent job {job_id} failed: {error}")
        jobs_registry.update_job(job_id, status="failed", error=str(error))
        raise
    finally:
        try:
            session.remove_torrent(handle)
        except Exception:
            pass
