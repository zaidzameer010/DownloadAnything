from typing import Any, Dict, Optional, Tuple
import re
import urllib.request
import urllib.error
from urllib.parse import parse_qs, unquote, urlparse

from app.schemas.formats import FormatSummary
from app.schemas.settings import AppSettings
from app.engine.title_extractor import resolve_filename
from app.engine.file_types import (
    ENGINE_FILE,
    FILE_TYPE_AUDIO,
    FILE_TYPE_STREAM,
    FILE_TYPE_VIDEO,
    classify_mime,
    normalize_mime,
)
from app.utils.http import build_headers, create_ssl_context, safe_urlopen
from app.utils.logger import get_logger, redact_url

from app.engine.media_classify import is_direct_file_url
from app.engine.probe_validation import ProbeFailure, classify_probe_exception

logger = get_logger(__name__)



# Content-Types that mean "not a downloadable file blob" for direct probing.
_REJECT_MIME_PREFIXES = (
    "text/html",
    "application/xhtml",
    "text/css",
    "application/javascript",
    "text/javascript",
)


def _is_valid_download_content_type(content_type: Optional[str]) -> bool:
    """Return True if Content-Type is acceptable for a direct file download.

    Accepts unknown/missing types and generic binaries. Rejects HTML/JS/CSS
    error pages. Classification uses MIME majors, not extension lists.
    """
    if not content_type:
        return True
    lower = normalize_mime(content_type)
    if not lower:
        return True
    if lower in ("application/octet-stream", "binary/octet-stream", "application/force-download"):
        return True
    if any(lower.startswith(prefix) for prefix in _REJECT_MIME_PREFIXES):
        return False
    return True


def fetch_file_headers(url: str, referer: Optional[str] = None, timeout: float = 3.0) -> Tuple[Optional[int], Optional[str], bool, Optional[str]]:
    """Fetch Content-Length, Content-Type, Accept-Ranges, and final URL via HEAD/GET.

    Returns a tuple of (size, content_type, supports_ranges, final_url).
    Public so probe routing can do a MIME-only classification without extension lists.
    """
    headers = build_headers(referer, accept="*/*")
    ssl_ctx = create_ssl_context()
    final_url = None

    try:
        req = urllib.request.Request(url, method="HEAD", headers=headers)
        with safe_urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            final_url = resp.geturl()
            content_length = resp.headers.get("Content-Length")
            content_type = resp.headers.get("Content-Type")
            accept_ranges = resp.headers.get("Accept-Ranges", "")
            size = int(content_length) if content_length else None
            return size, content_type, "bytes" in accept_ranges.lower(), final_url
    except Exception:
        pass

    # Fall back to GET with byte range if HEAD is blocked or not allowed.
    try:
        headers["Range"] = "bytes=0-0"
        req = urllib.request.Request(url, method="GET", headers=headers)
        with safe_urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            final_url = resp.geturl()
            content_range = resp.headers.get("Content-Range")
            content_type = resp.headers.get("Content-Type")
            accept_ranges = resp.headers.get("Accept-Ranges", "")
            size = None
            if content_range and "/" in content_range:
                try:
                    size = int(content_range.split("/")[-1])
                except ValueError:
                    pass
            return size, content_type, "bytes" in accept_ranges.lower(), final_url
    except Exception as e:
        logger.warning(f"Failed to fetch media headers for {redact_url(url)}: {e}")
        return None, None, False, None


def _sniff_container_media_type(url: str, referer: Optional[str] = None, timeout: float = 3.0) -> Optional[str]:
    """Try to identify MIME by reading the first few bytes of the resource."""
    headers = build_headers(referer, accept="*/*")
    ssl_ctx = create_ssl_context()
    headers["Range"] = "bytes=0-4095"
    try:
        req = urllib.request.Request(url, method="GET", headers=headers)
        with safe_urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            chunk = resp.read(4096)
            if not chunk:
                return None
            return _guess_media_type_from_bytes(chunk)
    except Exception:
        return None


def _guess_media_type_from_bytes(data: bytes) -> Optional[str]:
    """Quick magic-byte checks for common media containers."""
    if data[:4] == b"\x1aE\xdf\xa3":  # Matroska/WebM
        return "video/webm" if b"webm" in data[:32].lower() else "video/x-matroska"
    if data[:4] in (b"ftyp", b"moov") or data[4:8] == b"ftyp":  # ISO base media/MP4/MOV
        return "video/mp4"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if data[:4] == b"fLaC":
        return "audio/flac"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:2] == b"RIFF" or data[:4] == b"WAVE":
        return "audio/wav"
    if data[:4] == b"\x00\x00\x00\x20" or data[:4] == b"\x00\x00\x00\x0c":  # QuickTime/MPEG-TS heuristics
        # Too broad to trust alone.
        return None
    return None


def _extract_resolution_from_url(url: str) -> Optional[int]:
    """Best-effort extraction of a video height from the URL.

    Looks for common resolution tokens like 1080p, 720p in the path and in
    query parameters such as ?file=.../video_1080p.mp4.
    Returns the height in pixels, or None when no token is found.
    """
    if not url:
        return None
    parsed = urlparse(url)
    candidates = [unquote(parsed.path)]
    if parsed.query:
        candidates.append(unquote(parsed.query))
        for key, values in parse_qs(parsed.query).items():
            if key in {"file", "url", "src", "video"}:
                candidates.extend(unquote(v) for v in values)

    for text in candidates:
        match = re.search(r"(?:^|[^0-9])(\d{2,4})p(?:[^0-9]|$)", text)
        if match:
            height = int(match.group(1))
            if 144 <= height <= 4320:
                return height
    return None


def probe_direct_media(
    job_id: str,
    url: str,
    settings: AppSettings,
    referer: Optional[str] = None,
    page_title: Optional[str] = None,
    mime_hint: Optional[str] = None,
    allow_html_fallback: bool = False,
) -> Dict[str, Any]:
    """
    Directly probes a raw file URL using lightweight HTTP requests.

    Works for any downloadable blob (video, audio, installer, archive, …).
    Type is derived from Content-Type / byte sniff, not extension lists.
    """
    logger.info(f"Probing direct file URL: {redact_url(url)} for job: {job_id}")

    try:
        # 1. Fetch file metadata. final_url captures redirects.
        size, mime, supports_ranges, final_url = fetch_file_headers(
            url, referer=referer
        )

        # Prefer browser-provided MIME when the server omits one.
        if not mime and mime_hint:
            mime = mime_hint

        # 2. Reject obviously non-file responses (HTML error pages, etc.),
        # UNLESS the URL is explicitly a direct file link (e.g. .dmg/.zip)
        # that redirected to an auth/login page on an unauthenticated HEAD request.
        is_direct_ext = is_direct_file_url(url)
        if (mime is not None and not _is_valid_download_content_type(mime)) or (
            final_url and is_direct_ext and "unauthorized" in final_url.lower()
        ):
            if allow_html_fallback or is_direct_ext:
                logger.info(
                    f"Direct URL {redact_url(url)} returned HTML/redirect ({mime}); "
                    f"using direct file extension fallback."
                )
                mime = None
                final_url = url
                size = None
            else:
                logger.warning(
                    f"Direct URL {redact_url(url)} returned non-download Content-Type: {mime}"
                )
                raise ProbeFailure(
                    "no_media_found",
                    "The direct link did not return a downloadable file.",
                    "no_media_found",
                )

        # 3. If MIME is missing or generic, try byte sniffing for confidence.
        sniffed_mime = None
        clean = normalize_mime(mime) if mime else ""
        if not clean or clean in (
            "application/octet-stream",
            "binary/octet-stream",
            "application/force-download",
        ):
            sniffed_mime = _sniff_container_media_type(final_url or url, referer=referer)

        effective_mime = sniffed_mime or mime or mime_hint
        file_type = classify_mime(effective_mime)

        # Streams should not go through the progressive file path.
        if file_type == FILE_TYPE_STREAM:
            raise ProbeFailure(
                "no_media_found",
                "URL looks like a stream manifest; use the stream probe path.",
                "no_media_found",
            )

        # 4. Resolve title and filename hint against final URL if redirected.
        # For video, the UI output-format setting drives the final container.
        preferred_ext = settings.mergeFormat if file_type == FILE_TYPE_VIDEO else None
        resolved = resolve_filename(
            url=final_url or url,
            filename=None,
            mime=effective_mime,
            referer=referer,
            page_title=page_title,
            preferred_ext=preferred_ext,
        )

        ext = resolved.extension

        # 5. Codec/label driven by MIME type bucket.
        # Try to extract a video resolution from the URL for a more useful label.
        if file_type == FILE_TYPE_AUDIO:
            codec = "audio"
            label = "Best Available (Original Quality)"
            height = 0
        elif file_type == FILE_TYPE_VIDEO:
            codec = "video"
            resolution = _extract_resolution_from_url(final_url or url)
            if resolution:
                label = f"Original File ({resolution}p)"
                height = resolution
            else:
                label = "Best Available (Original Quality)"
                height = 0
        else:
            codec = file_type or "file"
            label = f"Original File ({file_type})" if file_type else "Original File"
            height = 0

        fmt = FormatSummary(
            label=label,
            height=height,
            width=0,
            fps=0,
            codecFamily=codec,
            ext=ext or "",
            tbr=None,
            estSizeBytes=size,
            formatId="best",
            isCombined=True,
            hdr=False,
            videoEstSizeBytes=size if file_type == FILE_TYPE_VIDEO else None,
            audioEstSizeBytes=size if file_type == FILE_TYPE_AUDIO else None,
            isStream=False,
            streamType=None,
            videoCodec=None,
            audioCodec=None,
            protocol="https" if (final_url or url).startswith("https") else "http",
        )

        sanitized: Dict[str, Any] = {
            "url": final_url or url,
            "title": resolved.title,
            "filename": resolved.filename,
            "duration": None,
            "thumbnail": None,
            "uploader": None,
            "formats": [fmt.model_dump()],
            "mediaType": ENGINE_FILE,
            "fileType": file_type,
            "mime": effective_mime,
        }

        return sanitized

    except Exception as e:
        logger.error(f"Error during direct media probe for {redact_url(url)}: {e}", exc_info=True)
        if isinstance(e, ProbeFailure):
            raise
        raise classify_probe_exception(e) from e
