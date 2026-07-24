"""Abstract interfaces for repositories, engines, and gateways."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from app.schemas.category import Category
from app.schemas.job import JobInfo
from app.schemas.settings import AppSettings


class ISettingsRepository(ABC):
    """Persistence for user-facing application settings."""

    @abstractmethod
    def load(self) -> AppSettings:
        """Return the current settings, creating defaults if needed."""
        raise NotImplementedError

    @abstractmethod
    def save(self, settings: AppSettings) -> None:
        """Persist the given settings."""
        raise NotImplementedError


class ICategoriesRepository(ABC):
    """Persistence for download categories."""

    @abstractmethod
    def load(self) -> List[Category]:
        """Return the stored categories."""
        raise NotImplementedError

    @abstractmethod
    def save(self, categories: List[Category]) -> None:
        """Persist the given categories."""
        raise NotImplementedError


class IJobRepository(ABC):
    """Persistence and lifecycle for download jobs."""

    @abstractmethod
    def create_job(self, job_id: str, url: str, status: str = "queued") -> JobInfo:
        raise NotImplementedError

    @abstractmethod
    def get_job(self, job_id: str) -> Optional[JobInfo]:
        raise NotImplementedError

    @abstractmethod
    def update_job(
        self, job_id: str, persist: bool = True, **kwargs: Any
    ) -> Optional[JobInfo]:
        raise NotImplementedError

    @abstractmethod
    def remove_job(self, job_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def list_jobs(self) -> Dict[str, JobInfo]:
        raise NotImplementedError

    @abstractmethod
    def trigger_pause(self, job_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def trigger_resume(self, job_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def is_paused(self, job_id: str) -> bool:
        raise NotImplementedError


class IConnectionManager(Protocol):
    """Outbound WebSocket gateway."""

    async def send_message(self, tab_id: int, message: dict[str, Any]) -> None:
        ...

    async def broadcast(self, message: dict[str, Any]) -> None:
        ...


class IProbeEngine(ABC):
    """Probe a URL and return normalized metadata."""

    @abstractmethod
    def probe(
        self,
        job_id: str,
        url: str,
        settings: AppSettings,
        referer: Optional[str] = None,
        page_title: Optional[str] = None,
        mime_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError


class IDownloadEngine(ABC):
    """Download a single job to completion and emit progress events."""

    @abstractmethod
    def download(self, job_id: str, url: str, output_dir: Path, **kwargs: Any) -> str:
        """Return the final file path."""
        raise NotImplementedError


class IDirectoryPicker(ABC):
    """Native directory picker gateway."""

    @abstractmethod
    async def pick(self, initial_dir: Optional[Path] = None) -> Optional[Path]:
        raise NotImplementedError
