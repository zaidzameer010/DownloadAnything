from pathlib import Path
from typing import Optional

from app.config import get_config_file_path
from app.repositories.base import JsonRepositoryBase
from app.schemas.settings import AppSettings
from app.utils.logger import get_logger

logger = get_logger(__name__)


class SettingsRepository(JsonRepositoryBase[AppSettings]):
    """Thread-safe JSON persistence for AppSettings."""

    def __init__(self, file_path: Optional[Path] = None) -> None:
        super().__init__(file_path or get_config_file_path("settings.json"))

    def load(self) -> AppSettings:
        with self._lock:
            if self._cache is not None:
                return self._cache

            data = self._read_json()
            if data is None:
                defaults = AppSettings()
                try:
                    self._write_json(defaults.model_dump())
                except Exception as e:
                    logger.error(f"Failed to write default settings.json: {e}")
                self._cache = defaults
                return defaults

            try:
                loaded = AppSettings(**data)
            except Exception as e:
                logger.error(f"Failed to parse settings.json: {e}")
                loaded = AppSettings()
            self._cache = loaded
            return loaded

    def save(self, settings: AppSettings) -> None:
        with self._lock:
            try:
                self._write_json(settings.model_dump())
                self._cache = settings
            except Exception as e:
                logger.error(f"Failed to save settings.json: {e}")
                raise RuntimeError(f"Failed to save configurations: {e}") from e

    def invalidate_cache(self) -> None:
        with self._lock:
            self._cache = None
