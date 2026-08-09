"""yt-dlp engine: probing and download job management.

All yt_dlp calls are blocking, so everything runs through a
:class:`~concurrent.futures.ThreadPoolExecutor`; the asyncio server schedules
work with ``asyncio.get_running_loop().run_in_executor`` and progress events
are pushed back onto the event loop with ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import contextlib
import mmap
import orjson
import os
import platform
import re
import resource
import struct
import shlex
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import urlsplit

import structlog
from send2trash import send2trash
import yt_dlp.utils
import yt_dlp.utils._utils
from yt_dlp import YoutubeDL
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.postprocessor.ffmpeg import FFmpegFixupM3u8PP
from yt_dlp.utils import determine_ext as _orig_determine_ext
from yt_dlp.utils import DownloadCancelled, UnsupportedError, prepend_extension
from yt_dlp.utils import KNOWN_EXTENSIONS, MEDIA_EXTENSIONS, sanitize_filename, smuggle_url, unsmuggle_url
from yt_dlp.utils._utils import _UnsafeExtensionError

try:
    from .db import Database, app_support_dir
    from .settings import DEFAULT_SETTINGS, SettingsStore
    from .stream_size import resolve_format_size, resolve_selector_size
    from .torrent import TorrentManager, is_magnet_url, is_torrent_input
except ImportError:
    from db import Database, app_support_dir
    from settings import DEFAULT_SETTINGS, SettingsStore
    from stream_size import resolve_format_size, resolve_selector_size
    from torrent import TorrentManager, is_magnet_url, is_torrent_input

log = structlog.get_logger()

# yt-dlp disallows non-media extensions by default for security. We allow them
# only when downloading arbitrary intercepted files (.dmg, .zip, .pdf, etc.).
_unsafe_ext_lock = threading.RLock()
_unsafe_ext_refcount = 0


@contextlib.contextmanager
def _allow_unsafe_extensions():
    """Temporarily disable yt-dlp's unsafe-extension guard for file downloads."""
    global _unsafe_ext_refcount
    _unsafe_ext_orig_enabled = getattr(_UnsafeExtensionError, "_enabled", True)
    with _unsafe_ext_lock:
        _unsafe_ext_refcount += 1
        if _unsafe_ext_refcount == 1:
            setattr(_UnsafeExtensionError, "_enabled", False)
    try:
        yield
    finally:
        with _unsafe_ext_lock:
            _unsafe_ext_refcount -= 1
            if _unsafe_ext_refcount == 0:
                setattr(_UnsafeExtensionError, "_enabled", _unsafe_ext_orig_enabled)


# How often (seconds) a single job may emit progress while downloading.
PROGRESS_INTERVAL = 0.1

# yt-dlp's generic extractor derives the extension from the URL path.  Some
# CDNs redirect to a .php endpoint that carries the real filename in a query
# parameter (e.g. remote_control.php?file=... .<ext>&acctoken=...).  We patch
# determine_ext so that when the path extension is not a real media type, we
# fall back to scanning the full URL (query string included) for a known one.
_MEDIA_EXTS = frozenset((*MEDIA_EXTENSIONS.video, *MEDIA_EXTENSIONS.audio, *MEDIA_EXTENSIONS.manifests))

_KNOWN_EXT_RE = re.compile(
    r'(?:^|\.)('
    + '|'.join(re.escape(ext) for ext in sorted(KNOWN_EXTENSIONS, key=len, reverse=True))
    + r')(?:[^\w.]|$)',
    re.IGNORECASE,
)


def _guess_media_ext(url: str) -> str | None:
    if not url:
        return None
    matches = _KNOWN_EXT_RE.findall(url)
    return matches[-1].lower() if matches else None


def _patched_determine_ext(url, default_ext=''):
    ext = _orig_determine_ext(url, default_ext=default_ext)
    if not url or ext in _MEDIA_EXTS:
        return ext
    return _guess_media_ext(url) or ext


def _ext_from_name(url: str, filename: str = "") -> str:
    """Return the file extension from a filename or URL path, or 'bin'."""
    if filename:
        ext = Path(filename).suffix
        if ext:
            return ext.lstrip(".").lower()
    clean = (url or "").split("?")[0].split("#")[0]
    ext = Path(clean).suffix
    return ext.lstrip(".").lower() or ""


def _patch_yt_dlp_determine_ext() -> None:
    """Apply our URL-extension patch once at module load."""
    yt_dlp.utils.determine_ext = _patched_determine_ext
    yt_dlp.utils._utils.determine_ext = _patched_determine_ext


_patch_yt_dlp_determine_ext()


# Use the bundled aria2-next downloader that pushes progress over a WebSocket
# JSON-RPC connection instead of parsing console output.
try:
    from .aria2next import ARIA2_NEXT_BINARY as _ARIA2_NEXT_BINARY
except ImportError:
    from aria2next import ARIA2_NEXT_BINARY as _ARIA2_NEXT_BINARY


class HlsPngTsWrapperStripPP(PostProcessor):
    """Remove PNG wrappers around MPEG-TS payloads from HLS segments."""

    _PNG_SIG = b"\x89PNG\r\n\x1a\n"
    _IEND = b"IEND"

    @classmethod
    def _png_end_position(cls, source: BinaryIO, start: int) -> int:
        source.seek(start)
        if source.read(8) != cls._PNG_SIG:
            return start
        position = start + 8
        while True:
            source.seek(position)
            length_bytes = source.read(4)
            chunk_type = source.read(4)
            if len(length_bytes) < 4 or len(chunk_type) < 4:
                return position
            length = struct.unpack(">I", length_bytes)[0]
            if chunk_type == cls._IEND:
                return position + 12
            position += 12 + length

    def _strip_to_ts(self, source_path: str, destination_path: str) -> None:
        with open(source_path, "rb") as source, open(destination_path, "wb") as destination:
            source.seek(0, os.SEEK_END)
            source_size = source.tell()
            source.seek(0)
            if source_size == 0:
                return
            with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                starts: list[int] = []
                offset = 0
                while True:
                    start = mapped.find(self._PNG_SIG, offset)
                    if start < 0:
                        break
                    starts.append(start)
                    offset = start + 1

                previous_end = 0
                for start in starts:
                    if start > previous_end:
                        destination.write(mapped[previous_end:start])
                    previous_end = self._png_end_position(source, start)
                if previous_end < source_size:
                    destination.write(mapped[previous_end:])

    def run(self, information: Any) -> tuple[list[str], Any]:
        info = cast(dict[str, Any], information)
        path = info.get("filepath")
        protocol = str(info.get("protocol") or "").lower()
        if not isinstance(path, str) or not path or not protocol.startswith("m3u8"):
            return [], info
        temporary_path = ""
        try:
            with open(path, "rb") as source:
                if source.read(len(self._PNG_SIG)) != self._PNG_SIG:
                    return [], info
            temporary_path = prepend_extension(path, "pngts")
            self._strip_to_ts(path, temporary_path)
            if os.path.getsize(temporary_path) == 0:
                os.unlink(temporary_path)
                return [], info
            os.replace(temporary_path, path)
        except (OSError, ValueError, struct.error):
            try:
                if os.path.exists(temporary_path):
                    os.unlink(temporary_path)
            except OSError:
                pass
        return [], info


def _add_hls_fixup_postprocessors(ydl: YoutubeDL) -> None:
    processors = cast(dict[str, list[PostProcessor]], getattr(ydl, "_pps", {}))
    post_process = processors.get("post_process")
    if post_process is None:
        return
    fixups = [
        HlsPngTsWrapperStripPP(ydl),
        FFmpegFixupM3u8PP(ydl),
    ]
    for processor in reversed(fixups):
        cast(Any, processor).set_downloader(ydl)
        post_process.insert(0, processor)




def _temp_root() -> Path:
    """Root directory for incomplete download temp files."""
    return app_support_dir() / "temp"


def _job_temp_dir(job_id: str) -> Path:
    """Per-job isolated temp subfolder."""
    return _temp_root() / job_id


def _trash_job_temp(job_id: str) -> None:
    """Move a job's isolated temp folder to the system trash, if it exists."""
    temp_dir = _job_temp_dir(job_id)
    if not temp_dir.exists():
        return
    try:
        send2trash(str(temp_dir))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not trash temp dir %s: %s", temp_dir, exc)


def _download_error_message(exc: BaseException) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    normalized = message.lower()
    if any(
        marker in normalized
        for marker in ("no space left on device", "enospc", "errnum=28", "errorcode=28")
    ):
        return (
            "Insufficient disk space for the download. Free space on the "
            "destination volume and retry; the full download must fit on disk."
        )
    return message


def _base_opts(settings: dict[str, Any], *, direct_file: bool = False) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": False,
        "no_warnings": False,
        "noprogress": True,
        "retries": settings["retries"],
        # Never pass an incomplete fragmented media file to FFmpeg. Fragment
        # failures must be retried and then reported instead of silently
        # skipped, otherwise postprocessing receives a truncated stream.
        "fragment_retries": settings["retries"],
        "skip_unavailable_fragments": False,
        # Pace retries. Some CDNs answer requests with intermittent 403s
        # (rate limiting or flaky origin); yt-dlp's default of retrying
        # immediately burns through all retries inside the same rejection
        # window. Exponential backoff gives the origin time to accept again.
        "retry_sleep_functions": {
            "http": lambda n: min(2.0 * n, 30.0),
            "fragment": lambda n: min(2.0 * n, 30.0),
        },
        # FFmpeg otherwise emits MPEG-TS for playlists marked as live. The
        # resulting TS can carry unset timestamps that fail when remuxed to
        # MKV, even when the HLS download itself completed successfully.
        "hls_use_mpegts": False,
        # Native HLS/DASH downloaders write each fragment to a .part file and
        # can resume from the last completed fragment. `hls_prefer_native` is
        # deprecated; use the per-protocol external_downloader map instead.
        "external_downloader": {"m3u8": "native", "dash": "native"},
        # Do not test every format URL before selection; that is slow and our
        # size resolver already HEADs the selected format when needed.
        "check_formats": False,
        # Default filename: title only, no video id.
        "outtmpl": "%(title)s.%(ext)s",
        # Larger download buffer reduces syscalls; resize-buffer is still on.
        "buffersize": 64 * 1024,
        # Use yt-dlp's native FormatSorter for codec and container preference.
        # For vcodec and acodec, the + prefix is needed so the built-in priority
        # order is treated as "best first" rather than reversed.
        "format_sort": ["res", "fps", "+vcodec", "+acodec", "br", "ext"],
        "extractor_args": {"generic": {"impersonate": ["chrome"]}},
        # yt-dlp blocks some sites with a hard-coded "Piracy" extractor that
        # always raises. Force it off so the generic extractor can handle the
        # direct media URLs the browser extension passes in.
        "allowed_extractors": [r"^(?!Piracy$).*$"],
    }
    if settings.get("proxy"):
        opts["proxy"] = settings["proxy"]
    if settings.get("rateLimit"):
        opts["ratelimit"] = int(settings["rateLimit"])
    requested = settings.get("concurrentFragments") or 1
    if requested > 1:
        max_jobs = max(1, settings.get("maxConcurrentDownloads", DEFAULT_SETTINGS["maxConcurrentDownloads"]))
        try:
            soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        except Exception:  # noqa: BLE001
            soft = 256
        # Reserve a few dozen FDs for the process itself, then divide the
        # remaining budget across jobs. Each concurrent fragment worker needs
        # roughly 3 FDs (socket, .part file, bookkeeping).
        per_job = max(1, (soft - 64) // max(1, max_jobs) // 3)
        opts["concurrent_fragment_downloads"] = max(1, min(requested, per_job))
    if settings.get("cookiesFromBrowser"):
        opts["cookiesfrombrowser"] = (settings["cookiesFromBrowser"],)
    if _ARIA2_NEXT_BINARY:
        aria2_path = str(_ARIA2_NEXT_BINARY)
        # Keep aria2-next for plain HTTP/FTP downloads; native yt-dlp downloaders
        # stay responsible for HLS and DASH so they can pause/resume by fragment.
        opts["external_downloader"].update({
            "default": aria2_path,
            "http": aria2_path,
            "ftp": aria2_path,
        })
        aria2_args: list[str] = []
        # Performance baseline for aria2-next: larger in-memory disk cache and
        # more retries for flaky sources. Do not impose a minimum speed here:
        # valid sources can be slower than an arbitrary threshold, and user
        # extras are appended later for explicit opt-in tuning.
        aria2_args.extend([
            "--disk-cache=64M",
            "--max-tries=10",
        ])
        conn = settings.get("aria2NextConnections")
        if conn is not None:
            aria2_args.extend(["-x", str(conn), "-s", str(conn)])
        max_concurrent = settings.get("aria2NextMaxConcurrent")
        if max_concurrent is not None:
            aria2_args.extend(["-j", str(max_concurrent)])
        min_split = (settings.get("aria2NextMinSplitSize") or "").strip()
        if min_split:
            aria2_args.extend(["--min-split-size", min_split])
        file_alloc = "none" if direct_file else (settings.get("aria2NextFileAllocation") or "").strip()
        if file_alloc:
            aria2_args.extend(["--file-allocation", file_alloc])
        extra = (settings.get("aria2NextExtraArgs") or "").strip()
        if extra:
            aria2_args.extend(shlex.split(extra))
        if aria2_args:
            opts["external_downloader_args"] = {"aria2-next": aria2_args}
    return opts


def _apply_smuggled_http_headers(opts: dict[str, Any], url: str) -> None:
    clean_url, smuggled_data = unsmuggle_url(url, {})
    referer = smuggled_data.get("referer")
    if referer:
        # Browsers never send URL fragments in Referer/Origin headers.
        parsed = urlsplit(str(referer))._replace(fragment="")
        referer_header = parsed.geturl()
        existing = cast(dict[str, str], opts.get("http_headers") or {})
        headers: dict[str, str] = {**existing, "Referer": referer_header}
        # Many HLS/CDN endpoints reject the request if Origin is missing.
        if parsed.scheme in {"http", "https"} and "Origin" not in headers:
            headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
        opts["http_headers"] = headers
        opts["impersonate"] = ImpersonateTarget.from_str("chrome")

        # Signed HLS URLs commonly carry authorization in the manifest query,
        # and the same query must be sent with every variant and media segment.
        query = urlsplit(clean_url).query
        if query:
            extractor_args = cast(dict[str, Any], opts.get("extractor_args") or {})
            generic_args = cast(dict[str, Any], extractor_args.get("generic") or {})
            opts["extractor_args"] = {
                **extractor_args,
                "generic": {
                    **generic_args,
                    "fragment_query": [query],
                    "variant_query": [query],
                },
            }


def build_format_rows(ydl: YoutubeDL, info: Any) -> list[dict[str, Any]]:
    """Reduce yt-dlp's pre-sorted format list to picker rows (video + audio only).

    The formats must already be sorted by yt-dlp's FormatSorter so the first
    format encountered for each resolution/label is the best available.
    """
    rows: list[dict[str, Any]] = []
    seen_labels: set[str] = set()

    for fmt in cast(list[dict[str, Any]], info.get("formats") or []):
        raw_vcodec = fmt.get("vcodec")
        raw_acodec = fmt.get("acodec")
        vcodec = raw_vcodec or "none"
        acodec = raw_acodec or "none"
        height = fmt.get("height")
        width = fmt.get("width")
        has_video = vcodec != "none" or height is not None or width is not None
        has_audio = acodec != "none"
        if not has_video and not has_audio:
            continue
        video_only = has_video and raw_vcodec is not None and raw_acodec == "none"

        fps = fmt.get("fps")
        if has_video and height:
            label = f"{height}p{int(fps) if fps and fps > 30 else ''}"
        elif has_video:
            label = fmt.get("format_note") or "video"
        else:
            label = f"audio {int(fmt['abr'])}k" if fmt.get("abr") else "audio"

        if label in seen_labels:
            continue
        seen_labels.add(label)

        # Video-only rows get merged with best audio at download time.
        selector = fmt["format_id"]
        if video_only:
            selector = f"{fmt['format_id']}+(ba[format_note*=original]/ba)/{fmt['format_id']}"

        row: dict[str, Any] = {
            "id": fmt.get("format_id"),
            "selector": selector,
            "label": label,
            "resolution": (
                f"{width}x{height}"
                if has_video and width and height
                else f"{height}p"
                if has_video and height
                else ""
            ),
            "ext": fmt.get("ext") or "",
            "tbr": fmt.get("tbr"),
            "fps": fps,
            "vcodec": vcodec if has_video else "",
            "acodec": acodec if has_audio else "",
            "kind": "video" if has_video else "audio",
            "height": height,
            "url": fmt.get("url") or "",
        }

        # Video-only rows need a selector that also resolves the matching audio.
        # For already-merged or audio-only rows, the format itself is enough.
        if video_only:
            resolved = resolve_selector_size(ydl, info, selector)
        else:
            resolved = resolve_format_size(ydl, fmt, info)
        if resolved:
            row["size"] = resolved[0]
            row["sizeIsEstimate"] = resolved[1]

        rows.append(row)

    # Display: video first, then highest resolution, then highest bitrate.
    rows.sort(key=lambda r: (r["kind"] != "video", -(r.get("height") or 0), -(r.get("tbr") or 0)))
    return rows


def _merge_format_rows(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in [*existing, *incoming]:
        url = str(row.get("url") or "").split("?", 1)[0].split("#", 1)[0]
        key = (url or str(row.get("label") or ""), str(row.get("kind") or ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    merged.sort(key=lambda row: (
        row.get("kind") != "video",
        -(row.get("height") or 0),
        -(row.get("tbr") or 0),
    ))
    return merged


def _extract_thumbnail(info: Any) -> str:
    if not isinstance(info, dict):
        return ""
    thumb = info.get("thumbnail")
    if isinstance(thumb, str) and thumb.strip():
        return thumb.strip()
    thumbnails = info.get("thumbnails")
    if isinstance(thumbnails, list) and thumbnails:
        for t in reversed(thumbnails):
            if isinstance(t, dict):
                url = t.get("url")
                if isinstance(url, str) and url.strip():
                    return url.strip()
    return ""


def _build_playlist_entries(info: Any) -> list[dict[str, Any]]:
    """Reduce flat playlist entries while preserving yt-dlp's canonical order."""
    entries: list[dict[str, Any]] = []
    for position, entry in enumerate(cast(list[dict[str, Any]], info.get("entries") or [])):
        if not entry:
            continue
        webpage = entry.get("webpage_url") or entry.get("url") or ""
        raw_playlist_index = entry.get("playlist_index")
        if isinstance(raw_playlist_index, (int, str)):
            try:
                playlist_index = int(raw_playlist_index)
            except ValueError:
                playlist_index = position + 1
        else:
            playlist_index = position + 1
        entries.append({
            "id": str(entry.get("id") or position),
            "title": entry.get("title") or "",
            "uploader": entry.get("uploader") or entry.get("channel") or "",
            "duration": entry.get("duration"),
            "thumbnail": _extract_thumbnail(entry),
            "webpageUrl": webpage,
            "url": entry.get("url") or webpage,
            "index": playlist_index,
        })

    entries.sort(key=lambda entry: entry["index"])
    return entries


def _build_probe_result(
    ydl: YoutubeDL,
    info: Any,
    request_url: str,
    settings: dict[str, Any],
    media_type: str,
    *,
    is_playlist: bool = False,
    formats: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a picker result dict from a yt-dlp info dict."""
    merge_format = (
        settings.get("mergeOutputFormat", DEFAULT_SETTINGS["mergeOutputFormat"]).strip().lower()
    )
    result_formats: list[dict[str, Any]] = formats if formats is not None else []
    filename = ""

    if not is_playlist:
        if result_formats:
            pass
        elif info.get("formats"):
            result_formats = build_format_rows(ydl, info)

        if not result_formats:
            # Direct media URLs yield no format list; synthesize one row.
            is_file = media_type == "file"
            ext = _ext_from_name(request_url) if is_file else merge_format
            row: dict[str, Any] = {
                "id": info.get("format_id") or "direct",
                "selector": "best",
                "label": "direct" if not is_file else "original",
                "resolution": "",
                "ext": ext,
                "tbr": info.get("tbr"),
                "fps": None,
                "vcodec": info.get("vcodec") or "",
                "acodec": info.get("acodec") or "",
                "kind": "file" if is_file else "video",
                "url": info.get("url") or request_url,
            }
            resolved = resolve_selector_size(ydl, info, "best")
            if resolved:
                row["size"] = resolved[0]
                row["sizeIsEstimate"] = resolved[1]
            result_formats = [row]

        # Filename preview uses the merge output format so it matches the final container,
        # unless this is a generic file that must keep its original extension.
        if media_type == "file":
            info["ext"] = _ext_from_name(request_url)
        else:
            info["ext"] = merge_format
        filename = Path(ydl.prepare_filename(info)).name

    return {
        "id": str(info.get("id")) if info.get("id") is not None else None,
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "thumbnail": _extract_thumbnail(info),
        "webpageUrl": info.get("webpage_url") or request_url,
        "extractor": info.get("extractor_key") or "",
        "isPlaylist": is_playlist,
        "playlistCount": info.get("playlist_count") or None,
        "filename": filename,
        "formats": result_formats,
    }


def probe(
    url: str,
    fallback_urls: str | list[str] | None,
    settings: dict[str, Any],
    media_type: str = "",
    fallback_sources: list[dict[str, str]] | None = None,
) -> tuple[dict[str, Any], Any | None]:
    """Probe a page URL with yt-dlp; fall back to extracted stream URLs.

    Returns a ``(result, info)`` tuple. ``result`` has ``ok``, ``engine``
    ('ytdlp' | 'extractor' | 'none' | 'file'), and on success the picker payload.
    ``info`` is the raw yt-dlp info dict on success, otherwise ``None``.

    The probe is playlist-aware:

    1. It first extracts with ``extract_flat='in_playlist'``. For a single video
       this usually returns the full info dict. For a playlist it returns a flat
       list of entries.
    2. If the first pass is a playlist, it then attempts to extract the target
       single video with ``noplaylist=True`` so the Video tab can show the exact
       episode the user clicked on (e.g. episode 4 in a playlist).
    """
    attempts: list[tuple[str, str, str]] = [("ytdlp", url, "")]
    if isinstance(fallback_urls, str):
        fallback_urls = [fallback_urls]
    source_labels = {
        source.get("url"): source.get("label", "")
        for source in fallback_sources or []
        if source.get("url")
    }
    candidates = [source.get("url") for source in fallback_sources or [] if source.get("url")]
    candidates.extend(fallback_urls or [])
    for candidate in candidates:
        if candidate and candidate != url and candidate not in (u for _, u, _ in attempts):
            attempts.append(("extractor", candidate, source_labels.get(candidate, "")))

    last_error: str | None = None
    combined_fallback: tuple[dict[str, Any], Any | None, str] | None = None
    for attempt_index, (engine, candidate, source_label) in enumerate(attempts):
        if engine == "extractor":
            log.info("Falling back to extractor", candidate=candidate, referer=url)
        request_url = (
            smuggle_url(candidate, {"referer": url})
            if engine == "extractor" and url
            else candidate
        )

        # Pass 1: flat playlist / single video (single videos are still fully extracted).
        flat_opts = {**_base_opts(settings), "skip_download": True, "extract_flat": "in_playlist"}
        _apply_smuggled_http_headers(flat_opts, request_url)
        try:
            with YoutubeDL(cast(Any, flat_opts)) as ydl:
                info: Any = ydl.extract_info(request_url, download=False)

                if not info:
                    last_error = "yt-dlp returned no information for this URL"
                    continue

                is_playlist = info.get("_type") == "playlist"
                entries: list[dict[str, Any]] | None = None
                if is_playlist:
                    entries = _build_playlist_entries(info)

                # Pass 2: if the first pass was a playlist, try to get the target single video.
                video_info: Any | None = None
                if is_playlist:
                    video_opts = {
                        **_base_opts(settings),
                        "skip_download": True,
                        "extract_flat": False,
                        "noplaylist": True,
                    }
                    _apply_smuggled_http_headers(video_opts, request_url)
                    try:
                        with YoutubeDL(cast(Any, video_opts)) as ydl_video:
                            video_info = ydl_video.extract_info(request_url, download=False)

                            # If noplaylist still returned a playlist, there is no target video.
                            if video_info and video_info.get("_type") == "playlist":
                                video_info = None

                            # Build the result. Start from the target video when available;
                            # otherwise fall back to the playlist info itself (pure playlist).
                            if video_info:
                                result = _build_probe_result(ydl_video, video_info, request_url, settings, media_type)
                            else:
                                result = _build_probe_result(
                                    ydl,
                                    info,
                                    request_url,
                                    settings,
                                    media_type,
                                    is_playlist=True,
                                    formats=[],
                                )
                    except Exception as exc:  # noqa: BLE001
                        log.debug("Could not extract target video from playlist URL", error=str(exc))
                        result = _build_probe_result(
                            ydl,
                            info,
                            request_url,
                            settings,
                            media_type,
                            is_playlist=True,
                            formats=[],
                        )
                else:
                    result = _build_probe_result(
                        ydl,
                        info,
                        request_url,
                        settings,
                        media_type,
                    )

                if engine == "extractor" and not is_playlist:
                    result_formats = result.get("formats") or []
                    if len(result_formats) == 1:
                        source_url = unsmuggle_url(request_url, {})[0]
                        result_formats[0]["url"] = source_url
                        if source_label:
                            result_formats[0]["id"] = source_label
                            result_formats[0]["label"] = source_label
                            result_formats[0]["resolution"] = source_label
                            height_match = re.search(r"(\d{3,5})p", source_label)
                            if height_match:
                                result_formats[0]["height"] = int(height_match.group(1))

                if is_playlist:
                    result["entries"] = entries
                    result["playlistTitle"] = info.get("title") or ""
                    result["isPlaylist"] = True
                    result["isVideoInPlaylist"] = video_info is not None
                    result["currentEntryId"] = (
                        str(video_info.get("id")) if video_info and video_info.get("id") is not None else None
                    )
                    result["playlistCount"] = info.get("playlist_count") or len(entries or []) or None

                log.info(
                    "Probe succeeded",
                    url=url,
                    engine=engine,
                    title=result.get("title"),
                    is_playlist=is_playlist,
                    is_video_in_playlist=result.get("isVideoInPlaylist"),
                    formats=len(result.get("formats") or []),
                    entries=len(entries or []),
                )
                if engine == "ytdlp":
                    return {"ok": True, "engine": engine, "url": request_url, "result": result}, info

                if combined_fallback is None:
                    combined_fallback = (result, info, request_url)
                else:
                    combined_result, combined_info, combined_url = combined_fallback
                    combined_result["formats"] = _merge_format_rows(
                        combined_result.get("formats") or [],
                        result.get("formats") or [],
                    )
                    combined_fallback = (combined_result, combined_info, combined_url)

                candidate_has_multiple_formats = len(result.get("formats") or []) > 1
                if candidate_has_multiple_formats or attempt_index == len(attempts) - 1:
                    combined_result, combined_info, combined_url = combined_fallback
                    log.info(
                        "Probe succeeded",
                        url=url,
                        engine="extractor",
                        title=combined_result.get("title"),
                        formats=len(combined_result.get("formats") or []),
                    )
                    return {
                        "ok": True,
                        "engine": "extractor",
                        "url": combined_url,
                        "result": combined_result,
                    }, combined_info
        except UnsupportedError:
            log.warning("URL not supported by yt-dlp", candidate=candidate)
            last_error = f"URL is not supported by yt-dlp: {candidate}"
            continue
        except DownloadCancelled:
            log.info("Probe cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - extractor errors vary wildly
            last_error = str(exc).strip() or exc.__class__.__name__
            log.warning("Probe attempt failed", engine=engine, candidate=candidate, error=last_error)
            continue

    log.warning("Probe failed", url=url, error=last_error or "No extractor could handle this URL")
    return {"ok": False, "engine": "none", "error": last_error or "No extractor could handle this URL"}, None


class DownloadJob:
    """State for a single download; mutated from the worker thread."""

    def __init__(
        self,
        url: str,
        directory: str,
        *,
        format_selector: str | None = None,
        filename: str | None = None,
        title: str = "",
        thumbnail: str = "",
        engine: str = "ytdlp",
        media_type: str = "",
        parent_id: str | None = None,
        is_playlist: bool = False,
        child_ids: list[str] | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.url = url
        self.directory = directory
        self.format_selector = format_selector
        self.custom_filename = filename
        self.engine = engine
        self.media_type = media_type
        self.parent_id = parent_id
        self.is_playlist = is_playlist
        self.child_ids = list(child_ids or [])
        # Merged selectors ("video+audio") download as separate components;
        # track per-component progress until every component reports a total.
        self.expected_components = 1 + format_selector.count("+") if format_selector else 1
        self.progress_components: dict[str, dict[str, Any]] = {}
        self.event_revision = 0
        self.cancel_event = threading.Event()
        self.pause_requested = False
        self.data: dict[str, Any] = {
            "id": self.id,
            "url": url,
            "title": title,
            "filename": filename or "",
            "thumbnail": thumbnail,
            "directory": directory,
            "engine": engine,
            "mediaType": media_type,
            "formatSelector": format_selector,
            "customFilename": filename,
            "parentId": parent_id or "",
            "isPlaylist": is_playlist,
            "childIds": self.child_ids,
            "playlistCount": None,
            "status": "queued",
            "percent": 0.0,
            "downloaded": 0,
            "total": None,
            "speed": None,
            "eta": None,
            "segmentsDone": None,
            "segmentsTotal": None,
            "error": None,
            "createdAt": time.time(),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> DownloadJob:
        """Rebuild a job from a persisted database row (see :mod:`db`)."""

        def _parse_child_ids(value: Any) -> list[str]:
            if not value:
                return []
            if isinstance(value, str):
                try:
                    parsed = orjson.loads(value)
                    if isinstance(parsed, list):
                        return [str(x) for x in parsed if x is not None]
                except orjson.JSONDecodeError:
                    pass
            if isinstance(value, list):
                return [str(x) for x in value if x is not None]
            return []

        job = cls(
            row["url"],
            row["directory"],
            format_selector=row["format_selector"],
            filename=row["custom_filename"],
            title=row["title"],
            thumbnail=row["thumbnail"],
            engine=row["engine"],
            media_type=row.get("media_type") or "",
            parent_id=row.get("parent_id") or None,
            is_playlist=bool(row.get("is_playlist")),
            child_ids=_parse_child_ids(row.get("child_ids")),
        )
        job.id = row["id"]
        job.data.update(
            id=job.id,
            status=row["status"],
            percent=row["percent"],
            downloaded=row["downloaded"],
            total=row["total"],
            error=row["error"],
            createdAt=row["created_at"],
            parentId=job.parent_id or "",
            isPlaylist=job.is_playlist,
            childIds=job.child_ids,
            playlistCount=len(job.child_ids) if job.child_ids else None,
        )
        return job

    def snapshot(self) -> dict[str, Any]:
        snapshot = dict(self.data)
        snapshot["revision"] = self.event_revision
        return snapshot


class DownloadManager:
    """Runs yt-dlp downloads in a thread pool and fans out progress events."""

    def __init__(
        self,
        settings_store: SettingsStore,
        db: Database,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._settings_store = settings_store
        self._db = db
        self._loop = loop
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, settings_store.get()["maxConcurrentDownloads"]),
            thread_name_prefix="ytdlp",
        )
        self._jobs: dict[str, DownloadJob] = {}
        self._lock = threading.RLock()
        self._last_emit: dict[str, float] = {}
        self._listeners: list[Any] = []
        self._worker_futures: dict[str, Any] = {}
        self._db_tasks: dict[str, asyncio.Task[Any]] = {}
        self._size_cache: dict[tuple[str, str], tuple[int, bool] | None] = {}
        self._probed: dict[str, tuple[str, Any]] = {}
        self._torrent_manager = TorrentManager(settings_store, db, loop)
        _temp_root().mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- persistence
    def _enqueue_db_operation(self, job_id: str, operation: Any) -> None:
        """Serialize database operations for one job in submission order."""
        def _schedule() -> None:
            previous = self._db_tasks.get(job_id)

            async def run() -> None:
                if previous is not None:
                    try:
                        await previous
                    except Exception:  # noqa: BLE001
                        log.exception("Previous database operation failed for job %s", job_id)
                try:
                    await operation()
                except Exception:  # noqa: BLE001
                    log.exception("Database operation failed for job %s", job_id)

            task = asyncio.ensure_future(run())
            self._db_tasks[job_id] = task

            def _cleanup(done: asyncio.Future[Any]) -> None:
                if self._db_tasks.get(job_id) is done:
                    self._db_tasks.pop(job_id, None)

            task.add_done_callback(_cleanup)

        try:
            self._loop.call_soon_threadsafe(_schedule)
        except RuntimeError:
            log.error("Could not schedule database operation for job %s: event loop is closed", job_id)

    def _persist(self, job: DownloadJob) -> None:
        """Schedule an ordered job snapshot write on the event loop."""
        if job.data.get("removed"):
            return
        snapshot = job.snapshot()

        async def write() -> None:
            if not job.data.get("removed"):
                await self._db.upsert_job(snapshot)

        self._enqueue_db_operation(job.id, write)

    def _delete_persisted(self, job_id: str) -> None:
        self._enqueue_db_operation(job_id, lambda: self._db.delete_job(job_id))

    async def restore(self) -> None:
        """Reload persisted jobs after a restart.

        Jobs that were active or queued become ``paused`` so the user can
        resume them explicitly (yt-dlp continues ``.part`` files).
        """
        rows = await self._db.list_jobs()
        self._torrent_manager.restore(rows)
        ytdlp_rows = [row for row in rows if row.get("engine") != "torrent"]
        for row in reversed(ytdlp_rows):  # oldest first, preserves submission order
            if row["status"] in {"queued", "downloading", "postprocessing"}:
                row["status"] = "paused"
            job = DownloadJob.from_row(row)
            with self._lock:
                self._jobs[job.id] = job
            self._persist(job)  # write back the corrected status
        if rows:
            log.info("Restored %d job(s) from database", len(rows))

    # ------------------------------------------------------------------ events
    def add_listener(self, callback: Any) -> None:
        """Register an async callable(job_snapshot) invoked on the event loop."""
        self._listeners.append(callback)
        self._torrent_manager.add_listener(callback)

    async def _dispatch_listener(self, listener: Any, snapshot: dict[str, Any]) -> None:
        try:
            await listener(snapshot)
        except Exception:  # noqa: BLE001
            log.exception("Job listener failed")

    def _emit(self, job: DownloadJob, *, force: bool = False) -> None:
        """Emit a job snapshot to listeners; safe to call from any thread."""
        with self._lock:
            self._emit_unlocked(job, force=force)

    def _update_parent(self, child: DownloadJob, *, force: bool = False) -> None:
        """Recompute a playlist parent's aggregate state from its children."""
        if not child.parent_id:
            return
        with self._lock:
            parent = self._jobs.get(child.parent_id)
            if not parent or not parent.is_playlist or parent.data.get("removed"):
                return
            if parent.data["status"] in {"completed", "failed", "cancelled"}:
                return

            now = time.monotonic()
            if not force and now - self._last_emit.get(parent.id, 0) < PROGRESS_INTERVAL:
                return

            children = [self._jobs.get(cid) for cid in parent.child_ids]
            children = [c for c in children if c and not c.data.get("removed")]
            if not children:
                return

            statuses = [c.data["status"] for c in children]
            downloaded = sum(int(c.data.get("downloaded") or 0) for c in children)
            totals = [c.data["total"] for c in children if c.data.get("total") is not None]
            total = sum(totals) if totals else None
            speeds = [c.data["speed"] for c in children if c.data.get("speed") is not None]
            speed = sum(speeds) if speeds else None
            eta = None
            if total is not None and speed:
                eta = max(0, (total - downloaded) + speed - 1) // speed
            percent = round(downloaded / total * 100, 1) if total else 0.0

            if all(s == "completed" for s in statuses):
                status = "completed"
                error = None
            elif all(s == "paused" for s in statuses):
                status = "paused"
                error = None
            elif any(s in {"failed", "cancelled"} for s in statuses) and not any(
                s in {"queued", "downloading", "postprocessing"} for s in statuses
            ):
                failed = sum(1 for s in statuses if s == "failed")
                cancelled = sum(1 for s in statuses if s == "cancelled")
                parts: list[str] = []
                if failed:
                    parts.append(f"{failed} failed")
                if cancelled:
                    parts.append(f"{cancelled} cancelled")
                status = "failed"
                error = "Playlist finished with " + ", ".join(parts) if parts else None
            else:
                status = "downloading"
                error = None

            parent.data.update(
                status=status,
                percent=percent,
                downloaded=downloaded,
                total=total,
                speed=speed,
                eta=eta,
                error=error,
            )
            self._emit_unlocked(parent, force=force)

    def _emit_unlocked(self, job: DownloadJob, *, force: bool = False) -> None:
        """Emit a job snapshot; caller must hold :attr:`_lock`."""
        if job.data.get("removed"):
            return
        now = time.monotonic()
        if not force and now - self._last_emit.get(job.id, 0) < PROGRESS_INTERVAL:
            return
        self._last_emit[job.id] = now
        job.event_revision += 1
        if force:
            self._persist(job)
        snapshot = job.snapshot()
        # Capture the listener list while we hold the lock so the dispatch
        # closure doesn't read mutable state from another thread.
        listeners = list(self._listeners)

        def _dispatch() -> None:
            for listener in listeners:
                asyncio.ensure_future(self._dispatch_listener(listener, snapshot))

        self._loop.call_soon_threadsafe(_dispatch)
        self._update_parent(job, force=force)

    def _submit(self, job: DownloadJob) -> None:
        future = self._executor.submit(self._run, job)
        with self._lock:
            self._worker_futures[job.id] = future

        def _clear(_future: Any) -> None:
            with self._lock:
                if self._worker_futures.get(job.id) is _future:
                    self._worker_futures.pop(job.id, None)

        future.add_done_callback(_clear)

    async def shutdown(self) -> None:
        """Stop workers before the database connection is closed."""
        log.info("Shutting down manager")
        with self._lock:
            active = [
                job for job in self._jobs.values()
                if job.data["status"] in {"queued", "downloading", "postprocessing"}
            ]
        for job in active:
            # Mark as a graceful shutdown/pause so interrupted jobs resume
            # instead of being treated as cancelled.
            job.pause_requested = True
            job.cancel_event.set()
        await self._loop.run_in_executor(
            None,
            lambda: self._executor.shutdown(wait=True, cancel_futures=True),
        )
        await self._torrent_manager.shutdown()
        pending_db = list(self._db_tasks.values())
        if pending_db:
            await asyncio.gather(*pending_db, return_exceptions=True)
        log.info("Manager shutdown complete")

    # ------------------------------------------------------------------- queue
    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [job.snapshot() for job in self._jobs.values()]
        all_jobs = jobs + self._torrent_manager.list_jobs()
        all_jobs.sort(key=lambda j: float(j.get("createdAt") or 0), reverse=True)
        return all_jobs

    def get(self, job_id: str) -> DownloadJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def probe(
        self,
        url: str,
        fallback_urls: str | list[str] | None,
        media_type: str = "",
        fallback_sources: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Run a probe and cache resolved sizes for the returned format rows."""
        if is_torrent_input(url):
            return self._torrent_manager.probe(url)
        fallback_count = 0
        if isinstance(fallback_urls, list):
            fallback_count = len(fallback_urls)
        elif isinstance(fallback_urls, str) and fallback_urls.strip():
            fallback_count = 1
        if fallback_count:
            log.info("Manager probe requested", url=url, fallback_count=fallback_count)
        else:
            log.info("Manager probe requested", url=url)
        result, info = probe(
            url,
            fallback_urls,
            self._settings_store.get(),
            media_type,
            fallback_sources,
        )

        if result.get("ok") and result.get("url"):
            with self._lock:
                self._probed[url] = (result["url"], info)
            log.debug("Probe cached", url=url, request_url=result["url"])

        if result.get("ok") and result.get("result") and result["result"].get("formats"):
            request_url = result.get("url") or url
            with self._lock:
                for fmt in result["result"]["formats"]:
                    selector = fmt.get("selector") or "best"
                    size = fmt.get("size")
                    if size is not None:
                        self._size_cache[(request_url, selector)] = (size, bool(fmt.get("sizeIsEstimate")))

        return result

    # ------------------------------------------------------------------ duplicates
    def _expected_target_name(
        self,
        url: str,
        filename: str | None,
        title: str,
        media_type: str,
        merge_format: str,
    ) -> tuple[str, str, str] | None:
        """Return (basename, stem, ext) for the file that would be written.

        For non-file media the extension is the merge output format and is only
        a guess; for intercepted files the extension is exact. Returns None
        when the name cannot be determined before download.
        """
        if filename:
            path = Path(filename)
            return path.name, path.stem, path.suffix.lstrip(".")

        if media_type == "file":
            clean_url = unsmuggle_url(url, {})[0]
            name = Path(clean_url.split("?")[0].split("#")[0]).name or "download"
            stem = Path(name).stem
            ext = _ext_from_name("", name) or "bin"
            return f"{stem}.{ext}", stem, ext

        # For media without a custom filename, guess from title or URL.
        clean_url = unsmuggle_url(url, {})[0]
        name = Path(clean_url.split("?")[0].split("#")[0]).name or "download"
        stem = title or Path(name).stem
        if not stem:
            return None
        ext = merge_format or "mp4"
        return f"{stem}.{ext}", stem, ext

    def _generate_unique_filename(
        self,
        directory: str,
        stem: str,
        ext: str,
    ) -> str:
        """Find a filename like 'stem (1).ext' that does not already exist."""
        base = re.sub(r"\s+\(\d+\)$", "", stem)
        base_path = Path(directory)
        for n in range(1, 1000):
            candidate = f"{base} ({n}).{ext}" if ext else f"{base} ({n})"
            if not (base_path / candidate).exists():
                return candidate
        return f"{base} (1).{ext}" if ext else f"{base} (1)"

    def resolve_duplicate(
        self,
        url: str,
        directory: str,
        format_selector: str | None,
        filename: str | None,
        title: str,
        media_type: str,
        action: str | None = None,
    ) -> dict[str, Any]:
        """Check for a duplicate active job or existing file.

        Magnet URLs use the torrent manager's duplicate policy because their
        output name is not known until metadata has been fetched.

        When ``action`` is None and a duplicate exists, returns a description
        that the client can present to the user. When ``action`` is
        ``override``, ``rename``, or ``skip`` the duplicate is resolved and a
        filename safe to start with is returned.
        """
        if is_magnet_url(url):
            return self._torrent_manager.resolve_duplicate(url, directory, action)

        # Playlists do not produce a single output file; only check for an active
        # duplicate playlist job and let the caller create child jobs.
        if media_type == "playlist":
            with self._lock:
                for job in self._jobs.values():
                    if job.url == url and job.directory == directory and job.is_playlist:
                        if job.data["status"] in {"queued", "downloading", "paused", "postprocessing"}:
                            return {
                                "status": "duplicate",
                                "type": "job",
                                "existing": job.snapshot(),
                                "filename": filename or "",
                                "suggestedName": "",
                            }
            return {"status": "ok", "filename": None}

        settings = self._settings_store.get()
        merge_format = (settings.get("mergeOutputFormat") or "mp4").strip().lower()
        expected = self._expected_target_name(url, filename, title, media_type, merge_format)
        target_name = expected[0] if expected else None
        target_stem = expected[1] if expected else None
        target_ext = expected[2] if expected else None

        def _is_active(job: DownloadJob) -> bool:
            return job.data["status"] in {"queued", "downloading", "paused", "postprocessing"}

        # Find an active job that would write the same output.
        duplicate_job: DownloadJob | None = None
        with self._lock:
            for job in self._jobs.values():
                if job.url != url or job.directory != directory:
                    continue
                if not _is_active(job):
                    continue
                if (job.format_selector or None) != (format_selector or None):
                    continue
                if filename:
                    job_name = Path(job.custom_filename or job.data.get("filename") or "").name
                    if job_name and job_name != Path(filename).name:
                        continue
                duplicate_job = job
                break

        # Find an existing file on disk.
        target_path = Path(directory) / target_name if target_name else None
        duplicate_file = target_path and target_path.exists()

        # No duplicate: just return the original filename.
        if not duplicate_job and not duplicate_file:
            return {"status": "ok", "filename": filename}

        if action == "skip":
            return {"status": "ok", "skipped": True}

        if action == "override":
            # Remove any active/completed job that matches the target.
            with self._lock:
                for job in list(self._jobs.values()):
                    if job.directory != directory:
                        continue
                    if filename and Path(job.custom_filename or job.data.get("filename") or "").name != Path(filename).name:
                        continue
                    if not filename and (job.url != url or (job.format_selector or None) != (format_selector or None)):
                        continue
                    self.remove(job.id)
            if duplicate_file and target_path:
                try:
                    send2trash(str(target_path))
                except Exception as exc:  # noqa: BLE001
                    log.warning("Could not trash %s: %s", target_path, exc)
                    try:
                        target_path.unlink()
                    except Exception:  # noqa: BLE001
                        pass
            return {"status": "ok", "filename": filename}

        if action == "rename":
            if not target_stem:
                # No usable stem; derive one from the URL or existing filename.
                clean_url = unsmuggle_url(url, {})[0]
                name = Path(clean_url.split("?")[0].split("#")[0]).name or "download"
                target_stem = Path(name).stem
            if not target_ext:
                target_ext = _ext_from_name("", filename or "") or "bin"
            new_filename = self._generate_unique_filename(directory, target_stem, target_ext)
            return {"status": "ok", "filename": new_filename}

        # No action supplied: report the duplicate for the UI.
        dup_type = "job" if duplicate_job is not None else "file"
        if duplicate_job is not None:
            existing = duplicate_job.snapshot()
            existing_filename = Path(duplicate_job.custom_filename or duplicate_job.data.get("filename") or " download").name
            suggested_stem = target_stem or Path(existing_filename).stem or "download"
            suggested_ext = target_ext or Path(existing_filename).suffix.lstrip(".") or "bin"
        else:
            existing = str(target_path)
            suggested_stem = target_stem or Path(existing).stem
            suggested_ext = target_ext or Path(existing).suffix.lstrip(".")

        suggested_name = self._generate_unique_filename(directory, suggested_stem, suggested_ext)
        return {
            "status": "duplicate",
            "type": dup_type,
            "existing": existing,
            "filename": target_name or filename or "",
            "suggestedName": suggested_name,
        }

    def _resolve_job_total(self, job: DownloadJob) -> None:
        """Set job.data['total'] from a cached probe result if possible."""
        if job.data.get("total"):
            return

        with self._lock:
            probed = self._probed.get(job.url)
        if not probed:
            log.debug("No cached probe for %s; total will come from progress hooks", job.url)
            return

        request_url, info = probed
        if not info:
            log.debug("Probed info missing for %s", job.url)
            return

        selector = job.format_selector or "best"
        with self._lock:
            cached = self._size_cache.get((request_url, selector))
        if cached:
            job.data["total"] = cached[0]
            return

        settings = self._settings_store.get()
        opts: dict[str, Any] = {**_base_opts(settings), "skip_download": True, "extract_flat": False}
        _apply_smuggled_http_headers(opts, request_url)
        try:
            with YoutubeDL(cast(Any, opts)) as ydl:
                resolved = resolve_selector_size(ydl, info, job.format_selector)
                if resolved:
                    job.data["total"] = resolved[0]
                    with self._lock:
                        self._size_cache[(request_url, selector)] = resolved
        except Exception:  # noqa: BLE001
            log.debug("Could not resolve total for %s: %s", request_url, job.format_selector)

    def start(
        self,
        url: str,
        directory: str,
        *,
        format_selector: str | None = None,
        filename: str | None = None,
        title: str = "",
        thumbnail: str = "",
        engine: str = "ytdlp",
        media_type: str = "",
        selected_files: list[str] | None = None,
        parent_id: str | None = None,
    ) -> DownloadJob:
        if is_torrent_input(url):
            return self._torrent_manager.start(url, directory, filename=filename, title=title, selected_files=selected_files)  # type: ignore[return-value]
        job = DownloadJob(
            url,
            directory,
            format_selector=format_selector,
            filename=filename,
            title=title,
            thumbnail=thumbnail,
            engine=engine,
            media_type=media_type,
            parent_id=parent_id,
        )
        with self._lock:
            self._jobs[job.id] = job
            cached = self._size_cache.get((url, format_selector or "best"))
            if cached:
                job.data["total"] = cached[0]
            self._emit_unlocked(job, force=True)
        log.info("Download started", job_id=job.id, url=url, format_selector=format_selector, directory=directory)
        self._submit(job)
        return job

    def _get_playlist_entries(self, url: str) -> tuple[str | None, list[dict[str, Any]] | None]:
        """Return (request_url, entries) for a playlist URL, reusing a cached probe."""
        with self._lock:
            probed = self._probed.get(url)
        if probed:
            request_url, info = probed
            if info and info.get("_type") == "playlist":
                return request_url, _build_playlist_entries(info)

        settings = self._settings_store.get()
        opts = {**_base_opts(settings), "skip_download": True, "extract_flat": "in_playlist"}
        _apply_smuggled_http_headers(opts, url)
        try:
            with YoutubeDL(cast(Any, opts)) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not re-extract playlist", url=url, error=str(exc))
            return None, None
        if not info or info.get("_type") != "playlist":
            return None, None
        return url, _build_playlist_entries(info)

    def start_playlist(
        self,
        url: str,
        directory: str,
        *,
        format_selector: str | None = None,
        title: str = "",
        thumbnail: str = "",
        selected_entry_urls: list[str] | None = None,
    ) -> DownloadJob:
        """Start a playlist download: one parent job plus a child job per entry."""
        request_url, entries = self._get_playlist_entries(url)
        if not entries:
            raise ValueError(f"No playlist entries found for {url}")

        if selected_entry_urls:
            selected_set = set(selected_entry_urls)
            entries = [e for e in entries if e.get("url") in selected_set or e.get("webpageUrl") in selected_set]

        parent = DownloadJob(
            url,
            directory,
            title=title,
            thumbnail=thumbnail,
            engine="ytdlp",
            media_type="playlist",
            is_playlist=True,
        )
        parent.data["playlistCount"] = len(entries)

        with self._lock:
            self._jobs[parent.id] = parent
            self._emit_unlocked(parent, force=True)

        settings = self._settings_store.get()
        merge_format = (settings.get("mergeOutputFormat") or "mp4").strip().lower()
        playlist_folder = sanitize_filename(title or "Playlist", restricted=False).strip() or "Playlist"
        child_ids: list[str] = []
        max_index = max((e.get("index", idx + 1) for idx, e in enumerate(entries)), default=len(entries))
        digits = max(3, len(str(max_index)))
        for idx, entry in enumerate(entries):
            child_url = entry.get("url") or entry.get("webpageUrl") or url
            child_title = entry.get("title") or ""
            child_thumbnail = entry.get("thumbnail") or ""
            raw_index = entry.get("index", idx + 1)
            rev_index = max_index - raw_index + 1

            # Prefix files with reversed playlist order to keep them sorted on disk.
            child_filename = None
            if child_title:
                stem = f"{rev_index:0{digits}d} - {child_title}"
                child_filename = f"{playlist_folder}/{stem}.{merge_format}"

            resolved = self.resolve_duplicate(child_url, directory, format_selector, child_filename, child_title, "", "rename")
            if resolved.get("status") == "duplicate" and child_filename:
                resolved = self.resolve_duplicate(child_url, directory, format_selector, None, child_title, "", "rename")
            final_filename = resolved.get("filename") if resolved.get("status") == "ok" else None

            child = self.start(
                child_url,
                directory,
                format_selector=format_selector,
                filename=final_filename,
                title=child_title,
                thumbnail=child_thumbnail,
                engine="ytdlp",
                parent_id=parent.id,
            )
            child_ids.append(child.id)

        with self._lock:
            parent.child_ids = child_ids
            parent.data["childIds"] = child_ids
            parent.data["status"] = "downloading"
            self._emit_unlocked(parent, force=True)

        log.info("Playlist download started", job_id=parent.id, url=url, entries=len(entries), directory=directory)
        return parent

    def _cancel_queued_worker(self, job_id: str) -> None:
        with self._lock:
            future = self._worker_futures.get(job_id)
        if future is not None:
            future.cancel()

    def cancel(self, job_id: str) -> bool:
        if self._torrent_manager.get(job_id):
            return self._torrent_manager.cancel(job_id)
        job = self.get(job_id)
        if not job:
            return False

        # Propagate to children for playlist parents.
        if job.is_playlist and job.child_ids:
            with self._lock:
                job.data.update(status="cancelled", speed=None, eta=None, error=None)
                self._emit_unlocked(job, force=True)
            log.info("Cancelling playlist", job_id=job_id)
            for child_id in list(job.child_ids):
                self.cancel(child_id)
            return True

        if job.data["status"] not in {"queued", "paused", "downloading", "postprocessing"}:
            return False
        job.cancel_event.set()
        log.info("Cancelling job", job_id=job_id, status=job.data["status"])
        if job.data["status"] in {"queued", "paused"}:
            self._cancel_queued_worker(job_id)
            with self._lock:
                job.data.update(status="cancelled", speed=None, eta=None)
                self._emit_unlocked(job, force=True)
        else:
            with self._lock:
                job.data.update(speed=None, eta=None)
                self._emit_unlocked(job, force=True)
        return True

    def pause(self, job_id: str) -> bool:
        if self._torrent_manager.get(job_id):
            return self._torrent_manager.pause(job_id)
        job = self.get(job_id)
        if not job:
            return False

        # Propagate to children for playlist parents.
        if job.is_playlist and job.child_ids:
            log.info("Pausing playlist", job_id=job_id)
            for child_id in list(job.child_ids):
                self.pause(child_id)
            return True

        status = job.data["status"]
        log.info("Pausing job", job_id=job_id, status=status)
        if status == "queued":
            job.pause_requested = True
            job.cancel_event.set()
            self._cancel_queued_worker(job_id)
            with self._lock:
                job.data.update(status="paused", speed=None, eta=None)
                self._emit_unlocked(job, force=True)
        elif status in {"downloading", "postprocessing"}:
            job.pause_requested = True
            job.cancel_event.set()  # progress hook raises DownloadCancelled
        else:
            return False
        return True

    def resume(self, job_id: str) -> bool:
        if self._torrent_manager.get(job_id):
            return self._torrent_manager.resume(job_id)
        job = self.get(job_id)
        if not job:
            return False

        # Propagate to children for playlist parents.
        if job.is_playlist and job.child_ids:
            log.info("Resuming playlist", job_id=job_id)
            for child_id in list(job.child_ids):
                self.resume(child_id)
            return True

        if job.data["status"] not in {"paused", "failed", "cancelled"}:
            return False
        log.info("Resuming job", job_id=job_id, status=job.data["status"])
        job.cancel_event.clear()
        job.pause_requested = False
        with self._lock:
            job.progress_components.clear()
            job.data.update(status="queued", error=None, speed=None, eta=None)
            self._emit_unlocked(job, force=True)
        self._submit(job)
        return True

    def reveal(self, job_id: str) -> dict[str, Any]:
        torrent_job = self._torrent_manager.get(job_id)
        if torrent_job is not None:
            storage = self._torrent_manager.storage_path(torrent_job)
            filepath = str(storage) if storage else ""
            job = None
        else:
            job = self.get(job_id)
            if not job:
                return {"ok": False, "error": "Unknown job"}
            filepath = job.data.get("finalFilepath") or (
                str(Path(job.directory) / job.data["filename"]) if job.data.get("filename") else job.directory
            )
        if not filepath or not Path(filepath).exists():
            if job and job.directory and Path(job.directory).exists():
                filepath = job.directory
            else:
                return {"ok": False, "error": "File not found"}
        try:
            if platform.system() == "Darwin":
                subprocess.run(["open", "-R", filepath], check=True)
            elif platform.system() == "Windows":
                subprocess.run(["explorer", f"/select,{filepath}"], check=True)
            else:
                subprocess.run(["xdg-open", str(Path(filepath).parent)], check=True)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not reveal file", filepath=filepath, error=str(exc))
            return {"ok": False, "error": f"Could not reveal file: {exc}"}

    def remove(self, job_id: str) -> bool:
        if self._torrent_manager.get(job_id):
            return self._torrent_manager.remove(job_id)
        job = self.get(job_id)
        if not job:
            return False

        # Propagate to children for playlist parents.
        if job.is_playlist and job.child_ids:
            log.info("Removing playlist", job_id=job_id)
            for child_id in list(job.child_ids):
                self.remove(child_id)
            with self._lock:
                job.data["removed"] = True
                self._jobs.pop(job_id, None)
                self._delete_persisted(job_id)
            return True

        log.info("Removing job", job_id=job_id, status=job.data["status"])
        active = job.data["status"] in {"downloading", "postprocessing"}
        if active:
            job.cancel_event.set()
        elif job.data["status"] == "queued":
            job.cancel_event.set()
            self._cancel_queued_worker(job_id)
        elif job.data["status"] in {"paused", "failed", "cancelled"}:
            # Worker is not running; trash the isolated temp files now.
            try:
                self._loop.run_in_executor(None, _trash_job_temp, job_id)
            except RuntimeError:
                log.error("Could not schedule trash for job %s: event loop is closed", job_id)
        with self._lock:
            job.data["removed"] = True
            self._jobs.pop(job_id, None)
            self._delete_persisted(job_id)
        return True

    def clear_finished(self) -> list[str]:
        with self._lock:
            done = [
                job_id
                for job_id, job in self._jobs.items()
                if job.data["status"] == "completed"
            ]
            for job_id in done:
                job = self._jobs.pop(job_id, None)
                if job:
                    job.data["removed"] = True
        torrent_done = self._torrent_manager.clear_finished()
        if done:
            for job_id in done:
                try:
                    self._loop.run_in_executor(None, _trash_job_temp, job_id)
                except RuntimeError:
                    log.error("Could not schedule trash for finished job %s", job_id)
                self._delete_persisted(job_id)
        return done + torrent_done

    # ------------------------------------------------------------------ worker
    @staticmethod
    def _is_media_progress(d: dict[str, Any]) -> bool:
        info = d.get("info_dict")
        if not isinstance(info, dict):
            return True
        info = cast(dict[str, Any], info)
        if info.get("requested_formats") is not None or info.get("fragments") is not None:
            return True
        if any(info.get(codec) not in (None, "none") for codec in ("vcodec", "acodec")):
            return True
        return str(info.get("ext") or "").lower() not in {
            "jpg", "jpeg", "png", "webp", "gif", "avif",
            "vtt", "srt", "ass", "ssa", "lrc", "ttml", "dfxp", "smi", "json3",
        }

    @staticmethod
    def _progress_component_key(d: dict[str, Any]) -> str:
        info = cast(dict[str, Any], d.get("info_dict"))
        if isinstance(info, dict) and info.get("format_id"):
            return f"format:{info['format_id']}"
        filepath = (
            d.get("filename")
            or (info.get("filepath") if isinstance(info, dict) else None)
            or (info.get("filename") if isinstance(info, dict) else None)
            or (info.get("_filename") if isinstance(info, dict) else None)
        )
        if filepath:
            clean_path = re.sub(r'(?:[._-]Frag\d+)+|\.part$', '', str(filepath))
            return f"path:{clean_path}"
        return "media"

    @staticmethod
    def _aggregate_progress(
        job: DownloadJob,
    ) -> tuple[int, int | None, int | None, int | None]:
        components = list(job.progress_components.values())
        downloaded = sum(int(component.get("downloaded") or 0) for component in components)
        totals = [int(component["total"]) for component in components if component.get("total")]
        observed_total = sum(totals) if totals else None
        # Prefer the pre-computed playlist size (byterange sum or
        # bitrate/duration estimate). It is far more stable than the
        # fragment downloader's live "total_bytes_estimate", which can
        # overshoot by 2-3x while the first few segments are being fetched.
        # If the actual download exceeds that estimate, grow the total so the
        # progress bar never sits at 100% while the file is still growing.
        precomputed = job.data.get("total")
        if job.expected_components > 1 and len(components) >= job.expected_components and observed_total is not None:
            total: int | None = max(int(precomputed or 0), observed_total)
        elif precomputed:
            total = int(precomputed)
        elif observed_total is not None:
            total = observed_total
        else:
            total = None
        if total is not None and downloaded > total:
            total = downloaded
        active_speeds = [int(component["speed"]) for component in components if component.get("speed")]
        speed = sum(active_speeds) if active_speeds else None
        eta = ((max(0, total - downloaded) + speed - 1) // speed if total and speed else None)

        segments_done = sum(
            int(component.get("segments_done") or 0)
            for component in components
            if component.get("segments_done") is not None
        )
        segment_totals = [
            int(component["segments_total"])
            for component in components
            if component.get("segments_total")
        ]
        segments_total = sum(segment_totals) if segment_totals else None

        if segments_total:
            job.data["segmentsTotal"] = segments_total
            job.data["segmentsDone"] = min(segments_done, segments_total)
        elif segments_done:
            job.data["segmentsDone"] = segments_done

        return downloaded, total, speed, eta

    def _progress_hook(self, job: DownloadJob) -> Any:
        def hook(d: dict[str, Any]) -> None:
            if job.cancel_event.is_set():
                raise DownloadCancelled("Cancelled by user")

            status = d.get("status")
            if status not in {"downloading", "finished"} or not self._is_media_progress(d):
                return

            with self._lock:
                key = self._progress_component_key(d)
                component = job.progress_components.setdefault(key, {})
                info = d.get("info_dict")
                if isinstance(info, dict) and not job.data.get("thumbnail"):
                    thumb = _extract_thumbnail(info)
                    if thumb:
                        job.thumbnail = thumb
                        job.data["thumbnail"] = thumb
                observed_total = d.get("total_bytes") or d.get("total_bytes_estimate")
                is_segmented = bool(d.get("segment_count") or d.get("fragment_count"))
                precomputed_total = job.data.get("total")
                if observed_total:
                    observed_total = int(observed_total)
                    # For segmented (HLS/DASH/m3u8) downloads the fragment
                    # downloader's "total_bytes_estimate" can be far off (it may
                    # overshoot by 2-3x while the first fragments are being
                    # fetched). When we have a pre-computed size from the playlist
                    # (byterange sum or bitrate/duration) we use that as the base
                    # and only let it grow if the actual download exceeds it.
                    if is_segmented and not precomputed_total:
                        current = component.get("total")
                        if current is None or observed_total > current:
                            component["total"] = observed_total
                    elif not precomputed_total:
                        component["total"] = observed_total
                    # When a pre-computed total exists, _aggregate_progress uses
                    # it and grows to the downloaded size if needed, so we do not
                    # overwrite it with the live estimate here.

                segment_count = d.get("segment_count") or d.get("fragment_count")
                if segment_count:
                    component["segments_total"] = int(segment_count)

                segments_done = d.get("segments_done")
                if segments_done is not None:
                    component["segments_done"] = int(segments_done)
                elif d.get("fragment_index") is not None:
                    fragment_index = int(d["fragment_index"])
                    total_seg = component.get("segments_total") or (int(segment_count) if segment_count else None)
                    if status == "finished":
                        component["segments_done"] = total_seg or fragment_index
                    else:
                        completed = fragment_index
                        if total_seg:
                            completed = min(completed, total_seg)
                        component["segments_done"] = max(component.get("segments_done", 0), completed)

                if status == "downloading":
                    component.update(
                        downloaded=d.get("downloaded_bytes") or 0,
                        speed=d.get("speed") or 0,
                    )
                    downloaded, total, speed, eta = self._aggregate_progress(job)
                    if eta is None and d.get("eta") is not None:
                        eta = int(d["eta"])
                    percent = round(min(downloaded / total, 1.0) * 100, 1) if total else job.data["percent"]
                    if job.expected_components > 1 and len(job.progress_components) < job.expected_components:
                        percent = min(percent, 99.9)
                    job.data.update(
                        status="downloading",
                        downloaded=downloaded,
                        total=total,
                        speed=speed,
                        eta=eta,
                        percent=percent,
                    )
                    if d.get("filename"):
                        job.data["filename"] = Path(d["filename"]).name
                    self._emit_unlocked(job)
                else:
                    # Lock the final total to the actual downloaded/observed size so
                    # the completed job reports real bytes, not the pre-computed estimate.
                    observed_total = d.get("total_bytes") or d.get("downloaded_bytes") or 0
                    info = d.get("info_dict")
                    info_dict = cast(dict[str, Any], info) if isinstance(info, dict) else None
                    is_combined_report = (
                        job.expected_components > 1
                        and info_dict is not None
                        and (
                            "+" in str(info_dict.get("format_id") or "")
                            or bool(info_dict.get("requested_formats"))
                        )
                    )
                    existing_total = sum(int(c.get("total") or 0) for c in job.progress_components.values())
                    # A finished report whose total matches the running total of
                    # previous parts is the combined/whole-file report. Replace the
                    # per-format or per-fragment parts so we don't double-count them
                    # when they are also represented by the final merged file.
                    if (
                        is_combined_report
                        or (
                            job.expected_components == 1
                            and observed_total
                            and existing_total
                            and abs(observed_total - existing_total) <= max(observed_total, existing_total) * 0.05
                        )
                    ):
                        job.progress_components.clear()
                        component = job.progress_components.setdefault(key, {})
                    component["total"] = observed_total or component.get("total")
                    component["downloaded"] = d.get("downloaded_bytes") or component.get("total") or 0
                    component["speed"] = 0
                    if component.get("segments_total"):
                        component["segments_done"] = component["segments_total"]
                    downloaded, total, _speed, _eta = self._aggregate_progress(job)
                    all_components_reported = len(job.progress_components) >= job.expected_components
                    is_complete = (
                        total is not None
                        and downloaded >= total
                        and (job.expected_components == 1 or all_components_reported or is_combined_report)
                    )
                    percent = round(downloaded / total * 100, 1) if total else job.data["percent"]
                    if job.expected_components > 1 and not all_components_reported and not is_combined_report:
                        percent = min(percent, 99.9)
                    job.data.update(
                        status="postprocessing" if is_complete else "downloading",
                        downloaded=downloaded,
                        total=total,
                        percent=percent,
                        speed=None,
                        eta=None,
                    )
                    if d.get("filename"):
                        filename = Path(d["filename"]).name
                        job.data["filename"] = filename
                        job.data["finalFilepath"] = str(Path(job.data["directory"]) / filename)
                    self._emit_unlocked(job, force=True)

        return hook

    def _post_hook(self, job: DownloadJob) -> Any:
        """Update the job with the final filename after yt-dlp post-processing."""

        def hook(filepath: str) -> None:
            path = Path(filepath)
            job.data["filename"] = path.name
            job.data["finalFilepath"] = str(filepath)

        return hook

    def _run_file(self, job: DownloadJob, settings: dict[str, Any], directory: str, temp_dir: Path, request_url: str) -> None:
        """Download an intercepted non-media file as-is."""
        if job.custom_filename:
            filename = Path(job.custom_filename).name
            stem = Path(filename).stem
            ext = _ext_from_name("", filename)
        else:
            clean_url = unsmuggle_url(request_url, {})[0]
            name = Path(clean_url.split("?")[0].split("#")[0]).name or "download"
            stem = Path(name).stem
            ext = _ext_from_name("", name)
            filename = None

        outtmpl = filename if filename else f"{stem}.{ext}"

        opts: dict[str, Any] = {
            **_base_opts(settings, direct_file=True),
            "outtmpl": outtmpl,
            "paths": {"home": directory, "temp": str(temp_dir)},
            "progress_hooks": [self._progress_hook(job)],
            "post_hooks": [self._post_hook(job)],
            "noplaylist": True,
            "continuedl": True,
        }
        _apply_smuggled_http_headers(opts, request_url)

        with self._lock:
            job.data.update(status="downloading", directory=directory, tempDirectory=str(temp_dir))
            self._emit_unlocked(job, force=True)
        log.info("Beginning file download", job_id=job.id, url=request_url, ext=ext)

        clean_url = unsmuggle_url(request_url, {})[0]
        info: dict[str, Any] = {
            "id": job.id,
            "title": stem,
            "url": clean_url,
            "ext": ext,
            "webpage_url": job.url,
            "extractor_key": "direct",
        }

        try:
            with _allow_unsafe_extensions(), YoutubeDL(cast(Any, opts)) as ydl:
                ydl.process_info(cast(Any, info))
        except DownloadCancelled:
            status = "paused" if job.pause_requested else "cancelled"
            log.info("File download cancelled/paused", job_id=job.id, status=status)
            with self._lock:
                job.data.update(status=status, speed=None, eta=None)
                self._emit_unlocked(job, force=True)
        except Exception as exc:  # noqa: BLE001
            msg = _download_error_message(exc)
            log.exception("File download failed", job_id=job.id, error=msg)
            with self._lock:
                job.data.update(status="failed", error=msg, speed=None, eta=None)
                self._emit_unlocked(job, force=True)
        else:
            with self._lock:
                actual_downloaded = job.data["downloaded"]
                final_total = max(job.data.get("total") or 0, actual_downloaded)
                job.data.update(
                    status="completed",
                    percent=100.0,
                    downloaded=actual_downloaded,
                    total=final_total,
                    speed=None,
                    eta=None,
                )
                log.info("File download completed", job_id=job.id, downloaded=actual_downloaded, total=final_total)

        with self._lock:
            removed = job.data.get("removed")
        if removed:
            _trash_job_temp(job.id)
            return

        try:
            if temp_dir.exists() and not any(temp_dir.iterdir()):
                temp_dir.rmdir()
        except OSError:
            pass

        with self._lock:
            self._emit_unlocked(job, force=True)

    def _run(self, job: DownloadJob) -> None:
        if job.data.get("removed"):
            _trash_job_temp(job.id)
            return
        if job.cancel_event.is_set() or job.data.get("status") != "queued":
            return

        settings = self._settings_store.get()
        directory = job.directory or settings["downloadDir"]
        temp_dir = _job_temp_dir(job.id)
        temp_dir.mkdir(parents=True, exist_ok=True)

        with self._lock:
            request_url = self._probed.get(job.url, (job.url, None))[0]

        # Intercepted non-media files (.dmg, .zip, .pdf, .html, etc.) are downloaded
        # as-is using a manually built info dict so yt-dlp does not try to remux or
        # extract a video player from them.
        if job.media_type == "file":
            self._run_file(job, settings, directory, temp_dir, request_url)
            return

        if job.custom_filename:
            relative_stem = Path(job.custom_filename).with_suffix("")
            if job.parent_id:
                outtmpl = f"{relative_stem.as_posix()}.%(ext)s"
            else:
                outtmpl = f"{relative_stem.name}.%(ext)s"
        else:
            outtmpl = "%(title)s.%(ext)s"

        opts: dict[str, Any] = {
            **_base_opts(settings),
            "outtmpl": outtmpl,
            "paths": {"home": directory, "temp": str(temp_dir)},
            "format": job.format_selector,
            "progress_hooks": [self._progress_hook(job)],
            "post_hooks": [self._post_hook(job)],
            "noplaylist": True,  # Only download a single video, never a full playlist.
            "continuedl": True,  # resume .part files (pause/restart recovery)
        }
        _apply_smuggled_http_headers(opts, request_url)
        merge_format = settings.get("mergeOutputFormat", DEFAULT_SETTINGS["mergeOutputFormat"]).strip().lower()
        opts["merge_output_format"] = merge_format

        postprocessors: list[dict[str, Any]] = []

        # Remux single and multi streams to the selected container so the setting is honored everywhere.
        postprocessors.append({
            "key": "FFmpegVideoRemuxer",
            "preferedformat": merge_format,
        })

        if settings.get("writeThumbnail"):
            opts["writethumbnail"] = True
            postprocessors.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg"})

        if settings.get("writeSubs"):
            opts["writesubtitles"] = True
            opts["subtitleslangs"] = ["all"]
            postprocessors.append({
                "key": "FFmpegEmbedSubtitle",
                "already_have_subtitle": False,
            })

        if settings.get("addMetadata"):
            postprocessors.append({
                "key": "FFmpegMetadata",
                "add_metadata": True,
                "add_chapters": True,
            })

        if settings.get("writeThumbnail"):
            postprocessors.append({
                "key": "EmbedThumbnail",
                "already_have_thumbnail": False,
            })

        if postprocessors:
            opts["postprocessors"] = postprocessors

        if job.cancel_event.is_set() or job.data.get("status") != "queued":
            return
        self._resolve_job_total(job)
        with self._lock:
            job.data.update(status="downloading", directory=directory, tempDirectory=str(temp_dir))
            self._emit_unlocked(job, force=True)
        log.info("Beginning download", job_id=job.id, url=request_url, format_selector=job.format_selector)
        try:
            with YoutubeDL(cast(Any, opts)) as ydl:
                _add_hls_fixup_postprocessors(ydl)
                ydl.download([request_url])
        except DownloadCancelled:
            status = "paused" if job.pause_requested else "cancelled"
            log.info("Download cancelled/paused", job_id=job.id, status=status)
            with self._lock:
                job.data.update(status=status, speed=None, eta=None)
                self._emit_unlocked(job, force=True)
        except Exception as exc:  # noqa: BLE001
            msg = _download_error_message(exc)
            log.exception("Download failed", job_id=job.id, error=msg)
            with self._lock:
                job.data.update(status="failed", error=msg, speed=None, eta=None)
                self._emit_unlocked(job, force=True)
        else:
            with self._lock:
                job.data.update(
                    status="completed",
                    percent=100.0,
                    speed=None,
                    eta=None,
                )
                if job.data.get("segmentsTotal"):
                    job.data["segmentsDone"] = job.data["segmentsTotal"]
                log.info(
                    "Download completed",
                    job_id=job.id,
                    downloaded=job.data["downloaded"],
                    total=job.data.get("total"),
                )

        with self._lock:
            removed = job.data.get("removed")
            if removed:
                pass
        if removed:
            _trash_job_temp(job.id)
            return

        # yt-dlp's MoveFilesAfterDownloadPP should have moved files from temp
        # to the destination. Clean up the now-empty job temp subfolder.
        try:
            if temp_dir.exists() and not any(temp_dir.iterdir()):
                temp_dir.rmdir()
        except OSError:
            pass

        with self._lock:
            self._emit_unlocked(job, force=True)
