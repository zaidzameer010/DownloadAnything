import json
import threading
from typing import Optional

from pydantic import BaseModel

from app.config import get_config_file_path, write_json_atomic
from app.utils.logger import logger


class AppSettings(BaseModel):
    mergeFormat: str = "mkv"
    embedThumbnail: bool = True
    embedSubs: bool = False
    cookiesFromBrowser: Optional[str] = (
        None  # e.g. "chrome", "firefox", "safari", "none" or null
    )

    # yt-dlp configs
    concurrentFragmentDownloads: int = 4
    downloadRetries: int = 10
    fragmentRetries: int = 10
    rateLimit: Optional[str] = None  # e.g. "50K", "1M", "5M" or null (unlimited)
    subtitlesLangs: str = "all"  # comma separated list of language tags or "all"
    ffmpegLocation: Optional[str] = None  # custom path to ffmpeg

    # aria2 configs
    useAria2: bool = True
    aria2MaxConnections: int = 16
    aria2ConcurrentDownloads: int = 5
    aria2Split: int = 16
    aria2MinSplitSize: str = "1M"
    aria2Preallocate: bool = True
    aria2CheckCertificate: bool = True
    aria2AlwaysResume: bool = True

    # libtorrent configs. Rate limits are KiB/s; zero means unlimited.
    torrentEnabled: bool = True
    torrentMaxActive: int = 32
    torrentDownloadLimit: int = 0
    torrentUploadLimit: int = 0
    torrentOutputDir: Optional[str] = None
    torrentSeedRatio: float = 100.0
    torrentSeedTimeMinutes: int = 100000
    torrentPeerLimit: int = 2000
    torrentUploadPeerLimit: int = 500


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
                write_json_atomic(SETTINGS_FILE, defaults.model_dump())
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
            write_json_atomic(SETTINGS_FILE, settings_data.model_dump())
            _settings_cache = settings_data
        except Exception as e:
            logger.error(f"Failed to save settings.json: {e}")
            raise RuntimeError(f"Failed to save configurations: {e}") from e
