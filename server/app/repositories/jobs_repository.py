import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import orjson

from app.config import get_config_file_path, write_json_atomic
from app.schemas.job import JobInfo
from app.services.interfaces import IJobRepository
from app.utils.logger import get_logger

logger = get_logger(__name__)


class JobRepository(IJobRepository):
    """Thread-safe JSON persistence and state for download jobs."""

    def __init__(self, file_path: Optional[Path] = None) -> None:
        self._jobs_file = file_path or get_config_file_path("jobs.json")
        self._jobs: Dict[str, JobInfo] = {}
        self._pause_events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._load_jobs()

    def _load_jobs(self) -> None:
        if not self._jobs_file.exists():
            return
        try:
            with open(self._jobs_file, "r") as f:
                data = orjson.loads(f.read())
            for k, v in data.items():
                if v.get("status") in [
                    "queued",
                    "probing",
                    "downloading",
                    "postprocessing",
                    "seeding",
                ]:
                    v["status"] = "paused"
                    v["speed"] = 0.0
                    v["eta"] = 0.0
                job = JobInfo(**v)
                self._jobs[k] = job
                self._pause_events[k] = threading.Event()
        except Exception as e:
            logger.error(f"Failed to load jobs from file: {e}")

    def _save_jobs(self) -> None:
        try:
            data = {k: v.model_dump() for k, v in self._jobs.items()}
            write_json_atomic(self._jobs_file, data)
        except Exception as e:
            logger.error(f"Failed to save jobs to file: {e}")

    def create_job(self, job_id: str, url: str, status: str = "queued") -> JobInfo:
        with self._lock:
            job = JobInfo(job_id=job_id, url=url, status=status, added_at=time.time())
            self._jobs[job_id] = job
            self._pause_events[job_id] = threading.Event()
            self._save_jobs()
            return job

    def get_job(self, job_id: str) -> Optional[JobInfo]:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.model_copy() if job else None

    def get_job_snapshot(self, job_id: str) -> Optional[Tuple[str, Optional[str]]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                return job.status, job.file_path
            return None

    def update_job(
        self, job_id: str, persist: bool = True, **kwargs: Any
    ) -> Optional[JobInfo]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            valid_updates = {k: v for k, v in kwargs.items() if hasattr(job, k)}
            updated_job = job.model_copy(update=valid_updates)
            self._jobs[job_id] = updated_job
            if persist:
                self._save_jobs()
            return updated_job

    def trigger_pause(self, job_id: str) -> bool:
        with self._lock:
            event = self._pause_events.get(job_id)
            if event is None:
                return False
            event.set()
            job = self._jobs.get(job_id)
            if job and job.status in [
                "queued",
                "downloading",
                "postprocessing",
                "seeding",
            ]:
                updated_job = job.model_copy(update={"status": "paused"})
                self._jobs[job_id] = updated_job
                self._save_jobs()
            return True

    def trigger_resume(self, job_id: str) -> bool:
        with self._lock:
            pause_event = self._pause_events.get(job_id)
            if pause_event is None:
                return False
            job = self._jobs.get(job_id)
            if job is None:
                return False

            if job.status == "paused":
                pause_event.clear()
                updated_job = job.model_copy(update={"status": "queued"})
            elif job.status == "failed":
                pause_event.clear()
                updated_job = job.model_copy(update={"status": "queued", "error": None})
            else:
                return False

            self._jobs[job_id] = updated_job
            self._save_jobs()
            return True

    def is_paused(self, job_id: str) -> bool:
        with self._lock:
            event = self._pause_events.get(job_id)
            return event.is_set() if event else False

    def remove_job(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)
            self._pause_events.pop(job_id, None)
            self._save_jobs()

    def list_jobs(self) -> Dict[str, JobInfo]:
        with self._lock:
            return {k: v.model_copy() for k, v in self._jobs.items()}
