"""
main.py — Media Acquisition Engine
===================================
FastAPI + yt-dlp + asyncio + WebSocket progress + AV1 codec priority.

Target: Python 3.13+. Modern asyncio, Pydantic v2, type-hinted throughout.

Design notes
------------
* State is encapsulated in :class:`DownloadManager` (no module-global mutable
  dicts referenced before definition — fixes the old circular ``app`` reference).
* Concurrency is enforced with an :class:`asyncio.Semaphore`, not a pool of
  worker tasks. Changing ``max_concurrent_downloads`` swaps the semaphore, so
  in-flight downloads are never orphaned/killed mid-download.
* Pause is honest: an in-flight yt-dlp download cannot truly suspend, so
  "pause" cancels the current pass and marks the task ``PAUSED``; "resume"
  re-queues (yt-dlp ``continuedl`` resumes partial files where supported).
* Per-connection send lock prevents interleaved WebSocket frames when worker
  broadcasts race with action responses.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, fields
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import orjson
import yt_dlp
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, ORJSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from yt_dlp.utils import DownloadError

# uvloop is a meaningful win on POSIX, but it has gone stale and partially
# breaks on Python 3.14 (its ``BaseDefaultEventLoopPolicy`` import fails at
# import time). Guard broadly so a broken uvloop never crashes startup.
if sys.platform != "win32":
    try:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except Exception:  # noqa: BLE001 - optional acceleration
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dma-engine")

try:
    _YT_DLP_VERSION: str = yt_dlp.version.__version__
except Exception:  # noqa: BLE001
    _YT_DLP_VERSION = "unknown"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

type JsonObj = dict[str, Any]

# ──────────────────────────────────────────────
#  Paths
# ──────────────────────────────────────────────


def ensure_system_path() -> None:
    """Prepend common Homebrew / local bin dirs to PATH so ffmpeg etc. resolve."""
    extra: list[str] = []
    if platform.system() == "Darwin":
        extra = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin"]
    if not extra:
        return
    current = os.environ.get("PATH", "").split(os.pathsep)
    changed = False
    for p in extra:
        if p not in current and os.path.exists(p):
            current.insert(0, p)
            changed = True
    if changed:
        os.environ["PATH"] = os.pathsep.join(current)


def app_data_dir() -> Path:
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        path = home / "Library" / "Application Support" / "DownloadAnything"
    elif system == "Windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else home / "AppData" / "Roaming"
        path = base / "DownloadAnything"
    else:
        path = home / ".config" / "DownloadAnything"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_download_path() -> Path:
    downloads = Path.home() / "Downloads"
    base = downloads if downloads.exists() else Path.home()
    return base / "DownloadAnything"


APP_DATA_DIR = app_data_dir()
SETTINGS_FILE = APP_DATA_DIR / "settings.json"
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "dist" / "static"
TMP_DIR = APP_DATA_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)


# ──────────────────────────────────────────────
#  Settings (validated Pydantic model)
# ──────────────────────────────────────────────


def _default_settings() -> JsonObj:
    base = str(default_download_path())
    return {
        "max_concurrent_downloads": 3,
        "fallback_codecs": ["av01", "vp09", "avc01"],
        "default_download_path": base,
        "categories": {
            "Videos": str(default_download_path() / "videos"),
            "Courses": str(default_download_path() / "courses"),
            "Music": str(default_download_path() / "music"),
            "Cinematic": str(default_download_path() / "cinematic"),
        },
        "rate_limit_bytes_per_sec": 0,
        "merge_output_format": "mp4",
        "concurrent_fragments": 16,
        "embed_thumbnail": False,
        "embed_subtitles": False,
        "subtitle_language": "en",
        "proxy": "",
        "cookies_from_browser": "none",
    }


class AppSettings(BaseModel):
    max_concurrent_downloads: int = Field(default=3, ge=1, le=32)
    fallback_codecs: list[str] = Field(
        default_factory=lambda: ["av01", "vp09", "avc01"]
    )
    default_download_path: str
    categories: dict[str, str]
    rate_limit_bytes_per_sec: int = Field(default=0, ge=0)
    merge_output_format: Literal["mp4", "mkv", "webm"] = "mp4"
    concurrent_fragments: int = Field(default=16, ge=1)
    embed_thumbnail: bool = False
    embed_subtitles: bool = False
    subtitle_language: str = "en"
    proxy: str = ""
    cookies_from_browser: str = "none"


class SettingsUpdate(BaseModel):
    max_concurrent_downloads: int | None = Field(default=None, ge=1, le=32)
    fallback_codecs: list[str] | None = None
    default_download_path: str | None = None
    categories: dict[str, str] | None = None
    rate_limit_bytes_per_sec: int | None = Field(default=None, ge=0)
    merge_output_format: Literal["mp4", "mkv", "webm"] | None = None
    concurrent_fragments: int | None = Field(default=None, ge=1)
    embed_thumbnail: bool | None = None
    embed_subtitles: bool | None = None
    subtitle_language: str | None = None
    proxy: str | None = None
    cookies_from_browser: str | None = None


def load_settings() -> AppSettings:
    base = _default_settings()
    if SETTINGS_FILE.exists():
        try:
            raw = orjson.loads(SETTINGS_FILE.read_bytes())
            return AppSettings.model_validate({**base, **raw})
        except (orjson.JSONDecodeError, ValidationError, OSError) as exc:
            logger.warning("Settings file unreadable (%s); using defaults.", exc)
    settings = AppSettings.model_validate(base)
    save_settings(settings)
    return settings


def save_settings(settings: AppSettings) -> None:
    SETTINGS_FILE.write_bytes(
        orjson.dumps(settings.model_dump(), option=orjson.OPT_INDENT_2)
    )


# ──────────────────────────────────────────────
#  Domain models
# ──────────────────────────────────────────────


class TaskStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    STITCHING = "stitching"
    EMBEDDING = "embedding"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    PAUSED = "paused"


_ACTIVE_STATES = frozenset(
    {
        TaskStatus.DOWNLOADING,
        TaskStatus.STITCHING,
        TaskStatus.EMBEDDING,
        TaskStatus.FINALIZING,
    }
)

# Broadcast throttle interval (seconds).
_BROADCAST_INTERVAL = 0.5


@dataclass
class DownloadTask:
    task_id: str
    url: str
    format_id: str | None = None
    category: str | None = None
    custom_path: str | None = None
    title: str = "Pending…"
    status: TaskStatus = TaskStatus.QUEUED
    speed: float = 0.0
    progress: float = 0.0
    eta: float = 0.0
    total_bytes: int = 0
    downloaded_bytes: int = 0
    filename: str = ""
    final_path: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    is_video: bool = True
    page_title: str | None = None
    is_stream: bool = False
    headers: dict[str, str] | None = None
    has_custom_title: bool = False
    fragment_index: int | None = None
    fragment_count: int | None = None

    # Runtime-only fields (private, never persisted).
    _pause_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _cancel: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _hold: bool = field(default=False, repr=False)  # cancel == pause, not abort
    _is_running: bool = field(default=False, repr=False)
    _in_queue: bool = field(default=False, repr=False)
    _last_broadcast: float = field(default=0.0, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _prev_parts_bytes: int = field(default=0, repr=False)

    def update(self, **changes: Any) -> None:
        valid = self.__dataclass_fields__
        for key, value in changes.items():
            if key in valid:
                setattr(self, key, value)

    def to_dict(self) -> JsonObj:
        """Serialise all persisted fields (auto-derived, never drifts)."""
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if not f.name.startswith("_")
        }


# ──────────────────────────────────────────────
#  Path safety & title helpers
# ──────────────────────────────────────────────


def is_safe_path(target: str | Path, roots: list[Path]) -> bool:
    try:
        resolved = Path(target).resolve()
    except (OSError, ValueError):
        return False
    return any(resolved == root or root in resolved.parents for root in roots)


def allowed_roots(settings: AppSettings) -> list[Path]:
    roots = [Path.home()]
    if settings.default_download_path:
        roots.append(Path(settings.default_download_path))
    roots.extend(Path(p) for p in settings.categories.values() if p)
    return roots


def ensure_target_dir(target_dir: str) -> Path:
    path = Path(target_dir)
    path.mkdir(parents=True, exist_ok=True)
    if not os.access(path, os.W_OK):
        raise PermissionError(f"Target directory '{target_dir}' is not writable")
    return path


_GENERIC_STREAM_NAMES = frozenset(
    {
        "master", "index", "playlist", "stream", "video", "audio",
        "media", "manifest", "chunklist", "output", "main", "live",
    }
)


def sanitise_title(
    raw: str,
    url: str,
    page_title: str | None = None,
    *,
    prefer_page: bool = False,
) -> str:
    stripped = raw.strip()
    if prefer_page and page_title and page_title.strip():
        return page_title.strip()
    if stripped and stripped.lower() not in _GENERIC_STREAM_NAMES:
        return stripped
    if page_title and page_title.strip():
        return page_title.strip()
    parsed = urlparse(url)
    host = parsed.hostname or ""
    stem = os.path.splitext(os.path.basename(parsed.path.rstrip("/")))[0]
    if stem and stem.lower() not in _GENERIC_STREAM_NAMES:
        return f"{host} – {stem}" if host else stem
    return host or stripped or "Stream"


@functools.cache
def find_ffmpeg_location() -> str | None:
    exe = "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"
    local_bin = APP_DATA_DIR / "bin"
    if (local_bin / exe).exists():
        return str(local_bin)
    found = shutil.which("ffmpeg")
    if found:
        return str(Path(found).parent)
    search: list[str] = []
    if platform.system() == "Darwin":
        search = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]
    elif platform.system() == "Windows":
        search = [r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin"]
    for candidate in search:
        if (Path(candidate) / exe).exists():
            return candidate
    return None


# ──────────────────────────────────────────────
#  yt-dlp extraction helpers
# ──────────────────────────────────────────────

_STREAM_PROTOCOLS = frozenset(
    {"m3u8", "m3u8_native", "dash", "rtmp", "rtmpe", "rtmps", "rtmpt", "rtmpte"}
)


def formats_are_stream(formats: list[JsonObj]) -> bool:
    """True when the dominant format protocol signals a segmented/live stream."""
    present = {f.get("protocol", "") for f in formats if f.get("protocol")}
    return bool(present & _STREAM_PROTOCOLS)


def format_view(fmt: JsonObj) -> JsonObj | None:
    vcodec = fmt.get("vcodec", "none")
    acodec = fmt.get("acodec", "none")
    if vcodec == "none" and acodec == "none":
        return None
    width, height = fmt.get("width"), fmt.get("height")
    return {
        "format_id": fmt.get("format_id"),
        "ext": fmt.get("ext"),
        "resolution": fmt.get("resolution")
        or (f"{width}x{height}" if width and height else None),
        "vcodec": vcodec,
        "acodec": acodec,
        "fps": fmt.get("fps"),
        "filesize": fmt.get("filesize")
        or fmt.get("filesize_estimate")
        or fmt.get("filesize_approx"),
        "tbr": fmt.get("tbr"),
        "vbr": fmt.get("vbr"),
        "abr": fmt.get("abr"),
        "format_note": fmt.get("format_note", ""),
        "protocol": fmt.get("protocol", ""),
    }


def build_probe_opts(settings: AppSettings, headers: dict[str, str] | None) -> JsonObj:
    opts: JsonObj = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    if headers:
        opts["http_headers"] = headers
    else:
        browser = settings.cookies_from_browser
        if browser and browser != "none":
            opts["cookiesfrombrowser"] = (browser,)
    return opts


def _extract_once(url: str, opts: JsonObj) -> Any:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def extract_with_fallback(url: str, opts: JsonObj) -> Any:
    """Extract with graceful fallbacks for cookie & anti-piracy errors."""
    try:
        return _extract_once(url, opts)
    except DownloadError as exc:
        msg = str(exc).lower()
        if "cookies" in msg and any(
            k in msg for k in ("database", "could not find", "keychain", "no such file")
        ):
            return _extract_once(url, {k: v for k, v in opts.items() if k != "cookiesfrombrowser"})
        if "primarily used for piracy" in msg:
            return _extract_once(url, {**opts, "allowed_extractors": ["generic"]})
        raise


async def estimate_stream_size(url: str, headers: dict[str, str] | None) -> int | None:
    """Best-effort total-byte estimate for an HLS stream by sampling segments.

    Returns ``None`` if aiohttp/m3u8 are absent or estimation fails.
    """
    try:
        import aiohttp
        import m3u8
    except ImportError:
        return None

    req_headers = {"User-Agent": DEFAULT_UA, **(headers or {})}
    timeout = aiohttp.ClientTimeout
    try:
        async with aiohttp.ClientSession(headers=req_headers) as session:
            async with session.get(url, timeout=timeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                manifest = await resp.text()

            playlist = m3u8.loads(manifest, uri=url)
            if playlist.is_variant:
                best = max(
                    playlist.playlists,
                    key=lambda p: getattr(p.stream_info, "bandwidth", 0) or 0,
                    default=None,
                )
                if best and best.absolute_uri:
                    async with session.get(best.absolute_uri, timeout=timeout(total=10)) as r:
                        if r.status != 200:
                            return None
                        playlist = m3u8.loads(await r.text(), uri=best.absolute_uri)

            segments = [s for s in playlist.segments if s.uri]
            if not segments:
                return None

            total = len(segments)
            if total <= 5:
                sample_urls = [s.absolute_uri for s in segments]
            else:
                indices = {0, total // 4, total // 2, 3 * total // 4, total - 1}
                sample_urls = [segments[i].absolute_uri for i in sorted(indices)]

            async def head_size(seg_url: str) -> int:
                try:
                    async with session.head(seg_url, timeout=timeout(total=5)) as r:
                        return int(r.headers.get("Content-Length", 0)) if r.status == 200 else 0
                except Exception:  # noqa: BLE001
                    return 0

            sizes = await asyncio.gather(*(head_size(u) for u in sample_urls))
            valid = [s for s in sizes if s > 0]
            if not valid:
                return None
            return int(sum(valid) / len(valid) * total)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Stream size estimation failed for %s: %s", url, exc)
        return None


# ──────────────────────────────────────────────
#  WebSocket client wrapper (serialised sends)
# ──────────────────────────────────────────────


class Client:
    """Thin wrapper that serialises sends and tracks in-flight action tasks."""

    __slots__ = ("ws", "_lock", "pending")

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self._lock = asyncio.Lock()
        self.pending: set[asyncio.Task[None]] = set()

    async def send_text(self, text: str) -> None:
        async with self._lock:
            await self.ws.send_text(text)

    async def send_json(self, obj: Any) -> None:
        async with self._lock:
            await self.ws.send_text(orjson.dumps(obj).decode())

    def cancel_pending(self) -> None:
        for task in self.pending:
            task.cancel()


# ──────────────────────────────────────────────
#  Download manager
# ──────────────────────────────────────────────


class DownloadManager:
    _PP_STATUS: dict[str, TaskStatus] = {
        "Merger": TaskStatus.STITCHING,
        "FFmpegMerger": TaskStatus.STITCHING,
        "EmbedThumbnail": TaskStatus.EMBEDDING,
        "FFmpegEmbedSubtitle": TaskStatus.EMBEDDING,
        "MoveFiles": TaskStatus.FINALIZING,
    }

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._tasks_file = TMP_DIR / "tasks.json"
        self._tasks: dict[str, DownloadTask] = {}
        self._queue: asyncio.Queue[DownloadTask] = asyncio.Queue()
        self._clients: set[Client] = set()
        self._coros: set[asyncio.Task[None]] = set()
        self._sem = asyncio.Semaphore(max(1, settings.max_concurrent_downloads))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dispatcher: asyncio.Task[None] | None = None
        self._save_task: asyncio.Task[None] | None = None
        self._shutting_down = False
        self._tasks_lock = threading.Lock()
        self._write_lock = threading.Lock()

    # ---- lifecycle -------------------------------------------------------

    def load_tasks(self) -> None:
        if not self._tasks_file.exists():
            return
        try:
            data = orjson.loads(self._tasks_file.read_bytes())
        except (orjson.JSONDecodeError, OSError) as exc:
            logger.error("Tasks file unreadable: %s", exc)
            return

        loaded: dict[str, DownloadTask] = {}
        for tid, td in data.items():
            try:
                status = TaskStatus(td.get("status", "queued"))
            except ValueError:
                status = TaskStatus.QUEUED
            # Anything that was in-flight cannot be resumed automatically.
            if status in _ACTIVE_STATES or status == TaskStatus.QUEUED:
                status = TaskStatus.PAUSED
            loaded[tid] = DownloadTask(
                task_id=td["task_id"],
                url=td["url"],
                format_id=td.get("format_id"),
                category=td.get("category"),
                custom_path=td.get("custom_path"),
                title=td.get("title", "Pending…"),
                status=status,
                progress=td.get("progress", 0.0),
                total_bytes=td.get("total_bytes", 0),
                downloaded_bytes=td.get("downloaded_bytes", 0),
                filename=td.get("filename", ""),
                final_path=td.get("final_path", ""),
                error=td.get("error", ""),
                started_at=td.get("started_at", 0.0),
                finished_at=td.get("finished_at", 0.0),
                is_video=td.get("is_video", True),
                page_title=td.get("page_title"),
                is_stream=td.get("is_stream", False),
                headers=td.get("headers"),
                has_custom_title=td.get("has_custom_title", False),
                fragment_index=td.get("fragment_index"),
                fragment_count=td.get("fragment_count"),
            )
        self._tasks.update(loaded)

    async def run(self) -> None:
        """Main dispatcher: pull from queue, spawn a semaphore-gated coroutine."""
        self._loop = asyncio.get_running_loop()
        self._dispatcher = asyncio.current_task()
        while not self._shutting_down:
            task = await self._queue.get()
            coro = asyncio.create_task(self._run_task(task))
            task._task = coro  # record synchronously so resume can detect liveness
            self._coros.add(coro)  # keep a ref so it isn't GC'd

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._dispatcher:
            self._dispatcher.cancel()
        # Mark in-flight tasks PAUSED so the next launch can resume them. We do
        # NOT raise a cancel into yt-dlp here — that would race the progress
        # hooks and could flip a restarting task to CANCELLED. Coroutines are
        # simply cancelled; their CancelledError handler also marks them PAUSED.
        with self._tasks_lock:
            running = [t for t in self._tasks.values() if t.status in _ACTIVE_STATES]
        for task in running:
            task.status = TaskStatus.PAUSED
        for coro in list(self._coros):
            coro.cancel()
        if self._coros:
            await asyncio.gather(*self._coros, return_exceptions=True)
        self._coros.clear()
        self.persist_now()

    def set_concurrency(self, n: int) -> None:
        # In-flight downloads hold the old semaphore until they release; new
        # ones acquire the new one — no task is ever killed mid-download.
        self._sem = asyncio.Semaphore(max(1, n))

    # ---- task registry ---------------------------------------------------

    def _require_task(self, task_id: str | None) -> DownloadTask:
        with self._tasks_lock:
            task = self._tasks.get(task_id) if task_id else None
        if not task:
            raise ValueError("Task not found")
        return task

    def _add_task(self, task: DownloadTask) -> None:
        with self._tasks_lock:
            self._tasks[task.task_id] = task

    def _remove_task(self, task_id: str) -> None:
        with self._tasks_lock:
            self._tasks.pop(task_id, None)

    def _active_count(self) -> int:
        with self._tasks_lock:
            return sum(1 for t in self._tasks.values() if t.status in _ACTIVE_STATES)

    # ---- persistence -----------------------------------------------------

    def _write_tasks(self) -> None:
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        with self._tasks_lock:
            snapshot = {tid: t.to_dict() for tid, t in self._tasks.items()}
        tmp = self._tasks_file.with_suffix(f".tmp.{uuid.uuid4().hex[:8]}")
        try:
            with tmp.open("wb") as fh:
                fh.write(orjson.dumps(snapshot, option=orjson.OPT_INDENT_2))
                fh.flush()
                os.fsync(fh.fileno())
            with self._write_lock:
                os.replace(tmp, self._tasks_file)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def persist_now(self) -> None:
        try:
            self._write_tasks()
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to persist tasks: %s", exc)

    def persist_later(self) -> None:
        """Coalesce bursts of writes into a single debounced save."""
        if self._loop is None:
            return
        if self._save_task and not self._save_task.done():
            return
        self._save_task = self._loop.create_task(self._delayed_save())

    async def _delayed_save(self) -> None:
        try:
            await asyncio.sleep(0.5)
            await self._loop.run_in_executor(None, self._write_tasks)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            logger.error("Delayed save failed: %s", exc)

    # ---- broadcast -------------------------------------------------------

    def payload(self) -> str:
        with self._tasks_lock:
            tasks = [t.to_dict() for t in self._tasks.values()]
        return orjson.dumps(
            {
                "type": "tasks",
                "data": tasks,
                "health": {
                    "status": "healthy",
                    "yt_dlp_version": _YT_DLP_VERSION,
                    "active_downloads": self._active_count(),
                },
                "settings": self.settings.model_dump(),
            }
        ).decode()

    async def broadcast(self) -> None:
        payload = self.payload()
        for client in list(self._clients):
            try:
                await client.send_text(payload)
            except Exception:  # noqa: BLE001 - drop dead clients
                self._clients.discard(client)

    def _schedule_broadcast(self) -> None:
        """Thread-safe broadcast trigger (called from yt-dlp hooks)."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(self.broadcast(), self._loop)

    # ---- yt-dlp option building -----------------------------------------

    def build_opts(self, task: DownloadTask) -> JsonObj:
        target_dir = (
            task.custom_path
            or self.settings.categories.get(task.category)
            or self.settings.default_download_path
        )
        if not is_safe_path(target_dir, allowed_roots(self.settings)):
            raise PermissionError(
                f"Target directory '{target_dir}' is not within an allowed location."
            )
        ensure_target_dir(target_dir)

        codecs = self.settings.fallback_codecs or ["av01", "vp09", "avc01"]
        # Multiple vcodec sort fields implement a strict AV1 > VP9 > AVC
        # preference (each later field only breaks ties among earlier ones).
        format_sort = [f"vcodec:{c}" for c in codecs] + ["res", "abr", "ext:mp4:m4a"]

        if task.format_id and task.format_id != "direct_stream":
            if "+" in task.format_id or not task.is_video:
                format_spec = task.format_id
            else:
                # If there's no '+' but it's a video, it might be a combined format (like 18) 
                # or an old video-only request. We'll pass it directly, as appending +ba 
                # to a combined format causes duplicate downloads and merging.
                format_spec = task.format_id
        else:
            format_spec = "bv*+ba/b"

        # Always keep %(ext)s so yt-dlp picks the correct merged container,
        # avoiding double-extension bugs (video.mp4.mp4).
        outtmpl = (
            f"{Path(task.filename).stem}.%(ext)s"
            if task.filename
            else "%(title).200B.%(ext)s"
        )

        frag_dir = TMP_DIR / "fragments" / task.task_id
        frag_dir.mkdir(parents=True, exist_ok=True)

        postprocessors: list[JsonObj] = [{"key": "FFmpegMetadata", "add_chapters": True}]
        opts: JsonObj = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "merge_output_format": self.settings.merge_output_format,
            "format": format_spec,
            "format_sort": format_sort,
            "concurrent_fragment_downloads": self.settings.concurrent_fragments,
            "retries": 10,
            "fragment_retries": 10,
            "outtmpl": outtmpl,
            "writethumbnail": False,
            "ignoreerrors": False,
            "noplaylist": True,
            "progress_hooks": [lambda d: self._progress_hook(task, d)],
            "postprocessor_hooks": [lambda d: self._postprocessor_hook(task, d)],
            "postprocessors": postprocessors,
            "postprocessor_args": {
                "default": ["-nostdin"],
                "ffmpeg_i": ["-hwaccel", "auto"],
            },
            "buffersize": 262144,
            "paths": {"home": str(target_dir), "temp": str(frag_dir)},
            "continuedl": True,
        }

        if self.settings.embed_thumbnail:
            opts["writethumbnail"] = True
            postprocessors.append({"key": "EmbedThumbnail"})

        if self.settings.embed_subtitles:
            opts["writesubtitles"] = True
            opts["writeautomaticsub"] = True
            opts["subtitleslangs"] = [self.settings.subtitle_language]
            postprocessors.append({"key": "FFmpegEmbedSubtitle"})

        ffmpeg_location = find_ffmpeg_location()
        if ffmpeg_location:
            opts["ffmpeg_location"] = ffmpeg_location

        if self.settings.rate_limit_bytes_per_sec > 0:
            opts["ratelimit"] = self.settings.rate_limit_bytes_per_sec

        proxy = self.settings.proxy.strip()
        if proxy:
            opts["proxy"] = proxy

        browser = self.settings.cookies_from_browser
        if browser and browser != "none" and not task.headers:
            opts["cookiesfrombrowser"] = (browser,)

        if task.headers:
            opts["http_headers"] = task.headers

        return opts

    # ---- yt-dlp hooks (run in the executor thread) -----------------------

    def _progress_hook(self, task: DownloadTask, d: JsonObj) -> None:
        if task._cancel.is_set():
            raise DownloadError("Download cancelled by user")

        status = d.get("status")
        updates = {}
        if status == "downloading":
            updates = self._downloading_updates(task, d)
        elif status == "finished":
            part_bytes = d.get("total_bytes") or d.get("downloaded_bytes") or d.get("info_dict", {}).get("filesize") or 0
            task._prev_parts_bytes += part_bytes
            updates = {
                "downloaded_bytes": task._prev_parts_bytes,
                "total_bytes": max(task.total_bytes, task._prev_parts_bytes),
            }
        elif status == "error":
            updates = {"status": TaskStatus.ERROR}

        if updates:
            task.update(**updates)

        now = time.monotonic()
        if now - task._last_broadcast >= _BROADCAST_INTERVAL or status in (
            "finished",
            "error",
        ):
            task._last_broadcast = now
            self._schedule_broadcast()

    def _downloading_updates(self, task: DownloadTask, d: JsonObj) -> JsonObj:
        updates: JsonObj = {"status": TaskStatus.DOWNLOADING}
        info = d.get("info_dict") or {}
        title = info.get("title")
        if title and not task.has_custom_title:
            updates["title"] = sanitise_title(
                title, task.url, task.page_title, prefer_page=task.is_stream
            )

        speed = float(d.get("speed") or 0.0)
        updates["speed"] = speed
        total = int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
        downloaded = int(d.get("downloaded_bytes") or 0)

        # Calculate overall sizes
        overall_downloaded = task._prev_parts_bytes + downloaded
        overall_total = max(task.total_bytes, task._prev_parts_bytes + total)

        frag_idx = d.get("fragment_index")
        frag_cnt = d.get("fragment_count")
        if frag_idx is not None:
            updates["fragment_index"] = frag_idx + 1
        if frag_cnt is not None:
            updates["fragment_count"] = frag_cnt

        if frag_idx is not None and frag_cnt and frag_cnt > 0:
            # Fragment-based progress is reliable for segmented streams; blend
            # with byte-based progress (take the higher) so the bar never moves
            # backward when fragment sizes are uneven.
            frag_pct = (frag_idx + 1) / frag_cnt * 100.0
            usable = overall_total if overall_total > 0 else task.total_bytes
            if usable > 0 and overall_downloaded > 0:
                byte_pct = overall_downloaded / usable * 100.0
                updates["progress"] = min(max(frag_pct, byte_pct), 99.9)
                updates["total_bytes"] = usable
            else:
                updates["progress"] = min(frag_pct, 99.9)
        elif overall_total > 0:
            updates["total_bytes"] = overall_total
            updates["progress"] = min(overall_downloaded / overall_total * 100.0, 99.9)
        elif task.total_bytes > 0 and overall_downloaded > 0:
            updates["progress"] = min(overall_downloaded / task.total_bytes * 100.0, 99.9)

        updates["downloaded_bytes"] = overall_downloaded

        if speed > 0:
            ref_total = updates.get("total_bytes", task.total_bytes)
            if ref_total > 0:
                updates["eta"] = max(0.0, (ref_total - downloaded) / speed)

        filename = d.get("filename")
        if filename:
            updates["filename"] = filename
        return updates

    def _postprocessor_hook(self, task: DownloadTask, d: JsonObj) -> None:
        if task._cancel.is_set():
            raise DownloadError("Download cancelled by user")

        status = d.get("status")
        if status not in ("started", "finished"):
            return
        pp = d.get("postprocessor") or ""

        if status == "started":
            task.update(status=self._PP_STATUS.get(pp, TaskStatus.FINALIZING), progress=99.0)
            self._schedule_broadcast()
        elif status == "finished" and pp in ("Merger", "FFmpegMerger"):
            info = d.get("info_dict") or {}
            filepath = info.get("filepath") or info.get("_filename")
            updates: JsonObj = {"progress": 99.9}
            if filepath:
                updates["final_path"] = filepath
                updates["filename"] = filepath
            task.update(**updates)
            self._schedule_broadcast()

    # ---- download execution ---------------------------------------------

    def run_download(self, task: DownloadTask, opts: JsonObj) -> None:
        """Single yt-dlp pass with one browser-cookie fallback."""
        try:
            self._download_once(task, opts)
            return
        except DownloadError as exc:
            msg = str(exc).lower()
            if opts.get("cookiesfrombrowser") and "cookies" in msg and any(
                k in msg for k in ("database", "could not find", "keychain", "no such file")
            ):
                logger.warning(
                    "Browser cookie extraction failed (%s); retrying without cookies.",
                    exc,
                )
                self._download_once(
                    task, {k: v for k, v in opts.items() if k != "cookiesfrombrowser"}
                )
                return
            raise

    def _download_once(self, task: DownloadTask, opts: JsonObj) -> None:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(task.url, download=True)
        if not info:
            raise DownloadError("No info extracted")
        self._record_final_path(task, info, ydl)
        if not task.has_custom_title:
            task.title = sanitise_title(
                info.get("title") or "", task.url, task.page_title, prefer_page=task.is_stream
            )

    @staticmethod
    def _record_final_path(task: DownloadTask, info: Any, ydl: yt_dlp.YoutubeDL) -> None:
        requested = info.get("requested_downloads") or []
        if requested:
            filepath = requested[0].get("filepath") or requested[0].get("_filename") or ""
            if filepath:
                task.final_path = filepath
                task.filename = filepath
                return
        if not task.final_path:
            task.final_path = ydl.prepare_filename(info)

    async def _run_task(self, task: DownloadTask) -> None:
        try:
            # Wait outside the concurrency slot while paused (covers pause of a
            # queued task without starving other downloads of a slot).
            if not await self._await_ready(task):
                return
            async with self._sem:
                await self._execute(task)
        finally:
            task._task = None
            task._in_queue = False
            self._coros.discard(asyncio.current_task())  # type: ignore[arg-type]

    async def _await_ready(self, task: DownloadTask) -> bool:
        while not task._pause_event.is_set():
            if task._cancel.is_set():
                task.update(status=TaskStatus.CANCELLED, speed=0.0, eta=0.0)
                await self.broadcast()
                self.persist_later()
                return False
            await asyncio.sleep(0.2)
        return True

    async def _execute(self, task: DownloadTask) -> None:
        assert self._loop is not None
        task._is_running = True
        task._prev_parts_bytes = 0
        task.update(status=TaskStatus.DOWNLOADING, started_at=time.time(), speed=0.0)
        await self.broadcast()
        self.persist_later()

        try:
            opts = self.build_opts(task)
            await self._loop.run_in_executor(None, self.run_download, task, opts)
        except PermissionError as exc:
            task.update(status=TaskStatus.ERROR, error=str(exc))
        except DownloadError as exc:
            if task._hold:
                task._hold = False
                task._cancel.clear()
                task.update(status=TaskStatus.PAUSED, speed=0.0, eta=0.0)
            elif task._cancel.is_set():
                task.update(status=TaskStatus.CANCELLED)
            else:
                task.update(status=TaskStatus.ERROR, error=str(exc))
                logger.error("Download error for %s: %s", task.task_id, exc)
        except asyncio.CancelledError:
            if self._shutting_down:
                task.status = TaskStatus.PAUSED
            elif task._hold:
                task._hold = False
                task._cancel.clear()
                task.status = TaskStatus.PAUSED
            else:
                task.update(status=TaskStatus.ERROR, error="Worker terminated")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error for task %s", task.task_id)
            if task._cancel.is_set():
                task.update(status=TaskStatus.CANCELLED)
            else:
                task.update(status=TaskStatus.ERROR, error=f"Unexpected: {exc}")
        else:
            # The pass completed and wrote the file — a late pause/cancel that
            # lost the race shouldn't turn a success into CANCELLED/PAUSED.
            task._hold = False
            task._cancel.clear()
            task.update(status=TaskStatus.COMPLETED, progress=100.0)
        finally:
            task._is_running = False
            task.finished_at = time.time()
            if not self._shutting_down:
                await self._loop.run_in_executor(
                    None, cleanup_task_files, task, self.settings
                )
                await self.broadcast()
                self.persist_later()

    # ---- client connections ---------------------------------------------

    def add_client(self, client: Client) -> None:
        self._clients.add(client)

    def remove_client(self, client: Client) -> None:
        client.cancel_pending()
        self._clients.discard(client)

    # ---- WebSocket action dispatch --------------------------------------

    async def handle_action(
        self, client: Client, action: str, request_id: str | None, payload: JsonObj
    ) -> None:
        handlers: dict[
            str, Callable[[Client, JsonObj], Awaitable[Any]]
        ] = {
            "get_settings": self._a_get_settings,
            "save_settings": self._a_save_settings,
            "get_health": self._a_get_health,
            "extract": self._a_extract,
            "download": self._a_download,
            "cancel": self._a_cancel,
            "pause": self._a_pause,
            "resume": self._a_resume,
            "reveal": self._a_reveal,
            "delete": self._a_delete,
        }
        handler = handlers.get(action)
        try:
            if handler is None:
                raise ValueError(f"Unknown action: {action}")
            data = await handler(client, payload)
            await client.send_json(
                {
                    "type": "response",
                    "action": action,
                    "request_id": request_id,
                    "ok": True,
                    "data": data,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Action %s failed: %s", action, exc)
            await client.send_json(
                {
                    "type": "response",
                    "action": action,
                    "request_id": request_id,
                    "ok": False,
                    "error": str(exc),
                }
            )

    async def _a_get_settings(self, _client: Client, _payload: JsonObj) -> JsonObj:
        return self.settings.model_dump()

    async def _a_save_settings(self, _client: Client, payload: JsonObj) -> JsonObj:
        updates = SettingsUpdate.model_validate(payload).model_dump(exclude_unset=True)
        previous = self.settings.max_concurrent_downloads
        self.settings = self.settings.model_copy(update=updates)
        save_settings(self.settings)
        if updates.get("max_concurrent_downloads", previous) != previous:
            self.set_concurrency(self.settings.max_concurrent_downloads)
        await self.broadcast()
        return self.settings.model_dump()

    async def _a_get_health(self, _client: Client, _payload: JsonObj) -> JsonObj:
        return {
            "status": "healthy",
            "yt_dlp_version": _YT_DLP_VERSION,
            "active_downloads": self._active_count(),
        }

    async def _a_extract(self, _client: Client, payload: JsonObj) -> JsonObj:
        url = payload.get("url")
        if not url:
            raise ValueError("URL is required for extraction")
        if url.startswith("blob:"):
            raise ValueError("blob: URLs cannot be downloaded server-side.")

        page_title = payload.get("page_title")
        headers = payload.get("headers")
        loop = asyncio.get_running_loop()

        extraction_method = "yt-dlp"
        try:
            info = await loop.run_in_executor(
                None, extract_with_fallback, url, build_probe_opts(self.settings, headers)
            )
        except Exception:
            if not url.startswith(("http://", "https://")):
                raise
            info = self._direct_link_info(url)
            extraction_method = info["_method"]

        fmt_list = info.get("formats", []) or []
        extractor = (info.get("extractor") or "").lower()
        if extraction_method == "yt-dlp":
            is_stream = (
                formats_are_stream(fmt_list) if extractor == "generic" else bool(info.get("is_live"))
            )
            extraction_method = "stream" if is_stream else "yt-dlp"
        else:
            is_stream = extraction_method == "stream"

        best_audio_size = 0
        best_acodec = "none"
        
        # yt-dlp sorts formats from worst to best by default.
        # Find the best audio-only format for filesize calculations.
        for f in reversed(fmt_list):
            if f.get("vcodec", "none") == "none" and f.get("acodec", "none") != "none":
                best_audio_size = f.get("filesize") or f.get("filesize_estimate") or f.get("filesize_approx") or 0
                best_acodec = f.get("acodec", "none")
                break

        def _format_view_with_audio(fmt: JsonObj) -> JsonObj | None:
            vcodec = fmt.get("vcodec", "none")
            acodec = fmt.get("acodec", "none")
            if vcodec == "none" and acodec == "none":
                return None
                
            size = fmt.get("filesize") or fmt.get("filesize_estimate") or fmt.get("filesize_approx") or 0
            fmt_id = fmt.get("format_id")
            
            if vcodec != "none" and acodec == "none":
                size = (size + best_audio_size) if (size and best_audio_size) else (size or best_audio_size or 0)
                acodec = best_acodec
                if fmt_id:
                    fmt_id = f"{fmt_id}+ba"

            width, height = fmt.get("width"), fmt.get("height")
            return {
                "format_id": fmt_id,
                "ext": fmt.get("ext"),
                "resolution": fmt.get("resolution")
                or (f"{width}x{height}" if width and height else None),
                "vcodec": vcodec,
                "acodec": acodec,
                "fps": fmt.get("fps"),
                "filesize": size if size > 0 else None,
                "tbr": fmt.get("tbr"),
                "vbr": fmt.get("vbr"),
                "abr": fmt.get("abr"),
                "format_note": fmt.get("format_note", ""),
                "protocol": fmt.get("protocol", ""),
            }

        formats = [view for f in fmt_list if (view := _format_view_with_audio(f))]
        estimated = await self._estimate_total(url, headers, fmt_list, is_stream)

        return {
            "title": sanitise_title(
                info.get("title") or "", url, page_title, prefer_page=is_stream
            ),
            "duration": info.get("duration"),
            "uploader": info.get("uploader"),
            "thumbnail": info.get("thumbnail"),
            "url": url,
            "extraction_method": extraction_method,
            "formats": formats,
            "estimated_total_bytes": estimated,
        }

    @staticmethod
    def _direct_link_info(url: str) -> JsonObj:
        filename = os.path.basename(urlparse(url).path) or "stream"
        ext = os.path.splitext(filename)[1].lstrip(".") or "mp4"
        if not (ext.isalnum() and 2 <= len(ext) <= 5):
            ext = "mp4"
        low = ext.lower()
        method = "stream" if low in ("m3u8", "mpd", "ts") else "direct"
        return {
            "_method": method,
            "title": filename,
            "duration": None,
            "uploader": "Direct Link",
            "thumbnail": None,
            "formats": [
                {
                    "format_id": "direct_stream",
                    "ext": ext,
                    "protocol": "m3u8" if low == "m3u8" else "https",
                    "resolution": "unknown",
                    "vcodec": "direct",
                    "acodec": "direct",
                    "filesize": None,
                }
            ],
        }

    async def _estimate_total(
        self,
        url: str,
        headers: dict[str, str] | None,
        fmt_list: list[JsonObj],
        is_stream: bool,
    ) -> int | None:
        if is_stream:
            urls = [
                f.get("url", "")
                for f in fmt_list
                if f.get("protocol") in ("m3u8", "m3u8_native") and f.get("url")
            ]
            if not urls and url.lower().endswith((".m3u8", ".mpd")):
                urls = [url]
            if not urls:
                return None
            sizes = await asyncio.gather(*(estimate_stream_size(u, headers) for u in urls[:3]))
            valid = [s for s in sizes if s and s > 0]
            return max(valid) if valid else None
        for f in fmt_list:
            size = f.get("filesize") or f.get("filesize_estimate") or f.get("filesize_approx")
            if size and size > 0:
                return int(size)
        return None

    async def _a_download(self, _client: Client, payload: JsonObj) -> JsonObj:
        url = payload.get("url")
        if not url:
            raise ValueError("URL is required")
        if url.startswith("blob:"):
            raise ValueError("blob: URLs cannot be downloaded server-side.")

        filename = payload.get("filename")
        has_custom_title = bool(filename)
        title = (
            Path(filename).stem
            if has_custom_title
            else sanitise_title(
                "",
                url,
                payload.get("page_title"),
                prefer_page=bool(payload.get("is_stream"))
                or not payload.get("is_video", True),
            )
        )
        est = payload.get("estimated_total_bytes")

        task = DownloadTask(
            task_id=uuid.uuid4().hex,
            url=url,
            format_id=payload.get("format_id"),
            category=payload.get("category"),
            custom_path=payload.get("custom_path"),
            is_video=payload.get("is_video", True),
            page_title=payload.get("page_title"),
            is_stream=bool(payload.get("is_stream", False)),
            headers=payload.get("headers"),
            filename=filename or "",
            title=title,
            has_custom_title=has_custom_title,
            total_bytes=int(est) if isinstance(est, (int, float)) else 0,
        )
        task._pause_event.set()
        task._in_queue = True
        self._add_task(task)
        await self._queue.put(task)
        await self.broadcast()
        self.persist_later()
        return {"task_id": task.task_id, "status": task.status}

    async def _a_cancel(self, _client: Client, payload: JsonObj) -> JsonObj:
        task = self._require_task(payload.get("task_id"))
        task._hold = False
        task._cancel.set()
        task.update(status=TaskStatus.CANCELLED, speed=0.0, eta=0.0)
        await self.broadcast()
        self.persist_later()
        return {"task_id": task.task_id, "status": task.status}

    async def _a_pause(self, _client: Client, payload: JsonObj) -> JsonObj:
        task = self._require_task(payload.get("task_id"))
        if task.status == TaskStatus.QUEUED or task.status in _ACTIVE_STATES:
            if task._is_running:
                # yt-dlp can't suspend mid-flight: cancel the pass but hold the
                # task as PAUSED (resume will re-queue).
                task._hold = True
                task._cancel.set()
            else:
                task._pause_event.clear()
            task.update(status=TaskStatus.PAUSED, speed=0.0, eta=0.0)
        await self.broadcast()
        self.persist_later()
        return {"task_id": task.task_id, "status": task.status}

    async def _a_resume(self, _client: Client, payload: JsonObj) -> JsonObj:
        task = self._require_task(payload.get("task_id"))
        terminal = task.status in (
            TaskStatus.CANCELLED,
            TaskStatus.ERROR,
            TaskStatus.COMPLETED,
        )
        task._hold = False
        task._cancel.clear()
        task.update(status=TaskStatus.QUEUED, speed=0.0, eta=0.0, error="")
        if terminal:
            task.update(
                progress=0.0,
                downloaded_bytes=0,
                fragment_index=None,
                fragment_count=None,
            )
        # Re-queue only if nothing will already process this task — i.e. there is
        # no live coroutine and it isn't still pending in the queue. Otherwise we
        # would create a duplicate download.
        will_run = (task._task is not None and not task._task.done()) or task._in_queue
        task._pause_event.set()
        if not will_run:
            task._in_queue = True
            await self._queue.put(task)
        await self.broadcast()
        self.persist_later()
        return {"task_id": task.task_id, "status": task.status}

    async def _a_reveal(self, _client: Client, payload: JsonObj) -> JsonObj:
        task = self._require_task(payload.get("task_id"))
        path = task.final_path or task.filename
        select = bool(path and os.path.exists(path))
        if not select:
            path = (
                task.custom_path
                or self.settings.categories.get(task.category)
                or self.settings.default_download_path
            )
            if not path or not os.path.exists(path):
                raise ValueError("Path does not exist on disk")
        await asyncio.get_running_loop().run_in_executor(
            None, reveal_in_file_manager, path, select
        )
        return {"status": "ok"}

    async def _a_delete(self, _client: Client, payload: JsonObj) -> JsonObj:
        task_id = payload.get("task_id")
        task = self._require_task(task_id)
        delete_file = bool(payload.get("delete_file", False))

        task._hold = False
        task._cancel.set()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, cleanup_task_files, task, self.settings)

        if delete_file:
            for fp in (task.final_path, task.filename):
                if not fp:
                    continue
                try:
                    p = Path(fp)
                    if p.is_dir():
                        shutil.rmtree(p)
                    elif p.exists():
                        p.unlink(missing_ok=True)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not delete %s: %s", fp, exc)

        self._remove_task(task_id)  # type: ignore[arg-type]
        await self.broadcast()
        self.persist_later()
        return {"deleted": task_id}


# ──────────────────────────────────────────────
#  Temp-file cleanup
# ──────────────────────────────────────────────


def cleanup_task_files(task: DownloadTask, settings: AppSettings) -> None:
    if task.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
        return

    frag_dir = TMP_DIR / "fragments" / task.task_id
    if frag_dir.is_dir():
        try:
            shutil.rmtree(frag_dir)
        except OSError as exc:
            logger.warning("Could not remove fragment dir %s: %s", frag_dir, exc)

    if task.status != TaskStatus.CANCELLED or not task.filename:
        return

    target_dir = Path(
        task.custom_path
        or settings.categories.get(task.category)
        or settings.default_download_path
    )
    if not target_dir.exists():
        return
    stem = Path(task.filename).stem.lower()
    for item in target_dir.iterdir():
        if not item.is_file():
            continue
        name = item.name.lower()
        is_temp = (
            item.suffix.lower() in (".part", ".ytdl")
            or "part-fragment" in name
            or "-frag" in name
        )
        if is_temp and stem in item.stem.lower():
            try:
                item.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not remove %s: %s", item, exc)


def reveal_in_file_manager(path: str, select: bool) -> None:
    system = platform.system()
    if system == "Darwin":
        cmd = ["open", "-R", path] if select else ["open", path]
    elif system == "Windows":
        normalised = os.path.normpath(path)
        cmd = ["explorer", "/select,", normalised] if select else ["explorer", normalised]
    else:
        cmd = [os.path.dirname(os.path.abspath(path))] if select else [path]
        cmd = ["xdg-open", *cmd]
    subprocess.run(cmd, check=False)  # noqa: S603 - trusted local args


# ──────────────────────────────────────────────
#  FastAPI application
# ──────────────────────────────────────────────

manager = DownloadManager(load_settings())


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_system_path()
    manager.load_tasks()
    dispatcher = asyncio.create_task(manager.run())
    app.state.dispatcher = dispatcher
    try:
        yield
    finally:
        await manager.shutdown()
        with suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(dispatcher, timeout=2.0)


app = FastAPI(
    title="Media Acquisition Engine",
    version="3.0.0",
    lifespan=lifespan,
    default_response_class=ORJSONResponse,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:8000",
        "tauri://localhost",
        "https://tauri.localhost",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def disable_static_cache(request: Request, call_next: Callable[[Request], Awaitable[Any]]):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> Any:
    index_html = BASE_DIR / "dist" / "index.html"
    if index_html.exists():
        return FileResponse(str(index_html))
    return JSONResponse(
        {
            "status": "ok",
            "service": "Media Acquisition Engine",
            "mode": "sidecar" if getattr(sys, "frozen", False) else "api-only",
            "message": (
                "Frontend UI files (index.html) not found. "
                "Use the desktop app or run 'fastapi run' from the project root."
            ),
        }
    )


@app.websocket("/ws/progress")
async def websocket_progress(websocket: WebSocket) -> None:
    await websocket.accept()
    client = Client(websocket)
    manager.add_client(client)
    try:
        await client.send_text(manager.payload())
        while True:
            raw = await websocket.receive_text()
            try:
                msg = orjson.loads(raw)
            except orjson.JSONDecodeError as exc:
                await client.send_json(
                    {
                        "type": "response",
                        "action": None,
                        "request_id": None,
                        "ok": False,
                        "error": f"Invalid JSON payload: {exc}",
                    }
                )
                continue
            action = msg.get("action")
            if not action:
                continue
            task = asyncio.create_task(
                manager.handle_action(
                    client, action, msg.get("request_id"), msg.get("payload") or {}
                )
            )
            client.pending.add(task)
            task.add_done_callback(client.pending.discard)
    except WebSocketDisconnect:
        pass
    finally:
        manager.remove_client(client)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
