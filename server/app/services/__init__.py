"""Application services."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from app.services.category_service import CategoryService
    from app.services.download_service import DownloadService
    from app.services.file_service import FileService
    from app.services.job_service import JobService
    from app.services.probe_service import ProbeService
    from app.services.settings_service import SettingsService
    from app.services.torrent_service import TorrentService

__all__ = [
    "CategoryService",
    "DownloadService",
    "FileService",
    "JobService",
    "ProbeService",
    "SettingsService",
    "TorrentService",
]

_IMPORT_MAP = {
    "CategoryService": "app.services.category_service",
    "DownloadService": "app.services.download_service",
    "FileService": "app.services.file_service",
    "JobService": "app.services.job_service",
    "ProbeService": "app.services.probe_service",
    "SettingsService": "app.services.settings_service",
    "TorrentService": "app.services.torrent_service",
}


def __getattr__(name: str) -> object:
    if name in _IMPORT_MAP:
        module = importlib.import_module(_IMPORT_MAP[name])
        return cast(object, getattr(module, name))
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
