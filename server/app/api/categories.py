import json
import os
from pathlib import Path
from typing import List
from pydantic import BaseModel
from app.config import settings, get_config_file_path
from app.utils.logger import logger


class Category(BaseModel):
    name: str
    path: str


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
        raise RuntimeError(f"Failed to save categories: {e}")
