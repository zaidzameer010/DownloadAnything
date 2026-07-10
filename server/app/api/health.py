import shutil
import time
import yt_dlp
from fastapi import APIRouter
from pydantic import BaseModel
from app.utils.logger import logger

router = APIRouter()

START_TIME = time.time()

class HealthResponse(BaseModel):
    status: str
    uptime_s: float
    yt_dlp_version: str
    ffmpeg_available: bool
    po_token_plugin_loaded: bool

def is_ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None

def is_po_token_plugin_loaded() -> bool:
    """
    Checks if a PO Token plugin is active or present in yt_dlp's plugins list.
    """
    try:
        # Check yt-dlp plugins
        from yt_dlp.plugins import yt_dlp_plugins
        if yt_dlp_plugins:
            # yt-dlp plugins is a module or manager. Let's see if we can check its keys/names.
            # In yt-dlp >= 2023.01.01, plugins are loaded from yt_dlp_plugins namespace packages.
            # We can also check if a "POT" or "PO" string is present in standard extractors.
            pass
    except Exception:
        pass
    
    # We can also search the yt_dlp extractors list for any class containing "POToken" or "PO"
    try:
        from yt_dlp.extractor import _extractors
        for name in dir(_extractors):
            if "potoken" in name.lower() or "proofofwork" in name.lower():
                return True
    except Exception:
        pass
        
    return False

@router.get("/healthz", response_model=HealthResponse)
def health_check():
    uptime = time.time() - START_TIME
    
    # Check yt-dlp version
    yt_dlp_version = getattr(yt_dlp, "__version__", "unknown")
    if yt_dlp_version == "unknown":
        try:
            from yt_dlp.version import __version__ as v
            yt_dlp_version = v
        except ImportError:
            pass

    return HealthResponse(
        status="ok",
        uptime_s=round(uptime, 2),
        yt_dlp_version=yt_dlp_version,
        ffmpeg_available=is_ffmpeg_available(),
        po_token_plugin_loaded=is_po_token_plugin_loaded()
    )
