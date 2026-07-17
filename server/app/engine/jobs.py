import json
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

from app.config import get_config_file_path, write_json_atomic
from app.utils.logger import logger

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
    stream_phase: str = "single"
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
    probe_format_ids: Optional[List[str]] = None
    probe_timestamp: Optional[float] = None
    probe_referer: Optional[str] = None
    media_type: Optional[str] = None
    mime: Optional[str] = None
    filename: Optional[str] = None
    torrent_files: Optional[List[Dict[str, Any]]] = None
    torrent_info_hash: Optional[str] = None
    torrent_piece_length: Optional[int] = None
    torrent_piece_count: Optional[int] = None
    torrent_peers: int = 0
    torrent_seeds: int = 0
    torrent_availability: float = 0.0
    torrent_completed_pieces: int = 0
    added_at: float = 0.0


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
                        if v.get("status") in [
                            "queued",
                            "probing",
                            "downloading",
                            "postprocessing",
                        ]:
                            v["status"] = "paused"
                            v["speed"] = 0.0
                            v["eta"] = 0.0
                        job = JobInfo(**v)
                        self._jobs[k] = job
                        self._pause_events[k] = threading.Event()
            except Exception as e:
                logger.error(f"Failed to load jobs from file: {e}")

    def _save_jobs(self):
        try:
            data = {k: v.model_dump() for k, v in self._jobs.items()}
            write_json_atomic(JOBS_FILE, data)
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
        self, job_id: str, persist: bool = True, **kwargs: object
    ) -> Optional[JobInfo]:
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
