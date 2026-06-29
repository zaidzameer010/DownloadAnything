import logging
import os
import platform
import sys
from pathlib import Path
from typing import Any, Literal
import orjson
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger("dma-engine")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

type JsonObj = dict[str, Any]


def ensure_system_path() -> None:
    """Prepend common Homebrew / local bin dirs to PATH so ffmpeg etc. resolve."""
    extra: list[str] = []
    if platform.system() == "Darwin":
        extra = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin"]
    if not extra:
        return
    current = os.environ.get("PATH", "").split(os.pathsep)
    changed = False
    for p in extra:
        if p not in current and os.path.exists(p):
            current.insert(0, p)
            changed = True
    if changed:
        os.environ["PATH"] = os.pathsep.join(current)


def app_data_dir() -> Path:
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        path = home / "Library" / "Application Support" / "DownloadAnything"
    elif system == "Windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        path = base / "DownloadAnything"
    else:
        path = home / ".config" / "DownloadAnything"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_download_path() -> Path:
    downloads = Path.home() / "Downloads"
    base = downloads if downloads.exists() else Path.home()
    return base / "DownloadAnything"


APP_DATA_DIR = app_data_dir()
SETTINGS_FILE = APP_DATA_DIR / "settings.json"
BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "dist" / "static"
TMP_DIR = APP_DATA_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)


def _default_settings() -> JsonObj:
    base = str(default_download_path())
    return {
        "max_concurrent_downloads": 3,
        "fallback_codecs": ["av01", "vp09", "avc01"],
        "default_download_path": base,
        "categories": {
            "Videos": str(default_download_path() / "videos"),
            "Courses": str(default_download_path() / "courses"),
            "Music": str(default_download_path() / "music"),
            "Cinematic": str(default_download_path() / "cinematic"),
        },
        "rate_limit_bytes_per_sec": 0,
        "merge_output_format": "mp4",
        "concurrent_fragments": 16,
        "embed_thumbnail": False,
        "embed_subtitles": False,
        "subtitle_language": "en",
        "proxy": "",
        "cookies_from_browser": "none",
    }


class AppSettings(BaseModel):
    max_concurrent_downloads: int = Field(default=3, ge=1, le=32)
    fallback_codecs: list[str] = Field(
        default_factory=lambda: ["av01", "vp09", "avc01"]
    )
    default_download_path: str
    categories: dict[str, str]
    rate_limit_bytes_per_sec: int = Field(default=0, ge=0)
    merge_output_format: Literal["mp4", "mkv", "webm"] = "mp4"
    concurrent_fragments: int = Field(default=16, ge=1)
    embed_thumbnail: bool = False
    embed_subtitles: bool = False
    subtitle_language: str = "en"
    proxy: str = ""
    cookies_from_browser: str = "none"


class SettingsUpdate(BaseModel):
    max_concurrent_downloads: int | None = Field(default=None, ge=1, le=32)
    fallback_codecs: list[str] | None = None
    default_download_path: str | None = None
    categories: dict[str, str] | None = None
    rate_limit_bytes_per_sec: int | None = Field(default=None, ge=0)
    merge_output_format: Literal["mp4", "mkv", "webm"] | None = None
    concurrent_fragments: int | None = Field(default=None, ge=1)
    embed_thumbnail: bool | None = None
    embed_subtitles: bool | None = None
    subtitle_language: str | None = None
    proxy: str | None = None
    cookies_from_browser: str | None = None


def load_settings() -> AppSettings:
    base = _default_settings()
    if SETTINGS_FILE.exists():
        try:
            raw = orjson.loads(SETTINGS_FILE.read_bytes())
            return AppSettings.model_validate({**base, **raw})
        except (orjson.JSONDecodeError, ValidationError, OSError) as exc:
            logger.warning("Settings file unreadable (%s); using defaults.", exc)
    settings = AppSettings.model_validate(base)
    save_settings(settings)
    return settings


def save_settings(settings: AppSettings) -> None:
    SETTINGS_FILE.write_bytes(
        orjson.dumps(settings.model_dump(), option=orjson.OPT_INDENT_2)
    )
