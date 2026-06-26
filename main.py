"""
main.py — Media Acquisition Engine
===================================
FastAPI + yt-dlp + asyncio + WebSocket progress + AV1 codec priority.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import yt_dlp
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

logger = logging.getLogger("dma-engine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ──────────────────────────────────────────────
#  System PATH
# ──────────────────────────────────────────────
def ensure_system_path() -> None:
    path_env = os.environ.get("PATH", "")
    paths = path_env.split(os.pathsep)
    additions: list[str] = []
    if platform.system() == "Darwin":
        additions = [
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",
            "/usr/local/bin",
            "/usr/local/sbin",
            "/opt/local/bin",
        ]
    updated = False
    for p in additions:
        if p not in paths and os.path.exists(p):
            paths.insert(0, p)
            updated = True
    if updated:
        os.environ["PATH"] = os.pathsep.join(paths)

ensure_system_path()

# ──────────────────────────────────────────────
#  Configuration & Persistence
# ──────────────────────────────────────────────
def get_app_data_dir() -> Path:
    home = Path.home()
    if platform.system() == "Darwin":
        p = home / "Library" / "Application Support" / "DownloadAnything"
    elif platform.system() == "Windows":
        appdata = os.environ.get("APPDATA")
        p = Path(appdata) / "DownloadAnything" if appdata else home / "AppData" / "Roaming" / "DownloadAnything"
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
STATIC_DIR = BASE_DIR / "frontend" / "static"
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
    "rate_limit_bytes_per_sec": 0,
    "merge_output_format": "mp4",
    "concurrent_fragments": 16,
    "embed_thumbnail": False,
    "embed_subtitles": False,
    "subtitle_language": "en",
    "proxy": "",
    "cookies_from_browser": "none",
}

def load_settings() -> Dict[str, Any]:
    if SETTINGS_FILE.exists():
        try:
            with SETTINGS_FILE.open("r", encoding="utf-8") as fh:
                return {**DEFAULT_SETTINGS, **json.load(fh)}
        except (json.JSONDecodeError, OSError):
            return DEFAULT_SETTINGS.copy()
    save_settings(DEFAULT_SETTINGS)
    return DEFAULT_SETTINGS.copy()

def save_settings(data: Dict[str, Any]) -> None:
    with SETTINGS_FILE.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)

SETTINGS: Dict[str, Any] = load_settings()

# ──────────────────────────────────────────────
#  Task Status Enum
# ──────────────────────────────────────────────
class TaskStatus(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    STITCHING = "stitching"
    EMBEDDING = "embedding"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLED = "cancelled"
    PAUSED = "paused"

# ──────────────────────────────────────────────
#  Download Task
# ──────────────────────────────────────────────
@dataclass
class DownloadTask:
    task_id: str
    url: str
    format_id: Optional[str]
    category: Optional[str]
    custom_path: Optional[str]
    title: str = "Pending…"
    status: str = TaskStatus.QUEUED
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
    page_title: Optional[str] = None
    is_stream: bool = False
    headers: Optional[Dict[str, str]] = None
    has_custom_title: bool = False
    fragment_index: Optional[int] = None
    fragment_count: Optional[int] = None
    _cancel: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    # Cleared = paused/waiting. Set = allowed to run. New tasks must call .set() before queuing.
    _pause_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    # Whether the executor thread for this task is currently active
    _is_running: bool = field(default=False, repr=False)
    # Timestamp of last WebSocket broadcast — written/read from download thread (float is GIL-safe)
    _last_broadcast: float = field(default=0.0, repr=False)

    def update(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)

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
            "headers": self.headers,
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
            "has_custom_title": self.has_custom_title,
            "fragment_index": self.fragment_index,
            "fragment_count": self.fragment_count,
        }

TASKS: Dict[str, DownloadTask] = {}
TASK_QUEUE: asyncio.Queue[DownloadTask] = asyncio.Queue()
WEBSOCKET_SUBSCRIBERS: List[WebSocket] = []

# ──────────────────────────────────────────────
#  Pydantic Schemas
# ──────────────────────────────────────────────
class ExtractRequest(BaseModel):
    url: str
    page_title: Optional[str] = None
    headers: Optional[Dict[str, str]] = None

class DownloadRequest(BaseModel):
    url: str
    format_id: Optional[str] = None
    category: Optional[str] = None
    custom_path: Optional[str] = None
    is_video: Optional[bool] = True
    page_title: Optional[str] = None
    is_stream: Optional[bool] = False
    headers: Optional[Dict[str, str]] = None
    filename: Optional[str] = None

class SettingsUpdate(BaseModel):
    max_concurrent_downloads: Optional[int] = None
    fallback_codecs: Optional[List[str]] = None
    default_download_path: Optional[str] = None
    categories: Optional[Dict[str, str]] = None
    rate_limit_bytes_per_sec: Optional[int] = None
    merge_output_format: Optional[str] = Field(default=None, pattern="^(mp4|mkv|webm)$")
    concurrent_fragments: Optional[int] = None
    embed_thumbnail: Optional[bool] = None
    embed_subtitles: Optional[bool] = None
    subtitle_language: Optional[str] = None
    proxy: Optional[str] = None
    cookies_from_browser: Optional[str] = None

# ──────────────────────────────────────────────
#  Path Safety
# ──────────────────────────────────────────────
def is_safe_path(target_path: str | Path, base_paths: List[Path]) -> bool:
    try:
        target = Path(target_path).resolve()
        for base in base_paths:
            resolved_base = base.resolve()
            if resolved_base in target.parents or target == resolved_base:
                return True
        return False
    except Exception:
        return False

def get_allowed_roots() -> List[Path]:
    roots = [Path.home()]
    default_root = SETTINGS.get("default_download_path")
    if default_root:
        roots.append(Path(default_root))
    for cat_path in SETTINGS.get("categories", {}).values():
        if cat_path:
            roots.append(Path(cat_path))
    return roots

def ensure_target_dir(target_dir: str) -> Path:
    p = Path(target_dir)
    p.mkdir(parents=True, exist_ok=True)
    if not os.access(p, os.W_OK):
        raise PermissionError(f"Target directory '{target_dir}' is not writable")
    return p

# ──────────────────────────────────────────────
#  Title Helpers
# ──────────────────────────────────────────────
_GENERIC_STREAM_NAMES = frozenset({
    "master", "index", "playlist", "stream", "video", "audio",
    "media", "manifest", "chunklist", "output", "main", "live",
})

def sanitise_title(raw_title: str, url: str, page_title: Optional[str] = None, prefer_page: bool = False) -> str:
    stripped = raw_title.strip()
    if prefer_page and page_title:
        cleaned = page_title.strip()
        if cleaned:
            return cleaned
    if stripped and stripped.lower() not in _GENERIC_STREAM_NAMES:
        return stripped
    if page_title:
        cleaned = page_title.strip()
        if cleaned:
            return cleaned
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        stem = os.path.basename(parsed.path.rstrip("/"))
        stem = os.path.splitext(stem)[0]
        if stem and stem.lower() not in _GENERIC_STREAM_NAMES:
            return f"{hostname} – {stem}" if hostname else stem
        return hostname or stripped or "Stream"
    except Exception:
        return stripped or "Stream"

# ──────────────────────────────────────────────
#  FFmpeg Detection
# ──────────────────────────────────────────────
def find_ffmpeg_location() -> Optional[str]:
    local_bin = get_app_data_dir() / "bin"
    ffmpeg_exe = "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"
    if (local_bin / ffmpeg_exe).exists():
        return str(local_bin)
    found = shutil.which("ffmpeg")
    if found:
        return str(Path(found).parent)
    search = []
    if platform.system() == "Darwin":
        search = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]
    elif platform.system() == "Windows":
        search = ["C:\\ffmpeg\\bin", "C:\\Program Files\\ffmpeg\\bin"]
    for p in search:
        if (Path(p) / ffmpeg_exe).exists():
            return p
    return None

# ──────────────────────────────────────────────
#  yt-dlp Options Builder
# ──────────────────────────────────────────────
def build_ydl_opts(task: DownloadTask, loop: Optional[asyncio.AbstractEventLoop]) -> Dict[str, Any]:
    target_dir = task.custom_path or (
        SETTINGS["categories"].get(task.category) if task.category else SETTINGS["default_download_path"]
    )

    if not is_safe_path(target_dir, get_allowed_roots()):
        raise PermissionError(f"Target directory '{target_dir}' is not within allowed locations.")

    ensure_target_dir(target_dir)

    codec_pref = SETTINGS.get("fallback_codecs", ["av01", "vp09", "avc01"])
    format_sort = [f"vcodec:{c}" for c in codec_pref] + ["res", "abr", "ext:mp4:m4a"]

    if task.format_id and task.format_id != "direct_stream":
        format_spec = f"{task.format_id}+ba/b" if task.is_video else task.format_id
    else:
        format_spec = "bv*+ba/b"

    ffmpeg_location = find_ffmpeg_location()

    if task.filename:
        # Always use %(ext)s so yt-dlp writes the correct merged extension.
        # Preserving the user's literal extension causes double-extension bugs
        # (e.g. video.mp4.mp4) when yt-dlp re-muxes to the same container.
        stem = Path(task.filename).stem
        outtmpl_val = f"{stem}.%(ext)s"
    else:
        outtmpl_val = "%(title).200B.%(ext)s"

    task_fragment_dir = TMP_DIR / "fragments" / task.task_id
    task_fragment_dir.mkdir(parents=True, exist_ok=True)

    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "merge_output_format": SETTINGS.get("merge_output_format", "mp4"),
        "format": format_spec,
        "format_sort": format_sort,
        "concurrent_fragment_downloads": SETTINGS.get("concurrent_fragments", 16),
        "retries": 10,
        "fragment_retries": 10,
        "outtmpl": outtmpl_val,
        "writethumbnail": False,
        "ignoreerrors": False,
        "noplaylist": True,
        "progress_hooks": [lambda d: _progress_hook(task, d, loop)],
        "postprocessor_hooks": [lambda d: _postprocessor_hook(task, d, loop)],
        "postprocessors": [
            {"key": "FFmpegMetadata", "add_chapters": True},
        ],
        "postprocessor_args": {"default": ["-nostdin"]},
        "buffersize": 1024 * 256,
        "paths": {
            "home": str(target_dir),
            "temp": str(task_fragment_dir),
        },
        "continuedl": True,
    }

    if SETTINGS.get("embed_thumbnail", False):
        opts["writethumbnail"] = True
        opts["postprocessors"].append({
            "key": "EmbedThumbnail",
            "already_have_thumbnail": False,
        })

    if SETTINGS.get("embed_subtitles", False):
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = [SETTINGS.get("subtitle_language", "en")]
        opts["postprocessors"].append({
            "key": "FFmpegEmbedSubtitle",
            "already_have_subtitle": False,
        })

    if ffmpeg_location:
        opts["ffmpeg_location"] = ffmpeg_location

    rate_limit = SETTINGS.get("rate_limit_bytes_per_sec", 0)
    if rate_limit and rate_limit > 0:
        opts["ratelimit"] = rate_limit

    proxy_val = SETTINGS.get("proxy", "").strip()
    if proxy_val:
        opts["proxy"] = proxy_val

    cookies_browser = SETTINGS.get("cookies_from_browser", "none")
    if cookies_browser and cookies_browser != "none" and not task.headers:
        opts["cookiesfrombrowser"] = (cookies_browser,)

    if task.headers:
        opts["http_headers"] = task.headers

    return opts

def build_probe_opts(headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    if headers:
        opts["http_headers"] = headers
    else:
        cookies_browser = SETTINGS.get("cookies_from_browser", "none")
        if cookies_browser and cookies_browser != "none":
            opts["cookiesfrombrowser"] = (cookies_browser,)
    return opts

# ──────────────────────────────────────────────
#  yt-dlp Extraction Helper
# ──────────────────────────────────────────────
def _extract_info(url: str, opts: Dict[str, Any]) -> Dict[str, Any]:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def _extract_with_fallback(url: str, base_opts: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return _extract_info(url, base_opts)
    except Exception as exc:
        exc_str = str(exc).lower()
        if "cookies" in exc_str and ("database" in exc_str or "could not find" in exc_str):
            clean_opts = {k: v for k, v in base_opts.items() if k != "cookiesfrombrowser"}
            return _extract_info(url, clean_opts)
        if "primarily used for piracy" in exc_str:
            generic_opts = {**base_opts, "allowed_extractors": ["generic"]}
            try:
                return _extract_info(url, generic_opts)
            except Exception:
                clean_opts = {k: v for k, v in generic_opts.items() if k != "cookiesfrombrowser"}
                return _extract_info(url, clean_opts)
        raise

# ──────────────────────────────────────────────
#  Progress & Postprocessor Hooks
# ──────────────────────────────────────────────
def _progress_hook(task: DownloadTask, d: Dict[str, Any], loop: Optional[asyncio.AbstractEventLoop]) -> None:
    if task._cancel.is_set():
        raise yt_dlp.utils.DownloadError("Download cancelled by user")

    status = d.get("status")
    updates: Dict[str, Any] = {}

    if status == "downloading":
        updates["status"] = TaskStatus.DOWNLOADING
        info = d.get("info_dict") or {}

        title = info.get("title")
        if title and not task.has_custom_title:
            updates["title"] = sanitise_title(title, task.url, task.page_title, prefer_page=task.is_stream)

        speed = float(d.get("speed") or 0.0)
        updates["speed"] = speed

        total = int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
        downloaded = int(d.get("downloaded_bytes") or 0)

        frag_idx = d.get("fragment_index")
        frag_cnt = d.get("fragment_count")

        if frag_idx is not None:
            updates["fragment_index"] = frag_idx + 1
        if frag_cnt is not None:
            updates["fragment_count"] = frag_cnt

        if frag_idx is not None and frag_cnt is not None and frag_cnt > 0:
            # Primary: fragment-based progress (reliable for segmented streams)
            frag_pct = ((frag_idx + 1) / frag_cnt) * 100.0
            usable_total = total if total > 0 else task.total_bytes
            if usable_total > 0 and downloaded > 0:
                # Blend: take the higher of frag-based vs byte-based progress
                # so the bar never goes backward when fragment sizes are uneven.
                byte_pct = (downloaded / usable_total) * 100.0
                updates["progress"] = min(max(frag_pct, byte_pct), 99.9)
                updates["total_bytes"] = usable_total
            else:
                updates["progress"] = min(frag_pct, 99.9)
        elif total > 0:
            updates["total_bytes"] = total
            updates["progress"] = min((downloaded / total) * 100.0, 99.9)
        elif task.total_bytes > 0:
            updates["total_bytes"] = task.total_bytes
            if downloaded > 0:
                updates["progress"] = min((downloaded / task.total_bytes) * 100.0, 99.9)

        updates["downloaded_bytes"] = downloaded

        if speed > 0:
            ref_total = updates.get("total_bytes", task.total_bytes)
            if ref_total > 0:
                remaining = ref_total - downloaded
                updates["eta"] = max(0.0, remaining / speed)

        filename = d.get("filename")
        if filename:
            updates["filename"] = filename

    elif status == "finished":
        # A stream/fragment finished downloading — don't jump to 100% yet;
        # postprocessing (merge/mux) is still pending.
        filename = d.get("filename")
        if filename:
            updates["filename"] = filename

    elif status == "error":
        updates["status"] = TaskStatus.ERROR

    if updates:
        _apply_updates(task, updates, loop)

    now = time.time()
    if now - task._last_broadcast >= 0.5 or status in ("finished", "error"):
        task._last_broadcast = now
        if loop:
            asyncio.run_coroutine_threadsafe(broadcast_progress(), loop)


_PP_STATUS_MAP: Dict[str, TaskStatus] = {
    "Merger": TaskStatus.STITCHING,
    "FFmpegMerger": TaskStatus.STITCHING,
    "EmbedThumbnail": TaskStatus.EMBEDDING,
    "FFmpegEmbedSubtitle": TaskStatus.EMBEDDING,
    "MoveFiles": TaskStatus.FINALIZING,
}


def _postprocessor_hook(task: DownloadTask, d: Dict[str, Any], loop: Optional[asyncio.AbstractEventLoop]) -> None:
    if task._cancel.is_set():
        raise yt_dlp.utils.DownloadError("Download cancelled by user")

    status = d.get("status")
    # Per yt-dlp docs: status is "started" | "processing" | "finished".
    # Ignore unknown values per spec.
    if status not in ("started", "finished"):
        return

    pp = d.get("postprocessor") or ""

    if status == "started":
        new_status = _PP_STATUS_MAP.get(pp, TaskStatus.FINALIZING)
        _apply_updates(task, {"status": new_status, "progress": 99.0}, loop)
        if loop:
            asyncio.run_coroutine_threadsafe(broadcast_progress(), loop)
    elif status == "finished" and pp in ("Merger", "FFmpegMerger"):
        # Merge complete — yt-dlp sets info_dict["filepath"] to the merged output path.
        # Per FFmpegMergerPP.run(): info["filepath"] is the final merged file before return.
        info_dict = d.get("info_dict") or {}
        filepath = info_dict.get("filepath") or info_dict.get("_filename")
        updates: Dict[str, Any] = {"progress": 99.9}
        if filepath:
            updates["final_path"] = filepath
            updates["filename"] = filepath
        _apply_updates(task, updates, loop)
        if loop:
            asyncio.run_coroutine_threadsafe(broadcast_progress(), loop)


def _apply_updates(task: DownloadTask, updates: Dict[str, Any], loop: Optional[asyncio.AbstractEventLoop]) -> None:
    if loop:
        loop.call_soon_threadsafe(lambda u=dict(updates), t=task: t.update(**u))
    else:
        task.update(**updates)

# ──────────────────────────────────────────────
#  Task Disk Persistence
# ──────────────────────────────────────────────
TASKS_FILE = TMP_DIR / "tasks.json"

def load_tasks() -> None:
    global TASKS
    if not TASKS_FILE.exists():
        return
    try:
        with TASKS_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        for task_id, td in data.items():
            task = DownloadTask(
                task_id=td["task_id"],
                url=td["url"],
                format_id=td.get("format_id"),
                category=td.get("category"),
                custom_path=td.get("custom_path"),
                title=td.get("title", "Pending…"),
                status=td.get("status", TaskStatus.QUEUED),
                speed=0.0,
                progress=td.get("progress", 0.0),
                eta=0.0,
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
            # On restart, any task that was actively downloading or queued cannot
            # be resumed automatically — mark as PAUSED so the user can resume.
            # Also cover mid-postprocessing states (stitching/embedding/finalizing)
            # since those processes are killed on shutdown.
            if task.status in (
                TaskStatus.DOWNLOADING, TaskStatus.QUEUED,
                TaskStatus.STITCHING, TaskStatus.EMBEDDING, TaskStatus.FINALIZING,
            ):
                task.status = TaskStatus.PAUSED
                # _pause_event is already cleared (default=asyncio.Event() is unset)
            TASKS[task_id] = task
    except Exception as e:
        logger.error("Error loading tasks: %s", e)

_write_tasks_lock = threading.Lock()

def _write_tasks() -> None:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    data = {tid: t.to_dict() for tid, t in TASKS.items()}
    tmp = TASKS_FILE.with_suffix(f".tmp.{uuid.uuid4().hex[:6]}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise
    try:
        with _write_tasks_lock:
            os.replace(str(tmp), str(TASKS_FILE))
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        raise

def save_tasks_now() -> None:
    try:
        _write_tasks()
    except Exception as e:
        logger.error("Error saving tasks: %s", e)

async def save_tasks_async() -> None:
    try:
        await asyncio.get_running_loop().run_in_executor(None, _write_tasks)
    except Exception as e:
        logger.error("Error saving tasks async: %s", e)

def schedule_save_tasks() -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_delayed_save())
    except RuntimeError:
        save_tasks_now()

async def _delayed_save() -> None:
    await asyncio.sleep(1.0)
    await save_tasks_async()

# ──────────────────────────────────────────────
#  Temp File Cleanup
# ──────────────────────────────────────────────
def cleanup_task_files(task: DownloadTask) -> None:
    if task.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
        return

    frag_dir = TMP_DIR / "fragments" / task.task_id
    if frag_dir.is_dir():
        try:
            shutil.rmtree(frag_dir)
            logger.info("Cleaned fragment dir: %s", frag_dir)
        except Exception as e:
            logger.warning("Failed to clean fragment dir %s: %s", frag_dir, e)

    if task.status == TaskStatus.CANCELLED and task.filename:
        target_dir = Path(task.custom_path or (
            SETTINGS["categories"].get(task.category) if task.category else SETTINGS["default_download_path"]
        ))
        if target_dir.exists():
            stem = Path(task.filename).stem
            for item in target_dir.iterdir():
                if not item.is_file():
                    continue
                name_lower = item.name.lower()
                is_temp = (
                    item.suffix.lower() in (".part", ".ytdl")
                    or "part-fragment" in name_lower
                    or "-frag" in name_lower
                )
                if not is_temp:
                    continue
                if stem.lower() in item.stem.lower():
                    try:
                        item.unlink(missing_ok=True)
                        logger.info("Cleaned temp file: %s", item)
                    except Exception as e:
                        logger.warning("Failed to clean %s: %s", item, e)

# ──────────────────────────────────────────────
#  Worker Pool
# ──────────────────────────────────────────────
async def download_worker(worker_id: int) -> None:
    loop = asyncio.get_running_loop()
    while True:
        task: DownloadTask = await TASK_QUEUE.get()
        try:
            if task._cancel.is_set():
                task.update(status=TaskStatus.CANCELLED)
                await broadcast_progress()
                save_tasks_now()
                continue

            # Wait while paused — polls until resumed or cancelled
            while not task._pause_event.is_set():
                if task._cancel.is_set():
                    task.update(status=TaskStatus.CANCELLED)
                    await broadcast_progress()
                    save_tasks_now()
                    break
                await asyncio.sleep(0.3)

            if task.status == TaskStatus.CANCELLED:
                continue

            task._is_running = True
            task.update(status=TaskStatus.DOWNLOADING, started_at=time.time())
            save_tasks_now()

            try:
                try:
                    opts = build_ydl_opts(task, loop)
                except PermissionError as exc:
                    task.update(status=TaskStatus.ERROR, error=str(exc))
                    continue

                def _run_download() -> None:
                    """
                    Single-pass yt-dlp download with cookie fallback.

                    We use one YoutubeDL instance and call extract_info(download=True).
                    This is the canonical yt-dlp usage: format selection, postprocessors,
                    progress hooks, and final-path reporting all work correctly this way.
                    """
                    current_opts = opts
                    info = None
                    try:
                        with yt_dlp.YoutubeDL(current_opts) as ydl:
                            info = ydl.extract_info(task.url, download=True)
                            if info:
                                requested = info.get("requested_downloads") or []
                                if requested:
                                    fp = requested[0].get("filepath") or requested[0].get("_filename", "")
                                    if fp:
                                        task.final_path = fp
                                        task.filename = fp
                                if not task.final_path:
                                    task.final_path = ydl.prepare_filename(info)
                    except Exception as exc:
                        exc_str = str(exc).lower()
                        if "cookies" in exc_str and ("database" in exc_str or "could not find" in exc_str or "keychain" in exc_str):
                            # Retry without cookiesfrombrowser
                            current_opts = {k: v for k, v in opts.items() if k != "cookiesfrombrowser"}
                            with yt_dlp.YoutubeDL(current_opts) as ydl:
                                info = ydl.extract_info(task.url, download=True)
                                if info:
                                    requested = info.get("requested_downloads") or []
                                    if requested:
                                        fp = requested[0].get("filepath") or requested[0].get("_filename", "")
                                        if fp:
                                            task.final_path = fp
                                            task.filename = fp
                                    if not task.final_path:
                                        task.final_path = ydl.prepare_filename(info)
                        else:
                            raise

                    if not info:
                        raise yt_dlp.utils.DownloadError("No info extracted")

                    # Update title from completed download metadata (don't mutate info dict)
                    if not task.has_custom_title:
                        raw_title = info.get("title") or ""
                        task.title = sanitise_title(raw_title, task.url, task.page_title, prefer_page=task.is_stream)

                await loop.run_in_executor(None, _run_download)
                if task._cancel.is_set():
                    task.update(status=TaskStatus.CANCELLED)
                else:
                    task.update(status=TaskStatus.COMPLETED, progress=100.0)
            except yt_dlp.utils.DownloadError as exc:
                if task._cancel.is_set():
                    task.update(status=TaskStatus.CANCELLED)
                else:
                    err_msg = str(exc)
                    task.update(status=TaskStatus.ERROR, error=err_msg)
                    logger.error("Download error for task %s: %s", task.task_id, err_msg)
            except asyncio.CancelledError:
                task.update(status=TaskStatus.CANCELLED, error="Worker terminated")
                raise
            except Exception as exc:
                logger.error("Unexpected download error for task %s: %s", task.task_id, exc, exc_info=True)
                if task._cancel.is_set():
                    task.update(status=TaskStatus.CANCELLED)
                else:
                    task.update(status=TaskStatus.ERROR, error=f"Unexpected: {exc}")
            finally:
                task._is_running = False
                task.finished_at = time.time()
                await asyncio.get_running_loop().run_in_executor(None, cleanup_task_files, task)
                await broadcast_progress()
                save_tasks_now()
        finally:
            TASK_QUEUE.task_done()

async def spawn_workers(app: FastAPI) -> None:
    n = SETTINGS.get("max_concurrent_downloads", 3)
    app.state.workers = [asyncio.create_task(download_worker(i)) for i in range(n)]

async def stop_workers(app: FastAPI) -> None:
    for w in getattr(app.state, "workers", []):
        w.cancel()
    await asyncio.gather(*getattr(app.state, "workers", []), return_exceptions=True)

# ──────────────────────────────────────────────
#  WebSocket Broadcast
# ──────────────────────────────────────────────
def get_broadcast_payload() -> str:
    return json.dumps({
        "type": "tasks",
        "data": [t.to_dict() for t in TASKS.values()],
        "health": {
            "status": "healthy",
            "yt_dlp_version": yt_dlp.version.__version__,
            "active_workers": len(getattr(app.state, "workers", [])),
        },
        "settings": SETTINGS,
    })

async def broadcast_progress() -> None:
    """Push current task state to all connected WebSocket clients.

    Does NOT trigger a disk save — callers that mutate task state are
    responsible for calling save_tasks_now() when appropriate.
    """
    payload = get_broadcast_payload()
    dead: List[WebSocket] = []
    for ws in WEBSOCKET_SUBSCRIBERS:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in WEBSOCKET_SUBSCRIBERS:
            WEBSOCKET_SUBSCRIBERS.remove(ws)

# ──────────────────────────────────────────────
#  FastAPI Application
# ──────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    load_tasks()
    await spawn_workers(app)
    yield
    save_tasks_now()
    await stop_workers(app)

app = FastAPI(title="Media Acquisition Engine", version="2.0.0", lifespan=lifespan)

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
#  HTTP Endpoints
# ──────────────────────────────────────────────
@app.get("/")
async def index():
    idx = BASE_DIR / "frontend" / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    is_frozen = getattr(sys, "frozen", False)
    return JSONResponse({
        "status": "ok",
        "service": "Media Acquisition Engine",
        "mode": "sidecar" if is_frozen else "api-only",
        "message": (
            "Frontend UI files (index.html) not found. "
            "Use the desktop app or run 'fastapi run' from the project root."
        ),
    })

# ──────────────────────────────────────────────
#  WebSocket Action Handler
# ──────────────────────────────────────────────
# Protocols that indicate a segmented/streaming source.
# Using yt-dlp's format protocol field is more reliable than inspecting URLs
# or extractor names ("generic" extractor is used by many non-stream platforms).
_STREAM_PROTOCOLS = frozenset({
    "m3u8", "m3u8_native", "dash", "rtmp", "rtmpe", "rtmps", "rtmpt", "rtmpte",
})


def _formats_are_stream(formats: List[Dict[str, Any]]) -> bool:
    """Return True if the dominant format protocol indicates a live/segmented stream."""
    protocols = {f.get("protocol", "") for f in formats if f.get("protocol")}
    return bool(protocols & _STREAM_PROTOCOLS)


async def estimate_stream_size(url: str, headers: Optional[Dict[str, str]] = None) -> Optional[int]:
    """Estimate total bytes for an HLS/m3u8 stream by sampling segment sizes.

    Returns None if the optional aiohttp/m3u8 packages are not installed,
    or if estimation fails for any reason — callers must handle None gracefully.
    """
    try:
        import aiohttp  # optional dependency
        import m3u8     # optional dependency
    except ImportError:
        logger.debug("estimate_stream_size: aiohttp or m3u8 not installed; skipping size estimation")
        return None

    req_headers = dict(headers or {})
    if "User-Agent" not in req_headers:
        req_headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=req_headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                manifest = await resp.text()

            playlist = m3u8.loads(manifest, uri=url)
            if playlist.is_variant:
                best = max(
                    playlist.playlists,
                    key=lambda p: p.stream_info.bandwidth if p.stream_info else 0,
                    default=None,
                )
                if best and best.absolute_uri:
                    async with session.get(best.absolute_uri, headers=req_headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status != 200:
                            return None
                        playlist = m3u8.loads(await resp.text(), uri=best.absolute_uri)

            segments = [s for s in playlist.segments if s.uri]
            if not segments:
                return None

            total = len(segments)
            if total <= 5:
                sample_urls = [s.absolute_uri for s in segments]
            else:
                indices = [0, total // 4, total // 2, 3 * total // 4, total - 1]
                sample_urls = [segments[i].absolute_uri for i in indices]

            async def _head_size(seg_url: str) -> int:
                try:
                    async with session.head(seg_url, headers=req_headers, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        if r.status == 200:
                            return int(r.headers.get("Content-Length", 0))
                except Exception:
                    pass
                return 0

            sizes = await asyncio.gather(*[_head_size(u) for u in sample_urls])
            valid = [s for s in sizes if s > 0]
            if not valid:
                return None

            avg = sum(valid) / len(valid)
            return int(avg * total)
    except Exception as e:
        logger.debug("Stream size estimation failed for %s: %s", url, e)
        return None

async def handle_client_action(websocket: WebSocket, action: str, request_id: str, payload: Dict[str, Any]):
    global SETTINGS
    try:
        if action == "get_settings":
            await websocket.send_json({
                "type": "response", "action": action, "request_id": request_id,
                "ok": True, "data": SETTINGS,
            })

        elif action == "save_settings":
            try:
                validated = SettingsUpdate(**payload)
                updates = {k: v for k, v in validated.model_dump().items() if v is not None}
            except Exception as e:
                await websocket.send_json({
                    "type": "response", "action": action, "request_id": request_id,
                    "ok": False, "error": f"Invalid settings: {e}",
                })
                return
            old_concurrency = SETTINGS.get("max_concurrent_downloads", 3)
            SETTINGS.update(updates)
            save_settings(SETTINGS)
            if "max_concurrent_downloads" in payload and payload["max_concurrent_downloads"] != old_concurrency:
                await stop_workers(app)
                await spawn_workers(app)
            await websocket.send_json({
                "type": "response", "action": action, "request_id": request_id,
                "ok": True, "data": SETTINGS,
            })
            await broadcast_progress()

        elif action == "get_health":
            await websocket.send_json({
                "type": "response", "action": action, "request_id": request_id,
                "ok": True, "data": {
                    "status": "healthy",
                    "yt_dlp_version": yt_dlp.version.__version__,
                    "active_workers": len(getattr(app.state, "workers", [])),
                },
            })

        elif action == "extract":
            url = payload.get("url")
            if not url:
                raise ValueError("URL is required for extraction")
            if url.startswith("blob:"):
                raise ValueError("blob: URLs cannot be downloaded server-side.")

            page_title = payload.get("page_title")
            headers = payload.get("headers")
            loop = asyncio.get_running_loop()

            probe_opts = build_probe_opts(headers)

            try:
                info = await loop.run_in_executor(None, _extract_with_fallback, url, probe_opts)
                extraction_method = "yt-dlp"
            except Exception as exc:
                # Extraction failed — treat as a direct/stream URL if it's HTTP
                if url.startswith(("http://", "https://")):
                    from urllib.parse import urlparse
                    path = urlparse(url).path
                    filename = os.path.basename(path) or "stream"
                    ext = os.path.splitext(filename)[1].lstrip(".") or "mp4"
                    if not (ext.isalnum() and 2 <= len(ext) <= 5):
                        ext = "mp4"
                    extraction_method = "stream" if ext.lower() in ("m3u8", "mpd", "ts") else "direct"
                    info = {
                        "title": filename,
                        "duration": None,
                        "uploader": "Direct Link",
                        "thumbnail": None,
                        "formats": [{
                            "format_id": "direct_stream",
                            "ext": ext,
                            "protocol": "m3u8" if ext.lower() == "m3u8" else "https",
                            "resolution": "unknown",
                            "vcodec": "direct",
                            "acodec": "direct",
                            "filesize": None,
                        }],
                    }
                else:
                    raise

            # Determine stream vs. non-stream.
            # If the extractor is "generic", we check if the formats indicate a stream.
            # For native extractors (like "youtube"), it should be "yt-dlp" unless it is a live stream.
            fmt_list = info.get("formats", [])
            extractor = (info.get("extractor") or "").lower()
            is_live = bool(info.get("is_live"))

            if extraction_method == "yt-dlp":
                if extractor == "generic":
                    is_stream = _formats_are_stream(fmt_list)
                else:
                    is_stream = is_live
                extraction_method = "stream" if is_stream else "yt-dlp"
            else:
                is_stream = extraction_method == "stream"

            formats = []
            for f in fmt_list:
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
                    "filesize": f.get("filesize") or f.get("filesize_estimate") or f.get("filesize_approx"),
                    "tbr": f.get("tbr"),
                    "vbr": f.get("vbr"),
                    "abr": f.get("abr"),
                    "format_note": f.get("format_note", ""),
                    "protocol": f.get("protocol", ""),
                })

            estimated_total_bytes: Optional[int] = None

            if is_stream:
                # For HLS streams: sample segment sizes to estimate total.
                # Only attempt for streams that expose an m3u8 manifest URL.
                stream_m3u8_urls = [
                    f.get("url", "") for f in fmt_list
                    if f.get("protocol", "") in ("m3u8", "m3u8_native")
                    and f.get("url")
                ]
                # Also try the original URL if it looks like a manifest
                if not stream_m3u8_urls and url.lower().endswith((".m3u8", ".mpd")):
                    stream_m3u8_urls.append(url)

                if stream_m3u8_urls:
                    sizes = await asyncio.gather(
                        *[estimate_stream_size(u, headers) for u in stream_m3u8_urls[:3]]
                    )
                    valid_sizes = [s for s in sizes if s and s > 0]
                    if valid_sizes:
                        estimated_total_bytes = max(valid_sizes)
            else:
                # For non-stream: use yt-dlp's reported filesize
                for f in fmt_list:
                    fs = f.get("filesize") or f.get("filesize_estimate") or f.get("filesize_approx")
                    if fs and fs > 0:
                        estimated_total_bytes = int(fs)
                        break

            res_data = {
                "title": sanitise_title(info.get("title") or "", url, page_title, prefer_page=is_stream),
                "duration": info.get("duration"),
                "uploader": info.get("uploader"),
                "thumbnail": info.get("thumbnail"),
                "url": url,
                "extraction_method": extraction_method,
                "formats": formats,
                "estimated_total_bytes": estimated_total_bytes,
            }
            await websocket.send_json({
                "type": "response", "action": action, "request_id": request_id,
                "ok": True, "data": res_data,
            })

        elif action == "download":
            url = payload.get("url")
            if not url:
                raise ValueError("URL is required")
            if url.startswith("blob:"):
                raise ValueError("blob: URLs cannot be downloaded server-side.")

            req_filename = payload.get("filename")
            has_custom_title = False
            if req_filename:
                filename_stem, _ = os.path.splitext(req_filename)
                task_title = filename_stem
                has_custom_title = True
            else:
                task_title = sanitise_title(
                    "", url, payload.get("page_title"),
                    prefer_page=bool(payload.get("is_stream")) or not payload.get("is_video", True),
                )

            est_bytes = payload.get("estimated_total_bytes")
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
                filename=req_filename or "",
                title=task_title,
                has_custom_title=has_custom_title,
                total_bytes=int(est_bytes) if est_bytes is not None else 0,
            )
            task._pause_event.set()
            TASKS[task.task_id] = task
            await TASK_QUEUE.put(task)
            await broadcast_progress()
            save_tasks_now()

            await websocket.send_json({
                "type": "response", "action": action, "request_id": request_id,
                "ok": True, "data": {"task_id": task.task_id, "status": task.status},
            })

        elif action == "cancel":
            task_id = payload.get("task_id")
            task = TASKS.get(task_id)
            if not task:
                raise ValueError("Task not found")
            task._cancel.set()
            task.update(status=TaskStatus.CANCELLED)
            await broadcast_progress()
            await websocket.send_json({
                "type": "response", "action": action, "request_id": request_id,
                "ok": True, "data": {"task_id": task_id, "status": TaskStatus.CANCELLED},
            })

        elif action == "pause":
            task_id = payload.get("task_id")
            task = TASKS.get(task_id)
            if not task:
                raise ValueError("Task not found")
            if task.status in (TaskStatus.DOWNLOADING, TaskStatus.QUEUED):
                task.update(status=TaskStatus.PAUSED, speed=0.0)
                task._pause_event.clear()
                await broadcast_progress()
                save_tasks_now()
            await websocket.send_json({
                "type": "response", "action": action, "request_id": request_id,
                "ok": True, "data": {"task_id": task_id, "status": task.status},
            })

        elif action == "resume":
            task_id = payload.get("task_id")
            task = TASKS.get(task_id)
            if not task:
                raise ValueError("Task not found")

            if task.status == TaskStatus.PAUSED:
                task._pause_event.set()
                if task._is_running:
                    # Download thread is still alive — simply unblock the pause gate
                    task.update(status=TaskStatus.DOWNLOADING)
                    await broadcast_progress()
                    save_tasks_now()
                else:
                    # Thread exited or never started — re-queue for a fresh download
                    task.update(status=TaskStatus.QUEUED, speed=0.0, eta=0.0, error="")
                    task._cancel.clear()
                    task._pause_event.set()
                    await TASK_QUEUE.put(task)
                    await broadcast_progress()
                    save_tasks_now()
            elif task.status in (TaskStatus.CANCELLED, TaskStatus.ERROR, TaskStatus.COMPLETED):
                is_completed = task.status == TaskStatus.COMPLETED
                task.update(status=TaskStatus.QUEUED, speed=0.0, eta=0.0, error="")
                task._cancel.clear()
                task._pause_event.set()
                if is_completed:
                    task.update(progress=0.0, total_bytes=0, downloaded_bytes=0,
                                fragment_index=None, fragment_count=None)
                await TASK_QUEUE.put(task)
                await broadcast_progress()
                save_tasks_now()

            await websocket.send_json({
                "type": "response", "action": action, "request_id": request_id,
                "ok": True, "data": {"task_id": task_id, "status": task.status},
            })

        elif action == "reveal":
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
                    subprocess.run(["xdg-open", os.path.dirname(os.path.abspath(path_to_open))], check=False)
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
                "type": "response", "action": action, "request_id": request_id,
                "ok": True, "data": {"status": "ok"},
            })

        elif action == "delete":
            task_id = payload.get("task_id")
            delete_file = bool(payload.get("delete_file", False))
            if task_id in TASKS:
                task = TASKS[task_id]
                task._cancel.set()
                task.update(status=TaskStatus.CANCELLED)

                await asyncio.get_running_loop().run_in_executor(None, cleanup_task_files, task)

                if delete_file:
                    for fp in (task.final_path, task.filename):
                        if fp:
                            try:
                                p = Path(fp)
                                if p.is_dir():
                                    shutil.rmtree(p)
                                elif p.exists():
                                    p.unlink(missing_ok=True)
                            except Exception as e:
                                logger.warning("Error deleting file %s: %s", fp, e)

                del TASKS[task_id]
                await broadcast_progress()
                save_tasks_now()

            await websocket.send_json({
                "type": "response", "action": action, "request_id": request_id,
                "ok": True, "data": {"deleted": task_id},
            })

        else:
            raise ValueError(f"Unknown action: {action}")

    except Exception as e:
        try:
            await websocket.send_json({
                "type": "response", "action": action, "request_id": request_id,
                "ok": False, "error": str(e),
            })
        except Exception:
            pass

@app.websocket("/ws/progress")
async def websocket_progress(websocket: WebSocket):
    await websocket.accept()
    WEBSOCKET_SUBSCRIBERS.append(websocket)
    try:
        await websocket.send_text(get_broadcast_payload())

        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError as e:
                try:
                    await websocket.send_json({
                        "type": "response", "action": None, "request_id": None,
                        "ok": False, "error": f"Invalid JSON payload: {e}",
                    })
                except Exception:
                    pass
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
    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False, log_level="debug")
