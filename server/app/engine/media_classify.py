"""Central media classification helpers used by probe, sniff fallback, and UI.

This module determines:
  - whether yt-dlp has a dedicated site extractor for a URL,
  - whether a URL/MIME points to a stream manifest,
  - whether a URL looks like a torrent,
  - the final mediaType label given a probe result.

File-type buckets (video/audio/installer/…) live in file_types.py and are
MIME-driven. This module only decides engine routing.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any
from urllib.parse import parse_qs, urlparse

from yt_dlp.extractor import list_extractor_classes

from app.engine.codec_filter import is_stream_format
from app.engine.file_types import (
    ENGINE_FILE,
    ENGINE_STREAM,
    ENGINE_TORRENT,
    ENGINE_YTDLP,
    FILE_TYPE_STREAM,
    FILE_TYPE_TORRENT,
    classify_mime,
    get_engine_media_type,
    normalize_mime,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Path tokens that signal HLS/DASH manifests when MIME is unavailable.
# These are path fragments, not a maintained file-extension allowlist.
_STREAM_PATH_TOKENS = (
    ".m3u8",
    ".mpd",
    "/manifest",
    ".ism",
    ".f4m",
    "/hls/",
    "/dash/",
)

# Pure Generic yt-dlp extractor names.
_GENERIC_EXTRACTOR_NAMES = {"generic", "generichtml5", "html5"}


def _url_stripped_path(url: str) -> str | None:
    """Return lower-case URL path without query/fragment, or None on parse failure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return None
        return (parsed.path or "").lower()
    except Exception:
        return None


@lru_cache(maxsize=1)
def _yt_dlp_extractor_classes() -> tuple[Any, ...]:
    """Lazy, once-built tuple of yt-dlp extractor classes.

    Importing/building the IE list is reasonably expensive; cache it per process.
    """
    return tuple(list_extractor_classes())


def has_dedicated_ytdlp_extractor(url: str) -> bool:
    """Return True if yt-dlp has a site-specific extractor suitable for `url`.

    Ignores the GenericIE catch-all so that unrelated manifests are not labeled
    as yt-dlp supported sites.
    """
    try:
        for ie_cls in _yt_dlp_extractor_classes():
            if ie_cls.__name__ == "GenericIE":
                continue
            if getattr(ie_cls, "IE_NAME", ie_cls.__name__).lower() == "generic":
                continue
            if ie_cls.suitable(url):
                return True
    except Exception as exc:
        logger.warning(f"yt-dlp extractor suitability check failed for {url}: {exc}")
    return False


def is_stream_manifest_url(url: str) -> bool:
    """True if the URL path contains common stream-manifest path tokens."""
    path = _url_stripped_path(url)
    if not path:
        return False
    return any(token in path for token in _STREAM_PATH_TOKENS)


def is_torrent_url(url: str) -> bool:
    """True for magnet links or .torrent path endings (scheme/path only, not an ext list)."""
    lower = url.lower().strip()
    if lower.startswith("magnet:"):
        return True
    try:
        parsed = urlparse(lower)
        return bool(parsed.path and parsed.path.endswith(".torrent"))
    except Exception:
        return False


# Extensions for HTML pages, scripts, web templates, or stream manifests (NOT direct download files).
NON_FILE_EXTENSIONS = frozenset({
    "html", "htm", "xhtml", "php", "asp", "aspx", "jsp", "cgi", "pl", "py", "rb",
    "js", "mjs", "jsx", "ts", "tsx", "css", "scss", "less",
    "m3u8", "mpd", "ism", "f4m", "torrent",
})


def is_direct_file_url(url: str) -> bool:
    """True if the URL path or query parameters indicate a file extension other than webpage/manifest extensions."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        path = (parsed.path or "").lower()
        if "/" in path:
            basename = path.split("/")[-1]
            if "." in basename:
                ext = basename.rsplit(".", 1)[1]
                if ext and ext not in NON_FILE_EXTENSIONS and len(ext) <= 10 and ext.isalnum():
                    return True
        query = parse_qs(parsed.query)
        for key in ("file", "filename", "path", "media", "url", "src"):
            for val in query.get(key, []):
                val_lower = val.lower()
                if "." in val_lower:
                    ext = val_lower.rsplit(".", 1)[1]
                    if ext and ext not in NON_FILE_EXTENSIONS and len(ext) <= 10 and ext.isalnum():
                        return True
    except Exception:
        pass
    return False


def classify_url(url: str, mime: str | None = None) -> str:
    """Classify a raw URL (+ optional MIME) into the high-level routing bucket.

    Returns one of: "torrent", "direct", "stream", "ytdlp", "unknown".

    MIME is preferred when present. URL path is checked for direct extensions,
    torrent/stream tokens, and yt-dlp site matching.
    """
    if is_torrent_url(url):
        return "torrent"

    mime_type = classify_mime(mime) if mime else None
    if mime_type == FILE_TYPE_TORRENT:
        return "torrent"
    if mime_type == FILE_TYPE_STREAM:
        return "stream"

    # Manifest path tokens beat progressive MIME (e.g. .m3u8 served as video/mp4).
    if is_stream_manifest_url(url):
        return "stream"

    # Any non-page MIME means treat as direct blob download.
    if mime:
        clean = normalize_mime(mime)
        if clean and not clean.startswith(("text/html", "application/xhtml")):
            if mime_type not in (FILE_TYPE_STREAM, FILE_TYPE_TORRENT):
                return "direct"

    if has_dedicated_ytdlp_extractor(url):
        return "ytdlp"

    if is_direct_file_url(url):
        return "direct"

    return "unknown"


def _get_extractor_key(info_dict: dict[str, Any]) -> str:
    """Return a normalized lower-case extractor key from the yt-dlp info dict."""
    return str(
        info_dict.get("extractor_key")
        or info_dict.get("extractor")
        or info_dict.get("ie_key")
        or ""
    ).lower()


def _has_dedicated_extractor(info_dict: dict[str, Any]) -> bool:
    key = _get_extractor_key(info_dict)
    if not key:
        return False
    if key in _GENERIC_EXTRACTOR_NAMES or key == "generic:html5":
        return False
    # "youtube:tab", "twitch:stream", etc. are still dedicated.
    return True


def classify_probe_result(
    url: str,
    info_dict: dict[str, Any] | None,
) -> str:
    """Return the canonical mediaType from yt-dlp output.

    Priority:
      1. extractor identity (dedicated yt-dlp site -> ytdlp)
      2. stream manifest URL path
      3. all usable formats are stream protocols -> stream
      4. a generic site with progressive formats -> ytdlp
    """
    if info_dict is None:
        info_dict = {}

    if _has_dedicated_extractor(info_dict):
        return ENGINE_YTDLP

    if is_stream_manifest_url(url):
        return ENGINE_STREAM

    formats = info_dict.get("formats") or []
    usable = [f for f in formats if isinstance(f, dict)]
    if usable and all(is_stream_format(f) for f in usable):
        return ENGINE_STREAM

    return ENGINE_YTDLP


def classify_download_item(
    url: str,
    mime: str | None = None,
) -> str:
    """Classify a browser-intercepted download into engine mediaType.

    Prefer MIME type buckets; fall back to URL routing.
    """
    mime_type = classify_mime(mime) if mime else None
    if mime_type in (FILE_TYPE_TORRENT, FILE_TYPE_STREAM):
        return get_engine_media_type(mime_type)

    bucket = classify_url(url, mime=mime)
    if bucket == "torrent":
        return ENGINE_TORRENT
    if bucket == "stream":
        return ENGINE_STREAM
    if bucket == "direct":
        return ENGINE_FILE
    if bucket == "ytdlp":
        return ENGINE_YTDLP
    # Unknown URL with a MIME (or none) from the downloads API is still a file blob.
    return ENGINE_FILE
