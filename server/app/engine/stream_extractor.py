from typing import Any, Dict, Optional
import yt_dlp

from app.engine import ytdlp_opts
from app.engine.codec_filter import is_stream_format
from app.engine.title_extractor import resolve_filename
from app.api.settings import load_settings
from app.utils.logger import logger, redact_url
from app.engine.probe_validation import (
    ProbeFailure,
    classify_probe_exception,
    validate_probe_info,
)


def determine_media_type(u: str, info_dict: Dict[str, Any]) -> str:
    lower_u = u.lower().split("?")[0]
    is_stream_u = (
        any(lower_u.endswith(ext) for ext in [".m3u8", ".mpd"])
        or "/manifest" in lower_u
    )

    if is_stream_u:
        return "stream"

    formats_list = info_dict.get("formats", [])
    if formats_list and all(is_stream_format(f) for f in formats_list):
        return "stream"

    return "ytdlp"


def probe_stream(
    job_id: str,
    url: str,
    referer: Optional[str] = None,
    page_title: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Synchronously extracts video/stream metadata using yt-dlp.
    """
    settings_data = load_settings()
    browser = settings_data.cookiesFromBrowser

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
            validate_probe_info(url, sanitized)
            sanitized["mediaType"] = determine_media_type(url, sanitized)

            info_title = sanitized.get("title")
            info_ext = sanitized.get("ext")
            info_filename = None
            if info_title and info_ext:
                info_filename = f"{info_title}.{info_ext}"
            resolved = resolve_filename(
                url=url,
                filename=info_filename,
                referer=referer,
                page_title=page_title,
            )
            sanitized["title"] = resolved.title
            sanitized["filename"] = resolved.filename
            return sanitized
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
                    validate_probe_info(url, sanitized)
                    sanitized["mediaType"] = determine_media_type(url, sanitized)

                    info_title = sanitized.get("title")
                    info_ext = sanitized.get("ext")
                    info_filename = None
                    if info_title and info_ext:
                        info_filename = f"{info_title}.{info_ext}"
                    resolved = resolve_filename(
                        url=url,
                        filename=info_filename,
                        referer=referer,
                        page_title=page_title,
                    )
                    sanitized["title"] = resolved.title
                    sanitized["filename"] = resolved.filename
                    return sanitized
            except Exception as retry_err:
                logger.error(f"Retry stream probe without cookies also failed: {retry_err}")
                raise classify_probe_exception(retry_err) from retry_err

        logger.error(
            f"Error during stream probe for URL {redact_url(url)}: {e}", exc_info=True
        )
        if isinstance(e, ProbeFailure):
            raise
        raise classify_probe_exception(e) from e
