"""Download job lifecycle service."""

import asyncio
from typing import Any, Dict, Optional

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
