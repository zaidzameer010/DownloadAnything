import json
import threading
from pathlib import Path
from pydantic import BaseModel
from typing import Optional
from fastapi import APIRouter, HTTPException, status
from app.utils.logger import logger

router = APIRouter(prefix="/api")

class AppSettings(BaseModel):
    mergeFormat: str = "mkv"
    embedThumbnail: bool = True
    embedSubs: bool = False
    cookiesFromBrowser: Optional[str] = None # e.g. "chrome", "firefox", "safari", "none" or null
    
    # yt-dlp configs
    concurrentFragmentDownloads: int = 4
    downloadRetries: int = 10
    fragmentRetries: int = 10
    rateLimit: Optional[str] = None # e.g. "50K", "1M", "5M" or null (unlimited)
    subtitlesLangs: str = "all" # comma separated list of language tags or "all"
    ffmpegLocation: Optional[str] = None # custom path to ffmpeg
    
    # aria2 configs
    useAria2: bool = True
    aria2MaxConnections: int = 16
    aria2ConcurrentDownloads: int = 5
    aria2Split: int = 16
    aria2MinSplitSize: str = "1M"
    aria2Preallocate: bool = True
    aria2CheckCertificate: bool = True
    aria2AlwaysResume: bool = True
    
    # General queue limit
    maxConcurrentDownloads: int = 2

from app.config import settings, get_config_file_path

# Persisted configurations file
SETTINGS_FILE = get_config_file_path("settings.json")

_settings_cache: Optional[AppSettings] = None
_settings_lock = threading.Lock()

def load_settings() -> AppSettings:
    global _settings_cache
    with _settings_lock:
        if _settings_cache is not None:
            return _settings_cache

        if not SETTINGS_FILE.exists():
            defaults = AppSettings()
            try:
                with open(SETTINGS_FILE, "w") as f:
                    json.dump(defaults.model_dump(), f, indent=2)
            except Exception as e:
                logger.error(f"Failed to write default settings.json: {e}")
            _settings_cache = defaults
            return defaults
            
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
                loaded = AppSettings(**data)
                _settings_cache = loaded
                return loaded
        except Exception as e:
            logger.error(f"Failed to read settings.json: {e}")
            fallback = AppSettings()
            _settings_cache = fallback
            return fallback

def save_settings_to_file(settings_data: AppSettings):
    global _settings_cache
    with _settings_lock:
        try:
            with open(SETTINGS_FILE, "w") as f:
                json.dump(settings_data.model_dump(), f, indent=2)
            _settings_cache = settings_data
        except Exception as e:
            logger.error(f"Failed to save settings.json: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to save configurations: {e}"
            )

@router.get("/settings", response_model=AppSettings)
async def get_settings():
    return load_settings()

@router.post("/settings", response_model=AppSettings)
async def save_settings(settings_data: AppSettings):
    save_settings_to_file(settings_data)
    return settings_data
