import asyncio
import os
import json
from pathlib import Path
from typing import List
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from app.config import settings
from app.utils.logger import logger

router = APIRouter(prefix="/api")

class Category(BaseModel):
    name: str
    path: str

from app.config import get_config_file_path

# Path to the categories storage file
CATEGORIES_FILE = get_config_file_path("categories.json")

def load_categories() -> List[Category]:
    if not CATEGORIES_FILE.exists():
        default_cats = [
            {"name": "Default", "path": settings.DEFAULT_OUTPUT_DIR}
        ]
        try:
            _write_json_atomic(CATEGORIES_FILE, default_cats)
        except Exception as e:
            logger.error(f"Failed to create default categories.json: {e}")
        return [Category(**c) for c in default_cats]
        
    try:
        with open(CATEGORIES_FILE, "r") as f:
            data = json.load(f)
            return [Category(**c) for c in data]
    except Exception as e:
        logger.error(f"Failed to read categories.json: {e}")
        return [Category(name="Default", path=settings.DEFAULT_OUTPUT_DIR)]

def _write_json_atomic(path: Path, payload: list[dict[str, str]]):
    tmp_path = path.with_name(f"{path.name}.tmp")
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp_path.replace(path)

def save_categories_to_file(categories: List[Category]):
    try:
        data = [c.model_dump() for c in categories]
        _write_json_atomic(CATEGORIES_FILE, data)
    except Exception as e:
        logger.error(f"Failed to save categories.json: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save categories: {e}"
        )

@router.get("/categories", response_model=List[Category])
async def get_categories():
    return await asyncio.to_thread(load_categories)

@router.post("/categories", response_model=List[Category])
async def save_categories(categories: List[Category]):
    await asyncio.to_thread(save_categories_to_file, categories)
    return categories
