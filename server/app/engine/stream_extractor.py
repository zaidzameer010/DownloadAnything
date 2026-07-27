from typing import Any, Dict, List, Optional, cast
import re
import urllib.parse
import yt_dlp

from app.engine import ytdlp_opts
from app.engine.codec_filter import format_size_bytes
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


def _log_stream_manifest(source_url: str, info: Dict[str, Any], is_stream: bool) -> None:
    """Log whether the stream was resolved from a master manifest or a direct media playlist."""
    if not is_stream:
        return

    manifest_url: Optional[str] = None
    for fmt in info.get("formats") or []:
        if isinstance(fmt, dict):
            fmt_dict = cast(Dict[str, Any], fmt)
            manifest_url = cast(Optional[str], fmt_dict.get("manifest_url"))
            if isinstance(manifest_url, str) and manifest_url.strip():
                break
    else:
        manifest_url = None

    stream_url = cast(Optional[str], info.get("url")) or source_url
    if manifest_url and (manifest_url == source_url or manifest_url == stream_url):
        logger.info(f"Stream master manifest found and used: {redact_url(manifest_url)}")
    elif manifest_url:
        logger.info(
            f"Stream master manifest found: {redact_url(manifest_url)}; "
            f"using selected variant: {redact_url(stream_url)}"
        )
    else:
        logger.info(f"No stream master manifest found; using stream URL: {redact_url(stream_url)}")


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
    formats = info.get("formats") or []
    if not isinstance(formats, list):
        return False
    for fmt in formats:
        fmt_dict = cast(Dict[str, Any], fmt)
        fmt_vcodec = fmt_dict.get("vcodec")
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

    from app.engine.file_types import ENGINE_STREAM, is_direct_download_type

    _log_stream_manifest(url, sanitized, sanitized.get("mediaType") == ENGINE_STREAM)

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



_PLAYLIST_MAX_ENTRIES = 100


def _is_playlist_path(parsed_url: urllib.parse.ParseResult) -> bool:
    """True for URLs whose path is a dedicated playlist page."""
    path = parsed_url.path.rstrip("/").lower()
    return path in ("/playlist", "/playlists")


def _resolve_playlist_urls(url: str) -> tuple[Optional[str], Optional[str]]:
    """
    For a URL carrying a `list` query parameter, return (video_url, playlist_url).
    If the URL is already a playlist page, returns (None, url).
    If there is no playlist signal, returns (None, None).
    """
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    list_values = qs.get("list")
    if not list_values:
        return None, None
    list_id = list_values[0]

    if _is_playlist_path(parsed):
        return None, url

    netloc = parsed.netloc
    if netloc in ("youtu.be", "www.youtu.be"):
        netloc = "www.youtube.com"
    playlist_url = urllib.parse.urlunparse(
        (
            parsed.scheme,
            netloc,
            "/playlist",
            "",
            urllib.parse.urlencode({"list": list_id}),
            "",
        )
    )
    return url, playlist_url


def _is_single_video_info(info: Dict[str, Any]) -> bool:
    """True when the extracted info is an individual video, not a playlist."""
    return bool(info.get("formats")) or info.get("_type") == "video"


def _extract_yt_info(
    url: str,
    settings: AppSettings,
    referer: Optional[str] = None,
    *,
    noplaylist: bool = True,
    playlistend: Optional[int] = None,
) -> Dict[str, Any]:
    """Extract sanitized yt-dlp info, with a single cookieless retry."""
    browser = settings.cookiesFromBrowser

    if browser and browser.lower() not in ("none", ""):
        logger.info(f"Using native cookies from browser: {browser}")

    extra_opts: Dict[str, Any] = {
        "skip_download": True,
        "noplaylist": noplaylist,
        "buffersize": 1024 * 256,
        "socket_timeout": 10,
        "extract_flat": "in_playlist",
        "verbose": False,
    }
    if playlistend is not None:
        extra_opts["playlistend"] = playlistend

    opts = ytdlp_opts.build_ytdlp_options(
        url=url,
        browser=browser,
        referer=referer,
        extra_opts=extra_opts,
    )

    logger.info(f"Probing stream/webpage URL: {redact_url(url)}")

    exc: Optional[BaseException] = None
    try:
        with yt_dlp.YoutubeDL(cast(Any, opts)) as ydl:
            info = ydl.extract_info(url, download=False)
            return cast(Dict[str, Any], ydl.sanitize_info(info))
    except Exception as e:
        exc = e
        if "cookiesfrombrowser" in opts and not isinstance(e, ProbeFailure):
            logger.warning(
                f"Stream probe failed with native cookies ({browser}): {e}. Retrying without cookies..."
            )
            clean_opts = opts.copy()
            clean_opts.pop("cookiesfrombrowser", None)
            try:
                with yt_dlp.YoutubeDL(cast(Any, clean_opts)) as ydl:
                    info = ydl.extract_info(url, download=False)
                    return cast(Dict[str, Any], ydl.sanitize_info(info))
            except Exception as retry_err:
                exc = retry_err

    if exc and ytdlp_opts.is_piracy_block_error(exc):
        logger.warning(
            f"Stream probe hit yt-dlp piracy block for {redact_url(url)}; forcing generic extractor..."
        )
        piracy_opts = opts.copy()
        piracy_opts["force_generic_extractor"] = True
        try:
            with yt_dlp.YoutubeDL(cast(Any, piracy_opts)) as ydl:
                info = ydl.extract_info(url, download=False, force_generic_extractor=True)
                return cast(Dict[str, Any], ydl.sanitize_info(info))
        except Exception as piracy_err:
            exc = piracy_err

    if exc is None:
        return None

    logger.error(
        f"Error during stream probe for URL {redact_url(url)}: {exc}", exc_info=True
    )
    if isinstance(exc, ProbeFailure):
        raise
    raise classify_probe_exception(exc) from exc


def _pick_thumbnail_url(thumbnails: Any) -> Optional[str]:
    """Return the highest-resolution thumbnail URL from a yt-dlp thumbnails list."""
    if not isinstance(thumbnails, list) or not thumbnails:
        return None
    best_url: Optional[str] = None
    best_area = -1
    for thumb in thumbnails:
        if not isinstance(thumb, dict):
            continue
        thumb_dict = cast(Dict[str, Any], thumb)
        url = thumb_dict.get("url")
        if not url:
            continue
        area = (thumb_dict.get("width") or 0) * (thumb_dict.get("height") or 0)
        if area > best_area:
            best_area = area
            best_url = url
    return best_url


def _build_playlist_metadata(
    raw_info: Dict[str, Any],
    avg_tbr: Optional[float] = None,
) -> Dict[str, Any]:
    """Flatten a yt-dlp playlist info dict into a UI-friendly metadata object.

    yt-dlp's ``extract_flat: 'in_playlist'`` only extracts basic entry metadata,
    so ``filesize``/``filesize_approx`` is usually absent. When a sample video's
    total bitrate is known, use it to estimate each entry's likely size from
    its duration.
    """
    raw_entries_any = raw_info.get("entries")
    raw_entries: List[Any] = raw_entries_any if isinstance(raw_entries_any, list) else []

    entries: List[Dict[str, Any]] = []
    for idx, entry in enumerate(raw_entries):
        if not isinstance(entry, dict):
            continue
        entry_dict = cast(Dict[str, Any], entry)
        entry_url = (
            entry_dict.get("webpage_url")
            or entry_dict.get("url")
            or entry_dict.get("original_url")
            or ""
        )
        duration = entry_dict.get("duration")
        size = format_size_bytes(entry_dict, duration)
        if not size and avg_tbr and duration:
            size = int(avg_tbr * 1000 * duration / 8)
        entries.append(
            {
                "index": idx,
                "title": entry_dict.get("title"),
                "url": entry_url,
                "duration": duration,
                "thumbnail": entry_dict.get("thumbnail")
                or _pick_thumbnail_url(entry_dict.get("thumbnails")),
                "uploader": entry_dict.get("uploader"),
                "size": int(size) if size else None,
            }
        )

    total_size = sum(e.get("size") or 0 for e in entries) or None

    return {
        "title": raw_info.get("title") or "Playlist",
        "entries": entries,
        "total_size": total_size,
    }


def probe_stream(
    job_id: str,
    url: str,
    settings: AppSettings,
    referer: Optional[str] = None,
    page_title: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Synchronously extracts video/stream metadata using yt-dlp.

    When the URL carries a playlist ID (e.g. a YouTube watch URL with `list=`)
    or is a playlist/channel page, also extracts the first 100 entries and
    returns them under the `playlist` key, while keeping a sample video's
    formats under `formats` for the Video tab.
    """
    url = normalize_numeric_id_url(url)

    video_url, playlist_url = _resolve_playlist_urls(url)

    if video_url and playlist_url:
        # Video-in-playlist: probe the current video and the playlist separately.
        try:
            playlist_info = _extract_yt_info(
                playlist_url, settings, referer, noplaylist=False, playlistend=_PLAYLIST_MAX_ENTRIES
            )
        except Exception as exc:
            logger.warning(f"Failed to extract playlist {redact_url(playlist_url)}: {exc}")
            playlist_info = None

        if playlist_info and playlist_info.get("entries"):
            video_info = _finalize_probe_info(
                _extract_yt_info(video_url, settings, referer, noplaylist=True),
                video_url,
                referer,
                page_title,
                preferred_ext=settings.mergeFormat,
            )
            video_info["playlist"] = _build_playlist_metadata(
                playlist_info,
                avg_tbr=video_info.get("tbr"),
            )
            video_info["mediaType"] = "playlist"
            return video_info

        # Playlist extraction failed or was empty; fall back to the single video.
        return _finalize_probe_info(
            _extract_yt_info(video_url, settings, referer, noplaylist=True),
            video_url,
            referer,
            page_title,
            preferred_ext=settings.mergeFormat,
        )

    # Try extracting with playlists enabled; this handles true playlist/feed URLs
    # and single videos uniformly.
    try:
        info = _extract_yt_info(url, settings, referer, noplaylist=False, playlistend=_PLAYLIST_MAX_ENTRIES)
    except Exception as exc:
        logger.warning(f"Playlist extraction failed for {redact_url(url)}: {exc}; falling back to single video.")
        info = _extract_yt_info(url, settings, referer, noplaylist=True)
        return _finalize_probe_info(
            info, url, referer, page_title, preferred_ext=settings.mergeFormat
        )

    if info.get("entries") and not _is_single_video_info(info):
        # True playlist / channel / feed. Pick the first entry as the sample video.
        sample_url: Optional[str] = None
        entries = info.get("entries") or []
        if not isinstance(entries, list):
            entries = []
        for entry in entries:
            if isinstance(entry, dict):
                entry_dict = cast(Dict[str, Any], entry)
                sample_url = (
                    entry_dict.get("webpage_url")
                    or entry_dict.get("url")
                    or entry_dict.get("original_url")
                )
                if sample_url:
                    break

        if not sample_url:
            # No usable entries; treat the whole thing as a single video.
            return _finalize_probe_info(
                info, url, referer, page_title, preferred_ext=settings.mergeFormat
            )

        video_info = _finalize_probe_info(
            _extract_yt_info(sample_url, settings, referer, noplaylist=True),
            sample_url,
            referer,
            page_title or info.get("title"),
            preferred_ext=settings.mergeFormat,
        )
        video_info["playlist"] = _build_playlist_metadata(
            info, avg_tbr=video_info.get("tbr")
        )
        video_info["mediaType"] = "playlist"
        # For a true playlist, prefer the playlist title in the UI.
        if info.get("title"):
            video_info["title"] = info.get("title")
        return video_info

    # Single video or direct stream.
    return _finalize_probe_info(
        info, url, referer, page_title, preferred_ext=settings.mergeFormat
    )
