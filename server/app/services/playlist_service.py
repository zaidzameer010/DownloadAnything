"""Playlist download orchestration.

A playlist job is a parent that owns one child yt-dlp job per selected entry.
The parent aggregates child progress and broadcasts a single combined progress
event so the UI can show one accordion row per playlist.
"""

import asyncio
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from app.config import settings as app_settings

from app.schemas.settings import AppSettings
from app.services.file_service import FileService
from app.services.interfaces import (
    IConnectionManager,
    IJobRepository,
    ISettingsRepository,
)
from app.utils.logger import bind_contextvars, clear_contextvars, get_logger

logger = get_logger(__name__)


def _sanitize_folder_name(name: Optional[str]) -> str:
    """Return a filesystem-safe subfolder name from a playlist title."""
    if not name:
        return "playlist"
    name = name.strip()
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name.strip(". ")
    if not name:
        return "playlist"
    if len(name) > 100:
        name = name[:100].rstrip()
    return name


def _best_format_placeholder(settings: AppSettings) -> Dict[str, Any]:
    """Minimal format summary so the yt-dlp downloader treats the entry as video."""
    ext = (settings.mergeFormat or "mkv").strip().lstrip(".").lower()
    return {
        "formatId": "best",
        "label": "Best Available",
        "ext": ext,
        "height": 1080,
        "width": 0,
        "fps": 30,
        "codecFamily": "video",
        "estSizeBytes": 0,
        "isStream": False,
        "isCombined": True,
    }


class PlaylistService:
    """Manages the lifecycle of a parent playlist job and its child entries."""

    def __init__(
        self,
        connection_manager: IConnectionManager,
        job_repository: IJobRepository,
        file_service: FileService,
        settings_repository: ISettingsRepository,
        download_service: Any,
    ) -> None:
        self._connection_manager = connection_manager
        self._job_repository = job_repository
        self._file_service = file_service
        self._settings_repository = settings_repository
        self._download_service = download_service

    async def _broadcast_jobs_list(self) -> None:
        jobs = list((await asyncio.to_thread(self._job_repository.list_jobs)).values())
        await self._connection_manager.broadcast(
            {"type": "jobs_list", "jobs": [j.model_dump() for j in jobs]}
        )

    async def start_download(
        self,
        tab_id: int,
        job_id: str,
        playlist: Dict[str, Any],
        output_dir: str,
        selected_indices: List[int],
        referer: Optional[str] = None,
        page_url: Optional[str] = None,
        conflict_resolution: str = "replace",
    ) -> None:
        bind_contextvars(job_id=job_id)
        try:
            settings = await asyncio.to_thread(self._settings_repository.load)

            raw_entries = playlist.get("entries")
            entries: List[Dict[str, Any]] = []
            if isinstance(raw_entries, list):
                for raw_entry in raw_entries:
                    if isinstance(raw_entry, dict):
                        entries.append(cast(Dict[str, Any], raw_entry))

            parent = await asyncio.to_thread(self._job_repository.get_job, job_id)
            if parent is None:
                original_url = cast(str, entries[0].get("url", "")) if entries else ""
                parent = await asyncio.to_thread(
                    self._job_repository.create_job, job_id, original_url or page_url or "", status="queued"
                )

            base_output_dir = output_dir or parent.output_dir or app_settings.DEFAULT_OUTPUT_DIR
            if not await self._file_service.is_path_allowed(base_output_dir):
                await asyncio.to_thread(
                    self._job_repository.update_job,
                    job_id,
                    status="failed",
                    error="Selected output directory is not allowed.",
                )
                await self._broadcast_jobs_list()
                return

            resolved_base = await self._file_service.resolve_output_dir(base_output_dir)
            playlist_title = cast(Optional[str], playlist.get("title")) or parent.title or "Playlist"
            playlist_dir = str(Path(resolved_base) / _sanitize_folder_name(playlist_title))

            selected_set = set(selected_indices)
            child_ids: List[str] = []

            for entry in entries:
                if entry.get("index") not in selected_set:
                    continue
                entry_url = entry.get("url")
                if not entry_url:
                    continue
                child_id = f"{job_id}_pl_{entry.get('index')}"
                child_ids.append(child_id)

                await asyncio.to_thread(
                    self._job_repository.create_job, child_id, entry_url, status="queued"
                )
                entry_size = cast(Optional[float], entry.get("size")) or 0.0
                await asyncio.to_thread(
                    self._job_repository.update_job,
                    child_id,
                    parent_job_id=job_id,
                    title=entry.get("title") or child_id,
                    duration=entry.get("duration"),
                    thumbnail=entry.get("thumbnail"),
                    uploader=entry.get("uploader"),
                    media_type="ytdlp",
                    mime="video/mp4",
                    format_id="best",
                    output_dir=playlist_dir,
                    referer=referer,
                    page_url=page_url,
                    formats=[_best_format_placeholder(settings)],
                    status="queued",
                    combined_total_bytes=entry_size,
                )

            if not child_ids:
                await asyncio.to_thread(
                    self._job_repository.update_job,
                    job_id,
                    status="failed",
                    error="No playlist entries were selected for download.",
                )
                await self._broadcast_jobs_list()
                return

            await asyncio.to_thread(
                self._job_repository.update_job,
                job_id,
                title=playlist_title,
                media_type="playlist",
                playlist_entries=entries,
                playlist_selected_indices=selected_indices,
                playlist_child_job_ids=child_ids,
                output_dir=playlist_dir,
                referer=referer,
                page_url=page_url,
                status="queued",
            )

            await self._connection_manager.broadcast(
                {
                    "type": "download_queued",
                    "jobId": job_id,
                    "outputPath": playlist_dir,
                    "url": parent.url,
                    "title": playlist_title,
                    "mediaType": "playlist",
                }
            )

            for child_id in child_ids:
                child = await asyncio.to_thread(self._job_repository.get_job, child_id)
                if not child:
                    continue
                await self._download_service.start_download(
                    tab_id,
                    child_id,
                    child.url,
                    "best",
                    playlist_dir,
                    conflict_resolution=conflict_resolution,
                    referer=child.referer,
                    media_type="ytdlp",
                )

            await asyncio.to_thread(
                self._job_repository.update_job, job_id, status="downloading"
            )
            await self._broadcast_jobs_list()
        finally:
            clear_contextvars()

    async def pause(self, job_id: str) -> bool:
        parent = await asyncio.to_thread(self._job_repository.get_job, job_id)
        if not parent or not parent.playlist_child_job_ids:
            return False
        for child_id in parent.playlist_child_job_ids:
            await asyncio.to_thread(self._job_repository.trigger_pause, child_id)
        await asyncio.to_thread(
            self._job_repository.update_job, job_id, status="paused"
        )
        await self._broadcast_jobs_list()
        return True

    async def resume(self, job_id: str) -> bool:
        parent = await asyncio.to_thread(self._job_repository.get_job, job_id)
        if not parent or not parent.playlist_child_job_ids:
            return False
        for child_id in parent.playlist_child_job_ids:
            child = await asyncio.to_thread(self._job_repository.get_job, child_id)
            if not child:
                continue
            if child.status in ("paused", "failed"):
                await asyncio.to_thread(self._job_repository.trigger_resume, child_id)
                if not self._download_service.is_active(child_id):
                    await self._download_service.start_download(
                        0,
                        child_id,
                        child.url,
                        child.format_id or "best",
                        child.output_dir or parent.output_dir,
                        conflict_resolution="replace",
                        referer=child.referer,
                        media_type=child.media_type or "ytdlp",
                    )
        await asyncio.to_thread(
            self._job_repository.update_job, job_id, status="downloading"
        )
        await self._broadcast_jobs_list()
        return True

    async def cancel(self, job_id: str) -> bool:
        parent = await asyncio.to_thread(self._job_repository.get_job, job_id)
        if not parent:
            return False
        child_ids = parent.playlist_child_job_ids or []
        if not child_ids:
            # Fallback: if the parent's child list was never persisted or is out
            # of sync, find any jobs that still reference this parent.
            all_jobs = await asyncio.to_thread(self._job_repository.list_jobs)
            child_ids = [
                j.job_id for j in all_jobs.values() if j.parent_job_id == job_id
            ]
        for child_id in child_ids:
            await asyncio.to_thread(self._job_repository.trigger_pause, child_id)
            if self._download_service.is_active(child_id):
                try:
                    await self._download_service.cancel(child_id)
                except Exception as exc:
                    logger.warning(f"Failed to cancel active child {child_id}: {exc}")
            child = await asyncio.to_thread(self._job_repository.get_job, child_id)
            if child and child.file_path and child.progress < 100.0:
                await self._file_service.maybe_trash_incomplete(
                    child.file_path, child.progress
                )
            await asyncio.to_thread(self._job_repository.remove_job, child_id)
        await asyncio.to_thread(self._job_repository.remove_job, job_id)
        await self._connection_manager.broadcast(
            {"type": "download_canceled", "jobId": job_id}
        )
        await self._broadcast_jobs_list()
        return True

    async def remove_child(self, parent_job_id: str, child_job_id: str) -> bool:
        """Remove a single child from its playlist parent.

        If the parent ends up with no children, the parent is removed too.
        Otherwise the parent's child list and aggregate progress are updated.
        """
        parent = await asyncio.to_thread(self._job_repository.get_job, parent_job_id)
        if not parent:
            return False
        child_ids = parent.playlist_child_job_ids or []
        if child_job_id not in child_ids:
            return False
        new_ids = [cid for cid in child_ids if cid != child_job_id]
        if not new_ids:
            await asyncio.to_thread(self._job_repository.remove_job, parent_job_id)
        else:
            await asyncio.to_thread(
                self._job_repository.update_job,
                parent_job_id,
                playlist_child_job_ids=new_ids,
            )
            await self._download_service.aggregate_parent_by_id(parent_job_id)
        return True

    async def remove(self, job_id: str) -> bool:
        """Alias for cancel; a playlist remove should stop and clean up children."""
        return await self.cancel(job_id)
