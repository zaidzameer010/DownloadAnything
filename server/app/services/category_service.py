"""Download categories service."""

import asyncio
from typing import List

from app.schemas.category import Category
from app.services.interfaces import ICategoriesRepository


class CategoryService:
    """Persistence and retrieval of download categories."""

    def __init__(self, categories_repository: ICategoriesRepository) -> None:
        self._repository = categories_repository

    async def get_categories(self) -> List[Category]:
        return await asyncio.to_thread(self._repository.load)

    async def save_categories(self, categories: List[Category]) -> List[Category]:
        def _save() -> List[Category]:
            self._repository.save(categories)
            return categories

        return await asyncio.to_thread(_save)
