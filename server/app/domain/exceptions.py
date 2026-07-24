"""Application-wide exception hierarchy."""

from dataclasses import dataclass


class AppError(Exception):
    """Base class for all recoverable application errors."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message


class ValidationError(AppError):
    """Input or configuration validation failed."""


class NotFoundError(AppError):
    """A requested resource was not found."""


class SecurityError(AppError):
    """A security/policy check failed (e.g., path traversal)."""


class DownloadPaused(AppError):
    """Raised when a download worker should abort because the job was paused."""


class DownloadError(AppError):
    """Download failed for a non-pause reason."""


@dataclass(frozen=True)
class ProbeError(AppError):
    """A probe failed in a way that can be categorized for the UI."""

    category: str
    message: str
    suggestion: str | None = None

    def __str__(self) -> str:
        return self.message
