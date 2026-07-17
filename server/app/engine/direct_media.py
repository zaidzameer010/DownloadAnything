from typing import Any, Dict, Optional
import urllib.request
import urllib.error
from urllib.parse import urlparse

from app.schemas.formats import FormatSummary
from app.engine.title_extractor import resolve_filename, _create_ssl_context, _build_headers
from app.utils.logger import logger, redact_url
from app.engine.probe_validation import ProbeFailure, classify_probe_exception


def _fetch_file_size_and_mime(url: str, referer: Optional[str] = None, timeout: float = 3.0) -> tuple[Optional[int], Optional[str]]:
    """Fetch Content-Length and Content-Type via a minimal HEAD/GET request."""
    headers = _build_headers(referer, accept="*/*")
    ssl_ctx = _create_ssl_context()
    try:
        req = urllib.request.Request(url, method="HEAD", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            content_length = resp.headers.get("Content-Length")
            content_type = resp.headers.get("Content-Type")
            size = int(content_length) if content_length else None
            return size, content_type
    except Exception:
        # Fall back to GET with byte range if HEAD is blocked
        try:
            headers["Range"] = "bytes=0-0"
            req = urllib.request.Request(url, method="GET", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
                content_range = resp.headers.get("Content-Range")
                content_type = resp.headers.get("Content-Type")
                size = None
                if content_range and "/" in content_range:
                    try:
                        size = int(content_range.split("/")[-1])
                    except ValueError:
                        pass
                return size, content_type
        except Exception as e:
            logger.warning(f"Failed to fetch media headers for {redact_url(url)}: {e}")
            return None, None


def probe_direct_media(
    job_id: str,
    url: str,
    referer: Optional[str] = None,
    page_title: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Directly probes a raw video/audio URL using lightweight HTTP requests.
    """
    logger.info(f"Probing direct media URL: {redact_url(url)} for job: {job_id}")

    try:
        # 1. Fetch file size and mime type
        size, mime = _fetch_file_size_and_mime(url, referer=referer)

        # 2. Resolve title and filename hint
        resolved = resolve_filename(
            url=url,
            filename=None,
            mime=mime,
            referer=referer,
            page_title=page_title,
        )

        ext = resolved.extension or "mp4"

        # 3. Create a single combined format summary
        codec = "video"
        if mime:
            codec = mime.split("/")[0]

        fmt = FormatSummary(
            label="Best Available (Original Quality)",
            height=0,
            fps=0,
            codecFamily=codec,
            ext=ext,
            tbr=None,
            estSizeBytes=size,
            formatId="best",
            isCombined=True,
            hdr=False,
            videoEstSizeBytes=size,
            audioEstSizeBytes=None,
            isStream=False,
            streamType=None,
            videoCodec=None,
            audioCodec=None,
            protocol="https" if url.startswith("https") else "http",
        )

        sanitized: Dict[str, Any] = {
            "url": url,
            "title": resolved.title,
            "filename": resolved.filename,
            "duration": None,
            "thumbnail": None,
            "uploader": None,
            "formats": [fmt.model_dump()],
            "mediaType": "file",  # Indicates standard direct file download
        }

        return sanitized

    except Exception as e:
        logger.error(f"Error during direct media probe for {redact_url(url)}: {e}", exc_info=True)
        raise classify_probe_exception(e) from e
