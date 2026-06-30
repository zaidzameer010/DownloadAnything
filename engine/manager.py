import asyncio
import functools
import logging
import os
import platform
import shutil
import subprocess
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
    JsonObj,
    AppSettings,
    SettingsUpdate,
    TMP_DIR,
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
    probe_url_metadata,
)
from engine.constants import GENERIC_STREAM_NAMES as _GENERIC_STREAM_NAMES
from engine.title_extractor import sanitise_title
from engine.downloader import YTDownloader, formats_are_stream

logger = logging.getLogger("dma-engine")


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
        "extractor_args": {"generic": {"impersonate": ["chrome"]}},
    }
    if headers:
        filtered_headers = {k: v for k, v in headers.items() if k.lower() != "cookie"}
        if filtered_headers:
            opts["http_headers"] = filtered_headers
    return opts


def _extract_once(url: str, opts: JsonObj) -> Any:
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl._ies.pop("KnownPiracy", None)
        ydl._ies.pop("KnownDRM", None)
        return ydl.extract_info(url, download=False)


def extract_with_fallback(url: str, opts: JsonObj) -> Any:
    """Extract with graceful fallbacks for anti-piracy errors."""
    try:
        return _extract_once(url, opts)
    except DownloadError as exc:
        msg = str(exc).lower()
        if "primarily used for piracy" in msg:
            return _extract_once(url, {**opts, "allowed_extractors": ["generic"]})
        raise


def cleanup_task_files(task: DownloadTask, settings: AppSettings) -> None:
    if task.status == TaskStatus.PAUSED:
        return

    frag_dir = TMP_DIR / "fragments" / task.task_id
    if frag_dir.is_dir():
        try:
            shutil.rmtree(frag_dir)
        except OSError as exc:
            logger.warning("Could not remove fragment dir %s: %s", frag_dir, exc)

    if task.status == TaskStatus.CANCELLED:
        for fp in (task.final_path, task.filename):
            if fp:
                try:
                    p = Path(fp).resolve()
                    if p.is_file():
                        p.unlink(missing_ok=True)
                    elif p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                except Exception as exc:
                    logger.warning("Could not clean up partial file %s: %s", fp, exc)

        target_dir = Path(
            task.custom_path
            or settings.categories.get(task.category)
            or settings.default_download_path
        ).resolve()

        if target_dir.exists() and target_dir.is_dir():
            stems = []
            if task.filename:
                stems.append(Path(task.filename).stem.lower())
                stems.append(Path(task.filename).name.lower())
            if task.title:
                import yt_dlp.utils
                stems.append(yt_dlp.utils.sanitize_filename(task.title).lower())
                stems.append(task.title.lower())
            
            stems = list({s for s in stems if s})

            for item in target_dir.iterdir():
                if not item.is_file():
                    continue
                name = item.name.lower()
                is_temp_file = (
                    item.suffix.lower() in (".part", ".ytdl", ".aria2", ".temp", ".tmp")
                    or "part-fragment" in name
                    or "-frag" in name
                )
                if any(s in name for s in stems) and (is_temp_file or name.endswith(".aria2")):
                    try:
                        item.unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning("Could not remove temp file %s: %s", item, exc)


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
        self._settings_dict = settings.model_dump()
        self._tasks_file = TMP_DIR / "tasks.json"
        self._tasks: dict[str, DownloadTask] = {}
        self._queue: asyncio.Queue[DownloadTask] = asyncio.Queue()
        self._clients: set[Client] = set()
        self._coros: set[asyncio.Task[None]] = set()
        self._worker_sem_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dispatcher: asyncio.Task[None] | None = None
        self._save_task: asyncio.Task[None] | None = None
        self._shutting_down = False

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
                using_aria2c=td.get("using_aria2c", False),
                prev_parts_bytes=td.get("prev_parts_bytes", 0),
            )
        self._tasks = loaded

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._worker_sem_event = asyncio.Event()
        self._worker_sem_event.set()
        self._dispatcher = asyncio.current_task()
        await self._worker_dispatcher()

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
        if self._loop and self._worker_sem_event:
            self._loop.call_soon_threadsafe(self._worker_sem_event.set)

    def _require_task(self, task_id: str | None) -> DownloadTask:
        if not task_id:
            raise ValueError("Task ID required")
        task = self._tasks.get(task_id)
        if not task:
            raise KeyError(f"No task with ID: {task_id}")
        return task

    def _add_task(self, task: DownloadTask) -> None:
        self._tasks[task.task_id] = task

    def _remove_task(self, task_id: str) -> None:
        self._tasks.pop(task_id, None)

    def _active_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t._is_running)

    def _write_tasks(self) -> None:
        serialized = {tid: t.to_dict() for tid, t in self._tasks.items()}
        try:
            TMP_DIR.mkdir(parents=True, exist_ok=True)
            self._tasks_file.write_bytes(orjson.dumps(serialized))
        except OSError as exc:
            logger.error("Could not persist tasks: %s", exc)

    def persist_now(self) -> None:
        if self._save_task:
            self._save_task.cancel()
            self._save_task = None
        self._write_tasks()

    def persist_later(self) -> None:
        if self._save_task is None and not self._shutting_down:
            self._save_task = asyncio.create_task(self._delayed_save())

    async def _delayed_save(self) -> None:
        await asyncio.sleep(0.5)
        self._save_task = None
        self._write_tasks()

    def payload(self) -> str:
        try:
            import yt_dlp
            yt_dlp_version = yt_dlp.version.__version__
        except Exception:
            yt_dlp_version = "unknown"

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
                "settings": self._settings_dict,
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


    async def _run_task(self, task: DownloadTask) -> None:
        if task.status == TaskStatus.CANCELLED:
            return
        await self._execute(task)

    async def _execute(self, task: DownloadTask) -> None:
        assert self._loop is not None
        task._is_running = True
        task.update(status=TaskStatus.DOWNLOADING, started_at=time.time(), speed=0.0)
        await self.broadcast()
        self.persist_later()

        try:
            downloader = YTDownloader(self.settings, self._loop, self.broadcast)
            opts = downloader.build_opts(task)
            await self.broadcast()
            await self._loop.run_in_executor(None, downloader.run_download, task, opts)
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
                metadata = await probe_url_metadata(url, headers)
                if metadata:
                    is_stream = metadata["is_stream"]
                    if is_stream:
                        logger.debug("Probed URL is a stream manifest, falling back to yt-dlp for resolution extraction: %s", url)
                    else:
                        filename = metadata["filename"]
                        filesize = metadata["filesize"]
                        extraction_method = "direct"
                        info = {
                            "title": filename,
                            "duration": None,
                            "uploader": "Direct Link",
                            "thumbnail": None,
                            "formats": [
                                {
                                    "format_id": "direct_stream",
                                    "ext": os.path.splitext(filename)[1].lstrip(".") or "mp4",
                                    "protocol": "https",
                                    "resolution": "unknown",
                                    "vcodec": "direct",
                                    "acodec": "direct",
                                    "filesize": filesize,
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
        path = urlparse(url).path.rstrip("/")
        filename = os.path.basename(path) or "stream"
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
                metadata = await probe_url_metadata(url, headers)
                if metadata:
                    if not filename:
                        filename = metadata["filename"]
                    if not est or est == 0:
                        est = metadata["filesize"] or 0
            except Exception as exc:
                logger.debug("Probe in _a_download failed: %s", exc)

        payload_title = payload.get("title")
        payload_page_title = payload.get("page_title")
        is_media = is_stream or payload.get("is_video", True)

        sanitised_page_title = None
        if payload_page_title and payload_page_title.strip() and payload_page_title.strip() != "Pending…":
            cleaned = sanitise_title(payload_page_title, url, prefer_page=True)
            if cleaned and cleaned.lower() not in ("stream", "download", "video", "audio", "unknown media") and cleaned != urlparse(url).hostname:
                sanitised_page_title = cleaned

        if is_media and sanitised_page_title:
            title = sanitised_page_title
            has_custom_title = False
        elif payload_title and payload_title.strip() and payload_title.strip() != "Pending…":
            title = payload_title.strip()
            has_custom_title = True
        else:
            prefer_metadata_title = is_stream or payload.get("is_video", True)
            if filename and not prefer_metadata_title:
                title = Path(filename).stem
                has_custom_title = True
            else:
                has_custom_title = False
                title = sanitise_title(
                    "",
                    url,
                    payload_page_title,
                    prefer_page=is_stream
                    or not payload.get("is_video", True),
                )

        # Resolve target directory
        target_dir = Path(
            payload.get("custom_path")
            or self.settings.categories.get(payload.get("category"))
            or self.settings.default_download_path
        ).resolve()

        # Collision Check: Same URL, title, or filename on disk (auto-increment name)
        base_title = title
        title_counter = 1
        collided = False
        while any(t.url == url or t.title == title for t in self._tasks.values()):
            title = f"{base_title} ({title_counter})"
            title_counter += 1
            collided = True

        if collided:
            has_custom_title = True

        if filename:
            stem = Path(filename).stem
            ext = Path(filename).suffix
            file_counter = 1
            while any(t.url == url or t.filename == filename for t in self._tasks.values()) or (target_dir / filename).exists():
                filename = f"{stem} ({file_counter}){ext}"
                file_counter += 1

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
        if not task._is_running:
            await asyncio.get_running_loop().run_in_executor(
                None, cleanup_task_files, task, self.settings
            )
        await self.broadcast()
        self.persist_later()
        return {"task_id": task.task_id, "status": task.status}

    async def _a_pause(self, _client: Client, payload: JsonObj) -> JsonObj:
        task = self._require_task(payload.get("task_id"))
        if task.status == TaskStatus.QUEUED or task.status in _ACTIVE_STATES:
            if task._is_running:
                task._hold = True
                task._cancel.set()
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
        
        if not task._is_running:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, cleanup_task_files, task, self.settings)
            
            if delete_file:
                def _delete_files():
                    for fp in (task.final_path, task.filename):
                        if not fp:
                            continue
                        try:
                            p = Path(fp)
                            if p.is_dir():
                                shutil.rmtree(p)
                            elif p.exists():
                                p.unlink(missing_ok=True)
                        except Exception as exc:
                            logger.warning("Could not delete %s: %s", fp, exc)
                await loop.run_in_executor(None, _delete_files)

        self._remove_task(task_id)
        await self.broadcast()
        self.persist_later()
        return {"deleted": task_id}

    async def _worker_dispatcher(self) -> None:
        try:
            while True:
                try:
                    while self._active_count() >= self.settings.max_concurrent_downloads:
                        await self._worker_sem_event.wait()
                        self._worker_sem_event.clear()

                    task = await self._queue.get()

                    # If task was paused, cancelled, or deleted, discard it from the runner
                    if task.status in (TaskStatus.PAUSED, TaskStatus.CANCELLED) or task.task_id not in self._tasks:
                        task._in_queue = False
                        self._queue.task_done()
                        continue

                    while self._active_count() >= self.settings.max_concurrent_downloads:
                        await self._worker_sem_event.wait()
                        self._worker_sem_event.clear()

                    task._is_running = True
                    task._in_queue = False
                    t = asyncio.create_task(self._run_concurrent_worker(task))
                    self._coros.add(t)
                    t.add_done_callback(self._coros.discard)
                    self._queue.task_done()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Error in worker dispatcher loop: %s", exc)
                    await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    async def _run_concurrent_worker(self, task: DownloadTask) -> None:
        task._task = asyncio.current_task()
        try:
            await self._run_task(task)
        except asyncio.CancelledError:
            pass
        finally:
            task._task = None
            task._is_running = False
            task.finished_at = time.time()
            if self._worker_sem_event:
                self._worker_sem_event.set()
            if not self._shutting_down:
                should_cleanup = task.status not in (TaskStatus.PAUSED,)
                if should_cleanup:
                    await self._loop.run_in_executor(
                        None, cleanup_task_files, task, self.settings
                    )
                await self.broadcast()
                self.persist_later()
