import asyncio
import threading
from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any
from engine.config import JsonObj

class TaskStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    STITCHING = "stitching"
    EMBEDDING = "embedding"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    PAUSED = "paused"


_ACTIVE_STATES = frozenset(
    {
        TaskStatus.DOWNLOADING,
        TaskStatus.STITCHING,
        TaskStatus.EMBEDDING,
        TaskStatus.FINALIZING,
    }
)

# Broadcast throttle interval (seconds).
_BROADCAST_INTERVAL = 0.1


@dataclass
class DownloadTask:
    task_id: str
    url: str
    format_id: str | None = None
    category: str | None = None
    custom_path: str | None = None
    title: str = "Pending…"
    status: TaskStatus = TaskStatus.QUEUED
    speed: float = 0.0
    progress: float = 0.0
    eta: float = 0.0
    total_bytes: int = 0
    downloaded_bytes: int = 0
    filename: str = ""
    final_path: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    is_video: bool = True
    page_title: str | None = None
    is_stream: bool = False
    headers: dict[str, str] | None = None
    has_custom_title: bool = False
    fragment_index: int | None = None
    fragment_count: int | None = None
    using_aria2c: bool = False
    prev_parts_bytes: int = 0

    # Runtime-only fields (private, never persisted).
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _hold: bool = field(default=False, repr=False)  # cancel == pause, not abort
    _is_running: bool = field(default=False, repr=False)
    _in_queue: bool = field(default=False, repr=False)
    _last_broadcast: float = field(default=0.0, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)

    def update(self, **changes: Any) -> None:
        valid = self.__dataclass_fields__
        for key, value in changes.items():
            if key in valid:
                setattr(self, key, value)

    def to_dict(self) -> JsonObj:
        """Serialise all persisted fields (auto-derived, never drifts)."""
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if not f.name.startswith("_")
        }
