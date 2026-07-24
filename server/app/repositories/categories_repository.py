from pathlib import Path
from typing import List, Optional

from app.config import get_config_file_path, settings
from app.repositories.base import JsonRepositoryBase
from app.schemas.category import Category
from app.utils.logger import get_logger

logger = get_logger(__name__)


class CategoriesRepository(JsonRepositoryBase[List[Category]]):
    """Thread-safe JSON persistence for download categories."""

    def __init__(self, file_path: Optional[Path] = None) -> None:
        super().__init__(file_path or get_config_file_path("categories.json"))

    def load(self) -> List[Category]:
        with self._lock:
            if self._cache is not None:
                return self._cache

            data = self._read_json()
            if data is None:
                default_cats = [Category(name="Default", path=settings.DEFAULT_OUTPUT_DIR)]
                try:
                    self._write_json([c.model_dump() for c in default_cats])
                except Exception as e:
                    logger.error(f"Failed to create default categories.json: {e}")
                self._cache = default_cats
                return default_cats

            try:
                loaded = [Category(**c) for c in data]
            except Exception as e:
                logger.error(f"Failed to read categories.json: {e}")
                loaded = [Category(name="Default", path=settings.DEFAULT_OUTPUT_DIR)]
            self._cache = loaded
            return loaded

    def save(self, categories: List[Category]) -> None:
        with self._lock:
            try:
                self._write_json([c.model_dump() for c in categories])
                self._cache = categories
            except Exception as e:
                logger.error(f"Failed to save categories.json: {e}")
                raise RuntimeError(f"Failed to save categories: {e}") from e

    def invalidate_cache(self) -> None:
        with self._lock:
            self._cache = None
