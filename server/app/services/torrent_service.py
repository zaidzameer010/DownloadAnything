"""Torrent download orchestration service."""

import asyncio
import os
from pathlib import Path
from typing import Dict, Optional, Set, cast

from app.config import settings as app_settings
from app.domain.exceptions import DownloadPaused
from app.engine.torrent import TorrentDownloader
from app.services.file_service import FileService
from app.services.interfaces import IConnectionManager, IDownloadEngine, IJobRepository, ISettingsRepository
from app.utils.logger import bind_contextvars, clear_contextvars, get_logger, redact_url

logger = get_logger(__name__)


class TorrentService:
    """Manage active torrent jobs, concurrency, and progress events."""

    def __init__(
        self,
        connection_manager: IConnectionManager,
        job_repository: IJobRepository,
        file_service: FileService,
        settings_repository: ISettingsRepository,
        torrent_downloader: Optional[IDownloadEngine] = None,
    ) -> None:
        self._connection_manager = connection_manager
        self._job_repository = job_repository
        self._file_service = file_service
        self._settings_repository = settings_repository
        self._torrent_downloader = torrent_downloader or TorrentDownloader()

        self._active_tasks: Dict[str, asyncio.Task[None]] = {}
        self._active_tasks_lock = asyncio.Lock()

        self._active_torrent_jobs: Set[str] = set()
        self._torrent_jobs_lock = asyncio.Lock()
        self._torrent_jobs_condition = asyncio.Condition(self._torrent_jobs_lock)

    def is_active(self, job_id: str) -> bool:
        return job_id in self._active_tasks

    async def notify_limit_changed(self) -> None:
        async with self._torrent_jobs_condition:
            self._torrent_jobs_condition.notify_all()

    async def start_download(
        self,
        tab_id: int,
        job_id: str,
        url: str,
        output_dir: str,
        selected_files: Optional[list[int]] = None,
    ) -> None:
        asyncio.create_task(
            self._run_torrent_task(
                tab_id, job_id, url, output_dir, selected_files
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

    async def _broadcast_jobs_list(self) -> None:
        jobs = list((await asyncio.to_thread(self._job_repository.list_jobs)).values())
        await self._connection_manager.broadcast(
            {"type": "jobs_list", "jobs": [j.model_dump() for j in jobs]}
        )

    async def _forward_progress_events(
        self, event_queue: asyncio.Queue[Dict[str, object]]
    ) -> None:
        while True:
            try:
                await self._connection_manager.broadcast(await event_queue.get())
                event_queue.task_done()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error(f"Error in torrent progress ws forwarder: {error}")

    async def _run_torrent_task(
        self,
        tab_id: int,
        job_id: str,
        url: str,
        output_dir: str,
        selected_files: Optional[list[int]] = None,
    ) -> None:
        _ = tab_id
        bind_contextvars(job_id=job_id)
        task = asyncio.current_task()
        if task is not None:
            async with self._active_tasks_lock:
                self._active_tasks[job_id] = cast(asyncio.Task[None], task)

        consumer_task: Optional[asyncio.Task[None]] = None
        loop = asyncio.get_running_loop()
        out_dir = ""
        try:
            while True:
                settings = self._settings_repository.load()
                if not settings.torrentEnabled:
                    await asyncio.to_thread(
                        self._job_repository.update_job,
                        job_id,
                        status="failed",
                        error="Torrent downloads are disabled in Settings.",
                    )
                    await self._connection_manager.broadcast(
                        {
                            "type": "download_failed",
                            "jobId": job_id,
                            "error": "Torrent downloads are disabled in Settings.",
                            "stage": "queued",
                        }
                    )
                    return

                out_dir = output_dir or app_settings.DEFAULT_OUTPUT_DIR
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
                    return

                out_dir = await self._file_service.resolve_output_dir(out_dir)

                async with self._torrent_jobs_condition:
                    while len(self._active_torrent_jobs) >= max(
                        1, self._settings_repository.load().torrentMaxActive
                    ):
                        await self._torrent_jobs_condition.wait()
                    self._active_torrent_jobs.add(job_id)
                    break

            event_queue: asyncio.Queue[Dict[str, object]] = asyncio.Queue()
            consumer_task = asyncio.create_task(
                self._forward_progress_events(event_queue)
            )

            await asyncio.to_thread(
                self._job_repository.update_job, job_id, status="queued"
            )
            queued_job = await asyncio.to_thread(
                self._job_repository.get_job, job_id
            )
            await self._connection_manager.broadcast(
                {
                    "type": "download_queued",
                    "jobId": job_id,
                    "outputPath": out_dir,
                    "url": url,
                    "title": queued_job.title if queued_job else None,
                    "mediaType": "torrent",
                }
            )
            await asyncio.to_thread(
                self._job_repository.update_job, job_id, status="downloading"
            )
            logger.info(f"Torrent download started for job {job_id}: {redact_url(url)}")

            filepath = await asyncio.to_thread(
                self._torrent_downloader.download,
                job_id,
                url,
                Path(out_dir),
                loop=loop,
                event_queue=event_queue,
                settings=settings,
                selected_files=selected_files,
            )

            size_bytes = None
            if await asyncio.to_thread(os.path.isfile, filepath):
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
            await asyncio.to_thread(
                self._job_repository.update_job, job_id, status="paused"
            )
            await self._broadcast_jobs_list()
        except Exception as error:
            logger.exception(f"Torrent download failed for job {job_id}: {error}")
            await self._connection_manager.broadcast(
                {
                    "type": "download_failed",
                    "jobId": job_id,
                    "error": str(error),
                    "stage": "downloading",
                }
            )
        finally:
            async with self._active_tasks_lock:
                self._active_tasks.pop(job_id, None)
            async with self._torrent_jobs_condition:
                self._active_torrent_jobs.discard(job_id)
                self._torrent_jobs_condition.notify_all()
            if consumer_task is not None:
                consumer_task.cancel()
                try:
                    await asyncio.wait_for(consumer_task, timeout=10.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        f"Torrent consumer for job {job_id} did not finish within 10s after cancellation"
                    )
                except asyncio.CancelledError:
                    pass
            clear_contextvars()
