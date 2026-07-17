import json
from typing import List

from pydantic import BaseModel

from app.config import get_config_file_path, settings, write_json_atomic
from app.utils.logger import logger


class Category(BaseModel):
    name: str
    path: str


# Path to the categories storage file
CATEGORIES_FILE = get_config_file_path("categories.json")


def load_categories() -> List[Category]:
    if not CATEGORIES_FILE.exists():
        default_cats = [{"name": "Default", "path": settings.DEFAULT_OUTPUT_DIR}]
        try:
            write_json_atomic(CATEGORIES_FILE, default_cats)
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
        write_json_atomic(CATEGORIES_FILE, data)
    except Exception as e:
        logger.error(f"Failed to save categories.json: {e}")
        raise RuntimeError(f"Failed to save categories: {e}") from e
