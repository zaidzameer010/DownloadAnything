"""Download management (ported from the retired Rust backend's download manager).

One task path: yt-dlp worker threads (native downloader — continuous
progress hooks, proper token/cipher handling, multi-format merge via
ffmpeg). Concurrency is bounded by a semaphore sized from
max_concurrent_downloads; every state change persists to the TaskDb and
broadcasts a snapshot. Pause = abort + relaunch with continuedl (.part
resume) — an approximation, not a mid-flight freeze.
"""

import asyncio
import base64
import contextlib
import hashlib
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import msgspec
import orjson
import structlog
from yt_dlp import YoutubeDL as _YoutubeDL
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.utils import DownloadCancelled, determine_ext
from yt_dlp.utils._utils import _UnsafeExtensionError

from . import aria2_next, extract
from .db import TaskDb
from .filter import query_media_ext
from .log import clean_text
from .models import DownloadReq, ExtractOptions
from .settings import (
    DEFAULT_UA,
    AppSettings,
    clean_task_tmp,
    out_dir,
    resolve_ffmpeg,
    task_tmp_dir,
    tmp_root,
)

log = structlog.get_logger(__name__)


class Task:
    """One yt-dlp download worker's mutable state."""

    def __init__(self, gid: str, req: dict[str, Any]) -> None:
        self.gid = gid
        self.req = req
        self.status = "waiting"
        self.downloaded = 0
        self.total = int(req.get("expectedSize") or 0)
        self.speed = 0.0
        # Fragment progress (HLS/DASH) — the only stable signal for VBR
        # streams; byte totals swing 20-90MB on an 12MB file.
        self.frags_done = 0
        self.frags_total = 0
        # Bytes of fully-finished format files — A+V pairs download
        # sequentially, so per-file progress rides on this base.
        self.done_bytes = 0
        # Merged pair: the seeded total spans both legs, so a single leg's
        # exact size must not replace it before a leg has finished.
        self.pair = False
        self.filename = ""
        self.error = ""
        self.created_at = int(req.get("createdAt") or time.time())
        self.cancel = threading.Event()
        # Download-phase flag — before_dl postprocessors must not flip the
        # task to "postprocessing" before any byte moved.
        self.dl_seen = False
        self.gen = 0
        self.thread: threading.Thread | None = None
        self.aria2_gid: str = ""
        self.aria2_gids: set[str] = set()
        # Torrent tasks: upload rate, aria2's bittorrent view and the
        # per-file rows the picker and list render.
        self.upload_speed = 0.0
        self.torrent: dict[str, Any] = {}
        self.torrent_files: list[dict[str, Any]] = []
        # Playlist tasks: per-entry rows the accordion renders. `done` is the
        # finished-legs byte base (same accumulation task.done_bytes uses);
        # status rides waiting → active → complete/error.
        self.playlist_items: list[dict[str, Any]] = [
            {
                "index": int(it.get("index") or i + 1),
                "id": str(it.get("id") or ""),
                "ie_key": str(it.get("ieKey") or ""),
                "url": str(it.get("url") or ""),
                "title": str(it.get("title") or ""),
                "thumbnail": it.get("thumbnail"),
                "duration_secs": it.get("durationSecs"),
                "status": "waiting",
                "downloaded": 0,
                "total": 0,
                "done": 0,
                "filename": "",
                "error": "",
            }
            for i, it in enumerate((req.get("playlist") or {}).get("items") or [])
        ]
        self._pl_by_idx = {it["index"]: it for it in self.playlist_items}
        self.lock = threading.Lock()

    def terminate_proc(self) -> None:
        self.cancel.set()

    def snapshot_entry(self, download_dir: str) -> dict[str, Any]:
        """DownloadInfo-shaped dict — the snapshot carries it verbatim."""
        with self.lock:
            dl, total, speed = self.downloaded, self.total, self.speed
            status, filename, error = self.status, self.filename, self.error
            frags_done, frags_total = self.frags_done, self.frags_total
            torrent = dict(self.torrent)
            tfiles = [dict(f) for f in self.torrent_files]
            pl_items = [{k: v for k, v in it.items() if k != "done"} for it in self.playlist_items]
        req = self.req
        entry: dict[str, Any] = {
            "gid": self.gid,
            "status": status,
            "totalLength": str(total),
            "completedLength": str(dl),
            "downloadSpeed": str(int(speed)),
            "dir": download_dir,
            "createdAt": self.created_at,
            "files": [
                {
                    "path": filename,
                    "length": str(total),
                    "completedLength": str(dl),
                }
            ],
            "errorMessage": error,
            "errorCode": "0" if status != "error" else "1",
            "downloadanything": {
                "url": req.get("url", ""),
                "filenameHint": req.get("filenameHint"),
                "filename": req.get("filename"),
                "title": req.get("title"),
                "durationSecs": req.get("durationSecs"),
                "quality": req.get("quality"),
                "reprId": req.get("reprId"),
                "pageUrl": req.get("pageUrl"),
                "thumbnail": req.get("thumbnail"),
                "referer": req.get("referer"),
                "media": req.get("media"),
                "createdAt": self.created_at,
            },
        }
        if frags_total:
            entry["fragments"] = {"done": frags_done, "total": frags_total}
        if pl_items:
            pl = req.get("playlist") or {}
            entry["playlist"] = {
                "title": pl.get("title") or "",
                "url": pl.get("url") or "",
                "count": int(pl.get("count") or len(pl_items)),
                "items": pl_items,
            }
            # The displayed size/progress is the sum of every item's merged
            # (video+audio) bytes — leg accumulators equal this mid-run;
            # archive-skipped items add their on-disk size at the end.
            pl_dl = sum(i["downloaded"] for i in pl_items)
            pl_total = sum(i["total"] for i in pl_items)
            if pl_dl > dl:
                entry["completedLength"] = str(pl_dl)
                entry["files"][0]["completedLength"] = str(pl_dl)
            if pl_total > total:
                entry["totalLength"] = str(pl_total)
                entry["files"][0]["length"] = str(pl_total)
        if torrent:
            entry["bittorrent"] = torrent
            if tfiles:
                entry["files"] = tfiles
        return entry


class Registry:
    """In-memory task registry; persistence lives in TaskDb. Restored tasks
    (paused/stopped history) live here too, so one snapshot covers all."""

    def __init__(self, db: TaskDb, settings: AppSettings) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._db = db
        self._sem = threading.Semaphore(max(1, min(16, settings.max_concurrent_downloads)))

    def set_max_concurrent(self, n: int) -> None:
        """Settings changed: swap in a freshly-sized semaphore. Workers hold
        whichever semaphore object they acquired, so in-flight permits stay
        consistent — new workers queue on the new limit."""
        with self._lock:
            self._sem = threading.Semaphore(max(1, min(16, n)))

    def add(self, req: dict[str, Any], gid: str | None = None, persist: bool = True) -> Task:
        task = Task(gid or secrets.token_hex(8), req)
        with self._lock:
            self._tasks[task.gid] = task
        if persist:
            self._db.insert_task(task.gid, req, "waiting", task.total, task.created_at)
        return task

    def get(self, gid: str) -> Task | None:
        with self._lock:
            return self._tasks.get(gid)

    def remove(self, gid: str) -> Task | None:
        with self._lock:
            return self._tasks.pop(gid, None)

    def gids(self) -> set[str]:
        with self._lock:
            return set(self._tasks)

    def restore(self, pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
        """Seed the registry from TaskDb rows (newest first). Tasks that were
        live at shutdown come back 'paused'; terminal rows keep their state."""
        for info, req in pairs:
            task = Task(info["gid"], req)
            task.status = info["status"]
            task.downloaded = int(info.get("completedLength") or 0)
            task.total = int(info.get("totalLength") or 0)
            task.filename = (info.get("files") or [{}])[0].get("path", "")
            task.error = info.get("errorMessage") or ""
            if info.get("createdAt"):
                task.created_at = int(info["createdAt"])
            if task.status == "complete" and task.playlist_items:
                # Per-item states aren't persisted — a completed row shows
                # its items done; a paused one re-resolves via the archive.
                for it in task.playlist_items:
                    it["status"] = "complete"
            elif task.status == "error" and task.playlist_items:
                # A task-level error means every entry failed (partial
                # playlists complete) — don't restore them as "queued".
                for it in task.playlist_items:
                    it["status"] = "error"
                    it["error"] = it["error"] or task.error
            if aria2_next.is_torrent_req(req):
                # Torrent gids are the aria2 gids — controls reach a restored
                # torrent before any worker runs.
                task.aria2_gid = task.gid
            with self._lock:
                self._tasks[task.gid] = task
        if pairs:
            log.info("tasks restored from db", count=len(pairs))

    def launch(self, task: Task, settings: AppSettings) -> None:
        # A previous worker may still be unwinding (terminate only flags
        # cancel) — join it inside the new thread before clearing cancel,
        # or two writers race the same .part/output and postprocessors run
        # twice. The gen bump and thread stash share the worker's staleness
        # lock: concurrent launches must serialize, or two workers can both
        # read the same gen and both pass the check below.
        with task.lock:
            prev = task.thread
            was_waiting = task.status == "waiting"
            task.gen += 1
            gen = task.gen
            task.status = "waiting"
        if not was_waiting:
            self._db.update_status(task.gid, "waiting")

        def chained() -> None:
            if prev is not None and prev.is_alive():
                prev.join()
            with task.lock:
                if task.gen != gen or task.status != "waiting":
                    # A newer launch or a pause/remove during the unwind
                    # superseded this worker — don't clear its cancel.
                    return
                task.cancel.clear()
                task.error = ""
                task.dl_seen = False
                task.done_bytes = 0
                task.frags_done = 0
                task.frags_total = 0
                task.speed = 0.0
            with self._sem:
                with task.lock:
                    if task.gen != gen or task.status != "waiting" or task.cancel.is_set():
                        return
                _run_task(task, settings, self._db)

        t = threading.Thread(target=chained, daemon=True, name=f"dl-{task.gid}")
        with task.lock:
            task.thread = t
        t.start()

    def entries(self, s: AppSettings) -> list[dict[str, Any]]:
        with self._lock:
            tasks = list(self._tasks.values())
        return [t.snapshot_entry(out_dir(s, t.req.get("category"))) for t in tasks]

    def all(self) -> list[Task]:
        with self._lock:
            return list(self._tasks.values())


class AppState(Protocol):
    """The slice of `app.Shared` the download layer needs."""

    db: TaskDb
    registry: Registry

    def settings(self) -> AppSettings: ...


def _progress_hook(task: Task, db: TaskDb) -> Callable[[dict[str, Any]], None]:
    # Progress persists to SQLite at most every 800ms (Rust manager parity).
    last_persist = 0.0
    # Fragment legs (A+V pairs download sequentially): each leg reports its
    # own fragment_index/count, so counts accumulate on a finished-legs base
    # — otherwise the bar resets to 0 when the audio leg starts.
    leg_frags = 0
    frags_base = 0
    playlist = bool(task._pl_by_idx)

    def hook(d: dict[str, Any]) -> None:
        nonlocal last_persist, leg_frags, frags_base
        if task.cancel.is_set():
            raise DownloadCancelled("aborted by user")
        status = d.get("status")
        persist = None
        item = None
        if playlist:
            idx = (d.get("info_dict") or {}).get("playlist_index")
            item = task._pl_by_idx.get(idx)
        with task.lock:
            task.dl_seen = True
            if status == "paused":
                task.status = "paused"
                task.speed = 0.0
            elif status == "downloading":
                task.status = "active"
                dl = d.get("downloaded_bytes") or 0
                # Monotonic floor: on a relaunched task done_bytes restarts
                # at 0 while a finished leg isn't re-reported — never let the
                # bar dip below already-downloaded progress (db-persisted).
                task.downloaded = max(task.downloaded, task.done_bytes + dl)
                if item is not None:
                    item["status"] = "active"
                    item["downloaded"] = item["done"] + dl
                if fc := d.get("fragment_count"):
                    # Fragment downloads (HLS/DASH): byte estimates swing
                    # with fragment-size variance — the fragment count is
                    # the stable progress signal; no byte total.
                    if item is not None and (est := d.get("total_bytes_estimate")):
                        # Per-item rows still need a fraction for the
                        # item-based playlist bar.
                        item["total"] = max(item["total"], item["done"] + est)
                    if not playlist:
                        # Playlist workers share the task; a task-level
                        # fragment counter would be clobbered by every
                        # parallel worker — item rows are its signal.
                        leg_frags = fc
                        task.frags_total = frags_base + leg_frags
                        task.frags_done = frags_base + (d.get("fragment_index") or 0)
                else:
                    if task.frags_total and task.frags_done >= task.frags_total:
                        # A byte leg after finished fragment legs (mixed HLS
                        # video + progressive audio): the fragment bar would
                        # sit at 100% for the rest of the task — drop it and
                        # ride the byte totals instead.
                        task.frags_total = task.frags_done = 0
                        frags_base = 0
                    if tb := d.get("total_bytes"):
                        # Exact size (progressive) — authoritative.
                        if item is not None:
                            item["total"] = max(item["total"], item["done"] + tb)
                        if playlist:
                            # Item sums are the playlist row's truth; the
                            # shared leg base only makes this a floor.
                            task.total = max(task.total, task.done_bytes + tb)
                        else:
                            exact = task.done_bytes + tb
                            # A merged pair's first leg reports only its own
                            # size — the seeded pair total wins until a leg
                            # has finished. Every other case is the true
                            # total, so an overestimated probe size cannot
                            # cap the bar below 100%.
                            task.total = (
                                max(task.total, exact)
                                if task.pair and not task.done_bytes
                                else exact
                            )
                            task.total = max(task.total, task.downloaded)
                    elif est := d.get("total_bytes_estimate"):
                        task.total = max(task.total, task.done_bytes + est)
                        if item is not None:
                            item["total"] = max(item["total"], item["done"] + est)
                task.speed = d.get("speed") or 0.0
            elif status == "finished":
                # The leg is done — a stale speed would keep inflating the
                # header/row readout through postprocessing.
                task.speed = 0.0
                if leg_frags:
                    frags_base += leg_frags
                    leg_frags = 0
                    task.frags_done = frags_base
                    task.frags_total = max(task.frags_total, frags_base)
                leg = d.get("total_bytes") or d.get("downloaded_bytes") or 0
                task.done_bytes += leg
                task.downloaded = max(task.downloaded, task.done_bytes)
                if not task.frags_total:
                    # Fragment legs have no byte total — faking one from the
                    # leg's own bytes makes the next leg read done > total.
                    task.total = max(task.total, task.downloaded)
                if item is not None:
                    item["done"] += leg
                    item["downloaded"] = item["done"]
                    item["total"] = max(item["total"], item["done"])
            if fn := d.get("filename"):
                if playlist:
                    # The playlist row names the playlist, not whichever
                    # leg happens to be in flight.
                    if item is not None:
                        item["filename"] = fn
                else:
                    task.filename = fn
            now = time.monotonic()
            if now - last_persist > 0.8:
                last_persist = now
                persist = (
                    task.gid,
                    task.status,
                    task.downloaded,
                    task.total,
                    int(task.speed),
                )
        # The SQLite commit stays outside task.lock — a WAL write must not
        # stall snapshot readers queued on the same lock.
        if persist:
            db.update_progress(*persist)

    return hook


def _safe_ext(info: dict[str, Any]) -> str:
    """Replacement for a URL-derived ext yt-dlp would reject as unsafe
    (script URLs like remote_control.php). Prefer the picked format's
    container ext, then a sane media default."""
    cands = [
        *(f.get("ext") for f in info.get("requested_formats") or ()),
        info.get("container"),
        "mp4" if info.get("vcodec") != "none" else "m4a",
        "mp4",
    ]
    for c in cands:
        if c and c != "none":
            try:
                return _UnsafeExtensionError.sanitize_extension(c)
            except _UnsafeExtensionError:
                continue
    return "mp4"


def _url_media_ext(url: str) -> str | None:
    """Media ext hidden in a script-path URL's query — the server puts the
    real file name as `remote_control.php?file=…_1440p.mp4&acctoken=…`."""
    if not url:
        return None
    try:
        return query_media_ext(urlsplit(url).query)
    except ValueError:
        return None


def _sanitize_ext(info: dict[str, Any], force: bool = False) -> None:
    """Replace an unsafe URL-derived ext (script paths) with the real
    media ext. Must run before the output filename is prepared — yt-dlp
    derives `%(ext)s` from the URL path, so `remote_control.php` would
    otherwise land as `<title>.php` (and merge to `<title>.php.mkv`)."""
    try:
        _UnsafeExtensionError.sanitize_extension(str(info.get("ext") or ""))
        return
    except _UnsafeExtensionError:
        pass
    if not force and not (info.get("url") or info.get("requested_formats")):
        return  # playlist/metadata dict — no media filename to fix
    formats = list(info.get("requested_formats") or ())
    # The merged output's container follows the video leg.
    formats.sort(key=lambda f: f.get("vcodec") in (None, "none"))
    for f in formats:
        if ext := _url_media_ext(str(f.get("url") or "")):
            info["ext"] = ext
            return
    if ext := _url_media_ext(str(info.get("url") or "")):
        info["ext"] = ext
        return
    info["ext"] = _safe_ext(info)


class YoutubeDL(_YoutubeDL):
    """yt-dlp names the output from the URL path before before_dl
    postprocessors run — sanitize the ext at the point the name is decided."""

    def prepare_filename(
        self,
        info_dict: dict[str, Any],
        dir_type: str = "",
        *,
        outtmpl: Any = None,
        warn: bool = False,
    ) -> str:
        _sanitize_ext(info_dict)
        return super().prepare_filename(info_dict, dir_type, outtmpl=outtmpl, warn=warn)


class _ExpectPP(PostProcessor):
    """before_dl postprocessor — seeds the task total with the merged
    video+audio size (yt-dlp sums requested_formats into filesize_approx)
    so progress spans the whole pair instead of resetting per file."""

    def __init__(self, task: Task, settings: AppSettings | None = None) -> None:
        super().__init__()
        self._task = task
        self._settings = settings

    def run(self, info: dict[str, Any]) -> Any:
        # Safety net for a selected format whose ext slipped through —
        # existing_video_file rejects unsafe extensions outright.
        _sanitize_ext(info, force=True)
        if self._settings is not None:
            aria2_next.prepare_info_for_external(info, self._settings)
        pair = len(info.get("requested_formats") or ()) > 1
        size = info.get("filesize") or info.get("filesize_approx")
        if not size and "requested_formats" in info:
            size = sum(
                (f.get("filesize") or f.get("filesize_approx") or 0)
                for f in info.get("requested_formats") or []
            )
        with self._task.lock:
            # Merged pairs download as sequential legs — the progress hook
            # must not mistake the first leg's exact size for the pair total.
            self._task.pair = self._task.pair or pair
            if size:
                self._task.total = max(self._task.total, int(size))
        return [], info


class _ReqThumbnailPP(PostProcessor):
    """Non-yt-dlp sites have no extractor thumbnails — the request's
    captured thumbnail (shown in the UI) is the only cover art. Fetches it
    into the task tmp dir and injects it into info['thumbnails'] so the
    registered EmbedThumbnail PP embeds it with yt-dlp's own container
    handling. Skipped when the extractor already provided a thumbnail."""

    MAX_BYTES = 32 * 1024 * 1024

    def __init__(self, task: Task, tmp_dir: str) -> None:
        super().__init__()
        self._task = task
        self._tmp = Path(tmp_dir)

    def run(self, info: dict[str, Any]) -> Any:
        if any(
            t.get("filepath") and os.path.exists(t["filepath"])
            for t in info.get("thumbnails") or []
        ):
            return [], info
        url = self._task.req.get("thumbnail")
        if not url:
            return [], info
        ext = determine_ext(url, "jpg").lower()
        if ext not in ("jpg", "jpeg", "png", "webp", "avif"):
            ext = "jpg"
        path = self._tmp / f"cover.{ext}"
        try:
            self._tmp.mkdir(parents=True, exist_ok=True)
            # urlopen rides the ydl's headers/cookies/impersonation — the
            # same session the media fetches use.
            data = self._downloader.urlopen(url).read(self.MAX_BYTES + 1)
            if len(data) > self.MAX_BYTES:
                self.report_warning("thumbnail too large — skipped")
                return [], info
            path.write_bytes(data)
            if _sniff_image_ext(str(path)) is None:
                # A login/error page served as an image would fail the embed
                # chain — drop it and let the download finish without a cover.
                self.report_warning("thumbnail is not an image — skipped")
                with contextlib.suppress(OSError):
                    path.unlink()
                return [], info
        except Exception as e:
            self.report_warning(f"thumbnail fetch failed: {e}")
            return [], info
        info["thumbnails"] = [{"url": url, "filepath": str(path)}]
        return [], info


def _sniff_image_ext(path: str) -> str | None:
    """Real image format from magic bytes — CDNs serve AVIF at .jpg URLs
    and the convertor trusts the extension, embedding AVIF tagged jpeg."""
    try:
        with open(path, "rb") as f:
            h = f.read(16)
    except OSError:
        return None
    if h[:3] == b"\xff\xd8\xff":
        return "jpg"
    if h[:4] == b"\x89PNG":
        return "png"
    if h[:4] == b"GIF8":
        return "gif"
    if h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        return "webp"
    if h[4:8] == b"ftyp":  # ISO-BMFF stills: avif/heic — ffmpeg re-encodes
        return "avif"
    return None


def _avif_to_jpg(ffmpeg: str, path: str) -> bool:
    """Transcode AVIF/HEIC bytes to JPEG in place. yt-dlp's convertor feeds
    thumbnails through `-f image2`, which can't probe ISO-BMFF stills — the
    mov demuxer + dav1d can, so this runs before the convertor sees them."""
    tmp = path + ".conv.jpg"
    try:
        subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-y",
                "-i",
                path,
                "-frames:v",
                "1",
                "-update",
                "1",
                "-bsf:v",
                "mjpeg2jpeg",
                tmp,
            ],
            check=True,
            capture_output=True,
        )
        os.replace(tmp, path)
        return True
    except (OSError, subprocess.CalledProcessError):
        with contextlib.suppress(OSError):
            os.remove(tmp)
        return False


def _fix_thumbnail_exts(
    info: dict[str, Any], ftm: dict[str, Any], ffmpeg: str | None = None
) -> None:
    """Correct mislabeled thumbnail formats so FFmpegThumbnailsConvertor
    converts instead of skipping, and EmbedThumbnail tags the right mime."""
    for t in info.get("thumbnails") or []:
        fp = t.get("filepath")
        if not fp:
            continue
        cur = os.path.splitext(fp)[1][1:].lower()
        real = _sniff_image_ext(fp)
        if real is None:
            # A login/error body served as an image would fail the embed
            # chain — drop it instead of failing the whole download.
            t["filepath"] = None
            ftm.pop(fp, None)
            continue
        if real == "avif":
            if ffmpeg and _avif_to_jpg(ffmpeg, fp):
                # Bytes are JPEG now — align the extension if it was already .avif.
                if cur in ("jpg", "jpeg"):
                    continue
                real = "jpg"
            else:
                # Undecodable here → the convertor would fail the download;
                # dropping the thumb keeps the embed chain alive.
                t["filepath"] = None
                ftm.pop(fp, None)
                continue
        elif real == (cur if cur != "jpeg" else "jpg"):
            continue
        new = os.path.splitext(fp)[0] + "." + real
        os.replace(fp, new)
        t["filepath"] = new
        dst = ftm.pop(fp, None)
        ftm[new] = new if dst in (None, fp) else os.path.splitext(dst)[0] + "." + real


def _pp_hook(task: Task) -> Callable[[dict[str, Any]], None]:
    """Postprocessor hook — carries the post-merge filepath, which the
    progress hook's tmpfile names never see. "postprocessing" is set only
    after the download phase began: before_dl processors (thumbnail
    convertor, pair-size seed) fire their hooks pre-download."""
    playlist = bool(task._pl_by_idx)

    def hook(d: dict[str, Any]) -> None:
        st = d.get("status")
        if st == "started" and task.dl_seen:
            with task.lock:
                task.status = "postprocessing"
                task.speed = 0.0
        if st in ("started", "finished"):
            info = d.get("info_dict") or {}
            if fp := info.get("filepath"):
                with task.lock:
                    if not playlist:
                        task.filename = fp
                    if fs := info.get("filesize"):
                        task.total = max(task.total, int(fs))
                    # Playlist legs: the post-merge filepath marks the item
                    # done — the progress hook's tmpfile names never saw it.
                    # Sizes stay as the leg bytes accumulated; the finish
                    # pass replaces them with the on-disk size (the hook's
                    # filesize is pre-postprocess, the path pre-move).
                    if st == "finished" and playlist:
                        it = task._pl_by_idx.get(info.get("playlist_index"))
                        if it is not None:
                            it["status"] = "complete"
                            it["filename"] = fp

    return hook


_PL_DIR_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_PL_DIR_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _playlist_dir(title: str | None) -> str:
    """Filesystem-safe playlist folder name — separators/reserved chars out,
    no leading/trailing dots or spaces, no device names, bounded length."""
    name = _PL_DIR_BAD.sub(" ", title or "")
    name = re.sub(r"\s+", " ", name).strip(" .")[:120]
    if name.upper() in _PL_DIR_RESERVED:
        name = f"_{name}"
    return name or "Playlist"


def _items_spec(items: list[dict[str, Any]]) -> str:
    """Picked playlist indices → yt-dlp's playlist_items syntax (1-3,5).
    Same range compressor as aria2's select-file syntax — one implementation."""
    return aria2_next.format_selection([int(i.get("index") or 0) for i in items])


def _archive_ids(gid: str) -> set[str]:
    """Media ids the task's download archive already recorded — paused-
    then-resumed playlist tasks skip finished entries, and the post-run
    pass reads the file to tell archive-skips from real failures."""
    try:
        lines = Path(task_tmp_dir(gid), "archive.txt").read_text().splitlines()
    except OSError:
        return set()
    return {ln.split()[-1] for ln in lines if ln.strip()}


# fMP4 legs concatenated from HLS/DASH fragments can carry trailing NOPTS
# packets (one per fragment boundary) — every copy-mux ffmpeg PP dies on
# "Can't write packet with unknown timestamp". setts repairs only missing
# timestamps (PREV_OUT* passthrough keeps real ones). Bare -bsf covers
# video+audio+subs; the ffmpeg PP output keys are enumerated since
# 'default' would leak the args to non-ffmpeg tools (AtomicParsley).
_SETTS_NOPTS = (
    "setts="
    "pts=if(eq(PTS\\,NOPTS)\\,PREV_OUTPTS+PREV_OUTDURATION\\,PTS):"
    "dts=if(eq(DTS\\,NOPTS)\\,PREV_OUTDTS+PREV_OUTDURATION\\,DTS)"
)
_TS_FIX_PPS = (
    "merger",
    "videoremuxer",
    "videoconvertor",
    "embedsubtitle",
    "metadata",
    "thumbnailsconvertor",
    "embedthumbnail",
    "fixuptimestamp",
    "fixupm3u8",
    "fixupm4a",
    "fixupstretched",
    "fixupduration",
    "fixupduplicatemoov",
)


def _merge(vsel: str) -> str:
    """video selector + the original audio track, else best audio."""
    return f"{vsel}+{extract.ORIG_AUDIO}/{vsel}+ba"


def _format_pick(req: dict[str, Any]) -> str:
    """The selected row's quality → yt-dlp format selector. Video rows pair
    with the original audio track (best audio when unmarked); audio rows
    likewise. A reprId names an exact yt-dlp format row and wins over the
    media kind — page rows reach here with media='dash' but carry real
    yt-dlp ids (MPD rep ids miss every rung and land on the height ladder
    instead). HLS variants are muxed (prefer the complete stream); split
    rows merge the picked video with audio. A muxed row at the EXACT picked
    height (`b[height=h]`) wins before the merge — one leg, no remux;
    `b[height<=h]` stays after merge because it can silently trade the
    pick for a lower muxed rung (the 1080p→360p fmt-18 trap). Height rungs
    bound every fallback to the picked size — a vanished id must never
    degrade to bare `b` (360p)."""
    quality = req.get("quality") or {}
    h, bw = quality.get("height"), quality.get("bandwidth")

    if quality.get("audioOnly"):
        return f"{extract.ORIG_AUDIO}/ba/b"  # original audio, else best available
    if rid := req.get("reprId"):
        # exact yt-dlp format row; a video row always arrives with audio —
        # its own when already muxed, else merged in (+ba).
        rid_terms = f"{rid}[acodec!=none]/{_merge(rid)}/{rid}"
        if h:
            return (
                f"{rid_terms}/b[height={h}]/{_merge(f'bv*[height<={h}]')}"
                f"/b[height<={h}]/{_merge('bv*')}/b"
            )
        if bw:
            k = max(1, bw // 1000)  # bps → yt-dlp tbr (kbps)
            return f"{rid_terms}/{_merge(f'bv*[tbr<={k}]')}/b[tbr<={k}]/{_merge('bv*')}/b"
        return f"{rid_terms}/{_merge('bv*')}/b"
    if req.get("media") in ("hls", "dash"):  # HLS/DASH manifest variants are muxed
        if h:
            pick = f"b[height<={h}]/{_merge(f'bv*[height<={h}]')}/"
        elif bw:
            k = max(1, bw // 1000)
            pick = f"b[tbr<={k}]/{_merge(f'bv*[tbr<={k}]')}/"
        else:
            pick = "b/"
        return f"{pick}{_merge('bv*')}/b"
    if h:
        return f"b[height={h}]/{_merge(f'bv*[height<={h}]')}/b[height<={h}]/{_merge('bv*')}/b"
    if bw:
        k = max(1, bw // 1000)
        return f"{_merge(f'bv*[tbr<={k}]')}/b[tbr<={k}]/{_merge('bv*')}/b"
    return f"{_merge('bv*')}/b"


def _ydl_dl_params(
    task: Task, s: AppSettings, db: TaskDb, pl_spec: str | None = None
) -> dict[str, Any]:
    req = task.req
    headers: dict[str, str] = {
        "User-Agent": DEFAULT_UA,
        "Accept-Language": extract.DEFAULT_ACCEPT_LANG,
    }
    if req.get("referer"):
        headers["Referer"] = req["referer"]
    extract.replay_ctx_headers(
        headers,
        (
            (
                c.get("url"),
                ((h.get("name"), h.get("value")) for h in c.get("headers") or ()),
            )
            for c in req.get("contexts") or []
        ),
        req.get("url") or "",
        ua_from_ctx=True,
        custom=extract._is_media_url(req.get("url") or ""),
    )

    # A verbatim filename (browser-intercepted downloads) is the whole output
    # name — never stem+ext like filenameHint. % must escape yt-dlp's
    # template interpolation or "100%.dmg" mangles it.
    out_tmpl = (
        req["filename"].replace("%", "%%")
        if req.get("filename")
        else f"{req['filenameHint']}.%(ext)s"
        if req.get("filenameHint")
        else None
    )
    pl = req.get("playlist")
    if pl:
        # Every entry lands in a playlist-named folder under the chosen out
        # dir — yt-dlp's own playlist templating (index-prefixed names) does
        # the per-item naming; the folder is the request's sanitized title.
        out_tmpl = f"{_playlist_dir(pl.get('title'))}/%(playlist_index)02d - %(title)s.%(ext)s"
    # Postprocessing is media-only — a verbatim-named binary would get an
    # ffmpeg remux pass that can only hurt it. Intercepts opt out per request.
    allow_pp = req.get("postprocess") is not False
    params: dict[str, Any] = {
        "format": _format_pick(req),
        # Relative outtmpl + paths.home=destination: partials land in the
        # per-task temp dir and yt-dlp moves finished files home itself.
        "outtmpl": out_tmpl or "%(title)s.%(ext)s",
        "paths": {
            "home": out_dir(s, req.get("category")),
            "temp": task_tmp_dir(task.gid),
            # Thumbnails/subtitles stage in the task tmp dir — never next to
            # the output file; the embed postprocessors read the tracked
            # filepath, and clean_task_tmp sweeps the dir on completion.
            "thumbnail": task_tmp_dir(task.gid),
            "subtitle": task_tmp_dir(task.gid),
        },
        "http_headers": headers,
        "continuedl": True,
        "progress_hooks": [_progress_hook(task, db)],
        "postprocessor_hooks": [_pp_hook(task)],
        "restrictfilenames": False,
        # Native HLS/DASH fragment fetches are serial otherwise — the
        # segmentation pick doubles as the fragment pool.
        "concurrent_fragment_downloads": s.parallel_segments or 8,
        # yt-dlp's native HTTP reader defaults to 1 KiB blocks — thousands
        # of Python-level reads per second on a fast link. 256 KiB keeps
        # progress granularity intact while cutting the syscall churn.
        "buffersize": 256 * 1024,
        "retries": s.retries,
        "fragment_retries": s.retries,
        "socket_timeout": s.timeout_secs,
        "postprocessor_args": {f"{pp}+ffmpeg_o": ["-bsf", _SETTS_NOPTS] for pp in _TS_FIX_PPS},
    }
    if ffmpeg := resolve_ffmpeg(s):
        params["ffmpeg_location"] = ffmpeg
    if s.cookies_file and os.path.isfile(os.path.expanduser(s.cookies_file)):
        params["cookiefile"] = os.path.expanduser(s.cookies_file)
    if s.cookies_browser and s.cookies_browser != "off":
        params["cookiesfrombrowser"] = (s.cookies_browser,)
    # Declared default: an untouched install merges to the settings pick.
    params["merge_output_format"] = s.media_format or "mp4"
    pps: list[dict[str, Any]] = []
    # Single-format streams (direct media, HLS/DASH variants) land in their
    # source container — the remuxer honors the pick everywhere, stream-copy
    # only (no re-encode, no quality loss). Runs before the embed chain.
    if allow_pp:
        pps.append({"key": "FFmpegVideoRemuxer", "preferedformat": s.media_format or "mp4"})
    extract.apply_common_ydl_opts(params, s.check_certificate, s.player_client)
    if allow_pp and s.embed_subtitles:
        # Manual subs only — writeautomaticsub stays off, so auto-generated
        # captions never make it into the container.
        params["writesubtitles"] = True
        # Live chat is a subtitle pseudo-lang — its replay JSON is never a
        # caption worth embedding.
        params["subtitleslangs"] = ["all", "-live_chat"]
        pps.append({"key": "FFmpegEmbedSubtitle"})
    if allow_pp and s.embed_metadata:
        pps.append({"key": "FFmpegMetadata"})  # chapters ride along
    elif allow_pp and s.embed_chapters:
        # Chapters without the metadata tags — same PP, metadata off.
        pps.append({"key": "FFmpegMetadata", "add_metadata": False})
    if allow_pp and s.embed_thumbnail:
        params["writethumbnail"] = True
        # post_process (not before_dl): the thumbnail write is deferred past
        # the format legs — the convertor still runs before EmbedThumbnail.
        pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "post_process"})
        # EmbedThumbnail is the LAST re-muxing PP (yt-dlp upstream order) —
        # any later ffmpeg re-mux demotes the Matroska attachment to a plain
        # video track and the cover art disappears.
        pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    if pps:
        params["postprocessors"] = pps
    if pl:
        items = pl.get("items") or []
        if pl_spec or items:
            # The UI's picked indices → yt-dlp's playlist_items spec —
            # sparse selections keep their absolute playlist_index. A
            # worker override (pl_spec) restricts this pass to its chunk.
            params["playlist_items"] = pl_spec or _items_spec(items)
        # Pause/restart skips entries already downloaded — yt-dlp records
        # each success in the task's tmp archive (swept with the task dir).
        params["download_archive"] = str(Path(task_tmp_dir(task.gid)) / "archive.txt")
        # A dead/private entry must not sink the rest of the playlist —
        # per-item errors land in the snapshot's item rows instead.
        params["ignoreerrors"] = True
    aria2_next.configure_ydl_params(params, task, s)
    return params


def _dl_extract_opts(task: Task, s: AppSettings) -> ExtractOptions:
    """The probe's extraction identity as the download can rebuild it —
    settings options plus the request's captured contexts. Referer stays
    out: the request's referer is the picked row's own, not the probe's."""
    o = s.to_downloadanything_options()
    if ctx := task.req.get("contexts"):
        o["contexts"] = ctx
    return msgspec.convert(o, ExtractOptions)


def _task_probe_path(task: Task, url: str) -> Path:
    """The task's own probe-cache file, inside the task tmp dir — survives a
    backend restart and is swept with the dir on finish/remove (and by the
    startup orphan sweep)."""
    name = hashlib.sha1(url.encode("utf-8")).hexdigest() + ".json"
    return Path(task_tmp_dir(task.gid), "probe", name)


def _task_probe_read(task: Task, url: str) -> dict[str, Any] | None:
    p = _task_probe_path(task, url)
    try:
        return orjson.loads(p.read_bytes())
    except FileNotFoundError:
        return None
    except Exception as e:
        log.debug("task probe cache unreadable", gid=task.gid, path=str(p), error=str(e))
        return None


def _task_probe_write(task: Task, url: str, info: Any) -> None:
    """tmp+rename write — playlist workers can read the shared file while a
    sibling is still resolving it."""
    try:
        raw = orjson.dumps(info)
    except Exception as e:
        log.debug("task probe cache skipped — not serializable", gid=task.gid, url=url, error=str(e))
        return
    p = _task_probe_path(task, url)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, p)
    except OSError as e:
        log.debug("task probe cache write failed", gid=task.gid, path=str(p), error=str(e))


def _download_info(ydl: YoutubeDL, task: Task, s: AppSettings, url: str) -> Any:
    """Reuse the probe's cached info dict when present — process_ie_result
    re-runs format selection under this ydl's params, so the picked row
    resolves without a second webpage/player/JS-challenge round-trip. The
    task's own copy is kept on disk (tmp/<gid>/probe) so a retry or resume
    after a backend restart replays without extracting — it lives until the
    task is finished or removed. A failed replay (stale signed URLs, odd
    artifacts) drops both layers and extracts fresh."""
    opts = _dl_extract_opts(task, s)
    # A media-file task URL (sniffed/direct rows, manifest page_urls) must
    # not hit a claiming site IE — udemy.com/assets/*.m3u8 lands on
    # UdemyIE and dies looking for a course id. Generic parses m3u8/mpd
    # manifests and progressive files natively.
    ie = "Generic" if extract._is_media_url(url) else None
    hit_opts, hit_url = opts, url
    from_disk = True
    cached = _task_probe_read(task, url)
    if cached is None:
        from_disk = False
        cached = extract.cached_info(opts, url)
        # Playlist listings cache under either probe signature — the video pass
        # caches playlist?list pages itself, the flat listing pass caches the
        # playlist a watch?v+list URL hides. Try the flipped signature on a miss.
        alt = None
        if task.req.get("playlist") and cached is None:
            alt = msgspec.structs.replace(opts, playlist=True)
            cached = extract.cached_info(alt, url)
            if cached is not None:
                hit_opts = alt
        page_url = task.req.get("pageUrl") or ""
        if cached is None and page_url and page_url != url:
            cached = extract.cached_info(opts, page_url)
            hit_url = page_url
            if cached is None and alt is not None:
                cached = extract.cached_info(alt, page_url)
                if cached is not None:
                    hit_opts = alt
        if cached is not None:
            _task_probe_write(task, url, cached)
    if cached is None:
        info = ydl.extract_info(url, download=True, ie_key=ie)
        _task_probe_write(task, url, info)
        return info
    log.info(
        "probe cache hit — skipping re-extract",
        gid=task.gid,
        url=url,
        source="disk" if from_disk else "probe",
    )
    try:
        return ydl.process_ie_result(cached, download=True)
    except Exception as e:
        if task.cancel.is_set() or isinstance(e, DownloadCancelled):
            raise
        if "Postprocessing:" in str(e):
            # ffmpeg-side failure — the cached dict downloaded fine;
            # re-extracting just reruns the same doomed pipeline.
            raise
        log.warning(
            "cached probe replay failed — re-extracting",
            gid=task.gid,
            url=url,
            error=clean_text(str(e)),
        )
        with contextlib.suppress(OSError):
            _task_probe_path(task, url).unlink(missing_ok=True)
        if not from_disk:
            extract.drop_cached_info(hit_opts, hit_url)
        info = ydl.extract_info(url, download=True, ie_key=ie)
        _task_probe_write(task, url, info)
        return info


def _reset_live_items(task: Task) -> None:
    """Pause/remove mid-run: the in-flight item goes back to waiting so a
    resume re-downloads it (done entries keep their mark)."""
    with task.lock:
        for it in task.playlist_items:
            if it["status"] == "active":
                it["status"] = "waiting"


def _finish_playlist(task: Task, infos: Any, s: AppSettings) -> str:
    """Post-run per-item resolution + the folder path the row shows. Items
    the pp hook never marked complete resolve three ways: present in some
    worker's playlist entries → done; in the download archive → skipped
    (already downloaded before this run) → done; absent → failed. Every
    resolved item takes its final path and size from disk — leg bytes are
    pre-merge and the extractor's filesize is pre-postprocess."""
    pl = task.req.get("playlist") or {}
    folder = _playlist_dir(pl.get("title") or "Playlist")
    final = str(Path(out_dir(s, task.req.get("category"))) / folder)
    seen: dict[Any, dict[str, Any]] = {}
    for info in infos if isinstance(infos, list) else [infos]:
        if not isinstance(info, dict):
            continue
        for e in info.get("entries") or []:
            if isinstance(e, dict):
                seen[e.get("playlist_index")] = e
    archived = _archive_ids(task.gid)
    folder_dir = Path(final)
    with task.lock:
        for it in task.playlist_items:
            e = seen.get(it["index"])
            if it["status"] != "complete":
                if e is not None:
                    it["status"] = "complete"
                elif it["id"] and it["id"] in archived:
                    # archive-skip — downloaded on an earlier run; not a failure.
                    it["status"] = "complete"
                else:
                    it["status"] = "error"
                    it["error"] = it["error"] or "download failed"
                    continue
            # The returned entry names the final moved file (the PP hook only
            # sees the pre-move temp name); archive-skips fall back to the
            # item's numbered output file.
            fp = ""
            if e is not None:
                rd = e.get("requested_downloads") or []
                fp = (rd[0].get("filepath") if rd else e.get("filepath")) or ""
            if not fp or not os.path.isfile(fp):
                hit = next(folder_dir.glob(f"{it['index']:02d} - *"), None)
                fp = str(hit) if hit and hit.is_file() else fp or it["filename"]
            if fp:
                it["filename"] = fp
            if fp and os.path.isfile(fp):
                size = os.path.getsize(fp)
                if size > 0:
                    it["done"] = it["downloaded"] = it["total"] = size
    return final


def _run_playlist_workers(task: Task, s: AppSettings, db: TaskDb, url: str) -> list[Any]:
    """Playlist entries download in parallel — each worker processes a
    contiguous playlist_index chunk under its own yt-dlp instance, bound by
    the concurrent-downloads setting. Hooks, tmp dir and the resume archive
    are the task's, so per-item tracking and pause work exactly like the
    single-thread path."""
    idxs = sorted(it["index"] for it in task.playlist_items)
    workers = min(max(1, s.max_concurrent_downloads or 1), len(idxs))
    step = -(-len(idxs) // workers)
    specs = [
        _items_spec([{"index": i} for i in idxs[o : o + step]]) for o in range(0, len(idxs), step)
    ]

    def work(spec: str) -> Any:
        return _ydl_download(
            task,
            s,
            db,
            _ydl_dl_params(task, s, db, spec),
            url,
            thumb_sub=f"w{spec.replace(',', '_')}",
        )

    infos: list[Any] = []
    first_err: Exception | None = None
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        # A generator here would lazily submit each worker only as the
        # previous result() returns — strictly sequential. Materialize.
        futs = [pool.submit(work, sp) for sp in specs]
        for fut in futs:
            try:
                infos.append(fut.result())
            except DownloadCancelled:
                # Pause/remove unwinds every worker — the caller's cancel
                # check reports the pause.
                pass
            except Exception as e:
                first_err = first_err or e
    if first_err is not None and not infos:
        raise first_err
    return infos


def _write_subs_parallel(
    write_subs: Callable[[dict[str, Any], str], Any],
    info: dict[str, Any],
    filename: str,
) -> list[Any]:
    """_write_subtitles loops requested_subtitles serially — 'all' stacks a
    long tail of tiny HTTP fetches after the media legs. Splitting the dict
    reuses the upstream write path (naming, existing-file checks, per-lang
    error handling) unchanged, one fetch per available track."""
    subs = info.get("requested_subtitles") or {}
    if len(subs) <= 1:
        return write_subs(info, filename) or []
    with ThreadPoolExecutor(max_workers=len(subs)) as pool:
        futs = [
            pool.submit(
                write_subs,
                {**info, "requested_subtitles": {lang: sub}},
                filename,
            )
            for lang, sub in subs.items()
        ]
        merged: list[Any] = []
        for fut in futs:
            merged.extend(fut.result() or [])
    return merged


def _ydl_download(
    task: Task, s: AppSettings, db: TaskDb, params: dict[str, Any], url: str, thumb_sub: str = ""
) -> Any:
    """One yt-dlp pass — the deferred-write machinery is per-call so
    playlist workers can run copies on parallel threads. `thumb_sub`
    namespaces the request thumbnail staging dir per worker."""
    with YoutubeDL(params) as ydl:
        # Download params may carry a cookiefile; close() would save the whole
        # jar back (decrypted browser cookies included) into the user's file.
        ydl.params["cookiefile"] = None
        # Seed the pair total before the download: A+V legs report their
        # sizes only via requested_formats — the bar would otherwise
        # reset when the audio leg starts.
        ydl.add_post_processor(_ExpectPP(task, s), when="before_dl")
        # Video+audio legs first: yt-dlp writes subtitles/thumbnails
        # ahead of the format download — stash those calls and replay
        # them at the top of post_process, where the embed PPs still
        # see the staged files (same info dict, before they fire).
        deferred: list[tuple[Any, dict[str, Any], str, str | None]] = []
        write_subs = ydl._write_subtitles
        write_thumbs = ydl._write_thumbnails
        post_process = ydl.post_process
        ydl._write_subtitles = lambda i, f: deferred.append(("s", i, f, None)) or {}
        ydl._write_thumbnails = lambda label, i, f, b=None: deferred.append((label, i, f, b)) or {}
        # Non-yt-dlp sites: the captured thumbnail is the only cover
        # art — fetched post-download so the registered EmbedThumbnail
        # (post_process, last) embeds it.
        req_thumb = None
        if (
            task.req.get("postprocess") is not False
            and s.embed_thumbnail
            and task.req.get("thumbnail")
        ):
            tmp = task_tmp_dir(task.gid)
            if thumb_sub:
                tmp = str(Path(tmp, thumb_sub))
            req_thumb = _ReqThumbnailPP(task, tmp)
            req_thumb.set_downloader(ydl)

        def deferred_post_process(
            filename: str,
            info: dict[str, Any],
            files_to_move: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            # The writes report (staged → final) file pairs that
            # post_process's __files_to_move consumes — the convertor
            # and MoveFilesAfterDownload both key off it.
            ftm = files_to_move if files_to_move is not None else {}
            # yt-dlp's dl() hands the ydl's progress hooks to the subtitle
            # FD too — those events would count caption bytes as media
            # progress and rename the task after the .vtt file. Mute them
            # for the replay (thumbnails use urlopen, not dl()).
            hooks = ydl._progress_hooks
            ydl._progress_hooks = []
            try:
                for label, i, f, b in deferred:
                    if label != "s":
                        # Extractor thumb exts can be junk ('v1691182225') —
                        # replace_extension rejects them and kills the whole
                        # download. Dropping ext falls back to the URL's ext.
                        for t in i.get("thumbnails") or []:
                            try:
                                _UnsafeExtensionError.sanitize_extension(str(t.get("ext") or ""))
                            except _UnsafeExtensionError:
                                t.pop("ext", None)
                    try:
                        ret = (
                            _write_subs_parallel(write_subs, i, f)
                            if label == "s"
                            else write_thumbs(label, i, f, b)
                        )
                    except _UnsafeExtensionError as e:
                        log.warning(
                            "skipping thumbnail write — unsafe ext", gid=task.gid, error=str(e)
                        )
                        continue
                    if ret:
                        ftm.update(dict(ret))
            finally:
                ydl._progress_hooks = hooks
            deferred.clear()
            if req_thumb:
                req_thumb.run(info)
            # Injected thumbnails (req_thumb) carry a filepath the
            # convertor must find in __files_to_move — an identity
            # entry (MoveFiles skips src==dst) satisfies it.
            for t in info.get("thumbnails") or []:
                if (fp := t.get("filepath")) and fp not in ftm:
                    ftm[fp] = fp
            _fix_thumbnail_exts(info, ftm, resolve_ffmpeg(s))
            return post_process(filename, info, ftm)

        ydl.post_process = deferred_post_process
        # Captured context cookies replay at download time, scoped to
        # the hosts the browser sent them on — the session the
        # extractor saw must reach the CDN/fragment requests too.
        if seeds := extract._cookie_seeds(task.req.get("contexts") or []):
            extract._seed_cookies(ydl, seeds)
        return _download_info(ydl, task, s, url)


def _run_task(task: Task, s: AppSettings, db: TaskDb) -> None:
    if aria2_next.is_torrent_req(task.req):
        _run_torrent_task(task, s, db)
        return
    url = task.req.get("url", "")
    log.info("task start", gid=task.gid, url=url)
    db.update_status(task.gid, "active")
    # shortcut: a playlist URL downloads every entry but only the last file
    # is tracked in the snapshot — single-item adds are the app's path.
    try:
        if task.playlist_items:
            infos = _run_playlist_workers(task, s, db, url)
            info = None
        else:
            info = _ydl_download(task, s, db, _ydl_dl_params(task, s, db), url)
        if task.cancel.is_set():
            # Cancel came from pause/remove — control already set the status.
            _reset_live_items(task)
            with task.lock:
                if task.status not in ("paused", "removed"):
                    task.status = "paused"
        elif task.playlist_items:
            # The row's target is the playlist folder; per-item statuses
            # resolve against the workers' returned playlists + archive.
            final = _finish_playlist(task, infos, s)
            with task.lock:
                failed = sum(1 for it in task.playlist_items if it["status"] == "error")
                # Item sizes now come from disk — the authoritative output
                # total (a failed item's partial bytes are not on disk).
                item_bytes = sum(
                    it["downloaded"] for it in task.playlist_items if it["status"] != "error"
                )
                task.filename = final
                if item_bytes:
                    task.downloaded = task.total = item_bytes
                else:
                    task.total = max(task.total, task.downloaded)
                final_size = task.downloaded
                if failed == len(task.playlist_items):
                    task.status = "error"
                    task.error = "all playlist items failed"
                else:
                    task.status = "complete"
                    task.error = ""
            if task.status == "complete":
                db.mark_complete(task.gid, final, final_size)
            else:
                db.mark_error(task.gid, "1", task.error)
        else:
            # The final path comes from the returned info: hooks fire while
            # the file is still in the task tmp dir — yt-dlp moves it home
            # (paths.home) after the last postprocessor.
            if isinstance(info, dict) and info.get("_type") == "playlist":
                entries = info.get("entries") or []
                info = entries[-1] if entries else info
            rd = (info or {}).get("requested_downloads") or []
            final = (
                (rd[0].get("filepath") if rd else None)
                or (info or {}).get("filepath")
                or task.filename
            )
            size = os.path.getsize(final) if final and os.path.isfile(final) else 0
            with task.lock:
                task.status = "complete"
                task.error = ""
                task.filename = final
                if size > 0:
                    # The real post-fixup file size wins — accumulated leg
                    # bytes and fragment estimates are pre-remux approximations.
                    task.total = size
                    task.downloaded = size
                else:
                    task.total = max(task.total, task.downloaded)
                final_size = size or task.downloaded
            db.mark_complete(task.gid, final, final_size)
    except DownloadCancelled:
        _reset_live_items(task)
        with task.lock:
            if task.status not in ("paused", "removed"):
                task.status = "paused"
    except Exception as e:
        if task.cancel.is_set():
            with task.lock:
                if task.status not in ("paused", "removed"):
                    task.status = "paused"
        else:
            with task.lock:
                task.status = "error"
                task.error = extract._clean_error(e)
            db.mark_error(task.gid, "1", task.error)
            log.exception("download failed", gid=task.gid, error=task.error)
    finally:
        with task.lock:
            task.speed = 0.0
            done_or_removed = task.status in ("complete", "removed")
            final_status = task.status
        if done_or_removed:
            clean_task_tmp(task.gid)
    log.info("task end", gid=task.gid, status=final_status)


# ---- torrent tasks (aria2-next libtorrent) ---------------------------------

_TORRENT_KEYS = [
    "gid",
    "status",
    "totalLength",
    "completedLength",
    "downloadSpeed",
    "uploadSpeed",
    "connections",
    "files",
    "bittorrent",
    "errorCode",
    "errorMessage",
]


def _torrent_view(st: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """aria2 tellStatus → the snapshot's bittorrent view + per-file rows."""
    bt = st.get("bittorrent") if isinstance(st.get("bittorrent"), dict) else {}
    info = bt.get("info") if isinstance(bt.get("info"), dict) else {}
    files = [
        {
            "index": str(f.get("index") or ""),
            "path": str(f.get("path") or ""),
            "length": str(f.get("length") or "0"),
            "completedLength": str(f.get("completedLength") or "0"),
            "selected": str(f.get("selected") or "false"),
        }
        for f in st.get("files") or []
        if isinstance(f, dict)
    ]
    torrent = {
        "infoHash": str(bt.get("infoHashV1") or bt.get("infoHash") or ""),
        "name": str(info.get("name") or ""),
        "mode": str(bt.get("mode") or ""),
        "state": str(bt.get("state") or ""),
        "fileSelectionState": str(bt.get("fileSelectionState") or ""),
        "numPeers": str(bt.get("numPeers") or "0"),
        "numSeeds": str(bt.get("numSeeds") or "0"),
        "uploadSpeed": str(int(float(st.get("uploadSpeed") or 0))),
        "progress": str(bt.get("progress") or ""),
        "availability": str(bt.get("availability") or ""),
        "seedingTime": str(bt.get("seedingTime") or "0"),
    }
    return torrent, files


def _apply_torrent_status(task: Task, st: dict[str, Any], gen: int | None = None) -> None:
    """Fold an aria2 tellStatus into the task's progress and status fields.
    A worker passes its generation — a superseded worker must never clobber
    the state a newer launch is setting up (pause→resume unwinds overlap)."""
    torrent, files = _torrent_view(st)
    with task.lock:
        if gen is not None and task.gen != gen:
            return
        task.downloaded = int(st.get("completedLength") or 0)
        if total := int(st.get("totalLength") or 0):
            task.total = total
        task.speed = float(st.get("downloadSpeed") or 0)
        task.upload_speed = float(st.get("uploadSpeed") or 0)
        task.torrent = torrent
        if files:
            task.torrent_files = files
            if root := aria2_next.root_path(files):
                task.filename = root
        status = str(st.get("status") or "")
        if status == "active":
            task.status = "seeding" if torrent["state"] in ("seeding", "finished") else "active"
        elif status == "paused":
            # pause-metadata parks a magnet here until the picker selects.
            task.status = "selecting" if torrent["fileSelectionState"] == "awaiting" else "paused"
        elif status == "waiting":
            task.status = "waiting"


def _submit_torrent(rpc: aria2_next.Aria2RpcClient, task: Task, s: AppSettings) -> None:
    """Attach to the torrent when aria2 already has the gid, else add it."""
    req = task.req
    gid = task.gid
    with task.lock:
        task.aria2_gid = gid
        task.aria2_gids.add(gid)
    try:
        st = rpc.tell_status(gid, ["gid", "status", "files", "bittorrent"])
    except aria2_next.Aria2RpcError:
        st = {}
    status = str(st.get("status") or "")
    if status in ("active", "waiting"):
        return
    if status == "paused":
        bt = st.get("bittorrent") if isinstance(st.get("bittorrent"), dict) else {}
        if str(bt.get("fileSelectionState") or "") == "awaiting":
            with task.lock:
                wanted = task.status in ("active", "waiting")
            if not wanted:
                return  # the picker owns the task
            # An explicit resume of a magnet awaiting selection means "all
            # files" — unpausing with a blank selection never completes.
            files = [f for f in st.get("files") or [] if isinstance(f, dict)]
            indices = [int(f["index"]) for f in files if str(f.get("index") or "").isdigit()]
            if not indices:
                # Metadata not ready — unpausing with a blank selection never
                # completes; keep it parked for the picker.
                return
            rpc.change_option(gid, {"select-file": aria2_next.format_selection(indices)})
        rpc.unpause(gid)
        return
    if status in ("complete", "error", "removed"):
        # A leftover result blocks the gid and the info hash — evict it
        # (retries while the previous task is still stopping) before adding.
        rpc.evict(gid)

    magnet = aria2_next.is_magnet(str(req.get("url") or ""))
    opts = aria2_next.task_options(s, req, dir_path=out_dir(s, req.get("category")), magnet=magnet)
    opts["gid"] = gid
    if b64 := str(req.get("torrent") or ""):
        rpc.add_torrent(b64, [], opts)
    elif magnet:
        rpc.add_uri([str(req["url"])], opts)
    else:
        data = aria2_next.fetch_torrent(
            str(req.get("url") or ""),
            aria2_next.torrent_headers(req),
            verify=s.check_certificate,
        )
        rpc.add_torrent(base64.b64encode(data).decode(), [], opts)


def _watch_seeding(task: Task, rpc: aria2_next.Aria2RpcClient, gen: int) -> None:
    """Payload done: the download slot is freed and this detached watcher
    keeps the row's seeding stats live until aria2 stops the torrent (seed
    limits) or the task is cancelled/removed."""

    def loop() -> None:
        while not task.cancel.is_set():
            try:
                st = rpc.tell_status(task.gid, _TORRENT_KEYS)
            except Exception as e:
                log.debug("seeding status poll failed", gid=task.gid, error=clean_text(str(e)))
                break
            _apply_torrent_status(task, st, gen)
            status = str(st.get("status") or "")
            if status == "complete":
                with task.lock:
                    if task.gen != gen:
                        break
                    task.status = "complete"
                    task.speed = 0.0
                    task.upload_speed = 0.0
                break
            if status in ("error", "removed", "paused"):
                break
            time.sleep(1.5)

    threading.Thread(target=loop, daemon=True, name=f"seed-{task.gid}").start()


def _run_torrent_task(task: Task, s: AppSettings, db: TaskDb) -> None:
    """aria2-next libtorrent worker: submit/attach, mirror tellStatus into
    the task, and hand seeding off to a slot-free watcher."""
    gid = task.gid
    with task.lock:
        gen = task.gen
    log.info("torrent task start", gid=gid, url=task.req.get("url"))
    db.update_status(gid, "active")
    daemon = aria2_next.get_daemon()
    try:
        rpc = daemon.ensure_running(s)
        _submit_torrent(rpc, task, s)
    except Exception as e:
        with task.lock:
            if task.gen != gen:
                return
            # A pause/remove during the daemon spawn owns the status.
            if task.cancel.is_set():
                if task.status not in ("paused", "removed"):
                    task.status = "paused"
                return
            task.status = "error"
            task.error = clean_text(str(e))
        db.mark_error(gid, "1", task.error)
        log.exception("torrent task failed", gid=gid)
        return

    last_persist = 0.0
    try:
        while True:
            if task.cancel.is_set():
                with task.lock:
                    # A newer launch owns the status once it bumped the gen.
                    if task.gen == gen and task.status not in ("paused", "removed", "selecting"):
                        task.status = "paused"
                break
            try:
                st = rpc.tell_status(gid, _TORRENT_KEYS)
            except aria2_next.Aria2RpcError as e:
                if task.cancel.is_set():
                    break
                with task.lock:
                    if task.gen != gen:
                        break
                    task.status = "error"
                    task.error = clean_text(str(e))
                db.mark_error(gid, "1", task.error)
                break
            _apply_torrent_status(task, st, gen)
            status = str(st.get("status") or "")
            state = str((st.get("bittorrent") or {}).get("state") or "")

            if status == "complete":
                with task.lock:
                    if task.gen != gen:
                        break
                    task.status = "complete"
                    task.speed = 0.0
                    task.upload_speed = 0.0
                    path, size = task.filename, task.total
                db.mark_complete(gid, path, size)
                log.info("torrent task complete", gid=gid)
                break
            if status == "error":
                msg = str(st.get("errorMessage") or "") or (
                    f"aria2-next error (code {st.get('errorCode') or '1'})"
                )
                with task.lock:
                    if task.gen != gen:
                        break
                    task.status = "error"
                    task.error = msg
                    task.speed = 0.0
                db.mark_error(gid, str(st.get("errorCode") or "1"), msg)
                log.warning("torrent task error", gid=gid, error=msg)
                break
            if status == "removed":
                break
            if status == "active" and state in ("seeding", "finished"):
                # Payload done: the app counts the task as done while seeding
                # continues in aria2 under a watcher that holds no slot.
                with task.lock:
                    if task.gen != gen:
                        break
                    task.status = "seeding"
                    path, size = task.filename, task.total
                db.mark_complete(gid, path, size)
                log.info("torrent seeding", gid=gid, name=task.torrent.get("name"))
                _watch_seeding(task, rpc, gen)
                return

            # A single-file magnet needs no picker — select it and let it run.
            if (
                str((st.get("bittorrent") or {}).get("fileSelectionState") or "") == "awaiting"
                and len(task.torrent_files) == 1
            ):
                rpc.change_option(gid, {"select-file": "1"})
                rpc.unpause(gid)
            elif status == "paused" and task.status == "selecting":
                # Park until the picker picks: holding the download slot
                # while nobody acts would starve the queue. set_torrent_files
                # unpauses and relaunches this worker.
                log.info("torrent awaiting file selection", gid=gid)
                return

            now = time.monotonic()
            if now - last_persist > 0.8 and task.status in (
                "active",
                "waiting",
                "selecting",
                "paused",
            ):
                last_persist = now
                with task.lock:
                    pstatus, done, total, speed = (
                        # The picker state is transient UI — persist as paused
                        # so a restart resumes it without a stale picker.
                        "paused" if task.status == "selecting" else task.status,
                        task.downloaded,
                        task.total,
                        int(task.speed),
                    )
                db.update_progress(gid, pstatus, done, total, speed)
            time.sleep(0.5 if status == "active" else 1.0)
    finally:
        with task.lock:
            if task.gen == gen and task.status in ("complete", "error"):
                task.speed = 0.0
    log.info("torrent task end", gid=gid, status=task.status)


def _req_to_dict(req: DownloadReq) -> dict[str, Any]:
    """DownloadReq Struct → the camelCase dict db/snapshot layers carry.
    `to_builtins` recurses into nested Structs and emits camelCase names."""
    return {
        k: v for k, v in msgspec.to_builtins(req).items() if v is not None and v != [] and v != {}
    }


def check_name_conflict(state: AppState, req: DownloadReq) -> dict[str, Any]:
    """Would the request's output name collide in its resolved dir — on
    disk or against another tracked task? Verbatim names collide only on
    the exact file; hints collide on the stem (ext is picked later).
    Returns a suggested rename with an incrementing suffix."""
    name = req.filename or req.filename_hint
    if not name or req.playlist:
        # Playlist adds name a folder, not a file — items inside never
        # collide with loose files and merge harmlessly with a rerun.
        return {"conflict": False}
    s = state.settings()
    d = out_dir(s, req.category)
    verbatim = bool(req.filename)
    stem = Path(name).stem if verbatim else name
    suffix = Path(name).suffix if verbatim else ""

    def taken(sname: str) -> bool:
        if verbatim:
            if (Path(d) / f"{sname}{suffix}").exists():
                return True
        else:
            try:
                # Casefolded: macOS/Windows filesystems collide on case alone,
                # and an unnoticed collision overwrites the existing file.
                if any(
                    f.is_file() and f.stem.casefold() == sname.casefold() for f in Path(d).iterdir()
                ):
                    return True
            except OSError:
                pass
        for t in state.registry.all():
            if out_dir(s, t.req.get("category")) != d:
                continue
            tname = t.req.get("filename") or t.req.get("filenameHint")
            if not tname:
                continue
            tstem = Path(tname).stem if t.req.get("filename") else tname
            if tstem.casefold() == sname.casefold():
                return True
        return False

    if not taken(stem):
        return {"conflict": False}
    i = 2
    while i < 1000 and taken(f"{stem} ({i})"):
        i += 1
    return {
        "conflict": True,
        "dir": d,
        "suggested": f"{stem} ({i}){suffix}",
    }


async def add_download(state: AppState, req: DownloadReq) -> tuple[str | None, str | None]:
    """→ (gid, None) | (None, error). Page-URL adds resolve through yt-dlp
    (it owns extraction internally); everything else downloads directly."""
    if not req.url and not req.torrent:
        return None, "no url"
    s = state.settings()
    req_dict = _req_to_dict(req)
    req_dict.setdefault("createdAt", int(time.time()))
    if aria2_next.is_torrent_req(req_dict):
        if not aria2_next.use_aria2_next(s):
            return None, "torrents require the aria2-next engine"
    # When repr_id is set, page_url is the authoritative URL that yt-dlp
    # probed (either a supported webpage URL or a manifest URL extracted by Rust).
    elif req.repr_id and req.page_url:
        req_dict["url"] = req.page_url
    task = state.registry.add(req_dict)
    log.info(
        "task added",
        gid=task.gid,
        url=req_dict["url"],
        format=req.repr_id,
    )
    state.registry.launch(task, s)
    return task.gid, None


async def control_task(state: AppState, gid: str, action: str) -> tuple[Any, str | None]:
    """Task control. Global-scope actions (…All, purge) hit every task;
    single-task actions need a live registry entry."""
    if action in ("pauseAll", "forcePauseAll", "unpauseAll", "purge"):
        return await asyncio.to_thread(_control_local, state, None, action)
    task = state.registry.get(gid)
    if task is None:
        return None, "task not found"
    # Launches workers / removes tmp trees — blocking fs, keep off-loop.
    return await asyncio.to_thread(_control_local, state, task, action)


def _control_local(state: AppState, task: Task | None, action: str) -> tuple[Any, str | None]:
    """Task control. `task=None` runs only the global-scope actions."""
    s = state.settings()
    daemon = aria2_next.get_daemon()
    match action:
        case "pauseAll" | "forcePauseAll":
            state.db.pause_all_active()
            for t in state.registry.all():
                if t.status in ("active", "waiting", "downloading", "postprocessing"):
                    with t.lock:
                        t.status = "paused"
                        t.speed = 0.0
                    t.terminate_proc()
            aria2_next.control_task_rpc(daemon, None, action)
            return "OK", None
        case "unpauseAll":
            # Per-task resume, never a blanket aria2 unpause: a magnet still
            # awaiting file selection must not start before the picker sets
            # select-file (unpausing with a blank selection never completes).
            for t in state.registry.all():
                if t.status == "paused":
                    if not aria2_next.is_torrent_req(t.req):
                        aria2_next.control_task_rpc(daemon, t, "unpause")
                    with t.lock:
                        alive = (
                            t.thread is not None and t.thread.is_alive() and not t.cancel.is_set()
                        )
                        t.status = "active" if alive else "waiting"
                    if not alive:
                        state.registry.launch(t, s)
            return "OK", None
        case "purge":
            doomed = [t for t in state.registry.all() if t.status == "complete"]
            for t in doomed:
                aria2_next.control_task_rpc(daemon, t, "remove")
                state.registry.remove(t.gid)
            aria2_next.control_task_rpc(daemon, None, "purge")
            # A purge can sweep dozens of task dirs — rmtree them in parallel,
            # bounded so a large history cannot spawn one thread per task.
            with ThreadPoolExecutor(max_workers=min(8, len(doomed) or 1)) as ex:
                for t in doomed:
                    ex.submit(clean_task_tmp, t.gid)
            state.db.purge_finished()
            return "OK", None
    if task is None:
        return None, None
    match action:
        case "pause" | "forcePause":
            # Terminal states stay terminal — pausing a finished task must
            # not resurrect it as 'paused'.
            if task.status in ("active", "waiting", "downloading", "postprocessing"):
                with task.lock:
                    task.status = "paused"
                    task.speed = 0.0
                task.terminate_proc()
                aria2_next.control_task_rpc(daemon, task, action)
                state.db.update_status(task.gid, "paused")
            return "OK", None
        case "unpause" | "resume":
            if task.status in ("paused", "waiting"):
                # Torrents unpause inside their worker, which knows whether a
                # magnet still needs a file selection.
                if not aria2_next.is_torrent_req(task.req):
                    aria2_next.control_task_rpc(daemon, task, action)
                with task.lock:
                    alive = (
                        task.thread is not None
                        and task.thread.is_alive()
                        and not task.cancel.is_set()
                    )
                    task.status = "active" if alive else "waiting"
                state.db.update_status(task.gid, task.status)
                if not alive:
                    state.registry.launch(task, s)
            return "OK", None
        case "finishMedia":
            aria2_next.control_task_rpc(daemon, task, "finishMedia")
            return "OK", None
        case "download" | "retry":
            task.terminate_proc()
            aria2_next.control_task_rpc(daemon, task, action)
            if action == "download":
                clean_task_tmp(task.gid)
                with task.lock:
                    task.aria2_gid = ""
                    task.aria2_gids.clear()
            with task.lock:
                task.status = "waiting"
                task.error = ""
                task.speed = 0.0
                if action == "download":
                    task.downloaded = 0
            state.db.update_status(task.gid, "waiting")
            state.registry.launch(task, s)
            return "OK", None
        case "openFile":
            path = task.filename
            if not path or not os.path.exists(path):
                return None, "No downloaded file associated with task"
            return _open_path(path, reveal=False)
        case "showFile":
            path = task.filename or task.snapshot_entry(out_dir(s, task.req.get("category")))["dir"]
            if not os.path.exists(path):
                return None, "No path associated with task"
            return _open_path(path, reveal=True)
        case "remove" | "forceRemove" | "delete":
            # Status first: the worker's cancel path won't flip 'removed'
            # to 'paused', so the row never flickers paused before deletion.
            with task.lock:
                task.status = "removed"
                file_to_del = task.filename
            task.terminate_proc()
            aria2_next.control_task_rpc(daemon, task, action)
            state.registry.remove(task.gid)
            state.db.delete_task(task.gid)
            clean_task_tmp(task.gid)
            # Only `delete` removes the downloaded file; `remove` drops the row.
            if action == "delete" and file_to_del and os.path.exists(file_to_del):
                try:
                    if os.path.isdir(file_to_del):
                        shutil.rmtree(file_to_del, ignore_errors=True)
                    else:
                        os.remove(file_to_del)
                except OSError as e:
                    log.warning("failed to delete file on disk", path=file_to_del, error=str(e))
            return "OK", None
        case other:
            return None, f"unknown action {other}"


def _open_path(path: str, reveal: bool) -> tuple[str | None, str | None]:
    """Open a file, or reveal it in the folder — macOS `open`, Windows
    explorer, Linux xdg-open."""
    try:
        if os.name == "nt":
            cmd = ["explorer", f"/select,{path}" if reveal else path]
        elif shutil.which("open"):  # macOS
            cmd = ["open", "-R", path] if reveal else ["open", path]
        else:
            cmd = ["xdg-open", os.path.dirname(path) if reveal else path]
        subprocess.Popen(cmd)
        return "OK", None
    except OSError as e:
        return None, str(e)


async def downloads_snapshot(state: AppState) -> dict[str, Any]:
    """GlobalStat + sectioned entries, newest first."""
    s = state.settings()
    entries = state.registry.entries(s)
    active: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    stopped: list[dict[str, Any]] = []
    total_speed = 0
    for e in entries:
        status = e["status"]
        if status in ("active", "downloading", "postprocessing"):
            active.append(e)
            total_speed += int(e["downloadSpeed"] or 0)
        elif status == "waiting":
            waiting.append(e)
        else:
            stopped.append(e)
    for section in (active, waiting, stopped):
        section.sort(key=lambda x: -(x.get("createdAt") or 0))
    return {
        "stat": {
            "downloadSpeed": str(total_speed),
            "numActive": str(len(active)),
            "numWaiting": str(len(waiting)),
            "numStopped": str(len(stopped)),
        },
        "active": active,
        "waiting": waiting,
        "stopped": stopped,
        "error": None,
    }


def sweep_orphaned_tmp(s: AppSettings, pending_gids: set[str]) -> None:
    """Tmp dirs whose task no longer exists — crash leftovers. Guarded by
    freshness: a dir written in the last hour may belong to a task the db
    lost — those are left alone."""
    try:
        root = Path(tmp_root())
        if not root.is_dir():
            return
        for p in root.iterdir():
            if p.name in pending_gids or not p.is_dir():
                continue
            newest = max(
                (f.stat().st_mtime for f in p.rglob("*") if f.is_file()),
                default=0.0,
            )
            if time.time() - newest > 3600:
                shutil.rmtree(p, ignore_errors=True)
                log.info("orphan tmp dir swept", dir=p.name)
    except OSError as e:
        log.warning("tmp sweep failed", error=e)


def restore_pending(state: AppState) -> None:
    """Startup restore: TaskDb rows → registry (paused where they were live)
    + orphaned tmp-dir sweep. No launching — the user resumes explicitly."""
    s = state.settings()
    pairs = state.db.load_all_tasks(out_dir(s))
    state.registry.restore(pairs)
    sweep_orphaned_tmp(s, state.db.all_task_gids())
