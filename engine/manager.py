import asyncio
import functools
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import orjson
import yt_dlp
from fastapi import WebSocket
from yt_dlp.utils import DownloadError

from engine.config import (
    APP_DATA_DIR,
    DEFAULT_UA,
    JsonObj,
    AppSettings,
    SettingsUpdate,
    TMP_DIR,
    load_settings,
    save_settings,
)
from engine.models import (
    _ACTIVE_STATES,
    _BROADCAST_INTERVAL,
    DownloadTask,
    TaskStatus,
)
from engine.probing import (
    estimate_stream_size,
    guess_extension_from_mime,
    parse_content_disposition,
    probe_direct_link,
)

logger = logging.getLogger("dma-engine")


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
        "hls", "dash", "m3u8", "mpd", "ts", "chunk",
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
    
    # Extract the base stem to check against generic names (e.g. "master.m3u8" -> "master")
    name_part = stripped.split("?")[0]
    stem_part = os.path.splitext(name_part)[0].strip()
    
    is_generic = (
        not stem_part 
        or stem_part.lower() in _GENERIC_STREAM_NAMES 
        or stripped.lower() in _GENERIC_STREAM_NAMES
    )

    if prefer_page and page_title and page_title.strip():
        return page_title.strip()
    if stripped and not is_generic:
        return stripped
    if page_title and page_title.strip():
        return page_title.strip()
    parsed = urlparse(url)
    host = parsed.hostname or ""
    stem = os.path.splitext(os.path.basename(parsed.path.rstrip("/")))[0]
    # Check if URL stem is also generic
    url_stem_part = stem.split("?")[0]
    url_stem = os.path.splitext(url_stem_part)[0].strip()
    url_is_generic = (
        not url_stem 
        or url_stem.lower() in _GENERIC_STREAM_NAMES 
        or stem.lower() in _GENERIC_STREAM_NAMES
    )
    if stem and not url_is_generic:
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
        with self._tasks_lock:
            self._tasks = loaded

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._dispatcher = asyncio.create_task(self._worker_dispatcher())

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._dispatcher:
            self._dispatcher.cancel()
        if self._save_task:
            self._save_task.cancel()

        # Gather running downloads
        running = [t for t in self._tasks.values() if t._is_running]
        for task in running:
            task._hold = True
            task._cancel.set()

        # Wait for workers to clean up/finish
        if running:
            logger.info("Suspending %d running downloads...", len(running))
            for _ in range(30):
                if not any(t._is_running for t in running):
                    break
                await asyncio.sleep(0.1)

        # Cancel remaining tasks in dispatcher group
        for c in self._coros:
            c.cancel()
        if self._coros:
            await asyncio.gather(*self._coros, return_exceptions=True)

        self.persist_now()

    def set_concurrency(self, n: int) -> None:
        self._sem = asyncio.Semaphore(max(1, n))

    def _require_task(self, task_id: str | None) -> DownloadTask:
        if not task_id:
            raise ValueError("Task ID required")
        with self._tasks_lock:
            task = self._tasks.get(task_id)
        if not task:
            raise KeyError(f"No task with ID: {task_id}")
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

    def _write_tasks(self) -> None:
        with self._tasks_lock:
            serialized = {tid: t.to_dict() for tid, t in self._tasks.items()}
        try:
            TMP_DIR.mkdir(parents=True, exist_ok=True)
            self._tasks_file.write_bytes(orjson.dumps(serialized))
        except OSError as exc:
            logger.error("Could not persist tasks: %s", exc)

    def persist_now(self) -> None:
        with self._write_lock:
            if self._save_task:
                self._save_task.cancel()
                self._save_task = None
            self._write_tasks()

    def persist_later(self) -> None:
        with self._write_lock:
            if self._save_task is None and not self._shutting_down:
                self._save_task = asyncio.create_task(self._delayed_save())

    async def _delayed_save(self) -> None:
        await asyncio.sleep(0.5)
        with self._write_lock:
            self._save_task = None
        self._write_tasks()

    def payload(self) -> str:
        try:
            import yt_dlp
            yt_dlp_version = yt_dlp.version.__version__
        except Exception:
            yt_dlp_version = "unknown"

        with self._tasks_lock:
            ordered = sorted(
                self._tasks.values(),
                key=lambda t: t.started_at or 0.0,
                reverse=True,
            )
            serialized = [t.to_dict() for t in ordered]
        return orjson.dumps(
            {
                "type": "tasks",
                "data": serialized,
                "settings": self.settings.model_dump(),
                "health": {
                    "active_workers": f"{self._active_count()} / {self.settings.max_concurrent_downloads}",
                    "yt_dlp_version": yt_dlp_version,
                },
            }
        ).decode()

    async def broadcast(self) -> None:
        if not self._clients:
            return
        text = self.payload()
        # Make a copy to prevent mutation during iteration
        for c in list(self._clients):
            try:
                await c.send_text(text)
            except Exception:  # noqa: BLE001
                self.remove_client(c)

    def _schedule_broadcast(self) -> None:
        if not self._clients or self._shutting_down:
            return
        assert self._loop is not None
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self.broadcast())
        )

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
        format_sort = [f"vcodec:{c}" for c in codecs] + ["res", "abr", "ext:mp4:m4a"]

        if task.format_id and task.format_id != "direct_stream":
            if "+" in task.format_id or not task.is_video:
                format_spec = task.format_id
            else:
                format_spec = task.format_id
        else:
            format_spec = "bv*+ba/b"

        if task.filename:
            stem = Path(task.filename).stem
            safe_stem = stem.replace("%", "%%")
            outtmpl = f"{safe_stem}.%(ext)s"
        else:
            import yt_dlp.utils
            sanitized = yt_dlp.utils.sanitize_filename(task.title).strip()
            if sanitized and sanitized != "Pending…":
                safe_stem = sanitized.replace("%", "%%")
                outtmpl = f"{safe_stem}.%(ext)s"
            else:
                outtmpl = "%(title).200B.%(ext)s"

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
        # Re-verify task state inside queue runner context
        if not await self._await_ready(task):
            task._in_queue = False
            return
        try:
            await self._execute(task)
        finally:
            task._in_queue = False

    async def _await_ready(self, task: DownloadTask) -> bool:
        while True:
            if task.status == TaskStatus.CANCELLED:
                return False
            # If paused, wait for resume (sets _pause_event)
            await task._pause_event.wait()
            if task.status == TaskStatus.QUEUED:
                return True
            # Loop again if status was updated elsewhere (e.g. paused/cancelled)

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
        from main import _YT_DLP_VERSION
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

        info = None
        extraction_method = None

        if url.startswith(("http://", "https://")):
            try:
                probe_res = await probe_direct_link(url, headers)
                if probe_res:
                    resp_url = probe_res["url"]
                    resp_headers = probe_res["headers"]
                    ct = resp_headers.get("Content-Type", "").lower()
                    
                    if "text/html" not in ct and "application/xhtml+xml" not in ct:
                        # Extract filename from Content-Disposition
                        filename = parse_content_disposition(resp_headers.get("Content-Disposition", ""))
                        if not filename:
                            filename = os.path.basename(urlparse(resp_url).path) or "download"
                            filename = unquote(filename)
                        
                        filename = os.path.basename(filename).strip()
                        if not os.path.splitext(filename)[1]:
                            ext = guess_extension_from_mime(ct)
                            if ext:
                                filename = f"{filename}.{ext}"
                        
                        # Extract filesize
                        filesize = None
                        cr = resp_headers.get("Content-Range", "")
                        if "/" in cr:
                            total_str = cr.split("/")[-1].strip()
                            if total_str.isdigit():
                                filesize = int(total_str)
                        if filesize is None:
                            cl = resp_headers.get("Content-Length", "")
                            if cl.isdigit():
                                filesize = int(cl)
                        
                        is_stream_mime = "mpegurl" in ct or "dash+xml" in ct or resp_url.lower().endswith((".m3u8", ".mpd"))
                        extraction_method = "stream" if is_stream_mime else "direct"
                        
                        info = {
                            "title": filename,
                            "duration": None,
                            "uploader": "Direct Link" if extraction_method == "direct" else "Stream Link",
                            "thumbnail": None,
                            "formats": [
                                {
                                    "format_id": "direct_stream",
                                    "ext": os.path.splitext(filename)[1].lstrip(".") or "mp4",
                                    "protocol": "m3u8" if extraction_method == "stream" else "https",
                                    "resolution": "unknown",
                                    "vcodec": "direct",
                                    "acodec": "direct",
                                    "filesize": filesize if filesize and filesize > 0 else None,
                                }
                            ],
                        }
            except Exception as exc:
                logger.debug("Pre-probe failed: %s", exc)

        if info is None:
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
        est = payload.get("estimated_total_bytes")
        is_stream = bool(payload.get("is_stream", False))
        headers = payload.get("headers")

        if not is_stream and (not est or est == 0 or not filename) and url.startswith(("http://", "https://")):
            try:
                probe_res = await probe_direct_link(url, headers)
                if probe_res:
                    resp_url = probe_res["url"]
                    resp_headers = probe_res["headers"]
                    ct = resp_headers.get("Content-Type", "").lower()
                    if "text/html" not in ct and "application/xhtml+xml" not in ct:
                        if not filename:
                            filename = parse_content_disposition(resp_headers.get("Content-Disposition", ""))
                            if not filename:
                                filename = os.path.basename(urlparse(resp_url).path) or "download"
                                filename = unquote(filename)
                            filename = os.path.basename(filename).strip()
                            if not os.path.splitext(filename)[1]:
                                ext = guess_extension_from_mime(ct)
                                if ext:
                                    filename = f"{filename}.{ext}"
                        
                        if not est or est == 0:
                            filesize = None
                            cr = resp_headers.get("Content-Range", "")
                            if "/" in cr:
                                total_str = cr.split("/")[-1].strip()
                                if total_str.isdigit():
                                    filesize = int(total_str)
                            if filesize is None:
                                cl = resp_headers.get("Content-Length", "")
                                if cl.isdigit():
                                    filesize = int(cl)
                            if filesize and filesize > 0:
                                est = filesize
            except Exception as exc:
                logger.debug("Probe in _a_download failed: %s", exc)

        payload_title = payload.get("title")
        if payload_title and payload_title.strip() and payload_title.strip() != "Pending…":
            title = payload_title.strip()
            has_custom_title = True
        else:
            has_custom_title = bool(filename)
            title = (
                Path(filename).stem
                if has_custom_title
                else sanitise_title(
                    "",
                    url,
                    payload.get("page_title"),
                    prefer_page=is_stream
                    or not payload.get("is_video", True),
                )
            )

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

    async def _worker_dispatcher(self) -> None:
        try:
            while True:
                task = await self._queue.get()
                t = asyncio.create_task(self._run_concurrent_worker(task))
                self._coros.add(t)
                t.add_done_callback(self._coros.discard)
                self._queue.task_done()
        except asyncio.CancelledError:
            pass

    async def _run_concurrent_worker(self, task: DownloadTask) -> None:
        async with self._sem:
            task._task = asyncio.current_task()
            try:
                await self._run_task(task)
            except asyncio.CancelledError:
                pass
            finally:
                task._task = None
