"""HTTP/direct download orchestration service."""

import asyncio
import os
import time
from pathlib import Path
from typing import Dict, Optional, Tuple, cast

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

        refreshed_url = cast(str, info.get("url") or url)

        original_lower = url.lower().split("?")[0]
        refreshed_lower = refreshed_url.lower().split("?")[0]
        if (
            original_lower.endswith("master.m3u8")
            or original_lower.endswith("/master")
            or original_lower.endswith("/master.m3u8")
        ) and not (
            refreshed_lower.endswith("master.m3u8")
            or refreshed_lower.endswith("/master")
            or refreshed_lower.endswith("/master.m3u8")
        ):
            refreshed_url = url

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
        except DownloadPaused:
            logger.info(f"Download job {job_id} paused cleanly.")
            await asyncio.to_thread(
                self._job_repository.update_job, job_id, status="paused"
            )
            await self._broadcast_jobs_list()
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
