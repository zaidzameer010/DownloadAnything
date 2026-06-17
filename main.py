"""
main.py — Enterprise Media Acquisition Engine
============================================
FastAPI + yt-dlp + asyncio.Queue + SSE progress + AV1 codec priority.
Author: Systems Automation Team
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, AsyncGenerator

import yt_dlp
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ──────────────────────────────────────────────
#  Configuration & Persistence
# ──────────────────────────────────────────────
def get_app_data_dir() -> Path:
    import platform
    home = Path.home()
    if platform.system() == "Darwin":
        p = home / "Library" / "Application Support" / "DownloadAnything"
    elif platform.system() == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            p = Path(appdata) / "DownloadAnything"
        else:
            p = home / "AppData" / "Roaming" / "DownloadAnything"
    else:
        p = home / ".config" / "DownloadAnything"
    p.mkdir(parents=True, exist_ok=True)
    return p

def get_default_download_path() -> Path:
    downloads = Path.home() / "Downloads"
    if downloads.exists():
        return downloads / "DownloadAnything"
    return Path.home() / "DownloadAnything"

APP_DATA_DIR = get_app_data_dir()
SETTINGS_FILE = APP_DATA_DIR / "settings.json"
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "dist-frontend" / "static"
TMP_DIR = APP_DATA_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS: Dict[str, Any] = {
    "max_concurrent_downloads": 3,
    "fallback_codecs": ["av01", "vp09", "avc01"],
    "default_download_path": str(get_default_download_path()),
    "categories": {
        "Videos":   str(get_default_download_path() / "videos"),
        "Courses":  str(get_default_download_path() / "courses"),
        "Music":    str(get_default_download_path() / "music"),
        "Cinematic": str(get_default_download_path() / "cinematic"),
    },
    "rate_limit_bytes_per_sec": 0,   # 0 = unlimited
    "merge_output_format": "mp4",
}

def load_settings() -> Dict[str, Any]:
    if SETTINGS_FILE.exists():
        try:
            with SETTINGS_FILE.open("r", encoding="utf-8") as fh:
                merged = {**DEFAULT_SETTINGS, **json.load(fh)}
                return merged
        except (json.JSONDecodeError, OSError):
            return DEFAULT_SETTINGS.copy()
    save_settings(DEFAULT_SETTINGS)
    return DEFAULT_SETTINGS.copy()

def save_settings(data: Dict[str, Any]) -> None:
    with SETTINGS_FILE.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)

SETTINGS: Dict[str, Any] = load_settings()

# ──────────────────────────────────────────────
#  Task Model & In-Memory State
# ──────────────────────────────────────────────
@dataclass
class DownloadTask:
    task_id: str
    url: str
    format_id: Optional[str]
    category: Optional[str]
    custom_path: Optional[str]
    title: str = "Pending…"
    status: str = "queued"        # queued | downloading | completed | error | cancelled
    speed: float = 0.0            # bytes/sec
    progress: float = 0.0         # 0..100
    eta: float = 0.0              # seconds
    total_bytes: int = 0
    downloaded_bytes: int = 0
    filename: str = ""
    final_path: str = ""
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    is_video: bool = True
    page_title: Optional[str] = None
    is_stream: bool = False
    _cancel: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _paused: bool = field(default=False, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "url": self.url,
            "format_id": self.format_id,
            "category": self.category,
            "custom_path": self.custom_path,
            "is_video": self.is_video,
            "page_title": self.page_title,
            "is_stream": self.is_stream,
            "title": self.title,
            "status": self.status,
            "speed": self.speed,
            "progress": self.progress,
            "eta": self.eta,
            "total_bytes": self.total_bytes,
            "downloaded_bytes": self.downloaded_bytes,
            "filename": self.filename,
            "final_path": self.final_path,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

TASKS: Dict[str, DownloadTask] = {}
TASK_QUEUE: "asyncio.Queue[DownloadTask]" = asyncio.Queue()
WEBSOCKET_SUBSCRIBERS: List[WebSocket] = []

# ──────────────────────────────────────────────
#  Pydantic Schemas
# ──────────────────────────────────────────────
class ExtractRequest(BaseModel):
    url: str
    page_title: Optional[str] = None

class DownloadRequest(BaseModel):
    url: str
    format_id: Optional[str] = None
    category: Optional[str] = None
    custom_path: Optional[str] = None
    is_video: Optional[bool] = True
    page_title: Optional[str] = None
    is_stream: Optional[bool] = False

class SettingsUpdate(BaseModel):
    max_concurrent_downloads: Optional[int] = None
    fallback_codecs: Optional[List[str]] = None
    default_download_path: Optional[str] = None
    categories: Optional[Dict[str, str]] = None
    rate_limit_bytes_per_sec: Optional[int] = None
    merge_output_format: Optional[str] = Field(default=None, pattern="^(mp4|mkv|webm)$")

# ──────────────────────────────────────────────
#  yt-dlp Options Builder
# ──────────────────────────────────────────────
def build_ydl_options(task: DownloadTask, extract_only: bool = False) -> Dict[str, Any]:
    target_dir = task.custom_path or (
        SETTINGS["categories"].get(task.category) if task.category else SETTINGS["default_download_path"]
    )
    try:
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        # Test write access
        test_file = Path(target_dir) / f".write_test_{uuid.uuid4().hex[:6]}"
        test_file.write_bytes(b"x")
        test_file.unlink(missing_ok=True)
    except (PermissionError, OSError) as exc:
        raise PermissionError(f"Target directory '{target_dir}' is not writable: {exc}") from exc

    codec_pref = SETTINGS.get("fallback_codecs", ["av01", "vp09", "avc01"])
    format_sort = [f"vcodec:{c}" for c in codec_pref] + ["res", "ext:mp4:m4a"]

    loop = asyncio.get_running_loop()
    if task.format_id and task.format_id != "direct_stream":
        format_spec = f"{task.format_id}+ba/b" if task.is_video else task.format_id
    else:
        format_spec = "bv*+ba/b"

    # Locate ffmpeg/ffprobe to ensure yt-dlp can merge/embed streams
    ffmpeg_location = None
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin:
        ffmpeg_location = str(Path(ffmpeg_bin).parent)
    else:
        import platform
        if platform.system() == "Darwin":
            for p in ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]:
                if (Path(p) / "ffmpeg").exists():
                    ffmpeg_location = p
                    break
        elif platform.system() == "Windows":
            for p in ["C:\\ffmpeg\\bin", "C:\\Program Files\\ffmpeg\\bin"]:
                if (Path(p) / "ffmpeg.exe").exists():
                    ffmpeg_location = p
                    break

    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "merge_output_format": SETTINGS.get("merge_output_format", "mp4"),
        "format": format_spec,
        "format_sort": format_sort,
        "concurrent_fragment_downloads": 16,
        "retries": 10,
        "fragment_retries": 10,
        "outtmpl": "%(title).200B.%(ext)s",
        "writethumbnail": False,
        "ignoreerrors": False,
        "noplaylist": True,
        "progress_hooks": [] if extract_only else [lambda d: _hook_sink(task, d, loop)],
        "postprocessors": [
            {"key": "FFmpegMetadata", "add_chapters": True},
        ],
    }

    # Route fragment temp files to the workspace temp folder so they don't
    # accumulate inside the user's actual download folder.
    fragment_cache = TMP_DIR / "fragments"
    fragment_cache.mkdir(parents=True, exist_ok=True)
    opts["paths"] = {
        "home": str(target_dir),
        "temp": str(fragment_cache),
    }
    opts["continuedl"] = True

    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location

    # Apply rate limit if configured (0 = unlimited)
    rate_limit = SETTINGS.get("rate_limit_bytes_per_sec", 0)
    if rate_limit and rate_limit > 0:
        opts["ratelimit"] = rate_limit

    return opts

# ──────────────────────────────────────────────
#  Stream Title Sanitisation
# ──────────────────────────────────────────────
# HLS/DASH manifests typically have their top-level playlist named "master",
# "index", "playlist", or similarly uninformative strings.  When yt-dlp
# returns one of these as the title, we derive a friendlier name from the
# URL's hostname + path stem instead.
_GENERIC_STREAM_NAMES = frozenset({
    "master", "index", "playlist", "stream", "video", "audio",
    "media", "manifest", "chunklist", "output", "main", "live",
})

def _sanitise_stream_title(title: str, url: str, page_title: Optional[str] = None, prefer_page_title: bool = False) -> str:
    """Return a human-readable title, falling back to page_title or a URL-derived name
    when the raw title is a generic manifest placeholder."""
    stripped = title.strip()
    
    if prefer_page_title and page_title:
        pt = page_title.strip()
        # Clean common page title suffixes (e.g. "Watch Movie | StreamingSite")
        for sep in (" | ", " - ", " – ", " — "):
            if sep in pt:
                parts = [p.strip() for p in pt.split(sep)]
                if len(parts[0]) > 3:
                    pt = parts[0]
                    break
        # Clean common prefix words
        for prefix in ("Watch ", "Download "):
            if pt.lower().startswith(prefix.lower()) and len(pt) > len(prefix):
                pt = pt[len(prefix):]
        if pt:
            return pt

    if stripped and stripped.lower() not in _GENERIC_STREAM_NAMES:
        return stripped

    if page_title:
        pt = page_title.strip()
        # Clean common page title suffixes (e.g. "Watch Movie | StreamingSite")
        for sep in (" | ", " - ", " – ", " — "):
            if sep in pt:
                parts = [p.strip() for p in pt.split(sep)]
                if len(parts[0]) > 3:
                    pt = parts[0]
                    break
        # Clean common prefix words
        for prefix in ("Watch ", "Download "):
            if pt.lower().startswith(prefix.lower()) and len(pt) > len(prefix):
                pt = pt[len(prefix):]
        if pt:
            return pt

    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        # Build "<hostname> – <path stem>" as a readable fallback
        hostname = p.hostname or ""
        stem = os.path.basename(p.path.rstrip("/"))
        stem = os.path.splitext(stem)[0]  # drop .m3u8/.mpd extension
        if stem and stem.lower() not in _GENERIC_STREAM_NAMES:
            return f"{hostname} – {stem}" if hostname else stem
        # Last resort: just use the hostname
        return hostname or stripped or "Stream"
    except Exception:
        return stripped or "Stream"

def _hook_sink(task: DownloadTask, d: Dict[str, Any], loop: asyncio.AbstractEventLoop) -> None:
    if task._cancel.is_set():
        raise RuntimeError("Download cancelled by user")

    if getattr(task, "_paused", False) or task.status == "paused":
        task.status = "paused"
        task.speed = 0.0
        asyncio.run_coroutine_threadsafe(broadcast_progress(), loop)
        while getattr(task, "_paused", False) or task.status == "paused":
            if task._cancel.is_set():
                raise RuntimeError("Download cancelled by user")
            time.sleep(0.5)
        if task.status == "paused":
            task.status = "downloading"
        asyncio.run_coroutine_threadsafe(broadcast_progress(), loop)

    status = d.get("status")
    info_dict = d.get("info_dict") or {}
    title = info_dict.get("title")
    if title:
        task.title = _sanitise_stream_title(title, task.url, task.page_title, prefer_page_title=task.is_stream)

    if status == "downloading":
        task.status = "downloading"
        task.speed = float(d.get("speed") or 0.0)
        
        combined_total = 0
        completed_size = 0
        requested_formats = info_dict.get("requested_formats") or []
        current_format_id = info_dict.get("format_id")
        
        if requested_formats:
            combined_total = sum(f.get("filesize") or f.get("filesize_estimate") or 0 for f in requested_formats)
            
            current_format_index = -1
            if current_format_id:
                for idx, fmt in enumerate(requested_formats):
                    if fmt.get("format_id") == current_format_id:
                        current_format_index = idx
                        break
            
            if current_format_index > 0:
                completed_size = sum(f.get("filesize") or f.get("filesize_estimate") or 0 for f in requested_formats[:current_format_index])
                
        current_total = d.get("total_bytes") or d.get("total_bytes_estimate") or info_dict.get("filesize") or info_dict.get("filesize_estimate") or 0
        
        task.total_bytes = int(combined_total if combined_total > 0 else current_total)
        task.downloaded_bytes = completed_size + int(d.get("downloaded_bytes") or 0)
        
        # Keep total_bytes and downloaded_bytes monotonically non-decreasing
        task.total_bytes = max(task.total_bytes, task.downloaded_bytes)
        
        if task.total_bytes > 0:
            task.progress = (task.downloaded_bytes / task.total_bytes) * 100.0
            
        if task.speed > 0:
            task.eta = max(0.0, (task.total_bytes - task.downloaded_bytes) / task.speed)
            
        task.filename = d.get("filename", task.filename)
    elif status == "finished":
        task.progress = 100.0
        task.filename = d.get("filename", task.filename)
    elif status == "error":
        task.status = "error"

    now = time.time()
    last = getattr(task, "_last_broadcast", 0.0)
    if now - last >= 0.5 or status in ("finished", "error"):
        setattr(task, "_last_broadcast", now)
        asyncio.run_coroutine_threadsafe(broadcast_progress(), loop)

# ──────────────────────────────────────────────
#  Tasks Disk Persistence
# ──────────────────────────────────────────────
TASKS_DIR = APP_DATA_DIR / "tmp"
TASKS_FILE = TASKS_DIR / "tasks.json"
_save_tasks_scheduled = False

def load_tasks() -> None:
    global TASKS
    if not TASKS_FILE.exists():
        return
    try:
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        with TASKS_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            for task_id, task_dict in data.items():
                task = DownloadTask(
                    task_id=task_dict["task_id"],
                    url=task_dict["url"],
                    format_id=task_dict.get("format_id"),
                    category=task_dict.get("category"),
                    custom_path=task_dict.get("custom_path"),
                    title=task_dict.get("title", "Pending…"),
                    status=task_dict.get("status", "queued"),
                    speed=0.0,
                    progress=task_dict.get("progress", 0.0),
                    eta=0.0,
                    total_bytes=task_dict.get("total_bytes", 0),
                    downloaded_bytes=task_dict.get("downloaded_bytes", 0),
                    filename=task_dict.get("filename", ""),
                    final_path=task_dict.get("final_path", ""),
                    error=task_dict.get("error", ""),
                    started_at=task_dict.get("started_at", 0.0),
                    finished_at=task_dict.get("finished_at", 0.0),
                    is_video=task_dict.get("is_video", True),
                    page_title=task_dict.get("page_title"),
                    is_stream=task_dict.get("is_stream", False),
                )
                if task.status in ("downloading", "queued"):
                    task.status = "paused"
                    task._paused = True
                TASKS[task_id] = task
    except Exception as e:
        print(f"Error loading tasks: {e}")

async def save_tasks_async() -> None:
    global _save_tasks_scheduled
    _save_tasks_scheduled = False
    try:
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        data = {task_id: task.to_dict() for task_id, task in TASKS.items()}
        temp_file = TASKS_FILE.with_suffix(".tmp")
        with temp_file.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        temp_file.replace(TASKS_FILE)
    except Exception as e:
        print(f"Error saving tasks: {e}")

def save_tasks_now() -> None:
    try:
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        data = {task_id: task.to_dict() for task_id, task in TASKS.items()}
        temp_file = TASKS_FILE.with_suffix(".tmp")
        with temp_file.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        temp_file.replace(TASKS_FILE)
    except Exception as e:
        print(f"Error saving tasks immediately: {e}")

def schedule_save_tasks() -> None:
    global _save_tasks_scheduled
    if _save_tasks_scheduled:
        return
    _save_tasks_scheduled = True
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_delayed_save())
    except RuntimeError:
        try:
            TASKS_DIR.mkdir(parents=True, exist_ok=True)
            data = {task_id: task.to_dict() for task_id, task in TASKS.items()}
            temp_file = TASKS_FILE.with_suffix(".tmp")
            with temp_file.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            temp_file.replace(TASKS_FILE)
        except Exception:
            pass

async def _delayed_save() -> None:
    await asyncio.sleep(1.0)
    await save_tasks_async()

# ──────────────────────────────────────────────
#  Worker Pool — Concurrent Download Execution
# ──────────────────────────────────────────────
async def download_worker(worker_id: int) -> None:
    loop = asyncio.get_running_loop()
    while True:
        task: DownloadTask = await TASK_QUEUE.get()
        try:
            if task._cancel.is_set():
                task.status = "cancelled"
                await broadcast_progress()
                continue

            while getattr(task, "_paused", False) or task.status == "paused":
                if task._cancel.is_set():
                    task.status = "cancelled"
                    await broadcast_progress()
                    break
                await asyncio.sleep(0.5)

            if task.status == "cancelled":
                continue

            setattr(task, "_active_thread", True)
            task.status = "downloading"
            task.started_at = time.time()
            save_tasks_now()
            opts = None
            try:
                opts = build_ydl_options(task, extract_only=False)
            except PermissionError as exc:
                task.status = "error"
                task.error = str(exc)
                task.finished_at = time.time()
                await broadcast_progress()
                save_tasks_now()
                continue

            def _run():
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(task.url, download=True)
                    raw_title = info.get("title", task.title) or ""
                    task.title = _sanitise_stream_title(raw_title, task.url, task.page_title, prefer_page_title=task.is_stream)
                    if "requested_downloads" in info and info["requested_downloads"]:
                        task.final_path = info["requested_downloads"][0].get("filepath", "")
                    else:
                        task.final_path = ydl.prepare_filename(info)

            try:
                await loop.run_in_executor(None, _run)
                if task._cancel.is_set():
                    task.status = "cancelled"
                elif task.status != "error":
                    task.status = "completed"
                    task.progress = 100.0
            except yt_dlp.utils.DownloadError as exc:
                if task._cancel.is_set():
                    task.status = "cancelled"
                else:
                    task.status = "error"
                    task.error = str(exc)
            except asyncio.CancelledError:
                task.status = "cancelled"
                task.error = "Worker terminated"
                raise
            except Exception as exc:  # noqa: BLE001
                if task._cancel.is_set():
                    task.status = "cancelled"
                else:
                    task.status = "error"
                    task.error = f"Unexpected: {exc}"
            finally:
                setattr(task, "_active_thread", False)
                task.finished_at = time.time()
                await broadcast_progress()
                save_tasks_now()
        finally:
            TASK_QUEUE.task_done()

async def spawn_workers(app: FastAPI) -> None:
    n = SETTINGS.get("max_concurrent_downloads", 3)
    app.state.workers = [
        asyncio.create_task(download_worker(i)) for i in range(n)
    ]

async def stop_workers(app: FastAPI) -> None:
    for w in getattr(app.state, "workers", []):
        w.cancel()
    await asyncio.gather(*getattr(app.state, "workers", []), return_exceptions=True)

# ──────────────────────────────────────────────
#  WebSocket Broadcast
# ──────────────────────────────────────────────
async def broadcast_progress() -> None:
    payload = json.dumps({
        "type": "tasks",
        "data": [t.to_dict() for t in TASKS.values()],
        "health": {
            "status": "healthy",
            "yt_dlp_version": yt_dlp.version.__version__,
            "active_workers": len(getattr(app.state, "workers", [])),
        },
        "settings": SETTINGS
    })
    dead: List[WebSocket] = []
    for ws in WEBSOCKET_SUBSCRIBERS:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in WEBSOCKET_SUBSCRIBERS:
            WEBSOCKET_SUBSCRIBERS.remove(ws)
    schedule_save_tasks()

# ──────────────────────────────────────────────
#  FastAPI Application
# ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    load_tasks()
    await spawn_workers(app)
    yield
    try:
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        data = {task_id: task.to_dict() for task_id, task in TASKS.items()}
        with TASKS_FILE.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        pass
    await stop_workers(app)

app = FastAPI(title="Media Acquisition Engine", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ──────────────────────────────────────────────
#  Endpoints
# ──────────────────────────────────────────────
@app.get("/")
async def index():
    idx = BASE_DIR / "dist-frontend" / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    return JSONResponse({"status": "ok", "service": "Media Acquisition Engine"})

async def handle_client_action(websocket: WebSocket, action: str, request_id: str, payload: Dict[str, Any]):
    global SETTINGS
    try:
        # 1. get_settings
        if action == "get_settings":
            await websocket.send_json({
                "type": "response",
                "action": action,
                "request_id": request_id,
                "ok": True,
                "data": SETTINGS
            })
            
        # 2. save_settings
        elif action == "save_settings":
            old_concurrency = SETTINGS.get("max_concurrent_downloads", 3)
            SETTINGS.update({k: v for k, v in payload.items() if v is not None})
            save_settings(SETTINGS)
            if "max_concurrent_downloads" in payload and payload["max_concurrent_downloads"] != old_concurrency:
                await stop_workers(app)
                await spawn_workers(app)
            await websocket.send_json({
                "type": "response",
                "action": action,
                "request_id": request_id,
                "ok": True,
                "data": SETTINGS
            })
            await broadcast_progress()

        # 3. get_health
        elif action == "get_health":
            await websocket.send_json({
                "type": "response",
                "action": action,
                "request_id": request_id,
                "ok": True,
                "data": {
                    "status": "healthy",
                    "yt_dlp_version": yt_dlp.version.__version__,
                    "active_workers": len(getattr(app.state, "workers", [])),
                }
            })

        # 4. extract
        elif action == "extract":
            url = payload.get("url")
            page_title = payload.get("page_title")
            if not url:
                raise ValueError("URL is required for extraction")
            if url.startswith("blob:"):
                raise ValueError("blob: URLs exist only in the browser and cannot be downloaded server-side.")
            
            loop = asyncio.get_running_loop()
            STREAM_EXTS = {".m3u8", ".mpd", ".ts", ".m4s"}
            STREAM_MIME_HINTS = ("m3u8", "mpd", "dash", "hls", "stream")

            probe_opts = {
                "quiet": True, "no_warnings": True, "noplaylist": True,
                "skip_download": True,
            }

            def _probe():
                with yt_dlp.YoutubeDL(probe_opts) as ydl:
                    return ydl.extract_info(url, download=False)

            extraction_method = "yt-dlp"
            try:
                info = await loop.run_in_executor(None, _probe)
                extractor = (info.get("extractor") or "").lower()
                if extractor in ("generic", "") or url.lower().endswith(tuple(STREAM_EXTS)):
                    if any(h in url.lower() for h in STREAM_MIME_HINTS) or \
                       url.lower().endswith(tuple(STREAM_EXTS)):
                        extraction_method = "stream"
            except Exception as exc:
                extraction_method = "direct"
                if url.startswith(("http://", "https://")):
                    from urllib.parse import urlparse
                    path = urlparse(url).path
                    filename = os.path.basename(path) or "stream"
                    ext = os.path.splitext(filename)[1].lstrip(".") or "mp4"
                    if not (ext.isalnum() and 2 <= len(ext) <= 5):
                        ext = "mp4"
                    if ext.lower() in ["m3u8", "mpd", "ts"]:
                        extraction_method = "stream"
                    info = {
                        "title": filename,
                        "duration": None,
                        "uploader": "Direct Link",
                        "thumbnail": None,
                        "formats": [
                            {
                                "format_id": "direct_stream",
                                "ext": ext,
                                "resolution": "unknown",
                                "vcodec": "direct",
                                "acodec": "direct",
                                "filesize": None,
                            }
                        ],
                    }
                else:
                    raise ValueError(f"Extraction failed: {exc}")

            formats = []
            for f in info.get("formats", []):
                vcodec = f.get("vcodec", "none")
                acodec = f.get("acodec", "none")
                if vcodec == "none" and acodec == "none":
                    continue
                formats.append({
                    "format_id": f.get("format_id"),
                    "ext": f.get("ext"),
                    "resolution": f.get("resolution") or (
                        f"{f.get('width')}x{f.get('height')}" if f.get("width") else None
                    ),
                    "vcodec": vcodec,
                    "acodec": acodec,
                    "fps": f.get("fps"),
                    "filesize": f.get("filesize") or f.get("filesize_estimate"),
                    "tbr": f.get("tbr"),
                    "vbr": f.get("vbr"),
                    "abr": f.get("abr"),
                    "format_note": f.get("format_note", ""),
                })

            is_stream = (extraction_method == "stream")
            res_data = {
                "title": _sanitise_stream_title(info.get("title") or "", url, page_title, prefer_page_title=is_stream),
                "duration": info.get("duration"),
                "uploader": info.get("uploader"),
                "thumbnail": info.get("thumbnail"),
                "url": url,
                "extraction_method": extraction_method,
                "formats": formats,
            }
            await websocket.send_json({
                "type": "response",
                "action": action,
                "request_id": request_id,
                "ok": True,
                "data": res_data
            })

        # 5. download
        elif action == "download":
            url = payload.get("url")
            if not url:
                raise ValueError("URL is required")
            if url.startswith("blob:"):
                raise ValueError("blob: URLs cannot be downloaded server-side.")
            
            task = DownloadTask(
                task_id=uuid.uuid4().hex,
                url=url,
                format_id=payload.get("format_id"),
                category=payload.get("category"),
                custom_path=payload.get("custom_path"),
                is_video=payload.get("is_video", True),
                page_title=payload.get("page_title"),
                is_stream=bool(payload.get("is_stream", False)),
                title=_sanitise_stream_title("", url, payload.get("page_title"), prefer_page_title=bool(payload.get("is_stream"))),
            )
            TASKS[task.task_id] = task
            await TASK_QUEUE.put(task)
            await broadcast_progress()
            save_tasks_now()
            
            await websocket.send_json({
                "type": "response",
                "action": action,
                "request_id": request_id,
                "ok": True,
                "data": {"task_id": task.task_id, "status": "queued"}
            })

        # 6. cancel
        elif action == "cancel":
            task_id = payload.get("task_id")
            task = TASKS.get(task_id)
            if not task:
                raise ValueError("Task not found")
            task._cancel.set()
            task.status = "cancelled"
            await broadcast_progress()
            await websocket.send_json({
                "type": "response",
                "action": action,
                "request_id": request_id,
                "ok": True,
                "data": {"task_id": task_id, "status": "cancelled"}
            })

        # 7. pause
        elif action == "pause":
            task_id = payload.get("task_id")
            task = TASKS.get(task_id)
            if not task:
                raise ValueError("Task not found")
            if task.status in ("downloading", "queued"):
                task._paused = True
                task.status = "paused"
                task.speed = 0.0
                await broadcast_progress()
                save_tasks_now()
            await websocket.send_json({
                "type": "response",
                "action": action,
                "request_id": request_id,
                "ok": True,
                "data": {"task_id": task_id, "status": task.status}
            })

        # 8. resume
        elif action == "resume":
            task_id = payload.get("task_id")
            task = TASKS.get(task_id)
            if not task:
                raise ValueError("Task not found")
            
            has_active_thread = getattr(task, "_active_thread", False)
            
            if task.status == "paused":
                task._paused = False
                if has_active_thread:
                    task.status = "downloading"
                    await broadcast_progress()
                    save_tasks_now()
                else:
                    task.status = "queued"
                    task.speed = 0.0
                    task.eta = 0.0
                    task.error = ""
                    task._cancel.clear()
                    task._paused = False
                    await TASK_QUEUE.put(task)
                    await broadcast_progress()
                    save_tasks_now()
            elif task.status in ("cancelled", "error", "completed"):
                task.status = "queued"
                task.progress = 0.0
                task.speed = 0.0
                task.eta = 0.0
                task.error = ""
                task.total_bytes = 0
                task.downloaded_bytes = 0
                task._cancel.clear()
                task._paused = False
                await TASK_QUEUE.put(task)
                await broadcast_progress()
                save_tasks_now()
                
            await websocket.send_json({
                "type": "response",
                "action": action,
                "request_id": request_id,
                "ok": True,
                "data": {"task_id": task_id, "status": task.status}
            })

        # 9. reveal
        elif action == "reveal":
            import subprocess
            import platform
            task_id = payload.get("task_id")
            task = TASKS.get(task_id)
            if not task:
                raise ValueError("Task not found")
            path_to_open = task.final_path or task.filename
            
            system = platform.system()
            if path_to_open and os.path.exists(path_to_open):
                if system == "Darwin":
                    subprocess.run(["open", "-R", path_to_open], check=False)
                elif system == "Windows":
                    subprocess.run(["explorer", "/select,", os.path.normpath(path_to_open)], check=False)
                else:
                    parent_dir = os.path.dirname(os.path.abspath(path_to_open))
                    subprocess.run(["xdg-open", parent_dir], check=False)
            else:
                target_dir = task.custom_path or (
                    SETTINGS["categories"].get(task.category) if task.category else SETTINGS["default_download_path"]
                )
                if os.path.exists(target_dir):
                    if system == "Darwin":
                        subprocess.run(["open", target_dir], check=False)
                    elif system == "Windows":
                        subprocess.run(["explorer", os.path.normpath(target_dir)], check=False)
                    else:
                        subprocess.run(["xdg-open", target_dir], check=False)
                else:
                    raise ValueError("Path does not exist on disk")
            
            await websocket.send_json({
                "type": "response",
                "action": action,
                "request_id": request_id,
                "ok": True,
                "data": {"status": "ok"}
            })

        # 10. delete
        elif action == "delete":
            task_id = payload.get("task_id")
            delete_file = bool(payload.get("delete_file", False))
            if task_id in TASKS:
                task = TASKS[task_id]
                task._cancel.set()
                task.status = "cancelled"
                
                if delete_file:
                    file_paths = []
                    if task.final_path:
                        file_paths.append(task.final_path)
                    if task.filename:
                        file_paths.append(task.filename)
                    
                    for fp in file_paths:
                        try:
                            p = Path(fp)
                            if p.exists():
                                if p.is_dir():
                                    shutil.rmtree(p)
                                else:
                                    p.unlink(missing_ok=True)
                        except Exception as e:
                            print(f"Error deleting file {fp}: {e}")
                            
                del TASKS[task_id]
                await broadcast_progress()
                save_tasks_now()
                
            await websocket.send_json({
                "type": "response",
                "action": action,
                "request_id": request_id,
                "ok": True,
                "data": {"deleted": task_id}
            })

        else:
            raise ValueError(f"Unknown action: {action}")

    except Exception as e:
        try:
            await websocket.send_json({
                "type": "response",
                "action": action,
                "request_id": request_id,
                "ok": False,
                "error": str(e)
            })
        except Exception:
            pass

@app.websocket("/ws/progress")
async def websocket_progress(websocket: WebSocket):
    await websocket.accept()
    WEBSOCKET_SUBSCRIBERS.append(websocket)
    try:
        # Send initial snapshot immediately
        payload = json.dumps({
            "type": "tasks",
            "data": [t.to_dict() for t in TASKS.values()],
            "health": {
                "status": "healthy",
                "yt_dlp_version": yt_dlp.version.__version__,
                "active_workers": len(getattr(app.state, "workers", [])),
            },
            "settings": SETTINGS
        })
        await websocket.send_text(payload)
        
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except Exception:
                continue
            
            action = msg.get("action")
            request_id = msg.get("request_id")
            payload_data = msg.get("payload", {})
            
            if action:
                asyncio.create_task(handle_client_action(websocket, action, request_id, payload_data))
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in WEBSOCKET_SUBSCRIBERS:
            WEBSOCKET_SUBSCRIBERS.remove(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False, log_level="info")