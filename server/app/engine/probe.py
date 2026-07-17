from typing import Any, Dict, Optional
from urllib.parse import urlparse

from app.engine.stream_extractor import probe_stream
from app.engine.direct_media import probe_direct_media

_DIRECT_MEDIA_EXTENSIONS = {
    "mp4", "m4v", "mkv", "webm", "mov", "avi", "flv", "wmv", "mpg", "mpeg",
    "mp3", "m4a", "aac", "wav", "flac", "ogg", "opus", "wma", "m2ts", "ts"
}


def _is_direct_media_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        return any(path.endswith(f".{ext}") for ext in _DIRECT_MEDIA_EXTENSIONS)
    except Exception:
        return False


def _is_stream_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        return any(path.endswith(ext) for ext in [".m3u8", ".mpd"]) or "/manifest" in path
    except Exception:
        return False


def probe_video(
    job_id: str,
    url: str,
    referer: Optional[str] = None,
    page_title: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Orchestrates video/media probing by routing to the appropriate specialized extractor.
    """
    if _is_direct_media_url(url):
        return probe_direct_media(
            job_id=job_id,
            url=url,
            referer=referer,
            page_title=page_title,
        )

    # Stream manifests and generic webpage extraction use the stream_extractor (yt-dlp).
    return probe_stream(
        job_id=job_id,
        url=url,
        referer=referer,
        page_title=page_title,
    )
