"""Download job lifecycle service."""

import asyncio
from typing import Any, Dict, Optional
import os
from app.engine.title_extractor import _is_unusable_stem
from app.schemas.job import JobInfo
from app.services.interfaces import IJobRepository

_ACTIVE_STATUSES = {"queued", "downloading", "postprocessing", "paused", "seeding"}


class JobService:
    """Higher-level job operations: duplicate detection, pause/resume, and CRUD."""

    def __init__(self, job_repository: IJobRepository) -> None:
        self._repository = job_repository

    async def find_duplicate(self, url: str) -> Optional[JobInfo]:
        return await asyncio.to_thread(self._find_duplicate_sync, url)

    def _find_duplicate_sync(self, url: str) -> Optional[JobInfo]:
        for job in self._repository.list_jobs().values():
            if job.url == url and job.status in _ACTIVE_STATUSES:
                return job
        return None

    async def find_duplicate_by_title(self, title: str) -> Optional[JobInfo]:
        return await asyncio.to_thread(self._find_duplicate_by_title_sync, title)

    def _find_duplicate_by_title_sync(self, title: str) -> Optional[JobInfo]:
        if not title or _is_unusable_stem(title):
            return None
        normalized = title.strip().casefold()
        for job in self._repository.list_jobs().values():
            if job.status not in _ACTIVE_STATUSES:
                continue
            job_title = job.title
            if not job_title or _is_unusable_stem(job_title):
                continue
            if job_title.strip().casefold() == normalized:
                return job
        return None

    async def find_duplicate_by_filename(
        self, filename: str, output_dir: str
    ) -> Optional[JobInfo]:
        return await asyncio.to_thread(
            self._find_duplicate_by_filename_sync, filename, output_dir
        )

    def _find_duplicate_by_filename_sync(
        self, filename: str, output_dir: str
    ) -> Optional[JobInfo]:
        if not filename or not output_dir:
            return None

        target_dir = os.path.normpath(os.path.expanduser(output_dir))
        target_name = os.path.basename(os.path.normpath(filename))
        if not target_name:
            return None

        for job in self._repository.list_jobs().values():
            if job.status not in _ACTIVE_STATUSES:
                continue
            if not job.filename or not job.output_dir:
                continue
            job_dir = os.path.normpath(os.path.expanduser(job.output_dir))
            job_name = os.path.basename(os.path.normpath(job.filename))
            if job_dir == target_dir and job_name.casefold() == target_name.casefold():
                return job
        return None

    async def get_job(self, job_id: str) -> Optional[JobInfo]:
        return await asyncio.to_thread(self._repository.get_job, job_id)

    async def list_jobs(self) -> Dict[str, JobInfo]:
        return await asyncio.to_thread(self._repository.list_jobs)

    async def create_job(
        self, job_id: str, url: str, status: str = "queued"
    ) -> JobInfo:
        return await asyncio.to_thread(
            self._repository.create_job, job_id, url, status
        )

    async def update_job(
        self, job_id: str, persist: bool = True, **kwargs: Any
    ) -> Optional[JobInfo]:
        return await asyncio.to_thread(
            self._repository.update_job, job_id, persist, **kwargs
        )

    async def remove_job(self, job_id: str) -> None:
        return await asyncio.to_thread(self._repository.remove_job, job_id)

    async def pause(self, job_id: str) -> bool:
        return await asyncio.to_thread(self._repository.trigger_pause, job_id)

    async def resume(self, job_id: str) -> bool:
        return await asyncio.to_thread(self._repository.trigger_resume, job_id)

    async def is_paused(self, job_id: str) -> bool:
        return await asyncio.to_thread(self._repository.is_paused, job_id)
