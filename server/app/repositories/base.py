"""Base helpers for JSON-backed repositories."""

import threading
from pathlib import Path
from typing import Any, Generic, TypeVar

import orjson

from app.config import write_json_atomic

T = TypeVar("T")


class JsonRepositoryBase(Generic[T]):
    """Thread-safe JSON file repository with an in-memory cache."""

    def __init__(self, file_path: Path) -> None:
        self._file_path = file_path
        self._cache: T | None = None
        self._lock = threading.Lock()

    def _read_json(self) -> Any:
        try:
            with open(self._file_path, "rb") as f:
                return orjson.loads(f.read())
        except FileNotFoundError:
            return None
        except orjson.JSONDecodeError:
            return None

    def _write_json(self, payload: Any) -> None:
        write_json_atomic(self._file_path, payload)
