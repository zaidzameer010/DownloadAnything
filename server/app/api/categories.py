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
            with open(CATEGORIES_FILE, "w") as f:
                json.dump(default_cats, f, indent=2)
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

def save_categories_to_file(categories: List[Category]):
    try:
        data = [c.model_dump() for c in categories]
        with open(CATEGORIES_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save categories.json: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save categories: {e}"
        )

@router.get("/categories", response_model=List[Category])
async def get_categories():
    return load_categories()

@router.post("/categories", response_model=List[Category])
async def save_categories(categories: List[Category]):
    save_categories_to_file(categories)
    return categories
