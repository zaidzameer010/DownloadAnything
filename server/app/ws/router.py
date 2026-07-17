import asyncio
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, cast

import orjson
import yt_dlp
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter
from app.config import get_app_data_dir, get_app_version, settings
from app.utils.logger import logger
from app.ws.manager import ws_manager
from app.engine.jobs import jobs_registry, DownloadPaused
from app.engine.probe import probe_video
from app.engine.probe_validation import classify_probe_exception
from app.engine.codec_filter import filter_and_summarize_formats
from app.engine.downloader import download_video
from app.engine.title_extractor import resolve_filename
from app.engine.torrent import download_torrent, is_magnet_url, probe_magnet
from app.utils.trash import send_to_trash
from app.schemas.messages import (
    ClientMessage,
    ClientHelloMessage,
    ClientProbeMessage,
    ClientChooseMessage,
    ClientRevealFileMessage,
    ClientPingMessage,
    ClientGetJobsMessage,
    ClientGetCategoriesMessage,
    ClientSaveCategoriesMessage,
    ClientBrowseDirectoryMessage,
    ClientGetSettingsMessage,
    ClientSaveSettingsMessage,
    ClientPauseMessage,
    ClientResumeMessage,
    ClientRemoveJobMessage,
    ClientDeleteFileMessage,
    ClientCancelDownloadMessage,
    ClientCheckFileExistsMessage,
    ClientCancelProbeMessage,
)
from app.api.browse import pick_directory_system
from app.api.settings import load_settings, save_settings_to_file
from app.api.categories import load_categories, save_categories_to_file

router = APIRouter()


@router.get("/ping")
async def ping() -> dict[str, str]:
    return {"status": "ok", "version": get_app_version()}


# Setup Pydantic TypeAdapter for dynamic parsing
message_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)

# Memory store for temporary probes before they are queued as jobs
pending_probes: dict[str, dict[str, object]] = {}
PENDING_PROBE_TTL_SECONDS = 15 * 60


def prune_pending_probes() -> None:
    cutoff = time.time() - PENDING_PROBE_TTL_SECONDS
    expired_ids = {
        probe_id
        for probe_id, metadata in pending_probes.items()
        if float(cast(Any, metadata.get("probeTimestamp")) or 0) < cutoff
    }
    for probe_id in expired_ids:
        pending_probes.pop(probe_id, None)


# Active probe tasks that can be cancelled
active_probe_tasks: dict[str, asyncio.Task[None]] = {}
probe_task_tabs: dict[str, int] = {}

# Active downloader tasks that can be cancelled/aborted
active_downloader_tasks: dict[str, asyncio.Task[None]] = {}
active_torrent_jobs: set[str] = set()
torrent_jobs_lock = asyncio.Lock()

# Concurrency manager for downloads
active_downloads = 0
active_downloads_cond = asyncio.Condition()


async def acquire_download_slot():
    global active_downloads
    async with active_downloads_cond:
        while True:
            settings_data = load_settings()
            limit = settings_data.aria2ConcurrentDownloads
            if active_downloads < limit:
                break
            _ = await active_downloads_cond.wait()
        active_downloads += 1


async def release_download_slot():
    global active_downloads
    async with active_downloads_cond:
        active_downloads = max(0, active_downloads - 1)
        active_downloads_cond.notify_all()


def is_path_within(path: str | Path, root: str | Path) -> bool:
    try:
        Path(path).expanduser().resolve().relative_to(
            Path(root).expanduser().resolve()
        )
        return True
    except (OSError, ValueError):
        return False


async def run_downloader_task(
    tab_id: int,
    job_id: str,
    url: str,
    format_id: str,
    output_dir: str,
    conflict_resolution: str = "replace",
    referer: str | None = None,
):
    _ = tab_id
    task = asyncio.current_task()
    if task is not None:
        active_downloader_tasks[job_id] = cast(asyncio.Task[None], task)
    event_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    # Update status to queued initially
    _ = await asyncio.to_thread(jobs_registry.update_job, job_id, status="queued")

    # Broadcast updated jobs list to clients
    jobs = list((await asyncio.to_thread(jobs_registry.list_jobs)).values())
    await ws_manager.broadcast(
        {"type": "jobs_list", "jobs": [j.model_dump() for j in jobs]}
    )

    # Send download_queued message
    out_dir = output_dir if output_dir else settings.DEFAULT_OUTPUT_DIR
    job = await asyncio.to_thread(jobs_registry.get_job, job_id)
    await ws_manager.broadcast(
        {
            "type": "download_queued",
            "jobId": job_id,
            "outputPath": out_dir,
            "url": url,
            "title": job.title if job else None,
            "duration": job.duration if job else None,
            "thumbnail": job.thumbnail if job else None,
            "uploader": job.uploader if job else None,
        }
    )

    async def consumer():
        while True:
            try:
                event = await event_queue.get()
                await ws_manager.broadcast(event)
                event_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in progress hook ws forwarder: {e}")

    consumer_task = asyncio.create_task(consumer())

    slot_acquired = False
    try:
        await acquire_download_slot()
        slot_acquired = True

        # Update status to downloading
        _ = await asyncio.to_thread(
            jobs_registry.update_job, job_id, status="downloading"
        )
        jobs = list((await asyncio.to_thread(jobs_registry.list_jobs)).values())
        await ws_manager.broadcast(
            {"type": "jobs_list", "jobs": [j.model_dump() for j in jobs]}
        )

        # Run blocking download in worker thread
        filepath = await asyncio.to_thread(
            download_video,
            job_id=job_id,
            url=url,
            format_id=format_id,
            output_dir=out_dir,
            loop=loop,
            event_queue=event_queue,
            conflict_resolution=conflict_resolution,
            referer=referer,
        )
        size_bytes = await asyncio.to_thread(os.path.getsize, filepath)

        await ws_manager.broadcast(
            {
                "type": "download_completed",
                "jobId": job_id,
                "filePath": filepath,
                "sizeBytes": size_bytes,
                "durationMs": None,
            }
        )
    except DownloadPaused:
        logger.info(f"Download job {job_id} paused cleanly.")
        jobs = list((await asyncio.to_thread(jobs_registry.list_jobs)).values())
        await ws_manager.broadcast(
            {"type": "jobs_list", "jobs": [j.model_dump() for j in jobs]}
        )
    except Exception as error:
        stage = "downloading"
        job = await asyncio.to_thread(jobs_registry.get_job, job_id)
        if job and job.status == "postprocessing":
            stage = "postprocessing"

        await ws_manager.broadcast(
            {
                "type": "download_failed",
                "jobId": job_id,
                "error": str(error),
                "stage": stage,
            }
        )
    finally:
        active_downloader_tasks.pop(job_id, None)
        consumer_task.cancel()
        try:
            await asyncio.wait_for(consumer_task, timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning(
                f"Download consumer for job {job_id} did not finish within 10s after cancellation"
            )
        except asyncio.CancelledError:
            pass
        if slot_acquired:
            await release_download_slot()


async def run_torrent_task(
    tab_id: int,
    job_id: str,
    url: str,
    output_dir: str,
):
    _ = tab_id
    task = asyncio.current_task()
    if task is not None:
        active_downloader_tasks[job_id] = cast(asyncio.Task[None], task)
    event_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    while True:
        torrent_settings = await asyncio.to_thread(load_settings)
        if not torrent_settings.torrentEnabled:
            await asyncio.to_thread(
                jobs_registry.update_job,
                job_id,
                status="failed",
                error="Torrent downloads are disabled in Settings.",
            )
            await ws_manager.broadcast({
                "type": "download_failed",
                "jobId": job_id,
                "error": "Torrent downloads are disabled in Settings.",
                "stage": "queued",
            })
            active_downloader_tasks.pop(job_id, None)
            return
        out_dir = output_dir or torrent_settings.torrentOutputDir or settings.DEFAULT_OUTPUT_DIR
        async with torrent_jobs_lock:
            if len(active_torrent_jobs) < max(1, torrent_settings.torrentMaxActive):
                active_torrent_jobs.add(job_id)
                break
        await asyncio.sleep(0.5)
    consumer_task = asyncio.create_task(
        _forward_progress_events(event_queue)
    )
    try:
        await asyncio.to_thread(jobs_registry.update_job, job_id, status="queued")
        queued_job = await asyncio.to_thread(jobs_registry.get_job, job_id)
        await ws_manager.broadcast({
            "type": "download_queued",
            "jobId": job_id,
            "outputPath": out_dir,
            "url": url,
            "title": queued_job.title if queued_job else None,
            "mediaType": "torrent",
        })
        await asyncio.to_thread(
            jobs_registry.update_job, job_id, status="downloading"
        )
        filepath = await asyncio.to_thread(
            download_torrent,
            job_id=job_id,
            url=url,
            output_dir=out_dir,
            loop=loop,
            event_queue=event_queue,
        )
        await ws_manager.broadcast({
            "type": "download_completed",
            "jobId": job_id,
            "filePath": filepath,
            "sizeBytes": (await asyncio.to_thread(os.path.getsize, filepath))
            if await asyncio.to_thread(os.path.isfile, filepath)
            else None,
            "durationMs": None,
        })
    except DownloadPaused:
        await ws_manager.broadcast({
            "type": "jobs_list",
            "jobs": [j.model_dump() for j in (await asyncio.to_thread(jobs_registry.list_jobs)).values()],
        })
    except Exception as error:
        await ws_manager.broadcast({
            "type": "download_failed",
            "jobId": job_id,
            "error": str(error),
            "stage": "downloading",
        })
    finally:
        active_downloader_tasks.pop(job_id, None)
        async with torrent_jobs_lock:
            active_torrent_jobs.discard(job_id)
        consumer_task.cancel()
        try:
            await asyncio.wait_for(consumer_task, timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning(
                f"Torrent consumer for job {job_id} did not finish within 10s after cancellation"
            )
        except asyncio.CancelledError:
            pass


async def _forward_progress_events(event_queue: asyncio.Queue[dict[str, object]]):
    while True:
        try:
            await ws_manager.broadcast(await event_queue.get())
            event_queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(f"Error in torrent progress ws forwarder: {error}")





async def run_probe_task(
    tab_id: int,
    job_id: str,
    url_to_probe: str,
    page_title: str | None = None,
    referer: str | None = None,
):
    task = asyncio.current_task()
    if task is not None:
        active_probe_tasks[job_id] = cast(asyncio.Task[None], task)
        probe_task_tabs[job_id] = tab_id
    try:
        prune_pending_probes()
        # Probe in threadpool. Magnets use libtorrent metadata instead of yt-dlp.
        info: dict[str, Any]
        if is_magnet_url(url_to_probe):
            torrent = await asyncio.to_thread(probe_magnet, url_to_probe)
            info = {
                "title": torrent["name"],
                "duration": None,
                "thumbnail": None,
                "uploader": None,
                "formats": [],
                "mediaType": "torrent",
                "torrent": torrent,
            }
        else:
            info = await asyncio.to_thread(
                probe_video,
                job_id=job_id,
                url=url_to_probe,
                referer=referer,
                page_title=page_title,
            )



        # Process formats
        formats_val = info.get("formats", [])
        duration_val = info.get("duration")
        formats_list_arg: list[dict[str, object]] = (
            cast(list[dict[str, object]], formats_val)
            if isinstance(formats_val, list)
            else []
        )
        duration_arg = cast(float | None, duration_val)
        formats_list = filter_and_summarize_formats(formats_list_arg, duration_arg)

        formats_json = [f.model_dump() for f in formats_list]

        # Save to pending_probes memory dict instead of jobs_registry!
        pending_probes[job_id] = {
            "url": url_to_probe,
            "title": info.get("title"),
            "filename": info.get("filename"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader"),
            "formats": formats_json,
            "formatIds": [format_item.formatId for format_item in formats_list],
            "probeTimestamp": time.time(),
            "probeReferer": referer,
            "mediaType": info.get("mediaType", "ytdlp"),
            "torrent": info.get("torrent"),
        }

        # Send result only to the initiating tab
        await ws_manager.send_message(
            tab_id,
            {
                "type": "probe_result",
                "jobId": job_id,
                "title": info.get("title", "Unknown Title"),
                "filename": info.get("filename"),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "uploader": info.get("uploader"),
                "formats": formats_json,
                "mediaType": info.get("mediaType", "ytdlp"),
                "torrent": info.get("torrent"),
            },
        )
    except asyncio.CancelledError:
        logger.info(f"Probe task for job {job_id} was cancelled.")
        pending_probes.pop(job_id, None)
    except Exception as err:
        failure = classify_probe_exception(err)
        logger.error(
            f"Probe failed for {url_to_probe} [{failure.category}]: {failure.message}"
        )

        is_unsupported = failure.category == "unsupported"
        suggestion = failure.suggestion
        await ws_manager.send_message(
            tab_id,
            {
                "type": "probe_failed",
                "jobId": job_id,
                "error": failure.message,
                "errorCategory": failure.category,
                "suggestion": suggestion,
                "isUnsupportedUrl": is_unsupported,
            },
        )
    finally:
        # Remove task from registry
        active_probe_tasks.pop(job_id, None)
        probe_task_tabs.pop(job_id, None)


@router.websocket("/ws")
@router.websocket("/ws/progress")
async def websocket_endpoint(websocket: WebSocket):
    tab_id = None
    try:
        await websocket.accept()

        # 1. Handshake
        initial_data = cast(object, orjson.loads(await websocket.receive_text()))
        try:
            handshake = message_adapter.validate_python(initial_data)
            if not isinstance(handshake, ClientHelloMessage):
                await websocket.close(code=4001, reason="Handshake expected")
                return
            tab_id = handshake.tabId
        except Exception as e:
            logger.error(f"Handshake validation failed: {e}")
            await websocket.close(code=4002, reason="Invalid handshake format")
            return

        # Re-register under connection manager
        await ws_manager.register(tab_id, websocket)
        logger.info(
            f"Handshake success for tab {tab_id}. Client version: {handshake.clientVersion}"
        )

        # Send Hello reply
        try:
            from yt_dlp.version import __version__ as yt_dlp_ver

            yt_dlp_version = yt_dlp_ver
        except ImportError:
            yt_dlp_version = getattr(yt_dlp, "__version__", "unknown") or "unknown"
        ffmpeg_available = shutil.which("ffmpeg") is not None

        await ws_manager.send_message(
            tab_id,
            {
                "type": "hello",
                "serverVersion": get_app_version(),
                "ytDlpVersion": yt_dlp_version,
                "ffmpegAvailable": ffmpeg_available,
                "heartbeatIntervalMs": 20000,
            },
        )

        # 2. Main message loop
        while True:
            data = cast(object, orjson.loads(await websocket.receive_text()))
            try:
                msg = message_adapter.validate_python(data)
            except Exception as e:
                logger.error(f"Invalid message format received: {e}")
                continue

            if isinstance(msg, ClientPingMessage):
                await ws_manager.send_message(tab_id, {"type": "pong", "ts": msg.ts})

            elif isinstance(msg, ClientProbeMessage):
                job_id = msg.jobId or f"job_{uuid.uuid4().hex[:8]}"

                # ── Standard HTTP/media URL/Torrent probe flow ──
                # Check duplicate job (any status)
                existing_jobs = await asyncio.to_thread(jobs_registry.list_jobs)
                duplicate_job = None
                for j_info in existing_jobs.values():
                    if j_info.url == msg.url:
                        duplicate_job = j_info
                        break

                if duplicate_job:
                    logger.info(
                        f"Duplicate probe requested for {msg.url}. Alerting client. JobId: {duplicate_job.job_id}"
                    )
                    await ws_manager.send_message(
                        tab_id,
                        {
                            "type": "duplicate_job_alert",
                            "jobId": duplicate_job.job_id,
                            "url": duplicate_job.url,
                            "title": duplicate_job.title or "Unknown Title",
                            "status": duplicate_job.status,
                        },
                    )
                    continue

                # Setup job ID for probing
                url_to_probe = msg.url
                await ws_manager.send_message(
                    tab_id,
                    {"type": "probe_started", "jobId": job_id, "url": url_to_probe},
                )

                probe_task_tabs[job_id] = tab_id
                active_probe_tasks[job_id] = asyncio.create_task(
                    run_probe_task(
                        tab_id,
                        job_id,
                        url_to_probe,
                        page_title=msg.title,
                        referer=msg.referer,
                    )
                )

            elif isinstance(msg, ClientCancelProbeMessage):
                job_id = msg.jobId
                logger.info(f"Cancel probe requested for jobId: {job_id}")
                task = active_probe_tasks.get(job_id)
                if task:
                    task.cancel()
                    logger.info(
                        f"Successfully cancelled probe task for jobId: {job_id}"
                    )

            elif isinstance(msg, ClientChooseMessage):
                job_id = msg.jobId
                prune_pending_probes()

                format_ids: list[Any] = []

                # Retrieve from pending_probes memory cache
                metadata_raw = pending_probes.pop(job_id, None)
                if metadata_raw is None:
                    if msg.url:
                        # Resolve title/filename centrally; do not duplicate
                        # extraction logic in the router.
                        resolved = await asyncio.to_thread(
                            resolve_filename,
                            url=msg.url,
                            filename=msg.filename,
                            mime=msg.mime,
                            referer=msg.referer,
                            page_title=msg.title,
                            timeout=3.0,
                        )
                        metadata_dict: dict[str, object] = {
                            "url": msg.url,
                            "title": resolved.title,
                            "duration": None,
                            "thumbnail": None,
                            "uploader": None,
                            "formats": [],
                            "mediaType": "file",
                            "mime": msg.mime,
                            "filename": resolved.filename,
                        }
                    else:
                        logger.warning(
                            f"Choose requested for untracked/missing probed job {job_id}"
                        )
                        continue
                else:
                    metadata_dict = metadata_raw
                    format_ids = cast(list[Any], metadata_dict.get("formatIds", []))
                    available_format_ids = {
                        str(format_id)
                        for format_id in format_ids
                    }
                    if (
                        msg.formatId != "best"
                        and available_format_ids
                        and msg.formatId not in available_format_ids
                    ):
                        # Restore the popped probe in case the client wants to retry with a valid format
                        pending_probes[job_id] = metadata_raw
                        await ws_manager.send_message(
                            tab_id,
                            {
                                "type": "probe_failed",
                                "jobId": job_id,
                                "error": "The selected format is no longer available. Please probe again.",
                                "errorCategory": "stale_probe",
                                "suggestion": "reprobe_required",
                            },
                        )
                        continue

                metadata_url = cast(str, metadata_dict["url"])
                metadata_title = cast(str | None, metadata_dict.get("title"))
                metadata_duration = cast(float | None, metadata_dict.get("duration"))
                metadata_thumbnail = cast(str | None, metadata_dict.get("thumbnail"))
                metadata_uploader = cast(str | None, metadata_dict.get("uploader"))
                metadata_formats = cast(list[object], metadata_dict.get("formats"))
                metadata_media_type = cast(str | None, metadata_dict.get("mediaType"))
                metadata_torrent = cast(dict[str, object] | None, metadata_dict.get("torrent"))
                metadata_mime = cast(
                    str | None, metadata_dict.get("mime")
                ) or msg.mime
                metadata_filename = cast(
                    str | None, metadata_dict.get("filename")
                ) or msg.filename
                probe_format_ids = [
                    str(format_id)
                    for format_id in format_ids
                ]
                probe_timestamp = cast(
                    float | None, metadata_dict.get("probeTimestamp")
                )
                probe_referer = cast(
                    str | None, metadata_dict.get("probeReferer")
                )
                effective_referer = msg.referer or probe_referer

                # Now create the actual job in the registry
                _ = await asyncio.to_thread(
                    jobs_registry.create_job, job_id, metadata_url, status="queued"
                )
                _ = await asyncio.to_thread(
                    jobs_registry.update_job,
                    job_id,
                    title=metadata_title,
                    duration=metadata_duration,
                    thumbnail=metadata_thumbnail,
                    uploader=metadata_uploader,
                    formats=metadata_formats,
                    format_id=msg.formatId,
                    output_dir=msg.outputDir,
                    total_bytes=msg.fileSize or 0.0,
                    referer=effective_referer,
                    probe_format_ids=probe_format_ids or None,
                    probe_timestamp=probe_timestamp,
                    probe_referer=probe_referer,
                    media_type=metadata_media_type,
                    mime=metadata_mime,
                    filename=metadata_filename,
                    torrent_files=(metadata_torrent or {}).get("files"),
                    torrent_piece_length=(metadata_torrent or {}).get("pieceLength"),
                    torrent_piece_count=(metadata_torrent or {}).get("pieceCount"),
                    torrent_info_hash=(metadata_torrent or {}).get("infoHash"),
                )

                conflict_res = (
                    getattr(msg, "conflictResolution", "replace") or "replace"
                )

                if metadata_media_type == "torrent" or is_magnet_url(metadata_url):
                    asyncio.create_task(
                        run_torrent_task(
                            tab_id=tab_id,
                            job_id=job_id,
                            url=metadata_url,
                            output_dir=msg.outputDir,
                        )
                    )
                else:
                    asyncio.create_task(
                        run_downloader_task(
                            tab_id=tab_id,
                            job_id=job_id,
                            url=metadata_url,
                            format_id=msg.formatId,
                            output_dir=msg.outputDir,
                            conflict_resolution=conflict_res,
                            referer=effective_referer,
                        )
                    )

            elif isinstance(msg, ClientCheckFileExistsMessage):
                exists = False
                check_filename = msg.filename or ""
                try:
                    base_path = Path(msg.path).expanduser().resolve()
                    if msg.title and msg.ext:
                        resolved = await asyncio.to_thread(
                            resolve_filename,
                            url=msg.url or "",
                            filename=f"{msg.title}.{msg.ext}",
                            mime=msg.mime,
                            page_title=msg.title,
                            timeout=3.0,
                            allow_network=False,
                        )
                        check_filename = resolved.filename
                    elif msg.filename:
                        resolved = await asyncio.to_thread(
                            resolve_filename,
                            url=msg.url or "",
                            filename=msg.filename,
                            mime=msg.mime,
                            page_title=msg.title,
                            timeout=3.0,
                            allow_network=False,
                        )
                        check_filename = resolved.filename

                    full_path = (base_path / check_filename).resolve()
                    full_path.relative_to(base_path)
                    exists = await asyncio.to_thread(full_path.exists)
                except (OSError, ValueError) as error:
                    logger.warning(
                        f"Rejected file existence check for {msg.path}/{check_filename}: {error}"
                    )

                await ws_manager.send_message(
                    tab_id,
                    {
                        "type": "file_exists_result",
                        "exists": exists,
                        "filename": check_filename,
                        "path": msg.path,
                        "jobId": msg.jobId,
                    },
                )

            elif isinstance(msg, ClientPauseMessage):
                job_id = msg.jobId
                logger.info(f"Pause request received for job {job_id}")
                success = await asyncio.to_thread(jobs_registry.trigger_pause, job_id)
                if success:
                    jobs = list(
                        (await asyncio.to_thread(jobs_registry.list_jobs)).values()
                    )
                    await ws_manager.broadcast(
                        {"type": "jobs_list", "jobs": [j.model_dump() for j in jobs]}
                    )
                else:
                    logger.warning(f"Failed to pause job {job_id}")

            elif isinstance(msg, ClientResumeMessage):
                job_id = msg.jobId
                logger.info(f"Resume request received for job {job_id}")
                success = await asyncio.to_thread(jobs_registry.trigger_resume, job_id)
                if success:
                    job = await asyncio.to_thread(jobs_registry.get_job, job_id)
                    if job:
                        # Broadcast queued status first
                        jobs = list(
                            (await asyncio.to_thread(jobs_registry.list_jobs)).values()
                        )
                        await ws_manager.broadcast(
                            {
                                "type": "jobs_list",
                                "jobs": [j.model_dump() for j in jobs],
                            }
                        )
                        # Start the appropriate downloader again.
                        if job.media_type == "torrent" or is_magnet_url(job.url):
                            if job_id not in active_downloader_tasks:
                                asyncio.create_task(
                                    run_torrent_task(
                                        tab_id=tab_id,
                                        job_id=job_id,
                                        url=job.url,
                                        output_dir=job.output_dir or "",
                                    )
                                )
                            else:
                                logger.info(f"Torrent task for job {job_id} is already running; resumed in-place.")
                        else:
                            asyncio.create_task(
                                run_downloader_task(
                                    tab_id=tab_id,
                                    job_id=job_id,
                                    url=job.url,
                                    format_id=job.format_id or "best",
                                    output_dir=job.output_dir or "",
                                    referer=job.referer,
                                )
                            )
                else:
                    logger.warning(f"Failed to resume job {job_id}")

            elif isinstance(msg, (ClientRemoveJobMessage, ClientCancelDownloadMessage)):
                job_id = msg.jobId
                is_cancel = isinstance(msg, ClientCancelDownloadMessage)
                logger.info(
                    f"{'Cancel' if is_cancel else 'Remove'} request received for job {job_id}"
                )

                # Remove from registry so progress hooks trigger an abort.
                job_before_remove = await asyncio.to_thread(
                    jobs_registry.get_job, job_id
                )
                _ = await asyncio.to_thread(jobs_registry.remove_job, job_id)

                # Queued tasks can be cancelled immediately. Active worker threads
                # must be allowed to observe the removed job and stop themselves.
                task = active_downloader_tasks.get(job_id)
                if task and (
                    job_before_remove is None
                    or job_before_remove.status not in {"downloading", "postprocessing"}
                ):
                    task.cancel()
                    try:
                        _ = await asyncio.wait_for(task, timeout=1.5)
                    except asyncio.CancelledError:
                        pass
                    except TimeoutError:
                        logger.warning(f"Timed out stopping download task {job_id}")
                    except Exception as error:
                        logger.warning(
                            f"Download task {job_id} stopped with an error: {error}"
                        )

                # Safe temp cleanup offloaded to thread
                temp_root = (get_app_data_dir() / "temp").resolve()
                app_temp_dir = (temp_root / job_id).resolve()
                if app_temp_dir.parent != temp_root:
                    logger.warning(f"Refusing to clean unsafe temp path: {app_temp_dir}")
                else:
                    def cleanup_temp_dir(path: Path):
                        if path.is_dir():
                            shutil.rmtree(path)

                    try:
                        _ = await asyncio.to_thread(cleanup_temp_dir, app_temp_dir)
                        logger.info(
                            f"Cleaned up temp folder on job removal: {app_temp_dir}"
                        )
                    except OSError as error:
                        logger.error(
                            f"Failed to remove temp folder {app_temp_dir}: {error}"
                        )

                # Only trash incomplete downloads; completed downloads must not be trashed on remove/cancel
                if job_before_remove and job_before_remove.file_path and job_before_remove.status != "completed":
                    output_root = job_before_remove.output_dir or settings.DEFAULT_OUTPUT_DIR
                    temp_root = get_app_data_dir() / "temp"
                    if is_path_within(job_before_remove.file_path, output_root) or is_path_within(
                        job_before_remove.file_path, temp_root
                    ):
                        try:
                            _ = await asyncio.to_thread(send_to_trash, job_before_remove.file_path)
                        except Exception as error:
                            logger.error(
                                f"Failed to trash file {job_before_remove.file_path} on job removal: {error}"
                            )
                    else:
                        logger.warning(
                            f"Refusing to trash file outside job roots on job removal: {job_before_remove.file_path}"
                        )

                if is_cancel:
                    await ws_manager.broadcast(
                        {"type": "download_canceled", "jobId": job_id}
                    )

                jobs = list((await asyncio.to_thread(jobs_registry.list_jobs)).values())
                await ws_manager.broadcast(
                    {"type": "jobs_list", "jobs": [j.model_dump() for j in jobs]}
                )

            elif isinstance(msg, ClientRevealFileMessage):
                job_id = msg.jobId
                logger.info(f"Reveal file request received for job {job_id}")
                job = await asyncio.to_thread(jobs_registry.get_job, job_id)

                def reveal_file_sync(file_path: str):
                    if os.path.exists(file_path):
                        import subprocess
                        import sys

                        if sys.platform == "darwin":
                            _ = subprocess.run(["open", "-R", file_path], check=True)
                        elif sys.platform == "win32":
                            _ = subprocess.run(
                                ["explorer", "/select,", os.path.normpath(file_path)],
                                check=True,
                            )
                        return True
                    return False

                if job and job.file_path:
                    output_root = job.output_dir or settings.DEFAULT_OUTPUT_DIR
                    temp_root = get_app_data_dir() / "temp"
                    if not (
                        is_path_within(job.file_path, output_root)
                        or is_path_within(job.file_path, temp_root)
                    ):
                        logger.warning(
                            f"Refusing to reveal file outside job roots: {job.file_path}"
                        )
                        continue
                    try:
                        success = await asyncio.to_thread(
                            reveal_file_sync, job.file_path
                        )
                        if success:
                            logger.info(f"Successfully revealed file {job.file_path}")
                        else:
                            logger.warning(
                                f"Cannot reveal file, path does not exist: {job.file_path}"
                            )
                    except Exception as e:
                        logger.error(f"Failed to reveal file {job.file_path}: {e}")

            elif isinstance(msg, ClientDeleteFileMessage):
                job_id = msg.jobId
                logger.info(f"Delete file request received for job {job_id}")
                job = await asyncio.to_thread(jobs_registry.get_job, job_id)

                if job and job.file_path:
                    output_root = job.output_dir or settings.DEFAULT_OUTPUT_DIR
                    temp_root = get_app_data_dir() / "temp"
                    if is_path_within(job.file_path, output_root) or is_path_within(
                        job.file_path, temp_root
                    ):
                        try:
                            _ = await asyncio.to_thread(send_to_trash, job.file_path)
                        except Exception as error:
                            logger.error(
                                f"Failed to trash file {job.file_path}: {error}"
                            )
                    else:
                        logger.warning(
                            f"Refusing to trash file outside job roots: {job.file_path}"
                        )

                await asyncio.to_thread(jobs_registry.remove_job, job_id)
                jobs = list((await asyncio.to_thread(jobs_registry.list_jobs)).values())
                await ws_manager.broadcast(
                    {"type": "jobs_list", "jobs": [j.model_dump() for j in jobs]}
                )

            elif isinstance(msg, ClientGetJobsMessage):
                jobs = list((await asyncio.to_thread(jobs_registry.list_jobs)).values())
                await ws_manager.send_message(
                    tab_id,
                    {"type": "jobs_list", "jobs": [j.model_dump() for j in jobs]},
                )

            elif isinstance(msg, ClientGetCategoriesMessage):
                categories = await asyncio.to_thread(load_categories)
                await ws_manager.send_message(
                    tab_id,
                    {
                        "type": "categories_list",
                        "categories": [c.model_dump() for c in categories],
                    },
                )

            elif isinstance(msg, ClientSaveCategoriesMessage):
                await asyncio.to_thread(save_categories_to_file, msg.categories)
                await ws_manager.broadcast(
                    {
                        "type": "categories_list",
                        "categories": [c.model_dump() for c in msg.categories],
                    }
                )

            elif isinstance(msg, ClientBrowseDirectoryMessage):
                try:
                    selected_path = await pick_directory_system(initial_dir=msg.path)
                    if selected_path:
                        await ws_manager.send_message(
                            tab_id,
                            {
                                "type": "directory_selected",
                                "path": selected_path,
                                "forField": msg.forField,
                            },
                        )
                except Exception as e:
                    await ws_manager.send_message(
                        tab_id, {"type": "browse_failed", "error": str(e)}
                    )

            elif isinstance(msg, ClientGetSettingsMessage):
                settings_data = await asyncio.to_thread(load_settings)
                await ws_manager.send_message(
                    tab_id,
                    {"type": "settings_data", "settings": settings_data.model_dump()},
                )

            elif isinstance(msg, ClientSaveSettingsMessage):
                await asyncio.to_thread(save_settings_to_file, msg.settings)
                await ws_manager.broadcast(
                    {"type": "settings_data", "settings": msg.settings.model_dump()}
                )
                async with active_downloads_cond:
                    active_downloads_cond.notify_all()

    except WebSocketDisconnect:
        logger.info(f"WebSocket connection closed for tab {tab_id}")
    except Exception as e:
        logger.error(f"WS error: {e}", exc_info=True)
    finally:
        if tab_id is not None and ws_manager.disconnect(tab_id, websocket):
            orphaned_probe_ids = [
                job_id for job_id, owner_tab_id in probe_task_tabs.items()
                if owner_tab_id == tab_id
            ]
            for job_id in orphaned_probe_ids:
                task = active_probe_tasks.get(job_id)
                if task is not None:
                    task.cancel()
                pending_probes.pop(job_id, None)
