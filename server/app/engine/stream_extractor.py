from typing import Any, Dict, Optional
import re
import yt_dlp

from app.engine import ytdlp_opts
from app.engine.media_classify import classify_probe_result
from app.engine.title_extractor import (
    resolve_filename,
    _clean_page_title,
    _title_contains_url_id,
    _is_unusable_stem,
)
from app.schemas.settings import AppSettings
from app.utils.logger import get_logger, redact_url

from app.engine.probe_validation import (
    ProbeFailure,
    classify_probe_exception,
    validate_probe_info,
)

logger = get_logger(__name__)



def normalize_numeric_id_url(url: str) -> str:
    """Strip leading zeros from a trailing numeric path segment.

    Some media APIs reject IDs with leading zeros while the canonical page URL
    still contains them. This normalizes the last numeric path segment so
    extractors that derive an API id from the URL get a clean integer id.
    """
    if not url:
        return url
    # Strip leading zeros from the final numeric path segment before query/frag.
    return re.sub(
        r"(/)(-?)(0+)(\d+)(?=$|[?#&])",
        r"\1\2\4",
        url,
        count=1,
    )


def _has_video_stream(info: Dict[str, Any]) -> bool:
    """Return True if the probe result contains at least one video stream."""
    vcodec = info.get("vcodec")
    if vcodec and vcodec != "none":
        return True
    for fmt in info.get("formats") or []:
        fmt_vcodec = fmt.get("vcodec")
        if fmt_vcodec and fmt_vcodec != "none":
            return True
    return False


def _finalize_probe_info(
    sanitized: Dict[str, Any],
    url: str,
    referer: Optional[str],
    page_title: Optional[str],
    preferred_ext: Optional[str] = None,
) -> Dict[str, Any]:
    """Attach media type, title, and filename to a sanitized yt-dlp info dict."""
    validate_probe_info(url, sanitized)
    sanitized["mediaType"] = classify_probe_result(url, sanitized)

    # For videos, the UI setting drives the ffmpeg conversion container.
    # For audio-only streams, fall back to the setting as well because there is
    # no separate audio output setting.
    has_video = _has_video_stream(sanitized)
    setting_ext = (preferred_ext or "").strip().lstrip(".").lower()
    source_ext = sanitized.get("ext") or setting_ext
    if has_video:
        effective_ext = setting_ext if setting_ext else source_ext
    else:
        effective_ext = source_ext
    if not re.fullmatch(r"[a-z0-9]{1,8}", effective_ext):
        effective_ext = setting_ext
    sanitized["ext"] = effective_ext

    info_title = sanitized.get("title")
    info_filename = None
    if info_title and effective_ext:
        info_filename = f"{info_title}.{effective_ext}"

    from app.engine.file_types import is_direct_download_type

    is_non_native = is_direct_download_type(sanitized.get("mediaType"))
    if is_non_native:
        resolved = resolve_filename(
            url=url,
            filename=info_filename,
            referer=referer,
            page_title=page_title,
            preferred_ext=effective_ext,
        )
        sanitized["title"] = resolved.title
        sanitized["filename"] = resolved.filename
    else:
        clean_page = _clean_page_title(page_title, referer or url) if page_title else None
        info_title = sanitized.get("title")
        title_is_generic = (
            not info_title
            or _is_unusable_stem(info_title)
            or _title_contains_url_id(info_title, url)
        )
        if title_is_generic:
            # Prefer page-extracted/network title (Content-Disposition, HTML metadata,
            # URL basename) over the cleaned tab title where possible.
            resolved = resolve_filename(
                url=url,
                filename=None,
                referer=referer,
                page_title=page_title,
                preferred_ext=effective_ext,
                allow_network=True,
            )
            if resolved.title and resolved.title != "video":
                sanitized["title"] = resolved.title
                sanitized["filename"] = resolved.filename
            elif clean_page:
                sanitized["title"] = clean_page
                sanitized["filename"] = f"{clean_page}.{effective_ext}"
            else:
                # yt-dlp often labels manifests with generic names like "master".
                # Prefer a safe placeholder over a misleading stream label.
                sanitized["title"] = "video"
                sanitized["filename"] = f"video.{effective_ext}"
        else:
            if info_filename:
                sanitized["filename"] = info_filename
            else:
                sanitized["filename"] = f"{info_title}.{effective_ext}"
    return sanitized


def probe_stream(
    job_id: str,
    url: str,
    settings: AppSettings,
    referer: Optional[str] = None,
    page_title: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Synchronously extracts video/stream metadata using yt-dlp.
    """
    url = normalize_numeric_id_url(url)

    browser = settings.cookiesFromBrowser

    if browser and browser.lower() not in ("none", ""):
        logger.info(f"Using native cookies from browser: {browser}")

    opts = ytdlp_opts.build_ytdlp_options(
        url=url,
        browser=browser,
        referer=referer,
        extra_opts={
            "skip_download": True,
            "noplaylist": True,
            "buffersize": 1024 * 256,
            "socket_timeout": 10,
            "extract_flat": "in_playlist",
            "verbose": False,
        },
    )

    logger.info(f"Probing stream/webpage URL: {redact_url(url)} for job: {job_id}")

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            sanitized = ydl.sanitize_info(info)
            return _finalize_probe_info(
                sanitized, url, referer, page_title, preferred_ext=settings.mergeFormat
            )
    except Exception as e:
        if "cookiesfrombrowser" in opts and not isinstance(e, ProbeFailure):
            logger.warning(
                f"Stream probe failed with native cookies ({browser}): {e}. Retrying without cookies..."
            )
            clean_opts = opts.copy()
            clean_opts.pop("cookiesfrombrowser", None)
            try:
                with yt_dlp.YoutubeDL(clean_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    sanitized = ydl.sanitize_info(info)
                    return _finalize_probe_info(
                        sanitized, url, referer, page_title, preferred_ext=settings.mergeFormat
                    )
            except Exception as retry_err:
                logger.error(f"Retry stream probe without cookies also failed: {retry_err}")
                raise classify_probe_exception(retry_err) from retry_err

        logger.error(
            f"Error during stream probe for URL {redact_url(url)}: {e}", exc_info=True
        )
        if isinstance(e, ProbeFailure):
            raise
        raise classify_probe_exception(e) from e
