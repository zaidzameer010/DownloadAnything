import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List
from pydantic import BaseModel
from app.config import get_config_file_path

JOBS_FILE = get_config_file_path("jobs.json")

class DownloadPaused(Exception):
    """Exception raised when a download is paused."""
    pass

class JobInfo(BaseModel):
    model_config = {"arbitrary_types_allowed": True, "frozen": False}

    job_id: str
    url: str
    status: str  # queued, probing, downloading, postprocessing, completed, failed, canceled, paused
    progress: float = 0.0
    # Video stream bytes
    downloaded_bytes: float = 0.0
    total_bytes: float = 0.0
    # Audio stream bytes (populated when downloading separate video+audio)
    audio_downloaded_bytes: float = 0.0
    audio_total_bytes: float = 0.0
    # Combined bytes across all streams
    combined_downloaded_bytes: float = 0.0
    combined_total_bytes: float = 0.0
    # Which stream phase: 'video', 'audio', or 'single'
    stream_phase: str = 'single'
    speed: float = 0.0
    eta: float = 0.0
    format_id: Optional[str] = None
    output_dir: Optional[str] = None
    error: Optional[str] = None
    title: Optional[str] = None
    duration: Optional[float] = None
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None
    file_path: Optional[str] = None
    formats: Optional[List[Any]] = None
    fragment_index: Optional[int] = None
    fragment_count: Optional[int] = None
    referer: Optional[str] = None
    media_type: Optional[str] = None

class JobRegistry:
    def __init__(self):
        self._jobs: Dict[str, JobInfo] = {}
        self._pause_events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._load_jobs()

    def _load_jobs(self):
        if JOBS_FILE.exists():
            try:
                with open(JOBS_FILE, "r") as f:
                    data = json.load(f)
                    for k, v in data.items():
                        if v.get("status") in ["queued", "probing", "downloading", "postprocessing"]:
                            v["status"] = "paused"
                            v["speed"] = 0.0
                            v["eta"] = 0.0
                        job = JobInfo(**v)
                        self._jobs[k] = job
                        self._pause_events[k] = threading.Event()
            except Exception as e:
                from app.utils.logger import logger
                logger.error(f"Failed to load jobs from file: {e}")

    def _save_jobs(self):
        try:
            with open(JOBS_FILE, "w") as f:
                data = {k: v.model_dump() for k, v in self._jobs.items()}
                json.dump(data, f, indent=2)
        except Exception as e:
            from app.utils.logger import logger
            logger.error(f"Failed to save jobs to file: {e}")

    def create_job(self, job_id: str, url: str, status: str = "queued") -> JobInfo:
        with self._lock:
            job = JobInfo(job_id=job_id, url=url, status=status)
            self._jobs[job_id] = job
            self._pause_events[job_id] = threading.Event()
            self._save_jobs()
            return job

    def get_job(self, job_id: str) -> Optional[JobInfo]:
        with self._lock:
            return self._jobs.get(job_id)

    def get_job_snapshot(self, job_id: str) -> Optional[Tuple[str, Optional[str]]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                return job.status, job.file_path
            return None

    def update_job(self, job_id: str, persist: bool = True, **kwargs) -> Optional[JobInfo]:
        """
        Update a job's fields.
        persist=False skips the filesystem write — use this for high-frequency
        progress ticks (bytes/speed/eta) to avoid disk I/O on every hook call.
        persist=True (default) writes to disk — use for status transitions.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                valid_updates = {k: v for k, v in kwargs.items() if hasattr(job, k)}
                updated_job = job.model_copy(update=valid_updates)
                self._jobs[job_id] = updated_job
                if persist:
                    self._save_jobs()
                return updated_job
            return None

    def trigger_pause(self, job_id: str) -> bool:
        with self._lock:
            event = self._pause_events.get(job_id)
            if event:
                event.set()
                job = self._jobs.get(job_id)
                if job and job.status in ["queued", "downloading", "postprocessing"]:
                    updated_job = job.model_copy(update={"status": "paused"})
                    self._jobs[job_id] = updated_job
                    self._save_jobs()
                return True
            return False

    def trigger_resume(self, job_id: str) -> bool:
        with self._lock:
            pause_event = self._pause_events.get(job_id)
            if pause_event:
                pause_event.clear()
                job = self._jobs.get(job_id)
                if job and job.status == "paused":
                    updated_job = job.model_copy(update={"status": "downloading"})
                    self._jobs[job_id] = updated_job
                    self._save_jobs()
                return True
            return False

    def is_paused(self, job_id: str) -> bool:
        with self._lock:
            event = self._pause_events.get(job_id)
            return event.is_set() if event else False

    def remove_job(self, job_id: str):
        with self._lock:
            if job_id in self._jobs:
                del self._jobs[job_id]
            if job_id in self._pause_events:
                del self._pause_events[job_id]
            self._save_jobs()

    def list_jobs(self) -> Dict[str, JobInfo]:
        with self._lock:
            return {k: v.model_copy() for k, v in self._jobs.items()}

# Global singleton
jobs_registry = JobRegistry()
