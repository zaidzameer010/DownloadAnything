"""Probe orchestration and pending-probe cache service."""

import asyncio
import time
from typing import Any, Dict, Optional, Union, cast

from app.engine.codec_filter import process_probe_formats
from app.engine.media_classify import has_dedicated_ytdlp_extractor
from app.engine.probe_validation import classify_probe_exception
from app.engine.torrent import is_magnet_url
from app.services.interfaces import IConnectionManager, IProbeEngine, ISettingsRepository
from app.utils.logger import bind_contextvars, clear_contextvars, get_logger

logger = get_logger(__name__)

PENDING_PROBE_TTL_SECONDS = 15 * 60


class ProbeService:
    """Manage active probes, pending-probe metadata, and TTL pruning."""

    def __init__(
        self,
        connection_manager: IConnectionManager,
        probe_orchestrator: IProbeEngine,
        torrent_prober: IProbeEngine,
        settings_repository: ISettingsRepository,
    ) -> None:
        self._connection_manager = connection_manager
        self._probe_orchestrator = probe_orchestrator
        self._torrent_prober = torrent_prober
        self._settings_repository = settings_repository

        self._pending_probes: Dict[str, Dict[str, object]] = {}
        self._pending_probes_lock = asyncio.Lock()

        self._active_probe_tasks: Dict[str, asyncio.Task[None]] = {}
        self._probe_task_tabs: Dict[str, int] = {}
        self._active_probe_tasks_lock = asyncio.Lock()

    async def start_probe(
        self,
        tab_id: int,
        job_id: str,
        url: str,
        page_title: Optional[str] = None,
        referer: Optional[str] = None,
    ) -> str:
        await self.prune_pending_probes()

        await self._connection_manager.send_message(
            tab_id,
            {
                "type": "probe_started",
                "jobId": job_id,
                "url": url,
            },
        )

        async with self._active_probe_tasks_lock:
            self._probe_task_tabs[job_id] = tab_id
            self._active_probe_tasks[job_id] = asyncio.create_task(
                self._run_probe_task(tab_id, job_id, url, page_title, referer)
            )
        return job_id

    async def cancel_probe(self, job_id: str) -> None:
        async with self._active_probe_tasks_lock:
            task = self._active_probe_tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                pass
        async with self._pending_probes_lock:
            self._pending_probes.pop(job_id, None)

    async def get_pending_probe(self, job_id: str) -> Optional[Dict[str, object]]:
        async with self._pending_probes_lock:
            return self._pending_probes.get(job_id)

    async def pop_pending_probe(self, job_id: str) -> Optional[Dict[str, object]]:
        async with self._pending_probes_lock:
            return self._pending_probes.pop(job_id, None)

    async def restore_pending_probe(
        self, job_id: str, metadata: Dict[str, object]
    ) -> None:
        async with self._pending_probes_lock:
            self._pending_probes[job_id] = metadata

    async def prune_pending_probes(self) -> None:
        cutoff = time.time() - PENDING_PROBE_TTL_SECONDS
        expired_ids: list[str] = []
        async with self._pending_probes_lock:
            for probe_id, metadata in list(self._pending_probes.items()):
                ts = cast(Union[str, float, int, None], metadata.get("probeTimestamp"))
                try:
                    probe_ts = float(ts) if ts is not None else 0.0
                except (TypeError, ValueError):
                    probe_ts = 0.0
                if probe_ts < cutoff:
                    expired_ids.append(probe_id)
            for probe_id in expired_ids:
                self._pending_probes.pop(probe_id, None)

    async def cancel_probes_for_tab(self, tab_id: int) -> None:
        async with self._active_probe_tasks_lock:
            orphaned = [
                job_id
                for job_id, owner_tab_id in self._probe_task_tabs.items()
                if owner_tab_id == tab_id
            ]
        for job_id in orphaned:
            task = None
            async with self._active_probe_tasks_lock:
                task = self._active_probe_tasks.get(job_id)
            if task and not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                except Exception:
                    pass
            async with self._pending_probes_lock:
                self._pending_probes.pop(job_id, None)

    async def _run_probe_task(
        self,
        tab_id: int,
        job_id: str,
        url_to_probe: str,
        page_title: Optional[str] = None,
        referer: Optional[str] = None,
    ) -> None:
        bind_contextvars(tab_id=tab_id, job_id=job_id)
        settings = await asyncio.to_thread(self._settings_repository.load)
        is_ytdlp_supported = False
        try:
            is_ytdlp_supported = (
                not is_magnet_url(url_to_probe)
                and has_dedicated_ytdlp_extractor(url_to_probe)
            )

            if is_magnet_url(url_to_probe):
                torrent = await asyncio.to_thread(
                    self._torrent_prober.probe,
                    job_id=job_id,
                    url=url_to_probe,
                    settings=settings,
                )
                info: Dict[str, Any] = {
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
                    self._probe_orchestrator.probe,
                    job_id=job_id,
                    url=url_to_probe,
                    referer=referer,
                    page_title=page_title,
                    settings=settings,
                )

            formats_json, format_ids = process_probe_formats(
                info, preferred_ext=settings.mergeFormat
            )

            async with self._pending_probes_lock:
                self._pending_probes[job_id] = {
                    "url": url_to_probe,
                    "title": info.get("title"),
                    "filename": info.get("filename"),
                    "duration": info.get("duration"),
                    "thumbnail": info.get("thumbnail"),
                    "uploader": info.get("uploader"),
                    "formats": formats_json,
                    "formatIds": format_ids,
                    "probeTimestamp": time.time(),
                    "probeReferer": referer,
                    "mediaType": info.get("mediaType", "ytdlp"),
                    "fileType": info.get("fileType"),
                    "mime": info.get("mime"),
                    "torrent": info.get("torrent"),
                }

            await self._connection_manager.send_message(
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
                    "fileType": info.get("fileType"),
                    "mime": info.get("mime"),
                    "torrent": info.get("torrent"),
                },
            )
        except asyncio.CancelledError:
            logger.info(f"Probe task for job {job_id} was cancelled.")
            async with self._pending_probes_lock:
                self._pending_probes.pop(job_id, None)
        except Exception as err:
            failure = classify_probe_exception(err)
            logger.error(
                f"Probe failed for {url_to_probe} [{failure.category}]: {failure.message}"
            )
            is_unsupported = failure.category == "unsupported"
            await self._connection_manager.send_message(
                tab_id,
                {
                    "type": "probe_failed",
                    "jobId": job_id,
                    "error": failure.message,
                    "errorCategory": failure.category,
                    "suggestion": failure.suggestion,
                    "isUnsupportedUrl": is_unsupported,
                    "skipFallback": is_ytdlp_supported,
                },
            )
        finally:
            async with self._active_probe_tasks_lock:
                self._active_probe_tasks.pop(job_id, None)
                self._probe_task_tabs.pop(job_id, None)
            clear_contextvars()
