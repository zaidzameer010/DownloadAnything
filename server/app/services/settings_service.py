"""Application settings service."""

import asyncio

from app.schemas.settings import AppSettings
from app.services.interfaces import ISettingsRepository


class SettingsService:
    """User-facing settings operations backed by an ISettingsRepository."""

    def __init__(self, settings_repository: ISettingsRepository) -> None:
        self._repository = settings_repository

    async def get_settings(self) -> AppSettings:
        return await asyncio.to_thread(self._repository.load)

    async def save_settings(self, settings: AppSettings) -> AppSettings:
        def _save() -> AppSettings:
            self._repository.save(settings)
            return settings

        return await asyncio.to_thread(_save)
