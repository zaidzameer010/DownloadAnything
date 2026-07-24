"""Filesystem path validation and file operations."""

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from app.api.browse import DirectoryPicker
from app.config import get_app_data_dir, settings as app_settings
from app.engine.title_extractor import resolve_filename
from app.services.interfaces import ICategoriesRepository, IDirectoryPicker
from app.utils.logger import get_logger
from app.utils.trash import send_to_trash

logger = get_logger(__name__)


class FileService:
    """Validate paths, browse directories, and reveal/delete downloaded files."""

    def __init__(
        self,
        categories_repository: ICategoriesRepository,
        directory_picker: Optional[IDirectoryPicker] = None,
    ) -> None:
        self._categories_repository = categories_repository
        self._directory_picker = directory_picker or DirectoryPicker()

    async def get_allowed_output_roots(self) -> List[Path]:
        return await asyncio.to_thread(self._get_allowed_output_roots_sync)

    def _get_allowed_output_roots_sync(self) -> List[Path]:
        roots: List[Path] = []
        try:
            default_root = Path(app_settings.DEFAULT_OUTPUT_DIR).expanduser().resolve()
            roots.append(default_root)
        except (OSError, RuntimeError, ValueError):
            pass

        try:
            for cat in self._categories_repository.load():
                if cat.path:
                    try:
                        resolved = Path(cat.path).expanduser().resolve()
                        roots.append(resolved)
                    except (OSError, RuntimeError, ValueError):
                        continue
        except Exception as exc:
            logger.warning(f"Failed to load categories for allowed roots: {exc}")

        try:
            roots.append((get_app_data_dir() / "temp").resolve())
        except (OSError, RuntimeError, ValueError):
            pass

        seen: set[Path] = set()
        unique: List[Path] = []
        for root in roots:
            try:
                if root not in seen:
                    seen.add(root)
                    unique.append(root)
            except TypeError:
                continue
        return unique

    async def is_path_allowed(self, path: str) -> bool:
        allowed_roots = await self.get_allowed_output_roots()
        return await asyncio.to_thread(self._is_path_allowed_sync, path, allowed_roots)

    def _is_path_allowed_sync(
        self, path: str, allowed_roots: Optional[List[Path]] = None
    ) -> bool:
        if allowed_roots is None:
            allowed_roots = self._get_allowed_output_roots_sync()
        try:
            resolved = Path(path).expanduser().resolve()
        except (OSError, ValueError):
            return False
        for root in allowed_roots:
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    async def resolve_output_dir(self, output_dir: Optional[str]) -> str:
        return await asyncio.to_thread(self._resolve_output_dir_sync, output_dir)

    def _resolve_output_dir_sync(self, output_dir: Optional[str]) -> str:
        if output_dir:
            return str(Path(output_dir).expanduser().resolve())
        return str(Path(app_settings.DEFAULT_OUTPUT_DIR).expanduser().resolve())

    async def check_file_exists(
        self,
        path: str,
        job_id: str,
        filename: Optional[str] = None,
        title: Optional[str] = None,
        ext: Optional[str] = None,
        url: Optional[str] = None,
        mime: Optional[str] = None,
    ) -> dict:
        check_filename = filename or ""
        exists = False
        try:
            allowed_roots = await self.get_allowed_output_roots()
            if not await asyncio.to_thread(
                self._is_path_allowed_sync, path, allowed_roots
            ):
                raise ValueError(f"Rejected file existence check for disallowed path: {path}")

            base_path = Path(path).expanduser().resolve()

            if filename or title or ext or url:
                resolved = await asyncio.to_thread(
                    resolve_filename,
                    url=url or "",
                    filename=filename,
                    mime=mime,
                    page_title=title,
                    preferred_ext=ext,
                    timeout=3.0,
                    allow_network=False,
                )
                check_filename = resolved.filename

            if any(sep in check_filename for sep in ("/", "\\", "..")):
                raise ValueError(
                    f"Rejected filename with path separators: {check_filename}"
                )

            full_path = (base_path / check_filename).resolve()
            full_path.relative_to(base_path)
            exists = await asyncio.to_thread(full_path.exists)
        except (OSError, ValueError) as error:
            logger.warning(
                f"Rejected file existence check for {path}/{check_filename}: {error}"
            )

        return {
            "type": "file_exists_result",
            "exists": exists,
            "filename": check_filename,
            "path": path,
            "jobId": job_id,
        }

    async def reveal_file(self, file_path: str) -> bool:
        if not await self.is_path_allowed(file_path):
            logger.warning(f"Refusing to reveal file outside allowed roots: {file_path}")
            return False
        try:
            return await asyncio.to_thread(self._reveal_file_sync, file_path)
        except Exception as error:
            logger.error(f"Failed to reveal file {file_path}: {error}")
            return False

    def _reveal_file_sync(self, file_path: str) -> bool:
        if not os.path.exists(file_path):
            return False
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", file_path], check=True)
            return True
        if sys.platform == "win32":
            subprocess.run(
                ["explorer", "/select,", os.path.normpath(file_path)],
                check=True,
            )
            return True
        return False

    async def delete_file(self, file_path: str) -> None:
        if not await self.is_path_allowed(file_path):
            logger.warning(f"Refusing to trash file outside allowed roots: {file_path}")
            return
        try:
            await asyncio.to_thread(send_to_trash, file_path)
        except Exception as error:
            logger.error(f"Failed to trash file {file_path}: {error}")

    async def maybe_trash_incomplete(self, file_path: Optional[str], progress: float) -> None:
        if not file_path or progress >= 100.0:
            return
        if not await self.is_path_allowed(file_path):
            logger.warning(
                f"Refusing to trash file outside allowed roots: {file_path}"
            )
            return
        try:
            await asyncio.to_thread(send_to_trash, file_path)
        except Exception as error:
            logger.error(f"Failed to trash file {file_path} on job removal: {error}")

    async def remove_temp_dir(self, job_id: str) -> None:
        temp_root = (get_app_data_dir() / "temp").resolve()
        app_temp_dir = (temp_root / job_id).resolve()
        if app_temp_dir.parent != temp_root:
            logger.warning(f"Refusing to clean unsafe temp path: {app_temp_dir}")
            return

        def _cleanup(path: Path) -> None:
            if path.is_dir():
                shutil.rmtree(path)

        try:
            await asyncio.to_thread(_cleanup, app_temp_dir)
            logger.debug(f"Cleaned up temp folder on job removal: {app_temp_dir}")
        except OSError as error:
            logger.error(f"Failed to remove temp folder {app_temp_dir}: {error}")

    async def browse_directory(self, initial_dir: Optional[str]) -> Optional[str]:
        path = await self._directory_picker.pick(
            Path(initial_dir) if initial_dir else None
        )
        return str(path) if path else None
