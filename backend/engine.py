"""yt-dlp engine: probing and download job management.

All yt_dlp calls are blocking, so everything runs through a
:class:`~concurrent.futures.ThreadPoolExecutor`; the asyncio server schedules
work with ``asyncio.get_running_loop().run_in_executor`` and progress events
are pushed back onto the event loop with ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import ctypes
import base64
import mmap
import orjson
import os
import platform
import re
import struct
import shlex
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import parse_qsl, quote, urljoin, urlsplit, urlunsplit

try:
    import resource
except ImportError:
    resource = None

import cachebox
import structlog
from send2trash import send2trash
import yt_dlp.utils
import yt_dlp.utils._utils
from yt_dlp import YoutubeDL
from yt_dlp.downloader.fragment import FragmentFD as _FragmentFD
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.networking import Request as _YdlRequest
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.postprocessor.ffmpeg import FFmpegFixupM3u8PP
from yt_dlp.utils import (
    KNOWN_EXTENSIONS,
    DownloadCancelled,
    DownloadError,
    UnsupportedError,
    determine_ext as _orig_determine_ext,
    prepend_extension,
    sanitize_filename,
    smuggle_url,
    unsmuggle_url,
)
from yt_dlp.utils._utils import _UnsafeExtensionError

try:
    from .constants import MANIFEST_MEDIA_EXTS, MEDIA_EXTS, NON_MEDIA_EXTENSIONS, VIDEO_CONTAINERS
except ImportError:  # flat (non-package) execution
    from constants import MANIFEST_MEDIA_EXTS, MEDIA_EXTS, NON_MEDIA_EXTENSIONS, VIDEO_CONTAINERS

try:
    _CHROME_IMPERSONATE: ImpersonateTarget | None = ImpersonateTarget.from_str("chrome")
except Exception:
    _CHROME_IMPERSONATE = None

if platform.system() == "Darwin":
    ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation",
        mode=os.RTLD_GLOBAL,
    )

try:
    from .db import Database, app_support_dir
    from .jobbase import JobManager
    from .settings import DEFAULT_SETTINGS, MAX_ARIA2_CONNECTIONS, MAX_CONCURRENT_DOWNLOADS, SettingsStore
    from .stream_size import resolve_format_size, resolve_selector_size
    from .torrent import TorrentManager, is_magnet_url, is_torrent_input
except ImportError:
    from db import Database, app_support_dir
    from jobbase import JobManager
    from settings import DEFAULT_SETTINGS, MAX_ARIA2_CONNECTIONS, MAX_CONCURRENT_DOWNLOADS, SettingsStore
    from stream_size import resolve_format_size, resolve_selector_size
    from torrent import TorrentManager, is_magnet_url, is_torrent_input

log = structlog.get_logger("da.engine")

# yt-dlp disallows non-media extensions by default for security. We allow them
# only when downloading arbitrary intercepted files (.dmg, .zip, .pdf, etc.).
_unsafe_ext_state = threading.local()
_unsafe_ext_original_sanitize = _UnsafeExtensionError.sanitize_extension.__func__


def _thread_local_sanitize_extension(
    cls: type[_UnsafeExtensionError],
    extension: str | None,
    /,
    *,
    prepend: bool = False,
    _allowed_exts: tuple[str, ...] = (),
) -> str | None:
    if getattr(_unsafe_ext_state, "enabled", False):
        if extension is None:
            return None
        if "/" in extension or "\\" in extension:
            raise cls(extension)
        return extension
    return _unsafe_ext_original_sanitize(
        cls,
        extension,
        prepend=prepend,
        _allowed_exts=_allowed_exts,
    )


setattr(
    _UnsafeExtensionError,
    "sanitize_extension",
    classmethod(_thread_local_sanitize_extension),
)


@contextlib.contextmanager
def _allow_unsafe_extensions():
    """Temporarily disable yt-dlp's unsafe-extension guard for file downloads."""
    previous = getattr(_unsafe_ext_state, "enabled", False)
    _unsafe_ext_state.enabled = True
    try:
        yield
    finally:
        _unsafe_ext_state.enabled = previous


# How often (seconds) a single job may emit progress while downloading.
PROGRESS_INTERVAL = 0.1
PROBE_WORKER_LIMIT = 8
PROBE_CACHE_MAX_ENTRIES = 128
# Downloads reuse a cached probe's info dict instead of re-extracting while it
# is younger than this; beyond it a fresh extraction is safer (signed URLs,
# changing manifests).
PROBE_REUSE_MAX_AGE = 600.0
# Keep native HLS/DASH fragment workers bounded across the download queue. Each
# worker can hold a response socket and a fragment .part file at the same time.
FRAGMENT_WORKER_BUDGET = 128
SIZE_CACHE_MAX_ENTRIES = 512


class _WorkSlotLimiter:
    def __init__(self, limit: int) -> None:
        self._condition = threading.Condition()
        self._limit = max(1, limit)
        self._active = 0

    def set_limit(self, limit: int) -> None:
        with self._condition:
            self._limit = max(1, min(limit, MAX_CONCURRENT_DOWNLOADS))
            self._condition.notify_all()

    def acquire(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._active >= self._limit:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            self._active += 1
            return True

    def release(self) -> None:
        with self._condition:
            if self._active <= 0:
                raise ValueError("Work slot released without a matching acquire")
            self._active -= 1
            self._condition.notify_all()


# yt-dlp's generic extractor derives the extension from the URL path.  Some
# CDNs redirect to a .php endpoint that carries the real filename in a query
# parameter (e.g. remote_control.php?file=... .<ext>&acctoken=...).  We patch
# determine_ext so that when the path extension is not a real media type, we
# fall back to scanning the full URL (query string included) for a known one.
_FRAG_PART_RE = re.compile(r'(?:[._-]Frag\d+)+|\.part$')

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
    if not url or ext in MEDIA_EXTS:
        return ext
    # Callers passing default_ext=None explicitly want content-based detection
    # for unknown extensions (yt-dlp falls back to urlhandle_detect_ext on a
    # falsy result). A literal path ext like 'php' from video.php?token=...
    # URLs would shadow that fallback and trip the unsafe-extension guard at
    # download time, so hand back empty instead.
    if default_ext is None:
        return _guess_media_ext(url) or ''
    return _guess_media_ext(url) or ext


# Only split off a final dot-suffix as an extension when the suffix is a
# single, clean, short token containing at least one letter. Dotted platform
# or version tags like "2.7.0-macos-arm64" should stay part of the stem.
_VALID_EXT_RE = re.compile(r"\.[A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*$")


def _stem_and_ext(name: str) -> tuple[str, str]:
    """Return (stem, ext) for a filename, keeping dotted version/platform tags in the stem.

    Examples:
        - "aria2-next-2.7.0-macos-arm64" -> ("aria2-next-2.7.0-macos-arm64", "")
        - "my.video.v2.mp4" -> ("my.video.v2", "mp4")
        - "archive.tar.gz" -> ("archive.tar", "gz")
    """
    if not name:
        return "", ""
    m = _VALID_EXT_RE.search(name)
    if not m:
        return name, ""
    return name[: m.start()], m.group(0)[1:].lower()


def _ext_from_name(url: str, filename: str = "") -> str:
    """Return the file extension from a filename or URL path (may be empty)."""
    name = filename or url or ""
    if not name:
        return ""
    clean = name.split("?")[0].split("#")[0]
    return _stem_and_ext(Path(clean).name)[1]


def _patch_yt_dlp_determine_ext() -> None:
    """Apply our URL-extension patch once at module load."""
    yt_dlp.utils.determine_ext = _patched_determine_ext
    yt_dlp.utils._utils.determine_ext = _patched_determine_ext


_patch_yt_dlp_determine_ext()


def _fast_urlencode(query: list[tuple[str, str]]) -> str:
    """Encode query parameters for the canonical probe URL."""
    return "&".join(f"{quote(k)}={quote(v)}" for k, v in query)


def _canonical_probe_url(url: str) -> str:
    """Normalize a URL by stripping tracking parameters, anchors, and standardizing short links."""
    try:
        parsed = urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            return url
        filtered_query = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if not k.lower().startswith("utm_")
            and k.lower() not in {"si", "fbclid", "gclid", "ref", "source", "feature"}
        ]
        clean_path = parsed.path.rstrip("/") or "/"
        host = parsed.netloc.lower()
        if host in {"youtu.be", "www.youtu.be"} and len(clean_path) > 1:
            video_id = clean_path.strip("/")
            filtered_query.insert(0, ("v", video_id))
            host = "www.youtube.com"
            clean_path = "/watch"
        return urlunsplit((
            parsed.scheme.lower(),
            host,
            clean_path,
            _fast_urlencode(filtered_query),
            "",
        ))
    except Exception:
        return url


# Use the bundled aria2-next downloader that pushes progress over an HTTP
# JSON-RPC connection instead of parsing console output.
try:
    from .aria2next import ARIA2_NEXT_BINARY as _ARIA2_NEXT_BINARY, is_disk_space_error
except ImportError:
    from aria2next import ARIA2_NEXT_BINARY as _ARIA2_NEXT_BINARY, is_disk_space_error


# Track fragments yt-dlp skips ("fragment not found"). A resumed download
# whose manifest URLs expired can silently complete with a large share of
# fragments missing, producing a truncated file that is still remuxed and
# moved as if it were complete. Counting skips lets the engine fail such
# downloads instead.
_MAX_SKIPPED_FRAGMENT_RATIO = 0.05
_SKIPPED_FRAGMENTS = threading.local()

_ORIG_REPORT_SKIP_FRAGMENT = _FragmentFD.report_skip_fragment


def _counting_report_skip_fragment(self, frag_index, err=None):
    skipped = getattr(_SKIPPED_FRAGMENTS, "list", None)
    if skipped is not None:
        skipped.append(frag_index)
    _ORIG_REPORT_SKIP_FRAGMENT(self, frag_index, err)


_FragmentFD.report_skip_fragment = _counting_report_skip_fragment


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


class _FinalTitlePP(PostProcessor):
    """Set the info dict title to the final output filename stem.

    Generic/HLS extractions often derive the title from the manifest URL
    ("master.m3u8" -> "master"); the embedded metadata Title should match the
    actual output file instead. Only used for jobs with a custom filename, so
    the outtmpl (which pins the stem) is unaffected by the override.
    """

    def __init__(self, ydl: Any, title: str) -> None:
        super().__init__(ydl)
        self._title = title

    def run(self, info: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
        info["title"] = self._title
        return [], info


class _InjectThumbnailPP(PostProcessor):
    """Make thumbnails downloadable and inject a custom one when missing.

    yt-dlp downloads thumbnails with only the entry's own ``http_headers``
    (never the stream's), so referer-protected CDNs 403 every entry and
    EmbedThumbnail ends up with nothing. Stamp the stream's effective headers
    (referer, smuggled User-Agent/Accept-Language, Origin) onto every entry —
    token-signed CDNs validate the UA the URL was minted with, so Referer
    alone still 403s — and inject the custom thumbnail URL (page meta tags)
    when the extractor returned none.
    """

    def __init__(self, ydl: Any, thumbnail_url: str, referer: str = "", stream_headers: dict[str, str] | None = None) -> None:
        super().__init__(ydl)
        self._thumbnail_url = thumbnail_url
        self._referer = referer
        self._headers = dict(stream_headers or ({"Referer": referer} if referer else {}))

    def run(self, info: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
        if not isinstance(info, dict):
            return [], info
        headers = self._headers
        existing = info.get("thumbnails") or []
        for entry in existing:
            if isinstance(entry, dict) and entry.get("url") and not entry.get("http_headers"):
                entry["http_headers"] = dict(headers)
        if existing:
            # Do not override thumbnails produced by a real extractor; the
            # custom URL is a fallback for sites yt-dlp cannot probe.
            return [], info
        if not self._thumbnail_url:
            return [], info
        ext = _guess_media_ext(self._thumbnail_url) or _patched_determine_ext(self._thumbnail_url, default_ext="jpg")
        info["thumbnail"] = self._thumbnail_url
        info["thumbnails"] = [{"id": "0", "url": self._thumbnail_url, "ext": ext, "http_headers": dict(headers)}]
        return [], info


class _ManifestReextractPP(PostProcessor):
    """Fix <video src> formats that actually serve stream manifests.

    Runs at pre_process, before format selection, so a fresh page extraction
    whose media-embed formats are KVS-style ``get_file/...mp4/`` manifest links
    does not fail with "Requested format is not available" and does not download
    the ~1 KB manifest text as the video. Cached probes were already corrected
    at probe time; this is a no-op for them and for real direct files.
    """

    def __init__(self, ydl: Any, cancel_event: threading.Event | None = None) -> None:
        super().__init__(ydl)
        self._cancel_event = cancel_event

    def run(self, info: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
        if isinstance(info, dict):
            info = _reextract_manifest_formats(self._downloader, info, self._cancel_event)
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


def _add_title_override(ydl: YoutubeDL, job: DownloadJob) -> None:
    """Embed the final filename stem as the metadata title for custom-named jobs."""
    if not job.custom_filename:
        return
    stem, _ = _stem_and_ext(Path(job.custom_filename).name)
    if not stem:
        return
    processors = cast(dict[str, list[PostProcessor]], getattr(ydl, "_pps", {}))
    post_process = processors.get("post_process")
    if post_process is None:
        return
    processor = _FinalTitlePP(ydl, Path(stem).name)
    processor.set_downloader(ydl)
    post_process.insert(0, processor)


def _temp_root() -> Path:
    """Root directory for incomplete download temp files."""
    return app_support_dir() / "temp"


def _job_temp_dir(job_id: str, parent_id: str | None = None) -> Path:
    """Per-job isolated temp subfolder. For playlist items, returns the parent playlist's temp folder."""
    target_id = parent_id or job_id
    return _temp_root() / target_id


def _trash_job_temp(job_id: str, parent_id: str | None = None) -> None:
    """Move a job's (or parent playlist's) isolated temp folder to the system trash, if it exists."""
    temp_dir = _job_temp_dir(job_id, parent_id)
    if not temp_dir.exists():
        return
    try:
        send2trash(str(temp_dir))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not trash temp dir", path=str(temp_dir), error=str(exc))


def _download_error_message(exc: BaseException) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    normalized = message.lower()
    if is_disk_space_error(message):
        return (
            "Insufficient disk space for the download. Free space on the "
            "destination volume and retry; the full download must fit on disk."
        )
    if any(marker in normalized for marker in ("cloudflare", "turnstile", "just a moment", "challenge-running")):
        return (
            "Site is protected by Cloudflare bot protection. Open the video page in your browser "
            "and use the DownloadAnything extension button to download the captured stream."
        )
    if "403" in normalized or "forbidden" in normalized:
        return (
            "Access was forbidden (HTTP 403). The media security token or URL has expired. "
            "Please refresh the video in your browser and try again."
        )
    if any(marker in normalized for marker in ("drm", "widevine", "fairplay", "playready", "unsupported encryption")):
        return (
            "This stream is protected by DRM (Digital Rights Management) and cannot be decrypted."
        )
    if any(marker in normalized for marker in ("geo-restricted", "not available in your country", "blocked in your region")):
        return (
            "This video is not available in your region. Configure a proxy or VPN in settings to download it."
        )
    return message


_THUMBNAIL_DATA_URI_MAX_BYTES = 300_000
_THUMBNAIL_MIME_BY_EXT = {"png": "image/png", "webp": "image/webp", "gif": "image/gif", "jpg": "image/jpeg", "jpeg": "image/jpeg"}

# ponytail: process-lifetime cache; thumbnails are immutable per (url, referer).
# Bound it so a long session with many playlist entries cannot grow unbounded.
_THUMBNAIL_CACHE: dict[tuple[str, str], str | None] = {}
_THUMBNAIL_CACHE_MAX = 128


def _fetch_thumbnail_data_uri(url: str, referer: str, ua: str = "") -> str | None:
    """Fetch a thumbnail with the job's referer and inline it as a data URI.

    The dashboard's <img> cannot send a custom Referer, so referer-gated CDNs
    403 the direct fetch. Fetch once here; data: URIs are also the only image
    source the Tauri CSP allows for arbitrary hosts. Token-signed CDNs also
    validate the User-Agent the URL was minted with, so the smuggled UA (when
    present) must ride along or the fetch 403s like the dashboard's would.
    """
    cache_key = (url, referer, ua)
    if cache_key in _THUMBNAIL_CACHE:
        return _THUMBNAIL_CACHE[cache_key]
    result: str | None = None
    headers: dict[str, str] = {"Referer": referer} if referer else {}
    if ua:
        headers["User-Agent"] = ua
    try:
        with YoutubeDL({"quiet": True, "skip_download": True, "http_headers": headers}) as ydl:
            # Cap the read at the size limit + 1 so a huge body cannot be
            # buffered into memory before the check.
            data = ydl.urlopen(url).read(_THUMBNAIL_DATA_URI_MAX_BYTES + 1)
        if data and len(data) <= _THUMBNAIL_DATA_URI_MAX_BYTES:
            ext = _ext_from_name(url)
            mime = _THUMBNAIL_MIME_BY_EXT.get(ext, "image/jpeg")
            result = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    except Exception:  # noqa: BLE001 - display-only; keep the original URL
        result = None
    if len(_THUMBNAIL_CACHE) >= _THUMBNAIL_CACHE_MAX:
        _THUMBNAIL_CACHE.clear()
    _THUMBNAIL_CACHE[cache_key] = result
    return result


def _should_embed_thumbnail(job: DownloadJob, settings: dict[str, Any] | None = None) -> bool:
    """Return True when the job should download/embed a thumbnail.

    Thumbnail embedding is only meaningful for video and audio downloads.
    Playlist parents, direct files, torrents, and unknown engines should not
    attempt to embed cover art (and should not run the thumbnail extractor on
    pages where it would pick up a site logo instead of real media art).
    """
    if job.is_playlist or job.media_type == "file" or job.engine not in ("ytdlp", "extractor"):
        return False
    if job.thumbnail:
        return True
    if settings and settings.get("writeThumbnail"):
        return True
    return False


def _base_opts(
    settings: dict[str, Any],
    *,
    direct_file: bool = False,
    for_probe: bool = False,
) -> dict[str, Any]:
    retries_count = 1 if for_probe else settings["retries"]
    timeout_sec = 10 if for_probe else max(10, int(settings.get("aria2NextTimeout") or 60))
    retry_sleeps = (
        {
            "http": lambda n: 0.2,
            "fragment": lambda n: 0.2,
            "extractor": lambda n: 0.2,
            "file_access": lambda n: 0.1,
        }
        if for_probe
        else {
            "http": lambda n: min(2.0 * n, 30.0),
            # Fragments fail permanently far more often than they fail
            # transiently (missing/corrupt segments answer 416 forever, and
            # yt-dlp only fast-skips 404/410). A tall backoff here turns each
            # dead fragment into a multi-minute stall, so keep the pacing tight.
            "fragment": lambda n: min(2.0 * n, 5.0),
            "extractor": lambda n: min(2.0 * n, 30.0),
            "file_access": lambda n: min(1.0 * n, 10.0),
        }
    )
    opts: dict[str, Any] = {
        "quiet": False,
        "no_warnings": False,
        "noprogress": True,
        "retries": retries_count,
        # Retry failed fragments, then skip any that stay unavailable (e.g.
        # expired ad segments on stitched HLS). A single dead fragment must not
        # fail an otherwise complete download; yt-dlp still fails when an
        # essential early fragment is missing. The retry count is capped
        # independently of the user's "retries" setting: with 20 retries and
        # backoff, one permanently dead fragment stalled downloads for ~10
        # minutes before the skip kicked in.
        "fragment_retries": min(retries_count, 10),
        "skip_unavailable_fragments": True,
        # Pace retries. Some CDNs answer requests with intermittent 403s
        # (rate limiting or flaky origin); yt-dlp's default of retrying
        # immediately burns through all retries inside the same rejection
        # window. Exponential backoff gives the origin time to accept again.
        "retry_sleep_functions": retry_sleeps,
        # FFmpeg otherwise emits MPEG-TS for playlists marked as live. The
        # resulting TS can carry unset timestamps that fail when remuxed to
        # MKV, even when the HLS download itself completed successfully.
        "hls_use_mpegts": False,
        # HLS/DASH run on yt-dlp's native downloader: manifest parsing,
        # decryption, and fragment retries all stay in-process. aria2-next is
        # only delegated plain HTTP(S) files below.
        "external_downloader": {"m3u8": "native", "dash": "native"},
        # Do not test every format URL before selection; that is slow and our
        # size resolver already HEADs the selected format when needed.
        "check_formats": False,
        # Default filename: title only, no video id.
        "outtmpl": "%(title)s.%(ext)s",
        # Larger download buffer reduces syscalls and context switches on fast pipes.
        "buffersize": 1024 * 1024,
        "http_chunk_size": 32 * 1024 * 1024,
        "socket_timeout": timeout_sec,
        # Preserve the remote Last-Modified timestamp on finished files
        # (yt-dlp's CLI default; without it aria2-next gets --remote-time=false
        # and direct downloads lose the server's mtime metadata).
        "updatetime": True,
        # Use yt-dlp's native FormatSorter for codec and container preference.
        # yt-dlp's format list is worst-to-best, so keep its default codec order
        # and let the picker select the best format while iterating in reverse.
        "format_sort": ["res", "fps", "vcodec", "acodec", "br", "ext"],
        "extractor_args": {"generic": {"impersonate": ["chrome"]}},
        # yt-dlp blocks some sites with a hard-coded "Piracy" extractor that
        # always raises. Force it off so the generic extractor can handle the
        # direct media URLs the browser extension passes in.
        "allowed_extractors": ["default", "-Piracy"],
    }
    if _CHROME_IMPERSONATE is not None:
        opts["impersonate"] = _CHROME_IMPERSONATE

    if settings.get("proxy"):
        opts["proxy"] = settings["proxy"]
    if settings.get("rateLimit"):
        opts["ratelimit"] = int(settings["rateLimit"])
    requested = settings.get("concurrentFragments") or 1
    if requested > 1:
        max_jobs = max(1, settings.get("maxConcurrentDownloads", DEFAULT_SETTINGS["maxConcurrentDownloads"]))
        try:
            soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0] if resource is not None else 256
        except Exception:  # noqa: BLE001
            soft = 256
        # Reserve FDs for the process itself, then divide the remaining budget across jobs.
        # Each concurrent fragment worker needs roughly 3 FDs (socket, .part file, bookkeeping).
        fd_per_job = max(1, (soft - 64) // max(1, max_jobs) // 3)
        worker_budget = max(FRAGMENT_WORKER_BUDGET, min(256, (soft - 64) // 3))
        worker_per_job = max(1, worker_budget // max(1, max_jobs))
        opts["concurrent_fragment_downloads"] = max(1, min(requested, fd_per_job, worker_per_job))
    if settings.get("cookiesFromBrowser"):
        opts["cookiesfrombrowser"] = (settings["cookiesFromBrowser"],)
    if settings.get("writeAutoSubs"):
        opts["writeautomaticsub"] = True
    sub_langs = (settings.get("subLangs") or "").strip()
    if sub_langs:
        opts["subtitleslangs"] = [s.strip() for s in sub_langs.split(",") if s.strip()]
    if _ARIA2_NEXT_BINARY:
        aria2_path = str(_ARIA2_NEXT_BINARY)
        # Keep manifest parsing and decryption in yt-dlp while aria2-next
        # downloads the unencrypted HLS/DASH fragments.
        opts["external_downloader"].update({
            "default": aria2_path,
            "http": aria2_path,
            "m3u8": aria2_path,
            "dash": aria2_path,
        })
        # Some extractors emit protocol `m3u8` (not `m3u8_native`). Without
        # this flag yt-dlp's downloader fallback for that protocol is
        # FFmpegFD; with it, the native HlsFD is chosen when aria2-next
        # refuses a manifest (e.g. encrypted), keeping fragment delegation
        # and the m3u8 fixup post-processors on the expected path.
        opts["hls_prefer_native"] = True
        aria2_args: list[str] = []
        # Performance baseline for aria2-next: larger in-memory disk cache and
        # paced retries for flaky sources. Do not impose a minimum speed here:
        # valid sources can be slower than an arbitrary threshold, and user
        # extras are appended later for explicit opt-in tuning.
        # `-j` is per aria2 process, not the app job queue. Each HTTP download
        # already owns its own process, so leave concurrent-download
        # count at Aria2cFD's default instead of mapping maxConcurrentDownloads.
        retries = max(1, int(settings.get("retries") or DEFAULT_SETTINGS["retries"]))
        aria2_args.extend([
            "--disk-cache=128M",
            "--socket-recv-buffer-size=1M",
            f"--max-tries={retries}",
            "--retry-wait=2",
            "--max-file-not-found=3",
            "--enable-dht=false",
            "--enable-peer-exchange=false",
            "--bt-enable-lpd=false",
            # Native aria2-next flags: stream-state save cadence, per-transfer
            # connection ceiling, and adaptive concurrency tuning.
            "--state-save-interval=10",
            "--optimize-concurrent-downloads=true",
        ])
        conn = settings.get("aria2NextConnections")
        if conn is None:
            conn = MAX_ARIA2_CONNECTIONS
        aria2_args.extend([
            "--stream-max-connections",
            str(min(int(conn), MAX_ARIA2_CONNECTIONS)),
        ])
        timeout = settings.get("aria2NextTimeout")
        if timeout:
            aria2_args.extend(["--connect-timeout", str(timeout), "--timeout", str(timeout)])
        max_dl_limit = settings.get("aria2NextMaxDownloadLimit")
        if max_dl_limit and max_dl_limit > 0:
            aria2_args.extend(["--max-download-limit", str(int(max_dl_limit))])
        piece_length = (settings.get("aria2NextMinSplitSize") or "").strip()
        if piece_length:
            aria2_args.extend(["--piece-length", piece_length])
        file_alloc = "none" if direct_file else (settings.get("aria2NextFileAllocation") or "").strip()
        if file_alloc:
            aria2_args.extend(["--file-allocation", file_alloc])
        extra = (settings.get("aria2NextExtraArgs") or "").strip()
        if extra:
            try:
                user_args = shlex.split(extra)
            except ValueError as exc:
                log.warning("Ignoring invalid aria2-next extra arguments", error=str(exc))
            else:
                blocked = [
                    arg
                    for arg in user_args
                    if (
                        arg == "--enable-rpc"
                        or arg.startswith("--enable-rpc=")
                        or arg.startswith("--rpc-")
                        or arg == "--stop-with-process"
                        or arg.startswith("--stop-with-process=")
                    )
                ]
                if blocked:
                    log.warning("Ignoring aria2-next arguments that conflict with internal RPC", arguments=blocked)
                else:
                    aria2_args.extend(user_args)
        if aria2_args:
            opts["external_downloader_args"] = {"aria2-next": aria2_args}
    return opts


def _apply_smuggled_http_headers(
    opts: dict[str, Any],
    url: str,
    *,
    fallback_referer: str | None = None,
) -> None:
    clean_url, smuggled_data = unsmuggle_url(url, {})
    smuggled_referer = smuggled_data.get("referer")
    smuggled_origin = smuggled_data.get("origin")
    referer = smuggled_referer or fallback_referer
    if referer:
        # Browsers never send URL fragments in Referer/Origin headers.
        parsed = urlsplit(str(referer))._replace(fragment="")
        referer_header = parsed.geturl()
        existing = cast(dict[str, str], opts.get("http_headers") or {})
        headers: dict[str, str] = {**existing, "Referer": referer_header}
        # Many HLS/CDN endpoints reject the request if Origin is missing.
        origin_val = smuggled_origin or (f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme in {"http", "https"} else None)
        if origin_val and "Origin" not in headers:
            headers["Origin"] = str(origin_val)
        opts["http_headers"] = headers
        # Apply chrome impersonation when referer was explicitly smuggled
        if smuggled_referer and _CHROME_IMPERSONATE is not None:
            opts["impersonate"] = _CHROME_IMPERSONATE

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

    # Re-minted URLs carry the exact header pair their token was signed with.
    # curl_cffi impersonation would override User-Agent (and yt-dlp's std
    # headers would add Accept-Language), breaking the signature, so both are
    # pinned explicitly and impersonation is disabled.
    smuggled_ua = smuggled_data.get("ua")
    smuggled_al = smuggled_data.get("al")
    if smuggled_ua or smuggled_al:
        headers = cast(dict[str, str], opts.get("http_headers") or {})
        if smuggled_ua:
            headers["User-Agent"] = str(smuggled_ua)
        if smuggled_al:
            headers["Accept-Language"] = str(smuggled_al)
        opts["http_headers"] = headers
        if smuggled_ua:
            opts.pop("impersonate", None)
            extractor_args = cast(dict[str, Any], opts.get("extractor_args") or {})
            generic_args = dict(cast(dict[str, Any], extractor_args.get("generic") or {}))
            generic_args.pop("impersonate", None)
            opts["extractor_args"] = {**extractor_args, "generic": generic_args}


_QUALITY_LABEL_MAX_LEN = 24


def _quality_label(text: Any) -> str:
    """Return text as a picker display label, never a URL.

    Source labels flow into the picker's Resolution column. Referer URLs and
    other opaque strings arrive mislabeled as sources (the re-mint path used
    to attach the referer); they must never land there.
    """
    stripped = str(text or "").strip()
    if not stripped or "://" in stripped:
        return ""
    return stripped[:_QUALITY_LABEL_MAX_LEN]


_THUMBNAIL_CLIP_RE = re.compile(
    r"(?:^|[/_.-])(?:vid|video|img|image)?(?:thumb|thumbnail|poster|preview)s?(?:[/_.-]|$)",
    re.IGNORECASE,
)


def _is_thumbnail_clip(fmt: dict[str, Any]) -> bool:
    """Detect thumbnail/preview video clips (e.g. 'vidthumb.mp4') that are not
    the real media and should not appear as downloadable formats."""
    url = str(fmt.get("url") or "")
    if not url:
        return False
    path = unsmuggle_url(url, {})[0].split("?")[0].split("#")[0]
    return bool(_THUMBNAIL_CLIP_RE.search(Path(path).name))


def build_format_rows(
    ydl: YoutubeDL,
    info: Any,
    cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Reduce yt-dlp's pre-sorted format list to picker rows (video + audio only).

    yt-dlp sorts formats from worst to best, so iterating in reverse keeps the
    best available format for each resolution/label.
    """
    if cancel_event and cancel_event.is_set():
        raise DownloadCancelled("Probe cancelled by user")
    candidate_items: list[tuple[dict[str, Any], dict[str, Any], bool, str]] = []
    seen_labels: set[str] = set()

    for fmt in reversed(cast(list[dict[str, Any]], info.get("formats") or [])):
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled("Probe cancelled by user")
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
        if has_video and _is_thumbnail_clip(fmt):
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

        candidate_items.append((row, fmt, video_only, selector))

    def _resolve_candidate_size(cand: tuple[dict[str, Any], dict[str, Any], bool, str]) -> dict[str, Any]:
        row, fmt, video_only, selector = cand
        if cancel_event and cancel_event.is_set():
            return row
        try:
            if video_only:
                resolved = resolve_selector_size(ydl, info, selector, allow_network=True, cancel_event=cancel_event)
            else:
                resolved = resolve_format_size(ydl, fmt, info, allow_network=True, cancel_event=cancel_event)
            if resolved:
                row["size"] = resolved[0]
                row["sizeIsEstimate"] = resolved[1]
        except Exception:  # noqa: BLE001
            pass
        return row

    if len(candidate_items) > 1:
        worker_count = min(8, len(candidate_items))
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="fmt-size") as executor:
            rows = list(executor.map(_resolve_candidate_size, candidate_items))
    elif candidate_items:
        rows = [_resolve_candidate_size(candidate_items[0])]
    else:
        rows = []

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
    info_dict = cast(dict[str, object], info)
    thumb = info_dict.get("thumbnail")
    if isinstance(thumb, str) and thumb.strip():
        return thumb.strip()
    thumbnails = info_dict.get("thumbnails")
    if isinstance(thumbnails, list) and thumbnails:
        for item in reversed(thumbnails):
            if not isinstance(item, dict):
                continue
            thumbnail = cast(dict[str, object], item).get("url")
            if isinstance(thumbnail, str) and thumbnail.strip():
                return thumbnail.strip()
    return ""


def _build_playlist_entries(info: Any) -> list[dict[str, Any]]:
    """Reduce flat playlist entries while preserving yt-dlp's canonical order."""
    raw_entries = cast(list[dict[str, Any]], info.get("entries") or [])
    if not raw_entries:
        return []

    def _process_entry(item: tuple[int, dict[str, Any]]) -> dict[str, Any] | None:
        position, entry = item
        if not entry:
            return None
        webpage = entry.get("webpage_url") or entry.get("url") or ""
        raw_playlist_index = entry.get("playlist_index")
        if isinstance(raw_playlist_index, (int, str)):
            try:
                playlist_index = int(raw_playlist_index)
            except ValueError:
                playlist_index = position + 1
        else:
            playlist_index = position + 1
        return {
            "id": str(entry.get("id") or position),
            "title": entry.get("title") or "",
            "uploader": entry.get("uploader") or entry.get("channel") or "",
            "duration": entry.get("duration"),
            "thumbnail": _extract_thumbnail(entry),
            "webpageUrl": webpage,
            "url": entry.get("url") or webpage,
            "index": playlist_index,
        }

    if len(raw_entries) >= 16:
        workers = min(os.cpu_count() or 4, 8)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="playlist-builder") as executor:
            entries = [e for e in executor.map(_process_entry, enumerate(raw_entries)) if e is not None]
    else:
        entries = [e for e in (_process_entry((pos, ent)) for pos, ent in enumerate(raw_entries)) if e is not None]

    entries.sort(key=lambda entry: entry["index"])
    return entries


_MANIFEST_SNIFF_BYTES = 2048


def _looks_like_manifest_body(body: bytes) -> bool:
    head = body[:_MANIFEST_SNIFF_BYTES].lstrip()
    if head.startswith(b'#EXTM3U'):
        return True
    return b'<MPD' in head[:200]


def _direct_media_formats(info: dict[str, Any]) -> list[dict[str, Any]]:
    formats = [f for f in info.get('formats') or [] if isinstance(f, dict)]
    return [
        f for f in formats
        if f.get('protocol') in ('http', 'https')
        and str(f.get('ext') or '').lower() in MANIFEST_MEDIA_EXTS
        and isinstance(f.get('url'), str) and f['url']
    ]


def _reextract_manifest_formats(
    ydl: YoutubeDL,
    info: Any,
    cancel_event: threading.Event | None = None,
) -> Any:
    """Replace <video src> formats that actually serve stream manifests.

    KVS-style sites hand out ``get_file/...mp4/`` links whose body is an HLS
    master playlist. yt-dlp's media-embed extractor trusts the tag's MIME type
    and labels them direct files, so a download saves the ~1 KB manifest text
    as the video and the remux fails. One ranged request on the first
    candidate detects the masquerade; the manifest URL is then extracted for
    real, yielding proper variant formats (heights, CDN segment playlists)
    for the picker and the downloader.
    """
    if not isinstance(info, dict) or info.get('_type') == 'playlist':
        return info
    candidates = _direct_media_formats(info)
    if not candidates:
        return info
    for fmt in candidates[:3]:
        if cancel_event is not None and cancel_event.is_set():
            return info
        try:
            request = _YdlRequest(fmt['url'], headers=dict(fmt.get('http_headers') or {}))
            request.headers['Range'] = 'bytes=0-2047'
            with ydl.urlopen(request) as response:
                body = response.read(_MANIFEST_SNIFF_BYTES)
            if not _looks_like_manifest_body(body):
                return info
            manifest_info = ydl.extract_info(fmt['url'], download=False)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(manifest_info, dict) or manifest_info.get('_type') == 'playlist':
            continue
        manifest_formats = manifest_info.get('formats') or []
        if not manifest_formats:
            continue
        replacement = copy.deepcopy(info)
        replacement['formats'] = manifest_formats
        return replacement
    return info


def _build_probe_result(
    ydl: YoutubeDL,
    info: Any,
    request_url: str,
    settings: dict[str, Any],
    media_type: str,
    *,
    is_playlist: bool = False,
    formats: list[dict[str, Any]] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Build a picker result dict from a yt-dlp info dict."""
    if cancel_event and cancel_event.is_set():
        raise DownloadCancelled("Probe cancelled by user")
    # Work on a copy: the caller caches the original info for download reuse,
    # so title/ext mutations here must not leak into it. Manifest masquerade
    # correction already ran on the caller's info dict before caching.
    info = copy.deepcopy(info) if isinstance(info, dict) else info
    merge_format = (
        settings.get("mergeOutputFormat", DEFAULT_SETTINGS["mergeOutputFormat"]).strip().lower()
    )
    result_formats: list[dict[str, Any]] = formats if formats is not None else []
    filename = ""

    if not is_playlist:
        if not result_formats and info.get("formats"):
            result_formats = build_format_rows(ydl, info, cancel_event)

        if not result_formats:
            # Direct media URLs yield no format list; synthesize one row.
            is_file = media_type == "file"
            if is_file:
                kind = "file"
                ext = _ext_from_name(request_url)
            elif media_type == "audio" or (info.get("vcodec") in (None, "none") and info.get("acodec") not in (None, "none")):
                kind = "audio"
                ext = "mp3"
            else:
                kind = "video"
                ext = merge_format
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
                "kind": kind,
                "url": info.get("url") or request_url,
            }
            resolved = resolve_selector_size(ydl, info, "best", allow_network=True)
            if resolved:
                row["size"] = resolved[0]
                row["sizeIsEstimate"] = resolved[1]
            result_formats = [row]

        # Filename preview uses the merge output format so it matches the final container,
        # unless this is a generic file that must keep its original extension.
        if media_type == "file":
            # Generic files keep the URL filename exactly; do not synthesize an extension.
            name = _url_filename(request_url)
            info["title"] = name
            filename = Path(ydl.prepare_filename(info, outtmpl="%(title)s")).name
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


_SEGMENT_NAME_RE = re.compile(
    r"(?:seg(?:ment)?|chunk|frag|init)(?:[-_.]?(?:\d+|v\d+|a\d+))(?:[-_.].*)?",
    re.IGNORECASE,
)
_B64_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_-]+")


def _url_filename(url: str) -> str:
    """Best-effort filename from a (possibly smuggled) URL, query/fragment stripped."""
    clean_url = unsmuggle_url(url, {})[0]
    return Path(clean_url.split("?")[0].split("#")[0]).name or "download"


def _is_segment_candidate(url: str) -> bool:
    parsed = urlsplit(url)
    filename = parsed.path.lower().rsplit("/", 1)[-1]
    if _SEGMENT_NAME_RE.fullmatch(filename):
        return True
    # Tokenized gateways hide the real target in base64 query params
    # (video.php?oid=<b64("1/show_hls/seg00000.ts")>) — decode and re-check
    # so segments captured through them never reach the probe.
    for _, value in parse_qsl(parsed.query, keep_blank_values=True):
        if len(value) < 8 or not _B64_TOKEN_RE.fullmatch(value):
            continue
        try:
            decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True).decode("utf-8")
        except Exception:  # noqa: BLE001 - not base64, not a hidden path
            continue
        if not decoded.isprintable():
            continue
        name = decoded.lower().rsplit("/", 1)[-1]
        if _SEGMENT_NAME_RE.fullmatch(name) or name.endswith((".ts", ".m4s")):
            return True
    return False


def _is_master_candidate(url: str) -> bool:
    parsed = urlsplit(url)
    path = parsed.path.lower()
    filename = path.rsplit("/", 1)[-1]
    return bool(
        path.endswith(".mpd")
        or re.fullmatch(r"(?:master|main|manifest|index|playlist)\.m3u8", filename)
        or re.search(r"(?:^|[/_.-])master(?:[/_.-]|$)", path)
        # Multi-rendition manifests encode every variant in the URL itself
        # probing one lists all qualities for the picker.
        or re.search(r"(?:^|[?&/])multi=", url.lower())
        or "/_tpl_/" in path
    )


# Header pair used to re-mint stream URLs server-side. Some file-host CDNs
# sign manifest tokens against the exact User-Agent and
# Accept-Language of the embed-page load, so URLs captured by the browser
# extension 403 when replayed with any other header pair. Minting here with
# our own fixed pair keeps every later request (manifest, variants, segments)
# consistent.
_REMINT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
_REMINT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"

_PACKER_RE = re.compile(r"return p\}\('(.*)',(\d+),(\d+),'(.*?)'\.split\('\|'\)")
_WORD_RE = re.compile(r"\b\w+\b")
_M3U8_RE = re.compile(r"https?://[^\s'\"<>]+\.m3u8[^\s'\"<>]*")
_EMBED_RE = re.compile(
    r'<iframe[^>]+src=["\'](https?://[^"\']+)["\']'
    r'|data-(?:embed|src|video)=["\'](https?://[^"\']+)["\']',
    re.IGNORECASE,
)


def _extract_m3u8_from_html(html: str) -> str | None:
    """Pull an HLS manifest URL out of page HTML, unpacking packer-obfuscated players."""
    text = html
    packed = _PACKER_RE.search(html)
    if packed:
        payload, base, _count, words = (
            packed.group(1),
            int(packed.group(2)),
            int(packed.group(3)),
            packed.group(4).split("|"),
        )
        table = words

        def _word(match: re.Match[str]) -> str:
            try:
                index = int(match.group(0), base)
            except ValueError:
                return match.group(0)
            # Mirror the packer: empty word slots leave the token untouched.
            return table[index] if index < len(table) and table[index] else match.group(0)

        text = _WORD_RE.sub(_word, payload)
    found = _M3U8_RE.search(text)
    return found.group(0) if found else None


def _remint_hls_urls(page_url: str, known_embeds: list[str] | None = None) -> list[tuple[str, str]]:
    """Fetch a page and its embeds to extract fresh manifest URLs with their referrers.

    Embed URLs the extension actually saw (``known_embeds``) are fetched in
    parallel with the page itself; if they already yield manifests, the slower
    page-embed discovery is skipped entirely.
    """
    headers = {"User-Agent": _REMINT_UA, "Accept-Language": _REMINT_ACCEPT_LANGUAGE}
    results: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    results_lock = threading.Lock()

    def _add_manifest(manifest: str | None, ref: str) -> None:
        if not manifest:
            return
        with results_lock:
            if manifest not in seen_urls:
                seen_urls.add(manifest)
                results.append((manifest, ref))

    def _fetch_html(target_url: str) -> str | None:
        try:
            with YoutubeDL({"quiet": True, "skip_download": True, "http_headers": headers}) as ydl:
                return ydl.urlopen(target_url).read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - any fetch failure just means no re-mint
            return None

    def _harvest_embed(embed_url: str) -> None:
        embed_html = _fetch_html(embed_url)
        if embed_html:
            _add_manifest(_extract_m3u8_from_html(embed_html), embed_url)

    known = [embed for embed in (known_embeds or []) if embed != page_url]
    page_html: str | None = None
    if known:
        with ThreadPoolExecutor(max_workers=min(6, len(known) + 1), thread_name_prefix="remint") as pool:
            page_future = pool.submit(_fetch_html, page_url)
            embed_futures = [pool.submit(_harvest_embed, embed) for embed in known]
            page_html = page_future.result()
            for future in embed_futures:
                future.result()
        # The player's own embeds produced fresh manifests; page-wide embed
        # discovery would only add latency for URLs the player is not using.
        if results:
            return results

    if page_html is None:
        page_html = _fetch_html(page_url)
    if not page_html:
        return results

    _add_manifest(_extract_m3u8_from_html(page_html), page_url)
    embeds = [m.group(1) or m.group(2) for m in _EMBED_RE.finditer(page_html)]
    fresh_embeds: list[str] = []
    seen_embeds: set[str] = set(known)
    for iframe_url in embeds[:8]:
        if iframe_url in seen_embeds:
            continue
        seen_embeds.add(iframe_url)
        fresh_embeds.append(iframe_url)
    if fresh_embeds:
        with ThreadPoolExecutor(max_workers=min(6, len(fresh_embeds)), thread_name_prefix="remint") as pool:
            list(pool.map(_harvest_embed, fresh_embeds))
    return results


def probe(
    url: str,
    fallback_urls: str | list[str] | None,
    settings: dict[str, Any],
    media_type: str = "",
    fallback_sources: list[dict[str, str]] | None = None,
    cancel_event: threading.Event | None = None,
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

    def _match_filter(info: dict[str, Any], *, incomplete: bool = False) -> str | None:
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled("Probe cancelled by user")
        return None

    url = url.strip().strip("'\"")

    def _normalize_stream_url(cand_url: str, base_url: str = "") -> str:
        cand_url = str(cand_url or "").strip().strip("'\"")
        if not cand_url:
            return ""
        parsed = urlsplit(cand_url)
        if not parsed.scheme:
            base = base_url or url
            if base:
                cand_url = urljoin(base, cand_url)
            elif cand_url.startswith("//"):
                cand_url = "https:" + cand_url
        return cand_url

    if isinstance(fallback_urls, str):
        fallback_urls = [fallback_urls]
    if isinstance(fallback_urls, list):
        norm_fallback_urls: list[str] = []
        for candidate in fallback_urls:
            if not isinstance(candidate, str):
                continue
            norm_cand = _normalize_stream_url(candidate, url)
            if not norm_cand or _is_segment_candidate(norm_cand):
                continue
            if not norm_cand.startswith(("http://", "https://")):
                continue
            norm_fallback_urls.append(norm_cand)
        fallback_urls = norm_fallback_urls

    if fallback_sources:
        norm_sources: list[dict[str, str]] = []
        for source in fallback_sources:
            raw_url = source.get("url")
            if not isinstance(raw_url, str):
                continue
            ref = source.get("referer") or source.get("origin") or url
            norm_cand = _normalize_stream_url(raw_url, ref)
            if not norm_cand or _is_segment_candidate(norm_cand):
                continue
            if not norm_cand.startswith(("http://", "https://")):
                continue
            entry = dict(source)
            entry["url"] = norm_cand
            norm_sources.append(entry)
        fallback_sources = norm_sources

    master_fallback = next(
        (
            source["url"]
            for source in fallback_sources or []
            if _is_master_candidate(source["url"])
        ),
        next(
            (candidate for candidate in fallback_urls or [] if _is_master_candidate(candidate)),
            None,
        ),
    )
    if not master_fallback and settings.get("multiResolutionExtractor") is False:
        if isinstance(fallback_urls, list):
            fallback_urls = fallback_urls[:1]
        if fallback_sources:
            fallback_sources = fallback_sources[:1]
    # Candidate URLs re-minted server-side after a 403, mapped to the header
    # pair their new token was signed with (smuggled alongside the URL).
    reminted: dict[str, dict[str, str]] = {}
    source_labels = {
        source.get("url"): source.get("label", "")
        for source in fallback_sources or []
        if source.get("url")
    }
    source_referers = {
        source.get("url"): source.get("referer", "")
        for source in fallback_sources or []
        if source.get("url") and source.get("referer")
    }
    source_origins = {
        source.get("url"): source.get("origin", "")
        for source in fallback_sources or []
        if source.get("url") and source.get("origin")
    }
    # When the extension captured a real manifest, probe it first: the page
    # URL is usually a generic player page yt-dlp rejects ("Unsupported URL"),
    # so trying it first just burns seconds before the fallback that works.
    primary_attempts: list[tuple[str, str, str]] = []
    if master_fallback:
        primary_attempts.append(("extractor", master_fallback, source_labels.get(master_fallback, "")))
    primary_attempts.append(("ytdlp", url, ""))

    seen_candidates: set[str] = {url}
    if master_fallback:
        seen_candidates.add(master_fallback)
    candidates: list[str] = []
    for source in fallback_sources or []:
        cand_url = source.get("url")
        if cand_url and cand_url not in seen_candidates:
            seen_candidates.add(cand_url)
            if not _is_thumbnail_clip({"url": cand_url}):
                candidates.append(cand_url)
    for cand_url in fallback_urls or []:
        if cand_url and cand_url not in seen_candidates:
            seen_candidates.add(cand_url)
            if not _is_thumbnail_clip({"url": cand_url}):
                candidates.append(cand_url)
    remaining_candidates = [
        ("extractor", candidate, source_labels.get(candidate, ""))
        for candidate in candidates
    ]

    last_error: str | None = None
    remint_lock = threading.Lock()
    combined_fallback: tuple[dict[str, Any], Any | None, str] | None = None

    # Re-mint prefetch: tokenized manifest URLs often 403 by probe time, and
    # the page re-fetch that recovers them takes seconds. Start it alongside
    # the first extractor attempt so the fresh URLs are ready when (if) the
    # failure lands, instead of adding the whole re-fetch after it.
    remint_prefetch_result: list[tuple[str, str]] | None = None
    prefetch_thread: threading.Thread | None = None
    if master_fallback or remaining_candidates:
        known_embeds = list(dict.fromkeys(
            ref
            for ref in source_referers.values()
            if ref.startswith(("http://", "https://")) and ref != url
        ))

        def _prefetch_remint() -> None:
            nonlocal remint_prefetch_result
            try:
                remint_prefetch_result = _remint_hls_urls(url, known_embeds)
            except Exception:  # noqa: BLE001 - prefetch is best-effort
                remint_prefetch_result = None

        prefetch_thread = threading.Thread(target=_prefetch_remint, name="remint-prefetch", daemon=True)
        prefetch_thread.start()

    def _consume_remint_prefetch() -> list[tuple[str, str]]:
        if prefetch_thread is None:
            return _remint_hls_urls(url)
        while prefetch_thread.is_alive():
            prefetch_thread.join(timeout=0.2)
            if cancel_event and cancel_event.is_set():
                raise DownloadCancelled("Probe cancelled by user")
        return remint_prefetch_result or []

    def _probe_single_attempt(
        engine: str,
        candidate: str,
        source_label: str,
    ) -> tuple[dict[str, Any], Any, str] | None:
        nonlocal last_error
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled("Probe cancelled by user")
        cand_referer = source_referers.get(candidate) or url
        cand_origin = source_origins.get(candidate)
        if not candidate.startswith(("http://", "https://")):
            candidate = urljoin(cand_referer, candidate)
        if not candidate.startswith(("http://", "https://")):
            log.warning("Skipping invalid candidate URL", candidate=candidate)
            return None
        if engine == "extractor":
            log.info("Falling back to extractor", candidate=candidate, referer=cand_referer)
        smuggle_payload: dict[str, Any] = {"referer": cand_referer, **reminted.get(candidate, {})}
        if cand_origin:
            smuggle_payload["origin"] = cand_origin
        request_url = (
            smuggle_url(candidate, smuggle_payload)
            if engine == "extractor" and cand_referer
            else candidate
        )

        flat_opts = {
            **_base_opts(settings, for_probe=True),
            "skip_download": True,
            "extract_flat": "in_playlist",
            "match_filter": _match_filter,
        }
        _apply_smuggled_http_headers(flat_opts, request_url)
        try:
            if cancel_event and cancel_event.is_set():
                raise DownloadCancelled("Probe cancelled by user")
            with YoutubeDL(cast(Any, flat_opts)) as ydl:
                info: Any = ydl.extract_info(request_url, download=False)
                if cancel_event and cancel_event.is_set():
                    raise DownloadCancelled("Probe cancelled by user")
                if not info:
                    last_error = "yt-dlp returned no information for this URL"
                    return None

                # Fix <video src> formats that actually serve stream manifests
                # before the info dict is cached for download reuse; otherwise
                # the picker offers and the downloader selects format ids that
                # only exist in the masquerading format list.
                if isinstance(info, dict) and info.get("_type") != "playlist":
                    info = _reextract_manifest_formats(ydl, info, cancel_event)

                is_playlist = info.get("_type") == "playlist"
                entries: list[dict[str, Any]] | None = None
                if is_playlist:
                    entries = _build_playlist_entries(info)

                video_info: Any | None = None
                if is_playlist:
                    if cancel_event and cancel_event.is_set():
                        raise DownloadCancelled("Probe cancelled by user")
                    video_opts = {
                        **_base_opts(settings, for_probe=True),
                        "skip_download": True,
                        "extract_flat": False,
                        "noplaylist": True,
                        "match_filter": _match_filter,
                    }
                    _apply_smuggled_http_headers(video_opts, request_url)
                    try:
                        with YoutubeDL(cast(Any, video_opts)) as ydl_video:
                            video_info = ydl_video.extract_info(request_url, download=False)
                            if video_info and video_info.get("_type") == "playlist":
                                video_info = None
                            if video_info:
                                result = _build_probe_result(
                                    ydl_video,
                                    video_info,
                                    request_url,
                                    settings,
                                    media_type,
                                    cancel_event=cancel_event,
                                )
                            else:
                                result = _build_probe_result(
                                    ydl,
                                    info,
                                    request_url,
                                    settings,
                                    media_type,
                                    is_playlist=True,
                                    formats=[],
                                    cancel_event=cancel_event,
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
                            cancel_event=cancel_event,
                        )
                else:
                    result = _build_probe_result(
                        ydl,
                        info,
                        request_url,
                        settings,
                        media_type,
                        cancel_event=cancel_event,
                    )

                if engine == "extractor" and not is_playlist:
                    result_formats = result.get("formats") or []
                    if len(result_formats) == 1:
                        if candidate in reminted:
                            result_formats[0]["url"] = request_url
                        else:
                            source_url = unsmuggle_url(request_url, {})[0]
                            result_formats[0]["url"] = source_url
                        label = _quality_label(source_label)
                        if label:
                            result_formats[0]["id"] = label
                            result_formats[0]["label"] = label
                            result_formats[0]["resolution"] = label
                            height_match = re.search(r"(\d{3,5})p", label)
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

                return result, info, request_url
        except UnsupportedError:
            last_error = f"URL is not supported by yt-dlp: {candidate}"
            return None
        except DownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc).strip() or exc.__class__.__name__
            log.warning("Probe attempt failed", engine=engine, candidate=candidate, error=last_error)
            is_cdn_error = any(code in last_error for code in ("403", "522", "520", "521", "523", "524", "404", "410", "Connection"))
            if engine == "extractor" and is_cdn_error and url:
                minted_candidates: list[tuple[str, str]] = []
                with remint_lock:
                    if not reminted:
                        fresh_streams = _consume_remint_prefetch()
                        for m_url, m_ref in fresh_streams:
                            if m_url and all(m_url != attempt_url for _, attempt_url, _ in primary_attempts + remaining_candidates):
                                reminted[m_url] = {"ua": _REMINT_UA, "al": _REMINT_ACCEPT_LANGUAGE, "referer": m_ref}
                        if reminted:
                            log.info("Re-minted stream URLs from page", page=url, count=len(reminted))
                    minted_candidates = [
                        # The referer is a page URL, not a display label; keep
                        # the original source's label (or none) instead.
                        (m_url, source_label)
                        for m_url in reminted
                        if m_url != candidate
                    ]
                for m_url, m_lbl in minted_candidates:
                    attempt = _probe_single_attempt("extractor", m_url, m_lbl)
                    if attempt is not None:
                        return attempt
            return None

    # Step 1: Probe primary targets (master manifest or main page URL)
    for engine, candidate, source_label in primary_attempts:
        if cancel_event and cancel_event.is_set():
            raise DownloadCancelled("Probe cancelled by user")
        outcome = _probe_single_attempt(engine, candidate, source_label)
        if outcome is not None:
            res, inf, req_u = outcome
            is_playlist = bool(res.get("isPlaylist"))
            formats_count = len(res.get("formats") or [])
            log.info(
                "Probe succeeded",
                url=url,
                engine=engine,
                title=res.get("title"),
                is_playlist=is_playlist,
                formats=formats_count,
            )
            # If multiResolutionExtractor is disabled, or it's a playlist, or it has multiple formats,
            # or there are no further candidates to probe: return immediately.
            if (
                settings.get("multiResolutionExtractor") is False
                or is_playlist
                or formats_count > 1
                or not remaining_candidates
            ):
                return {"ok": True, "engine": engine, "url": req_u, "result": res}, inf
            combined_fallback = (res, inf, req_u)
            break

    # Step 2: Probe remaining candidates. When multiResolutionExtractor is active and multiple
    # candidate streams exist, probe them concurrently in parallel to avoid sequential network delays.
    if remaining_candidates:
        if settings.get("multiResolutionExtractor") is not False and len(remaining_candidates) > 1:
            worker_count = min(6, len(remaining_candidates))
            executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="multi-res-probe")
            try:
                future_to_cand = {
                    executor.submit(_probe_single_attempt, eng, cand, lbl): (eng, cand, lbl)
                    for eng, cand, lbl in remaining_candidates
                }
                for future in as_completed(future_to_cand):
                    if cancel_event and cancel_event.is_set():
                        executor.shutdown(wait=False, cancel_futures=True)
                        raise DownloadCancelled("Probe cancelled by user")
                    try:
                        outcome = future.result()
                    except DownloadCancelled:
                        executor.shutdown(wait=False, cancel_futures=True)
                        raise
                    except Exception as exc:  # noqa: BLE001
                        last_error = str(exc)
                        continue
                    if outcome is None:
                        continue
                    res, inf, req_u = outcome
                    if combined_fallback is None:
                        combined_fallback = (res, inf, req_u)
                    else:
                        combined_result, combined_info, combined_url = combined_fallback
                        combined_result["formats"] = _merge_format_rows(
                            combined_result.get("formats") or [],
                            res.get("formats") or [],
                        )
                        combined_fallback = (combined_result, combined_info, combined_url)
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
        else:
            for eng, cand, lbl in remaining_candidates:
                if cancel_event and cancel_event.is_set():
                    raise DownloadCancelled("Probe cancelled by user")
                outcome = _probe_single_attempt(eng, cand, lbl)
                if outcome is not None:
                    res, inf, req_u = outcome
                    if combined_fallback is None:
                        combined_fallback = (res, inf, req_u)
                    else:
                        combined_result, combined_info, combined_url = combined_fallback
                        combined_result["formats"] = _merge_format_rows(
                            combined_result.get("formats") or [],
                            res.get("formats") or [],
                        )
                        combined_fallback = (combined_result, combined_info, combined_url)
                    if len(res.get("formats") or []) > 1:
                        break

    if combined_fallback is not None:
        combined_result, combined_info, combined_url = combined_fallback
        log.info(
            "Probe succeeded",
            url=url,
            engine=combined_result.get("extractor") or "extractor",
            title=combined_result.get("title"),
            formats=len(combined_result.get("formats") or []),
        )
        return {
            "ok": True,
            "engine": combined_result.get("extractor") or "extractor",
            "url": combined_url,
            "result": combined_result,
        }, combined_info

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
        referer: str = "",
        parent_id: str | None = None,
        is_playlist: bool = False,
        child_ids: list[str] | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.url = url
        self.request_url = url
        self.directory = directory
        self.format_selector = format_selector
        self.custom_filename = filename
        self.engine = engine
        self.media_type = media_type
        self.referer = referer
        self.parent_id = parent_id
        self.is_playlist = is_playlist
        self.child_ids = list(child_ids or [])
        # Merged selectors ("video+audio") download as separate components;
        # track per-component progress until every component reports a total.
        primary_selector = (format_selector or "").split("/")[0].strip()
        self.expected_components = 1 + primary_selector.count("+") if "+" in primary_selector else 1
        self.progress_components: dict[str, dict[str, Any]] = {}
        self.initial_total: int | None = None
        self.event_revision = 0
        self.thumbnail = thumbnail
        self.thumbnail_scanned = False
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
            "referer": referer,
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

        clean_url, smuggled = unsmuggle_url(row["url"], {})
        referer = row.get("referer") or smuggled.get("referer") or ""
        job = cls(
            row["url"] if smuggled.get("ua") else clean_url,
            row["directory"],
            format_selector=row["format_selector"],
            filename=row["custom_filename"],
            title=row["title"],
            thumbnail=row["thumbnail"],
            engine=row["engine"],
            media_type=row.get("media_type") or "",
            referer=referer,
            parent_id=row.get("parent_id") or None,
            is_playlist=bool(row.get("is_playlist")),
            child_ids=_parse_child_ids(row.get("child_ids")),
        )
        job.id = row["id"]
        job.request_url = row["url"]
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
        job.initial_total = row.get("total")
        return job

    def snapshot(self) -> dict[str, Any]:
        snapshot = dict(self.data)
        snapshot["revision"] = self.event_revision
        return snapshot


class DownloadManager(JobManager):
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
        initial_limit = max(1, min(int(settings_store.get()["maxConcurrentDownloads"]), MAX_CONCURRENT_DOWNLOADS))
        self._executor = ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_DOWNLOADS,
            thread_name_prefix="ytdlp",
        )
        self._probe_executor = ThreadPoolExecutor(
            max_workers=PROBE_WORKER_LIMIT,
            thread_name_prefix="probe",
        )
        self._work_slots = _WorkSlotLimiter(initial_limit)
        self._init_job_state()
        self._size_cache: cachebox.LRUCache = cachebox.LRUCache(maxsize=SIZE_CACHE_MAX_ENTRIES)
        self._probed: cachebox.TTLCache = cachebox.TTLCache(
            maxsize=PROBE_CACHE_MAX_ENTRIES,
            global_ttl=PROBE_REUSE_MAX_AGE,
        )
        self._torrent_manager = TorrentManager(
            settings_store,
            db,
            loop,
            self._work_slots,
            MAX_CONCURRENT_DOWNLOADS,
        )
        settings_store.subscribe(self._on_settings_changed)
        _temp_root().mkdir(parents=True, exist_ok=True)

    def _on_settings_changed(self, settings: dict[str, Any]) -> None:
        self._work_slots.set_limit(int(settings["maxConcurrentDownloads"]))

    async def restore(self) -> None:
        """Reload persisted jobs after a restart.

        Jobs that were active or queued become ``paused`` so the user can
        resume them explicitly (yt-dlp continues ``.part`` files).
        """
        rows = await self._db.list_jobs()

        def _load_ytdlp_jobs() -> None:
            ytdlp_rows = [row for row in rows if row.get("engine") != "torrent"]
            for row in reversed(ytdlp_rows):  # oldest first, preserves submission order
                if row["status"] in {"queued", "downloading", "postprocessing"}:
                    row["status"] = "paused"
                job = DownloadJob.from_row(row)
                with self._lock:
                    self._jobs[job.id] = job
                self._persist(job)  # write back the corrected status

        # Row parsing and torrent restore are sync disk/JSON work; keep them
        # off the event loop.
        await asyncio.gather(
            self._loop.run_in_executor(None, self._torrent_manager.restore, rows),
            self._loop.run_in_executor(None, _load_ytdlp_jobs),
        )
        if rows:
            log.info("Restored jobs from database", count=len(rows))

    # ------------------------------------------------------------------ events
    def add_listener(self, callback: Any) -> None:
        """Register an async callable(job_snapshot) invoked on the event loop."""
        super().add_listener(callback)
        self._torrent_manager.add_listener(callback)

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
            total = sum(totals) if len(totals) == len(children) else None
            speeds = [c.data["speed"] for c in children if c.data.get("speed") is not None]
            speed = sum(speeds) if speeds else None
            eta = None
            if total is not None and speed:
                eta = max(0, (total - downloaded) + speed - 1) // speed
            percent = (
                round(downloaded / total * 100, 1)
                if total
                else round(sum(float(c.data.get("percent") or 0) for c in children) / len(children), 1)
            )
            percent = max(0.0, min(100.0, percent))

            active_statuses = {"queued", "downloading", "postprocessing"}
            if all(s == "completed" for s in statuses):
                status = "completed"
                error = None
            elif any(s in active_statuses for s in statuses):
                status = "downloading"
                error = None
            elif any(s == "paused" for s in statuses):
                status = "paused"
                error = None
            else:
                failed = sum(1 for s in statuses if s == "failed")
                cancelled = sum(1 for s in statuses if s == "cancelled")
                parts: list[str] = []
                if failed:
                    parts.append(f"{failed} failed")
                if cancelled:
                    parts.append(f"{cancelled} cancelled")
                # An intentional cancel of every child is not a failure.
                status = "cancelled" if failed == 0 else "failed"
                error = "Playlist finished with " + ", ".join(parts) if parts else None

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
        self._emit_core(job, force=force)
        self._update_parent(job, force=force)

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

        def _shutdown_executor(wait: bool) -> None:
            self._executor.shutdown(wait=wait, cancel_futures=True)

        def _shutdown_probe_executor() -> None:
            self._probe_executor.shutdown(wait=True, cancel_futures=True)

        # Executor teardowns and the torrent session shutdown are independent;
        # run them concurrently and bound each so a stuck worker cannot hang
        # app shutdown forever.
        results = await asyncio.gather(
            asyncio.wait_for(
                self._loop.run_in_executor(None, _shutdown_executor, True),
                timeout=30.0,
            ),
            asyncio.wait_for(
                self._loop.run_in_executor(None, _shutdown_probe_executor),
                timeout=30.0,
            ),
            self._torrent_manager.shutdown(),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, asyncio.TimeoutError):
                log.warning("Executor shutdown timed out; continuing")
            elif isinstance(result, BaseException):
                log.warning("Subsystem shutdown failed", error=str(result))
        pending_listeners = list(self._listener_tasks)
        pending_db = list(self._db_tasks.values())
        pending = [*pending_listeners, *pending_db]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        log.info("Manager shutdown complete")

    # ------------------------------------------------------------------- queue
    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [job.snapshot() for job in self._jobs.values()]
        all_jobs = jobs + self._torrent_manager.list_jobs()
        all_jobs.sort(key=lambda j: float(j.get("createdAt") or 0), reverse=True)
        return all_jobs

    def probe(
        self,
        url: str,
        fallback_urls: str | list[str] | None,
        media_type: str = "",
        fallback_sources: list[dict[str, str]] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Run a probe and cache resolved sizes for the returned format rows."""
        url = url.strip().strip("'\"")
        if media_type != "file" and is_torrent_input(url):
            return self._torrent_manager.probe(url, cancel_event)

        canonical_key = _canonical_probe_url(url)
        with self._lock:
            cached = self._probed.get(canonical_key) or self._probed.get(url)
            if cached and len(cached) >= 4:
                cached_url, cached_info, cached_at, cached_result = cached
                if (time.time() - cached_at) < PROBE_REUSE_MAX_AGE and cached_result.get("ok"):
                    log.info("Probe cache hit", url=url, age=round(time.time() - cached_at, 2))
                    return copy.deepcopy(cached_result)

        fallback_count = 0
        if isinstance(fallback_urls, list):
            fallback_count = len(fallback_urls)
        elif isinstance(fallback_urls, str) and fallback_urls.strip():
            fallback_count = 1
        log.info("Probe requested", url=url, fallback_count=fallback_count or None)
        result, info = probe(
            url,
            fallback_urls,
            self._settings_store.get(),
            media_type,
            fallback_sources,
            cancel_event,
        )

        if result.get("ok") and result.get("url"):
            with self._lock:
                cache_entry = (result["url"], info, time.time(), result)
                self._probed[url] = cache_entry
                if canonical_key != url:
                    self._probed[canonical_key] = cache_entry
            log.debug("Probe cached", url=url, canonical=canonical_key, request_url=result["url"])

        if result.get("ok") and result.get("result") and result["result"].get("formats"):
            request_url = result.get("url") or url
            with self._lock:
                for fmt in result["result"]["formats"]:
                    selector = fmt.get("selector") or "best"
                    size = fmt.get("size")
                    if size is not None:
                        is_estimate = bool(fmt.get("sizeIsEstimate"))
                        self._size_cache[(request_url, selector)] = (size, is_estimate)
                        # Exact-URL downloads (extractor engine, no format selector)
                        # look up the size by their own stream URL.
                        fmt_url = fmt.get("url")
                        if fmt_url:
                            self._size_cache[(fmt_url, "best")] = (size, is_estimate)

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
            # Preserve any playlist subfolder so duplicate checks and renames
            # target the real on-disk path (e.g. "Playlist/01 - Ep.mp4").
            subfolder = Path(filename).parent.as_posix()
            prefix = f"{subfolder}/" if subfolder and subfolder != "." else ""
            stem, ext = _stem_and_ext(Path(filename).name)
            target = f"{prefix}{stem}.{ext}" if ext else f"{prefix}{stem}"
            return target, stem, ext

        if media_type == "file":
            name = _url_filename(url)
            stem, ext = _stem_and_ext(name)
            target = f"{stem}.{ext}" if ext else stem
            return target, stem, ext

        # For media without a custom filename, guess from title or URL.
        name = _url_filename(url)
        stem = title or _stem_and_ext(name)[0]
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
        try:
            existing = {entry.name for entry in Path(directory).iterdir()}
        except OSError:
            existing = set()
        for n in range(1, 1000):
            candidate = f"{base} ({n}).{ext}" if ext else f"{base} ({n})"
            if candidate not in existing:
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
        if media_type == "torrent" or is_magnet_url(url):
            return self._torrent_manager.resolve_duplicate(
                url,
                directory,
                action,
                torrent_name=filename or title,
            )

        url = unsmuggle_url(url, {})[0]

        # Playlists do not produce a single output file; only check for an active
        # duplicate playlist job and let the caller create child jobs.
        if media_type == "playlist":
            duplicate_playlist: DownloadJob | None = None
            with self._lock:
                for job in self._jobs.values():
                    if (
                        job.url == url
                        and job.directory == directory
                        and job.is_playlist
                        and job.data["status"] in {"queued", "downloading", "paused", "postprocessing"}
                    ):
                        duplicate_playlist = job
                        break
            if duplicate_playlist is None or action == "rename":
                return {"status": "ok", "filename": None}
            if action == "skip":
                return {"status": "ok", "skipped": True}
            if action == "resume":
                return {"status": "ok", "filename": None}
            if action == "override":
                self.remove(duplicate_playlist.id)
                return {"status": "ok", "filename": None}
            return {
                "status": "duplicate",
                "type": "job",
                "existing": duplicate_playlist.snapshot(),
                "filename": filename or "",
                "suggestedName": "",
            }

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

        if action == "resume":
            # Continue what already exists: an active job keeps running, a
            # paused job is resumed, and an existing file is left for yt-dlp
            # to resume via its .part file.
            if duplicate_job is not None:
                if duplicate_job.data["status"] == "paused":
                    self.resume(duplicate_job.id)
                return {"status": "ok", "skipped": True}
            return {"status": "ok", "filename": filename}

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
                    log.warning("Could not trash file", path=str(target_path), error=str(exc))
                    try:
                        target_path.unlink()
                    except Exception:  # noqa: BLE001
                        pass
            return {"status": "ok", "filename": filename}

        if action == "rename":
            if not target_stem:
                # No usable stem; derive one from the URL or existing filename.
                target_stem = _stem_and_ext(_url_filename(url))[0]
            if not target_ext:
                target_ext = _ext_from_name("", filename or "")
            # Keep playlist children inside their playlist subfolder; scan that
            # folder for existing names so the index does not collide there.
            subfolder = Path(filename).parent.as_posix() if filename else "."
            unique_dir = str(Path(directory) / subfolder) if subfolder != "." else directory
            new_filename = self._generate_unique_filename(unique_dir, target_stem, target_ext)
            if subfolder != ".":
                new_filename = f"{subfolder}/{new_filename}"
            return {"status": "ok", "filename": new_filename}

        # No action supplied: report the duplicate for the UI.
        dup_type = "job" if duplicate_job is not None else "file"
        if duplicate_job is not None:
            existing = duplicate_job.snapshot()
            existing_filename = Path(duplicate_job.custom_filename or duplicate_job.data.get("filename") or "download").name
            existing_stem, existing_ext = _stem_and_ext(existing_filename)
            suggested_stem = target_stem or existing_stem or "download"
            suggested_ext = target_ext or existing_ext
        else:
            existing = str(target_path)
            existing_stem, existing_ext = _stem_and_ext(existing)
            suggested_stem = target_stem or existing_stem
            suggested_ext = target_ext or existing_ext

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
            if not job.initial_total:
                job.initial_total = job.data["total"]
            return

        selector = job.format_selector or "best"
        with self._lock:
            cached = self._size_cache.get((job.request_url, selector))
            probed = self._probed.get(job.url)
        if cached:
            job.data["total"] = cached[0]
            job.initial_total = cached[0]
            return

        # Playlist children are not individually probed before the worker starts.
        # Extract the entry now so we can resolve its total size up-front and also
        # cache the info dict for the download step (avoiding a second page fetch).
        if not probed and job.engine == "ytdlp":
            settings = self._settings_store.get()
            opts: dict[str, Any] = {
                **_base_opts(settings),
                "skip_download": True,
                "extract_flat": False,
                "noplaylist": True,
            }
            _apply_smuggled_http_headers(opts, job.request_url, fallback_referer=job.referer)
            try:
                with YoutubeDL(cast(Any, opts)) as ydl:
                    info = ydl.extract_info(job.request_url, download=False)
                if info and info.get("_type") != "playlist":
                    with self._lock:
                        self._probed[job.url] = (job.request_url, info, time.time(), None)
                    probed = (job.request_url, info, time.time(), None)
            except Exception as exc:  # noqa: BLE001
                log.debug("Could not extract child for size", url=job.url, error=str(exc))

        if not probed:
            log.debug("No cached probe; total will come from progress hooks", url=job.url)
            return

        request_url, info = probed[0], probed[1]
        if not info:
            log.debug("Probed info missing", url=job.url)
            return

        settings = self._settings_store.get()
        opts: dict[str, Any] = {**_base_opts(settings), "skip_download": True, "extract_flat": False}
        _apply_smuggled_http_headers(opts, request_url, fallback_referer=job.referer)
        try:
            with YoutubeDL(cast(Any, opts)) as ydl:
                resolved = resolve_selector_size(ydl, info, job.format_selector)
                if resolved:
                    job.data["total"] = resolved[0]
                    job.initial_total = resolved[0]
                    if len(resolved) > 2 and resolved[2] > 0:
                        job.expected_components = max(job.expected_components, resolved[2])
                    with self._lock:
                        # Key by job.request_url — the same key start() looks up
                        # at enqueue time — not the local probe-tuple request_url,
                        # which can differ when job.request_url was resolved via a
                        # different probe path (e.g. referer fallback).
                        self._size_cache[(job.request_url, selector)] = resolved
        except Exception:  # noqa: BLE001
            log.debug("Could not resolve total", url=job.request_url, selector=job.format_selector)

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
        referer: str = "",
        parent_id: str | None = None,
    ) -> DownloadJob:
        url = url.strip().strip("'\"")
        if media_type != "file" and is_torrent_input(url):
            return self._torrent_manager.start(url, directory, filename=filename, title=title, selected_files=selected_files)  # type: ignore[return-value]
        clean_url, smuggled = unsmuggle_url(url, {})
        effective_referer = referer or smuggled.get("referer") or ""
        # Re-minted URLs keep their smuggled header pair (ua/al); stripping the
        # fragment here would lose the signature the CDN token is bound to.
        job_url = url if smuggled.get("ua") else clean_url
        job = DownloadJob(
            job_url,
            directory,
            format_selector=format_selector,
            filename=filename,
            title=title,
            thumbnail=thumbnail,
            engine=engine,
            media_type=media_type,
            referer=effective_referer,
            parent_id=parent_id,
        )
        with self._lock:
            # A format selector means the client wants yt-dlp to resolve a format
            # from the page URL, so use the cached page probe. Without a selector
            # the client passed an exact stream URL (e.g. a chosen resolution from
            # the extractor fallback list) and that URL must be preserved.
            probed = None
            if format_selector:
                if effective_referer and effective_referer != clean_url:
                    probed = self._probed.get(effective_referer)
                if not probed:
                    probed = self._probed.get(clean_url)
            # Never drive a download from a playlist-level probe: that would make
            # every playlist child or single-video-from-playlist download the
            # first entry.
            if probed and probed[1].get("_type") == "playlist":
                probed = None
            job.request_url = probed[0] if probed else url
            self._jobs[job.id] = job
            cached = self._size_cache.get((job.request_url, format_selector or "best"))
            if cached:
                job.data["total"] = cached[0]
                job.initial_total = cached[0]
                if len(cached) > 2 and cached[2] > 0:
                    job.expected_components = max(job.expected_components, cached[2])
            self._emit_unlocked(job, force=True)
        log.info("Download started", job_id=job.id, url=clean_url, referer=effective_referer, format_selector=format_selector, directory=directory)
        self._submit(job)
        if job.thumbnail and job.referer:
            threading.Thread(target=self._hydrate_thumbnail, args=(job,), name="thumb-hydrate", daemon=True).start()
        return job

    def _hydrate_thumbnail(self, job: DownloadJob) -> None:
        """Swap a referer-gated thumbnail URL for a data URI the dashboard can render.

        The dashboard's <img> cannot send the job's Referer, so protected CDNs
        403 the direct fetch. Fetch once in the background and inline the bytes.
        """
        uri = _fetch_thumbnail_data_uri(
            job.data.get("thumbnail") or "",
            job.referer,
            unsmuggle_url(job.request_url or job.url, {})[1].get("ua") or "",
        )
        if not uri:
            return
        with self._lock:
            if job.data.get("removed") or job.data.get("thumbnail", "").startswith("data:"):
                return
            job.data["thumbnail"] = uri
            self._emit_unlocked(job, force=True)

    def _get_playlist_entries(
        self,
        url: str,
        referer: str | None = None,
    ) -> tuple[str | None, list[dict[str, Any]] | None]:
        """Return (request_url, entries) for a playlist URL, reusing a cached probe."""
        with self._lock:
            probed = self._probed.get(url)
        if probed:
            request_url, info = probed[0], probed[1]
            if info and info.get("_type") == "playlist":
                return request_url, _build_playlist_entries(info)

        settings = self._settings_store.get()
        opts = {**_base_opts(settings), "skip_download": True, "extract_flat": "in_playlist"}
        _apply_smuggled_http_headers(opts, url, fallback_referer=referer)
        try:
            with YoutubeDL(cast(Any, opts)) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not re-extract playlist", url=url, error=str(exc))
            return None, None
        if not info or info.get("_type") != "playlist":
            return None, None
        # Cache the fresh extraction so subsequent calls reuse it instead of
        # re-extracting the full playlist every time.
        with self._lock:
            self._probed[url] = (url, info, time.time(), None)
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
        referer: str = "",
    ) -> DownloadJob:
        """Start a playlist download: one parent job plus a child job per entry."""
        clean_url, smuggled = unsmuggle_url(url, {})
        effective_referer = referer or smuggled.get("referer") or ""
        request_url, entries = self._get_playlist_entries(clean_url, referer=effective_referer)
        if not entries:
            raise ValueError(f"No playlist entries found for {url}")

        # Prefer the original audio track (YouTube's "original" format_note) when
        # available; fall back to the best audio, and finally to the best available
        # format (e.g. audio-only entries). This matches the audio preference used
        # for single-video downloads while still letting yt-dlp merge video+audio.
        child_format_selector = format_selector or "bestvideo+(ba[format_note*=original]/ba)/best"

        if selected_entry_urls:
            selected_set = set(selected_entry_urls)
            entries = [e for e in entries if e.get("url") in selected_set or e.get("webpageUrl") in selected_set]
            if not entries:
                raise ValueError("None of the selected playlist entries are available")

        parent = DownloadJob(
            clean_url,
            directory,
            title=title,
            thumbnail=thumbnail,
            engine="ytdlp",
            media_type="playlist",
            referer=effective_referer,
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
            playlist_index = entry.get("index", idx + 1)

            # Prefix files with their playlist index so filenames match episode
            # order. Numbering is reversed: the first playlist item gets the
            # highest index and the last item gets 001.
            child_filename = None
            safe_child_title = sanitize_filename(child_title, restricted=False).strip() if child_title else ""
            if safe_child_title:
                stem = f"{max_index + 1 - playlist_index:0{digits}d} - {safe_child_title}"
                child_filename = f"{playlist_folder}/{stem}.{merge_format}"

            resolved = self.resolve_duplicate(child_url, directory, child_format_selector, child_filename, child_title, "", "rename")
            if resolved.get("status") == "duplicate" and child_filename:
                resolved = self.resolve_duplicate(child_url, directory, child_format_selector, None, child_title, "", "rename")
            final_filename = resolved.get("filename") if resolved.get("status") == "ok" else None

            child = self.start(
                child_url,
                directory,
                format_selector=child_format_selector,
                filename=final_filename,
                title=child_title,
                thumbnail=child_thumbnail,
                engine="ytdlp",
                referer=parent.referer,
                parent_id=parent.id,
            )
            child_ids.append(child.id)

        with self._lock:
            parent.child_ids = child_ids
            parent.data["childIds"] = child_ids
            parent.data["status"] = "downloading"
            self._emit_unlocked(parent, force=True)
            last_child = self._jobs.get(child_ids[-1]) if child_ids else None
        if last_child is not None:
            self._update_parent(last_child, force=True)

        log.info("Playlist download started", job_id=parent.id, url=clean_url, referer=effective_referer, entries=len(entries), directory=directory)
        return parent

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
        self._cancel_queued_worker(job_id)
        log.info("Cancelling job", job_id=job_id, status=job.data["status"])
        with self._lock:
            job.data.update(status="cancelled", speed=None, eta=None)
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
            # _update_parent refuses to recompute terminal parents; reset the
            # parent first or it stays failed/cancelled while children restart.
            with self._lock:
                if job.data["status"] in {"failed", "cancelled"}:
                    job.data.update(status="queued", error=None, speed=None, eta=None)
                    self._emit_unlocked(job, force=True)
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
            existing = self._worker_futures.get(job.id)
        if existing is None or existing.done():
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
                subprocess.run(["open", "-R", filepath], check=True, timeout=10)
            elif platform.system() == "Windows":
                # explorer.exe returns a nonzero exit code even on success.
                subprocess.run(["explorer", f"/select,{filepath}"], timeout=10)
            else:
                subprocess.run(["xdg-open", str(Path(filepath).parent)], check=True, timeout=10)
            return {"ok": True}
        except subprocess.TimeoutExpired:
            log.warning("Reveal command timed out", filepath=filepath)
            return {"ok": False, "error": "Could not reveal file: timed out"}
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
            # Mark the parent as removed and drop it from the in-memory index first
            # so any late child emissions are ignored and the playlist does not
            # flicker to "cancelled" in the UI.
            with self._lock:
                child_futures = [
                    future
                    for child_id in job.child_ids
                    if (future := self._worker_futures.get(child_id)) is not None
                ]
                job.data["removed"] = True
                self._jobs.pop(job_id, None)
                self._last_emit.pop(job_id, None)
            for child_id in list(job.child_ids):
                self.remove(child_id)

            def trash_temp_after_children() -> None:
                for future in child_futures:
                    with contextlib.suppress(Exception):
                        future.result()
                _trash_job_temp(job_id)

            try:
                self._loop.run_in_executor(None, trash_temp_after_children)
            except RuntimeError:
                log.error("Could not schedule trash for playlist: event loop is closed", job_id=job_id)
            self._delete_persisted(job_id)
            return True

        log.info("Removing job", job_id=job_id, status=job.data["status"])
        # Mark the job removed before cancelling so the worker's final emit is
        # suppressed and the UI does not briefly show "cancelled" after remove.
        with self._lock:
            job.data["removed"] = True
            self._jobs.pop(job_id, None)
            self._last_emit.pop(job_id, None)
            # Keep the parent's child list and count consistent.
            if job.parent_id:
                parent = self._jobs.get(job.parent_id)
                if parent is not None and job_id in parent.child_ids:
                    parent.child_ids.remove(job_id)
                    parent.data["childIds"] = parent.child_ids
                    parent.data["playlistCount"] = len(parent.child_ids) or None
        active = job.data["status"] in {"downloading", "postprocessing"}
        if active:
            job.cancel_event.set()
        elif job.data["status"] == "queued":
            job.cancel_event.set()
            self._cancel_queued_worker(job_id)
        elif job.data["status"] in {"paused", "failed", "cancelled"} and job.parent_id is None:
            # Worker is not running; trash the isolated temp files now.
            try:
                self._loop.run_in_executor(None, _trash_job_temp, job_id)
            except RuntimeError:
                log.error("Could not schedule trash for job: event loop is closed", job_id=job_id)
        self._delete_persisted(job_id)
        return True

    def clear_finished(self) -> list[str]:
        with self._lock:
            done = []
            for job_id, job in self._jobs.items():
                if job.data["status"] != "completed":
                    continue
                parent = self._jobs.get(job.parent_id) if job.parent_id else None
                if parent is not None and parent.data["status"] != "completed":
                    continue
                done.append(job_id)

            temp_job_ids: set[str] = set()
            for job_id in done:
                job = self._jobs.pop(job_id, None)
                self._last_emit.pop(job_id, None)
                if job is None:
                    continue
                job.data["removed"] = True
                if job.is_playlist or job.parent_id is None:
                    temp_job_ids.add(job.id)
                elif job.parent_id not in self._jobs:
                    temp_job_ids.add(job.parent_id)

        torrent_done = self._torrent_manager.clear_finished()
        for temp_job_id in temp_job_ids:
            try:
                self._loop.run_in_executor(None, _trash_job_temp, temp_job_id)
            except RuntimeError:
                log.error("Could not schedule trash for finished job", job_id=temp_job_id)
        for job_id in done:
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
        return str(info.get("ext") or "").lower() not in NON_MEDIA_EXTENSIONS

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
            clean_path = _FRAG_PART_RE.sub('', str(filepath))
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
        precomputed = job.initial_total or job.data.get("total")
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

    @staticmethod
    def _existing_fragment_bytes(filename: str | None) -> int:
        """Sum sizes of fragment files already on disk for this destination.

        On resume, yt-dlp's fragment downloader seeds ``downloaded_bytes`` from
        the concatenated output file only. Fragments downloaded by a previous
        session but not yet appended (e.g. written by aria2-next, or the
        process stopped between fragment download and append) sit on disk as
        ``<name>-FragN`` / ``<name>.part-FragN`` and are invisible to the
        progress counter — making a 70%-complete resume report 0%.
        """
        if not filename:
            return 0
        path = Path(filename)
        try:
            entries = list(os.scandir(path.parent))
        except OSError:
            return 0
        name = path.name
        prefixes = (f"{name}-Frag", f"{name}.part-Frag")
        total = 0
        for entry in entries:
            if not entry.name.startswith(prefixes):
                continue
            if entry.name.endswith(".aria2"):
                continue
            try:
                total += entry.stat().st_size
            except OSError:
                pass
        return total

    def _progress_hook(self, job: DownloadJob) -> Any:
        seeded_components: set[str] = set()

        def hook(d: dict[str, Any]) -> None:
            if job.cancel_event.is_set():
                raise DownloadCancelled("Cancelled by user")

            status = d.get("status")
            if status not in {"downloading", "finished"} or not self._is_media_progress(d):
                return

            # Pure functions of `d`; run outside the lock to keep the hot path
            # from serializing other workers.
            key = self._progress_component_key(d)
            info = d.get("info_dict")
            if isinstance(info, dict) and not job.data.get("thumbnail") and not job.thumbnail_scanned:
                thumb = _extract_thumbnail(info)
            else:
                thumb = None

            with self._lock:
                # A single-component job (plain file, single-format download)
                # is exactly one stream of bytes, so it must never hold more
                # than one progress component. The component key is the
                # reporter's identity (format_id or filename) and it can
                # legitimately change mid-job — .part -> final filename on the
                # finished report, a retried attempt after a resume error, a
                # format_id appearing late. A new key for a single-component
                # job is the same bytes under a different name; keeping the
                # old component alongside it double-counts the file.
                #
                # Some selectors (e.g. YouTube's default 'best' with no explicit
                # '+') resolve to separate video and audio streams even though
                # the selector string contains no '+'. Detect that during
                # downloading by counting distinct format reporters.
                if (
                    status == "downloading"
                    and job.expected_components == 1
                    and job.progress_components
                    and key not in job.progress_components
                ):
                    is_new_format = key.startswith("format:")
                    has_format_keys = any(k.startswith("format:") for k in job.progress_components)
                    if is_new_format or has_format_keys:
                        format_keys = {k for k in job.progress_components if k.startswith("format:")}
                        if is_new_format:
                            format_keys.add(key)
                        # A combined report (format_id contains '+' or requested_formats)
                        # is one stream of already-combined bytes, not a new component.
                        info_dict = d.get("info_dict")
                        is_combined = (
                            isinstance(info_dict, dict)
                            and (
                                "+" in str(info_dict.get("format_id") or "")
                                or bool(info_dict.get("requested_formats"))
                            )
                        )
                        if is_combined:
                            job.progress_components.clear()
                        else:
                            job.expected_components = max(job.expected_components, len(format_keys))
                    else:
                        job.progress_components.clear()
                component = job.progress_components.setdefault(key, {})
                if thumb and not job.data.get("thumbnail"):
                    job.thumbnail_scanned = True
                    job.data["thumbnail"] = thumb
                observed_total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if observed_total is not None:
                    observed_total = int(observed_total)

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
                    # Native fragment downloader reports per-fragment progress;
                    # seed the component once with bytes already on disk from a
                    # previous session so a resumed download doesn't restart the
                    # progress bar at 0%. (aria2-next's aggregated hook lacks
                    # fragment_index and already accounts for its own files.)
                    is_native_fragment = (
                        d.get("fragment_index") is not None
                        or d.get("fragment_count") is not None
                    )
                    if is_native_fragment and key not in seeded_components:
                        seeded_components.add(key)
                        component["resumed_bytes"] = self._existing_fragment_bytes(
                            d.get("filename")
                        )
                    resumed = int(component.get("resumed_bytes") or 0)
                    comp_downloaded = resumed + (d.get("downloaded_bytes") or 0)
                    component.update(
                        downloaded=comp_downloaded,
                        speed=d.get("speed") or 0,
                    )

                    seg_total = component.get("segments_total")
                    seg_done = component.get("segments_done")
                    is_segmented = bool(seg_total)

                    if is_segmented and seg_total and seg_total > 0:
                        if seg_done is not None and seg_done >= seg_total:
                            component["total"] = comp_downloaded
                        else:
                            base_total = job.initial_total if job.expected_components == 1 else component.get("initial_total")
                            current_total = int(component.get("total") or base_total or observed_total or 0)
                            if current_total > 0:
                                # Preserve the initial probed total (e.g. 199 MB) without artificial inflation.
                                # If actual downloaded bytes reach or exceed the estimate while segments are
                                # still pending, project the remaining size based on the completed segment average.
                                if comp_downloaded >= current_total and seg_done and seg_done > 0:
                                    avg_seg = comp_downloaded / float(seg_done)
                                    projected = int(comp_downloaded + (seg_total - seg_done) * avg_seg)
                                    component["total"] = max(comp_downloaded, projected)
                                else:
                                    component["total"] = current_total
                            elif observed_total:
                                component["total"] = observed_total
                    elif observed_total:
                        component["total"] = observed_total

                    downloaded, total, speed, eta = self._aggregate_progress(job)
                    if eta is None and d.get("eta") is not None:
                        eta = int(d["eta"])
                    percent = round(min(downloaded / total, 1.0) * 100, 1) if total else job.data["percent"]
                    if job.expected_components > 1 and len(job.progress_components) < job.expected_components:
                        percent = min(percent, 99.9)
                    if (
                        job.data.get("segmentsTotal")
                        and job.data.get("segmentsDone") is not None
                        and job.data["segmentsDone"] < job.data["segmentsTotal"]
                    ):
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
                        job.data["filename"] = os.path.basename(d["filename"])
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
                        filename = os.path.basename(d["filename"])
                        job.data["filename"] = filename
                        job.data["finalFilepath"] = str(Path(job.data["directory"]) / filename)
                    self._emit_unlocked(job, force=True)

        return hook

    def _post_hook(self, job: DownloadJob) -> Any:
        """Update the job with the final filename after yt-dlp post-processing."""

        def hook(filepath: str) -> None:
            if job.cancel_event.is_set():
                raise DownloadCancelled("Cancelled by user")
            path = Path(filepath)
            final_size: int | None = None
            try:
                if path.is_file():
                    final_size = path.stat().st_size
            except OSError:
                pass
            # job.data is shared with the event loop and other workers; all
            # writes go through the same RLock they read under.
            with self._lock:
                job.data["filename"] = path.name
                job.data["finalFilepath"] = str(filepath)
                if final_size is not None:
                    job.data["downloaded"] = final_size
                    job.data["total"] = final_size

        return hook

    def _run_file(self, job: DownloadJob, settings: dict[str, Any], directory: str, temp_dir: Path, request_url: str) -> None:
        """Download an intercepted non-media file as-is."""
        if job.custom_filename:
            filename = Path(job.custom_filename).name
            stem, ext = _stem_and_ext(filename)
        else:
            name = _url_filename(request_url)
            stem, ext = _stem_and_ext(name)
            filename = None

        outtmpl = (filename if filename else (f"{stem}.{ext}" if ext else stem)).replace("%", "%%")

        def _file_postprocessor_hook(d: dict[str, Any]) -> None:
            if job.cancel_event.is_set():
                raise DownloadCancelled("Cancelled by user")

        opts: dict[str, Any] = {
            **_base_opts(settings, direct_file=True),
            "outtmpl": outtmpl,
            "paths": {"home": directory, "temp": str(temp_dir)},
            "progress_hooks": [self._progress_hook(job)],
            "postprocessor_hooks": [_file_postprocessor_hook],
            "post_hooks": [self._post_hook(job)],
            "noplaylist": True,
            "continuedl": True,
        }
        _apply_smuggled_http_headers(opts, request_url, fallback_referer=job.referer)

        with self._lock:
            job.data.update(status="downloading", directory=directory, tempDirectory=str(temp_dir))
            self._emit_unlocked(job, force=True)
        log.info("Beginning file download", job_id=job.id, url=request_url, referer=job.referer, ext=ext)

        clean_url = unsmuggle_url(request_url, {})[0]

        try:
            # process_info mutates the dict in place; each attempt gets a
            # fresh copy from this pristine template so a failed attempt's
            # download state cannot leak into the retry.
            info_template: dict[str, Any] = {
                "id": job.id,
                "title": stem,
                "url": clean_url,
                "ext": ext,
                "webpage_url": job.url,
                "extractor_key": "direct",
            }

            def _clear_partials() -> None:
                try:
                    for partial in temp_dir.iterdir():
                        if partial.is_file():
                            partial.unlink()
                except OSError as exc:
                    log.warning("Could not clear partial download", job_id=job.id, error=str(exc))

            def _is_range_error(exc: Exception) -> bool:
                text = str(exc)
                return "416" in text or "Requested Range Not Satisfiable" in text

            def _attempt(download_opts: dict[str, Any]) -> None:
                try:
                    with _allow_unsafe_extensions(), YoutubeDL(cast(Any, download_opts)) as ydl:
                        ydl.process_info(cast(Any, copy.deepcopy(info_template)))
                except Exception as exc:
                    if isinstance(exc, DownloadCancelled) or not _is_range_error(exc):
                        raise
                    # A 416 while resuming means the partial file no longer
                    # matches the remote resource (stale .part, or the
                    # tokenized URL now serves different/expired content).
                    # Resume can never succeed; restart from scratch.
                    log.warning(
                        "416 on resume; clearing partials and restarting download from scratch",
                        job_id=job.id,
                    )
                    _clear_partials()
                    fresh_opts = {**download_opts, "continuedl": False}
                    with _allow_unsafe_extensions(), YoutubeDL(cast(Any, fresh_opts)) as ydl:
                        ydl.process_info(cast(Any, copy.deepcopy(info_template)))

            try:
                _attempt(opts)
            except Exception as exc:
                if isinstance(exc, DownloadCancelled):
                    raise
                log.warning(
                    "aria2-next downloader failed; retrying with the native HTTP downloader",
                    job_id=job.id,
                    error=str(exc),
                )
                fallback_opts = {**opts, "continuedl": True}
                fallback_opts.pop("external_downloader", None)
                fallback_opts.pop("external_downloader_args", None)
                _attempt(fallback_opts)
        except DownloadCancelled:
            status = "paused" if job.pause_requested else "cancelled"
            log.info("File download cancelled/paused", job_id=job.id, status=status)
            with self._lock:
                job.data.update(status=status, speed=None, eta=None)
        except Exception as exc:  # noqa: BLE001
            msg = _download_error_message(exc)
            log.exception("File download failed", job_id=job.id, error=msg)
            with self._lock:
                if job.cancel_event.is_set():
                    status = "paused" if job.pause_requested else "cancelled"
                    job.data.update(status=status, speed=None, eta=None)
                else:
                    job.data.update(status="failed", error=msg, speed=None, eta=None)
        else:
            with self._lock:
                if job.cancel_event.is_set():
                    # Cancelled while post-processing was still running; the
                    # merge may have completed in the background but the user
                    # asked to cancel — never resurrect to completed.
                    status = "paused" if job.pause_requested else "cancelled"
                    job.data.update(status=status, speed=None, eta=None)
                else:
                    final_path_str = job.data.get("finalFilepath")
                    final_path = Path(final_path_str) if final_path_str else (Path(directory) / job.data.get("filename", ""))
                    final_size = None
                    try:
                        if final_path.is_file():
                            final_size = final_path.stat().st_size
                    except OSError:
                        pass
                    actual_downloaded = final_size if final_size is not None else job.data["downloaded"]
                    final_total = final_size if final_size is not None else max(job.data.get("total") or 0, actual_downloaded)
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
            if job.parent_id is None:
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
        if job.data.get("removed") or job.cancel_event.is_set() or job.data.get("status") != "queued":
            return
        if not self._acquire_work_slot(job):
            return
        try:
            self._run_with_slot(job)
        except DownloadCancelled:
            status = "paused" if job.pause_requested else "cancelled"
            log.info("Download cancelled/paused before start", job_id=job.id, status=status)
            with self._lock:
                job.data.update(status=status, speed=None, eta=None)
                self._emit_unlocked(job, force=True)
        except Exception as exc:  # noqa: BLE001
            msg = _download_error_message(exc)
            log.exception("Job execution failed before download", job_id=job.id, error=msg)
            with self._lock:
                job.data.update(status="failed", error=msg, speed=None, eta=None)
                self._emit_unlocked(job, force=True)
        finally:
            self._work_slots.release()

    def _fresh_probe_info(self, job: DownloadJob) -> dict[str, Any] | None:
        """Return the cached probe's info dict when fresh enough to download from.

        Re-extracting every download doubles the page fetches (and extractor
        challenges); a probe younger than PROBE_REUSE_MAX_AGE is still accurate
        enough to drive the download directly.
        """
        with self._lock:
            probed = self._probed.get(job.url)
            # Only fall back to the referer's probe when a format selector is
            # set. With no selector the client passed an exact stream URL (e.g.
            # a chosen resolution from multiResolutionExtractor); clobbering
            # job.request_url with the page-level probe would switch to the
            # first-probed candidate instead of the one the user selected.
            if not probed and job.format_selector and job.referer and job.referer != job.url:
                probed = self._probed.get(job.referer)
        if not probed or len(probed) < 3:
            return None
        request_url, info, probed_at = probed[0], probed[1], probed[2]
        if not isinstance(info, dict) or info.get("_type") == "playlist":
            return None
        if time.time() - probed_at > PROBE_REUSE_MAX_AGE:
            return None
        if request_url and job.format_selector:
            job.request_url = request_url
        return cast(dict[str, Any], info)

    def _run_with_slot(self, job: DownloadJob) -> None:
        if job.data.get("removed"):
            if job.parent_id is None:
                _trash_job_temp(job.id)
            return
        if job.cancel_event.is_set() or job.data.get("status") != "queued":
            return

        settings = self._settings_store.get()
        directory = job.directory or settings["downloadDir"]
        temp_dir = _job_temp_dir(job.id, job.parent_id)
        temp_dir.mkdir(parents=True, exist_ok=True)

        request_url = job.request_url

        # Intercepted non-media files (.dmg, .zip, .pdf, .html, etc.) are downloaded
        # as-is using a manually built info dict so yt-dlp does not try to remux or
        # extract a video player from them.
        if job.media_type == "file":
            self._run_file(job, settings, directory, temp_dir, request_url)
            return

        with self._lock:
            job.data.update(status="downloading", directory=directory, tempDirectory=str(temp_dir))
            self._emit_unlocked(job, force=True)

        self._resolve_job_total(job)

        # Reuse the probe's extraction when fresh so the download does not
        # re-fetch the page (and re-run extractor challenges) a second time.
        cached_info = self._fresh_probe_info(job)
        if cached_info is not None:
            request_url = job.request_url

        if job.custom_filename:
            stem, _ = _stem_and_ext(job.custom_filename)
            # '%' is an outtmpl format specifier; escape it so a literal '%'
            # in a user-supplied name survives to the output filename.
            stem = stem.replace("%", "%%")
            if job.parent_id:
                outtmpl = f"{stem}.%(ext)s"
            else:
                outtmpl = f"{Path(stem).name}.%(ext)s"
        else:
            outtmpl = "%(title)s.%(ext)s"

        opts: dict[str, Any] = {
            **_base_opts(settings),
            "outtmpl": outtmpl,
            "paths": {"home": directory, "temp": str(temp_dir)},
            "format": job.format_selector,
            "progress_hooks": [self._progress_hook(job)],
            "post_hooks": [self._post_hook(job)],
            "noplaylist": True,
            "continuedl": True,
        }
        _apply_smuggled_http_headers(opts, request_url, fallback_referer=job.referer)
        merge_format = settings.get("mergeOutputFormat", DEFAULT_SETTINGS["mergeOutputFormat"]).strip().lower()
        opts["merge_output_format"] = merge_format

        postprocessors: list[dict[str, Any]] = []

        # Remux single and multi streams to the selected container so the setting
        # is honored everywhere, but only when the target is a video container.
        # Audio-only downloads are converted to MP3 instead of keeping their
        # native audio container.
        if job.media_type == "audio":
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                # Quality 0 lets ffmpeg pick the highest VBR quality.
                "preferredquality": "0",
            })
        elif merge_format in VIDEO_CONTAINERS:
            postprocessors.append({
                "key": "FFmpegVideoRemuxer",
                "preferedformat": merge_format,
            })

        # Thumbnails: yt-dlp-provided when writeThumbnail is on, but also use any
        # custom thumbnail the client passed (e.g. extracted from page meta tags
        # for non-yt-dlp sites) and ensure it gets embedded in the final file.
        # Only do this for video/audio downloads; files and playlist parents
        # should not have cover art extracted from a page logo.
        if _should_embed_thumbnail(job, settings):
            opts["writethumbnail"] = True
            postprocessors.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg"})

        if settings.get("writeSubs"):
            opts["writesubtitles"] = True
        if settings.get("writeAutoSubs"):
            opts["writeautomaticsub"] = True
        if settings.get("writeSubs") or settings.get("writeAutoSubs"):
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

        if _should_embed_thumbnail(job, settings):
            postprocessors.append({
                "key": "EmbedThumbnail",
                "already_have_thumbnail": False,
            })

        if postprocessors:
            opts["postprocessors"] = postprocessors

        def _stream_postprocessor_hook(d: dict[str, Any]) -> None:
            if job.cancel_event.is_set():
                raise DownloadCancelled("Cancelled by user")
        opts["postprocessor_hooks"] = [_stream_postprocessor_hook]

        if job.cancel_event.is_set():
            return
        log.info("Beginning download", job_id=job.id, url=request_url, referer=job.referer, format_selector=job.format_selector)
        _SKIPPED_FRAGMENTS.list = []

        def _attempt_media(download_opts: dict[str, Any]) -> None:
            with YoutubeDL(cast(Any, download_opts)) as ydl:
                _add_hls_fixup_postprocessors(ydl)
                _add_title_override(ydl, job)
                if _should_embed_thumbnail(job, settings):
                    ydl.add_post_processor(
                        _InjectThumbnailPP(ydl, job.thumbnail, job.referer, opts.get("http_headers")),
                        when="pre_process",
                    )
                ydl.add_post_processor(_ManifestReextractPP(ydl, job.cancel_event), when="pre_process")
                if cached_info is not None:
                    try:
                        ydl.process_ie_result(copy.deepcopy(cached_info), download=True)
                    except DownloadCancelled:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        log.warning(
                            "Cached probe unusable for download; re-extracting",
                            job_id=job.id,
                            error=str(exc),
                        )
                        ydl.download([request_url])
                else:
                    ydl.download([request_url])

        try:
            try:
                _attempt_media(opts)
            except Exception as exc:
                if isinstance(exc, DownloadCancelled) or not opts.get("external_downloader"):
                    raise
                log.warning(
                    "aria2-next downloader failed; retrying with native downloader",
                    job_id=job.id,
                    error=str(exc),
                )
                fallback_opts = {**opts}
                fallback_opts.pop("external_downloader", None)
                fallback_opts.pop("external_downloader_args", None)
                _attempt_media(fallback_opts)

            # A silent mass-skip ("fragment not found") means the manifest URLs
            # expired mid-download (typical after resuming a long-paused job):
            # yt-dlp completes with a truncated file and post-processes it as if
            # it were whole. Fail instead so a retry re-extracts fresh URLs.
            skipped = getattr(_SKIPPED_FRAGMENTS, "list", None) or []
            total_segments = job.data.get("segmentsTotal") or 0
            if (
                skipped
                and total_segments
                and len(skipped) / max(total_segments, 1) > _MAX_SKIPPED_FRAGMENT_RATIO
            ):
                raise DownloadError(
                    f"{len(skipped)} of {total_segments} fragments were unavailable; "
                    "the stream URLs likely expired. Retry the download to fetch a fresh manifest."
                )
        except DownloadCancelled:
            status = "paused" if job.pause_requested else "cancelled"
            log.info("Download cancelled/paused", job_id=job.id, status=status)
            with self._lock:
                job.data.update(status=status, speed=None, eta=None)
        except Exception as exc:  # noqa: BLE001
            msg = _download_error_message(exc)
            log.exception("Download failed", job_id=job.id, error=msg)
            with self._lock:
                if job.cancel_event.is_set():
                    status = "paused" if job.pause_requested else "cancelled"
                    job.data.update(status=status, speed=None, eta=None)
                else:
                    job.data.update(status="failed", error=msg, speed=None, eta=None)
        else:
            with self._lock:
                if job.cancel_event.is_set():
                    # Cancelled while post-processing was still running; never
                    # resurrect to completed (see _run_file's same guard).
                    status = "paused" if job.pause_requested else "cancelled"
                    job.data.update(status=status, speed=None, eta=None)
                else:
                    final_path_str = job.data.get("finalFilepath")
                    final_path = Path(final_path_str) if final_path_str else (Path(directory) / job.data.get("filename", ""))
                    final_size = None
                    try:
                        if final_path.is_file():
                            final_size = final_path.stat().st_size
                    except OSError:
                        pass

                    job.data.update(
                        status="completed",
                        percent=100.0,
                        speed=None,
                        eta=None,
                    )
                    if final_size is not None:
                        job.data["downloaded"] = final_size
                        job.data["total"] = final_size
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
            if job.parent_id is None:
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
