"""HTTP/direct download orchestration service."""

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

from app.config import settings as app_settings
from app.domain.exceptions import DownloadPaused
from app.engine.codec_filter import process_probe_formats
from app.engine.downloader import YtdlpDownloader, DirectDownloader, is_expired_url_error
from app.engine.file_types import ENGINE_STREAM, is_direct_download_type
from app.engine.title_extractor import _is_unusable_stem, _strip_trailing_extension
from app.schemas.settings import AppSettings
from app.services.file_service import FileService
from app.services.interfaces import IConnectionManager, IDownloadEngine, IJobRepository, IProbeEngine, ISettingsRepository
from app.utils.logger import bind_contextvars, clear_contextvars, get_logger, redact_url

logger = get_logger(__name__)


class DownloadSlots:
    """Bounded context manager for concurrent HTTP downloads."""

    def __init__(self, settings_repository: ISettingsRepository) -> None:
        self._settings_repository = settings_repository
        self._cond = asyncio.Condition()
        self._active = 0

    async def acquire(self) -> None:
        async with self._cond:
            while True:
                settings = self._settings_repository.load()
                limit = max(1, settings.aria2NextConcurrentDownloads)
                if self._active < limit:
                    self._active += 1
                    return
                await self._cond.wait()

    async def release(self) -> None:
        async with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()

    async def notify_limit_changed(self) -> None:
        async with self._cond:
            self._cond.notify_all()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.release()


class DownloadService:
    """Manage active HTTP/direct downloads, concurrency slots, and URL refresh."""

    def __init__(
        self,
        connection_manager: IConnectionManager,
        job_repository: IJobRepository,
        probe_engine: IProbeEngine,
        settings_repository: ISettingsRepository,
        file_service: FileService,
        ytdlp_downloader: Optional[IDownloadEngine] = None,
        direct_downloader: Optional[IDownloadEngine] = None,
    ) -> None:
        self._connection_manager = connection_manager
        self._job_repository = job_repository
        self._probe_engine = probe_engine
        self._settings_repository = settings_repository
        self._file_service = file_service
        self._ytdlp_downloader = ytdlp_downloader or YtdlpDownloader()
        self._direct_downloader = direct_downloader or DirectDownloader()

        self._download_slots = DownloadSlots(settings_repository)
        self._active_tasks: Dict[str, asyncio.Task[None]] = {}
        self._active_tasks_lock = asyncio.Lock()

    async def notify_limit_changed(self) -> None:
        await self._download_slots.notify_limit_changed()

    def is_active(self, job_id: str) -> bool:
        return job_id in self._active_tasks

    async def start_download(
        self,
        tab_id: int,
        job_id: str,
        url: str,
        format_id: str,
        output_dir: str,
        conflict_resolution: str = "replace",
        referer: Optional[str] = None,
        media_type: Optional[str] = None,
    ) -> None:
        asyncio.create_task(
            self._run_download_task(
                tab_id,
                job_id,
                url,
                format_id,
                output_dir,
                conflict_resolution,
                referer,
                media_type,
            )
        )

    async def cancel(self, job_id: str) -> bool:
        async with self._active_tasks_lock:
            task = self._active_tasks.get(job_id)
        if not task or task.done():
            return False
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=10.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:
            pass
        return True

    async def _refresh_download_url(
        self,
        job_id: str,
        url: str,
        format_id: str,
        referer: Optional[str],
        settings: AppSettings,
    ) -> Tuple[str, str, Optional[str]]:
        job = await asyncio.to_thread(self._job_repository.get_job, job_id)
        if not job:
            return url, format_id, None

        media_type = job.media_type
        if not (is_direct_download_type(media_type) or media_type == ENGINE_STREAM):
            return url, format_id, None

        try:
            info = await asyncio.to_thread(
                self._probe_engine.probe,
                job_id=job_id,
                url=url,
                referer=referer,
                page_title=job.title,
                mime_hint=job.mime,
                settings=settings,
            )
        except Exception as exc:
            logger.warning(
                f"Pre-download re-probe failed for {redact_url(url)}: {exc}"
            )
            return url, format_id, str(exc)

        formats_json, format_ids = process_probe_formats(
            info, preferred_ext=settings.mergeFormat
        )

        selected_format_id = format_id or "best"
        if selected_format_id != "best" and selected_format_id not in format_ids:
            if "best" in format_ids:
                selected_format_id = "best"
            else:
                return (
                    url,
                    format_id,
                    "The selected format is no longer available. Please probe again.",
                )

        # For stream manifests, yt-dlp may resolve the canonical URL to a
        # variant playlist (e.g. a master .m3u8 pointing at zz.m3u8). The
        # selected format_id was derived from the original manifest's format
        # list, so re-extracting that variant URL with the same format_id
        # fails with "Requested format is not available". Keep the original
        # stream URL and let yt-dlp resolve variants during format selection.
        if media_type == ENGINE_STREAM:
            refreshed_url = url
        else:
            refreshed_url = cast(str, info.get("url") or url)

        existing_title = job.title
        existing_filename = job.filename
        refreshed_title = info.get("title")
        refreshed_filename = info.get("filename")

        if refreshed_title and not _is_unusable_stem(refreshed_title):
            title = (
                existing_title
                if existing_title and existing_title != "video"
                else refreshed_title
            )
        else:
            title = existing_title or "video"

        refreshed_stem = (
            _strip_trailing_extension(str(refreshed_filename).lower())
            if refreshed_filename
            else None
        )
        if refreshed_filename and refreshed_stem and not _is_unusable_stem(refreshed_stem):
            filename = existing_filename or refreshed_filename
        else:
            ext = info.get("ext") or settings.mergeFormat
            filename = existing_filename or f"{title}.{ext}"

        await asyncio.to_thread(
            self._job_repository.update_job,
            job_id,
            persist=True,
            url=refreshed_url,
            title=title,
            filename=filename,
            duration=info.get("duration")
            if info.get("duration") is not None
            else job.duration,
            thumbnail=info.get("thumbnail")
            if info.get("thumbnail") is not None
            else job.thumbnail,
            uploader=info.get("uploader")
            if info.get("uploader") is not None
            else job.uploader,
            formats=formats_json,
            format_id=selected_format_id,
            probe_format_ids=format_ids or None,
            probe_timestamp=time.time(),
            probe_referer=referer,
            media_type=info.get("mediaType") or media_type,
            mime=info.get("mime") or job.mime,
        )

        return refreshed_url, selected_format_id, None

    async def _broadcast_jobs_list(self) -> None:
        jobs = list((await asyncio.to_thread(self._job_repository.list_jobs)).values())
        await self._connection_manager.broadcast(
            {"type": "jobs_list", "jobs": [j.model_dump() for j in jobs]}
        )

    async def aggregate_parent_progress(self, child_job_id: str) -> None:
        """Public wrapper to recompute a playlist parent from its children."""
        child = await asyncio.to_thread(self._job_repository.get_job, child_job_id)
        if not child or not child.parent_job_id:
            return
        await self.aggregate_parent_by_id(child.parent_job_id)

    async def aggregate_parent_by_id(self, parent_job_id: str) -> None:
        """Recompute a playlist parent's progress and status from its children."""
        parent = await asyncio.to_thread(self._job_repository.get_job, parent_job_id)
        if not parent or not parent.playlist_child_job_ids:
            return

        children: List[Any] = []
        for cid in parent.playlist_child_job_ids:
            c = await asyncio.to_thread(self._job_repository.get_job, cid)
            if c:
                children.append(c)

        if not children:
            return

        total = sum((c.combined_total_bytes or c.total_bytes or 0) for c in children)
        downloaded = sum((c.combined_downloaded_bytes or c.downloaded_bytes or 0) for c in children)
        speed = sum((c.speed or 0) for c in children)
        progress = (downloaded / total * 100) if total > 0 else 0.0

        statuses = [c.status for c in children]
        terminal = {"completed", "failed", "canceled"}
        all_terminal = all(s in terminal for s in statuses)
        if all_terminal:
            if all(s == "completed" for s in statuses):
                parent_status = "completed"
                progress = 100.0
            else:
                parent_status = "failed"
        elif any(s in ("downloading", "queued", "postprocessing") for s in statuses):
            parent_status = "downloading"
        elif any(s == "paused" for s in statuses):
            parent_status = "paused"
        else:
            parent_status = parent.status or "downloading"

        update_kwargs: Dict[str, Any] = {
            "status": parent_status,
            "progress": progress,
            "combined_total_bytes": total,
            "combined_downloaded_bytes": downloaded,
            "speed": speed,
            "eta": 0.0,
        }
        if parent_status == "completed":
            update_kwargs["file_path"] = parent.output_dir
        if parent_status == "failed":
            failed = sum(1 for s in statuses if s in ("failed", "canceled"))
            update_kwargs["error"] = f"{failed}/{len(children)} entries failed"

        await asyncio.to_thread(
            self._job_repository.update_job, parent.job_id, **update_kwargs
        )

        await self._connection_manager.broadcast(
            {
                "type": "download_progress",
                "jobId": parent.job_id,
                "status": parent_status,
                "progress": progress,
                "combinedDownloadedBytes": downloaded,
                "combinedTotalBytes": total,
                "speed": speed,
                "eta": 0,
            }
        )

        if parent_status == "completed":
            await self._connection_manager.broadcast(
                {
                    "type": "download_completed",
                    "jobId": parent.job_id,
                    "filePath": parent.output_dir,
                    "sizeBytes": int(downloaded),
                    "durationMs": None,
                }
            )
        elif parent_status == "failed":
            await self._connection_manager.broadcast(
                {
                    "type": "download_failed",
                    "jobId": parent.job_id,
                    "error": update_kwargs.get("error", "Playlist download failed"),
                    "stage": "downloading",
                }
            )

        await self._broadcast_jobs_list()

    async def _run_download_task(
        self,
        tab_id: int,
        job_id: str,
        url: str,
        format_id: str,
        output_dir: str,
        conflict_resolution: str,
        referer: Optional[str],
        media_type: Optional[str],
    ) -> None:
        _ = tab_id
        bind_contextvars(job_id=job_id)
        settings = await asyncio.to_thread(self._settings_repository.load)
        event_queue: asyncio.Queue[Dict[str, object]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        await asyncio.to_thread(self._job_repository.update_job, job_id, status="queued")
        await self._broadcast_jobs_list()

        out_dir = output_dir if output_dir else app_settings.DEFAULT_OUTPUT_DIR
        if not await self._file_service.is_path_allowed(out_dir):
            await asyncio.to_thread(
                self._job_repository.update_job,
                job_id,
                status="failed",
                error="Selected output directory is not allowed.",
            )
            await self._connection_manager.broadcast(
                {
                    "type": "download_failed",
                    "jobId": job_id,
                    "error": "Selected output directory is not allowed.",
                    "stage": "queued",
                }
            )
            clear_contextvars()
            return

        out_dir = await self._file_service.resolve_output_dir(out_dir)
        job = await asyncio.to_thread(self._job_repository.get_job, job_id)
        await self._connection_manager.broadcast(
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

        async def consumer() -> None:
            while True:
                try:
                    event = await event_queue.get()
                    await self._connection_manager.broadcast(event)
                    if isinstance(event, dict) and event.get("jobId"):
                        await self.aggregate_parent_progress(str(event.get("jobId")))
                    event_queue.task_done()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in progress hook ws forwarder: {e}")

        consumer_task = asyncio.create_task(consumer())
        task = asyncio.current_task()
        if task is not None:
            async with self._active_tasks_lock:
                self._active_tasks[job_id] = cast(asyncio.Task[None], task)

        try:
            async with self._download_slots:
                is_paused = await asyncio.to_thread(
                    self._job_repository.is_paused, job_id
                )
                job_exists = await asyncio.to_thread(
                    self._job_repository.get_job, job_id
                ) is not None
                if is_paused or not job_exists:
                    await self._broadcast_jobs_list()
                    return

                url, format_id, refresh_error = await self._refresh_download_url(
                    job_id, url, format_id, referer, settings
                )
                if refresh_error:
                    error_category = (
                        "expired_url" if is_expired_url_error(refresh_error) else None
                    )
                    await asyncio.to_thread(
                        self._job_repository.update_job,
                        job_id,
                        status="failed",
                        error=refresh_error,
                        error_category=error_category,
                    )
                    job = await asyncio.to_thread(
                        self._job_repository.get_job, job_id
                    )
                    page_url = getattr(job, "page_url", None) or url
                    await self._connection_manager.broadcast(
                        {
                            "type": "download_failed",
                            "jobId": job_id,
                            "error": refresh_error,
                            "stage": "queued",
                            "errorCategory": error_category,
                            "needsUrl": error_category == "expired_url",
                            "pageUrl": page_url,
                        }
                    )
                    if error_category == "expired_url" and page_url:
                        await self._connection_manager.broadcast(
                            {
                                "type": "needs_refresh",
                                "jobId": job_id,
                                "pageUrl": page_url,
                            }
                        )
                    await self.aggregate_parent_progress(job_id)
                    return

                await asyncio.to_thread(
                    self._job_repository.update_job, job_id, status="downloading"
                )
                await self._broadcast_jobs_list()

                logger.info(f"Download started for job {job_id}: {redact_url(url)}")

                downloader = (
                    self._direct_downloader
                    if is_direct_download_type(media_type)
                    else self._ytdlp_downloader
                )
                filepath = await asyncio.to_thread(
                    downloader.download,
                    job_id,
                    url,
                    Path(out_dir),
                    format_id=format_id,
                    loop=loop,
                    event_queue=event_queue,
                    settings=settings,
                    conflict_resolution=conflict_resolution,
                    referer=referer,
                )
                size_bytes = await asyncio.to_thread(os.path.getsize, filepath)

                await self._connection_manager.broadcast(
                    {
                        "type": "download_completed",
                        "jobId": job_id,
                        "filePath": filepath,
                        "sizeBytes": size_bytes,
                        "durationMs": None,
                    }
                )
                await self.aggregate_parent_progress(job_id)
        except DownloadPaused:
            logger.info(f"Download job {job_id} paused cleanly.")
            await asyncio.to_thread(
                self._job_repository.update_job, job_id, status="paused"
            )
            await self._broadcast_jobs_list()
            await self.aggregate_parent_progress(job_id)
        except Exception as error:
            stage = "downloading"
            job = await asyncio.to_thread(self._job_repository.get_job, job_id)
            if job and job.status == "postprocessing":
                stage = "postprocessing"

            error_message = str(error)
            error_category = getattr(job, "error_category", None)
            if not error_category and is_expired_url_error(error_message):
                error_category = "expired_url"
                await asyncio.to_thread(
                    self._job_repository.update_job,
                    job_id,
                    error_category=error_category,
                )

            page_url = getattr(job, "page_url", None) or getattr(job, "url", None)
            await self._connection_manager.broadcast(
                {
                    "type": "download_failed",
                    "jobId": job_id,
                    "error": error_message,
                    "stage": stage,
                    "errorCategory": error_category,
                    "needsUrl": error_category == "expired_url",
                    "pageUrl": page_url,
                }
            )
            if error_category == "expired_url" and page_url:
                await self._connection_manager.broadcast(
                    {
                        "type": "needs_refresh",
                        "jobId": job_id,
                        "pageUrl": page_url,
                    }
                )
            await self.aggregate_parent_progress(job_id)
        finally:
            async with self._active_tasks_lock:
                self._active_tasks.pop(job_id, None)
            consumer_task.cancel()
            try:
                await asyncio.wait_for(consumer_task, timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning(
                    f"Download consumer for job {job_id} did not finish within 10s after cancellation"
                )
            except asyncio.CancelledError:
                pass
            finally:
                clear_contextvars()
