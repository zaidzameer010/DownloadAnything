"""
downloader.py — yt-dlp native downloading pipeline and monkey-patches.
"""
import os
import re
import time
import shutil
import asyncio
import logging
import functools
import platform
import subprocess
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.downloader.external import Aria2cFD, _BY_NAME
from yt_dlp.utils import DownloadError

from engine.config import AppSettings, APP_DATA_DIR, TMP_DIR, JsonObj
from engine.constants import GENERIC_STREAM_NAMES, MEDIA_EXTS_SET
from engine.models import DownloadTask, TaskStatus, _BROADCAST_INTERVAL
from engine.title_extractor import sanitise_title

logger = logging.getLogger("dma-engine")

# Storing mapped postprocessor statuses
PP_STATUS = {
    "Merger": TaskStatus.STITCHING,
    "FFmpegMerger": TaskStatus.STITCHING,
    "FFmpegEmbedSubtitle": TaskStatus.EMBEDDING,
    "EmbedThumbnail": TaskStatus.EMBEDDING,
}

_STREAM_PROTOCOLS = frozenset(
    {"m3u8", "m3u8_native", "dash", "rtmp", "rtmpe", "rtmps", "rtmpt", "rtmpte"}
)


def formats_are_stream(formats: list[JsonObj]) -> bool:
    """True when the dominant format protocol signals a segmented/live stream."""
    present = {f.get("protocol", "") for f in formats if f.get("protocol")}
    return bool(present & _STREAM_PROTOCOLS)


def is_safe_path(target: str | Path, roots: list[Path]) -> bool:
    try:
        resolved = Path(target).resolve()
        return any(resolved == r or r in resolved.parents for r in roots)
    except Exception:
        return False


def allowed_roots(settings: AppSettings) -> list[Path]:
    roots = [APP_DATA_DIR, TMP_DIR]
    roots.append(Path(settings.default_download_path).resolve())
    for p in settings.categories.values():
        roots.append(Path(p).resolve())
    return roots


def ensure_target_dir(target_dir: str) -> Path:
    p = Path(target_dir).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


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


class DMAAria2cFD(Aria2cFD):
    def _call_process(self, cmd: list[Any], info_dict: dict[str, Any]) -> tuple[str, str, int]:
        progress_re = re.compile(
            r"\[#\w+\s+"
            r"(?P<downloaded>[\d.]+)(?P<dl_unit>[a-zA-Z]+)/"
            r"((?P<total>[\d.]+)(?P<tot_unit>[a-zA-Z]+)|(?P<unknown>unknown))"
            r"\((?P<percent>\d+)%\)"
            r".*?"
            r"DL:(?P<speed>[\d.]+)(?P<speed_unit>[a-zA-Z/]+)"
            r"(?:\s+ETA:(?P<eta>[\w\d]+))?"
        )

        def parse_bytes(value_str: str, unit: str) -> int:
            try:
                val = float(value_str)
                unit = unit.lower()
                if 'g' in unit:
                    return int(val * 1024 * 1024 * 1024)
                elif 'm' in unit:
                    return int(val * 1024 * 1024)
                elif 'k' in unit:
                    return int(val * 1024)
                return int(val)
            except Exception:
                return 0

        def parse_eta(eta_str: str) -> int | None:
            if not eta_str:
                return None
            try:
                seconds = 0
                matches = re.findall(r'(\d+)([hms])', eta_str)
                if not matches:
                    if eta_str.isdigit():
                        return int(eta_str)
                    return None
                for val, unit in matches:
                    v = int(val)
                    if unit == 'h':
                        seconds += v * 3600
                    elif unit == 'm':
                        seconds += v * 60
                    elif unit == 's':
                        seconds += v
                return seconds
            except Exception:
                return None

        # Filter cmd args to ensure we get stdout summaries
        filtered_cmd = []
        for arg in cmd:
            arg_str = arg.decode('utf-8') if isinstance(arg, bytes) else arg
            if arg_str in ('--quiet', '-q'):
                continue
            if arg_str.startswith('--console-log-level='):
                continue
            if arg_str.startswith('--summary-interval='):
                continue
            filtered_cmd.append(arg)

        # Insert options before option terminator '--'
        try:
            idx = next(i for i, arg in enumerate(filtered_cmd) if arg in (b'--', '--'))
        except StopIteration:
            idx = len(filtered_cmd) - 1

        is_bytes = isinstance(filtered_cmd[0], bytes)
        def to_cmd_type(val: str) -> bytes | str:
            return val.encode('utf-8') if is_bytes else val

        filtered_cmd.insert(idx, to_cmd_type('--summary-interval=1'))
        filtered_cmd.insert(idx, to_cmd_type('--console-log-level=info'))

        try:
            str_cmd = [a.decode('utf-8') if isinstance(a, bytes) else a for a in filtered_cmd]
            p = subprocess.Popen(
                str_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as err:
            self.report_error(f'Unable to run external downloader: {err}')
            return "", "", -1

        assert p.stdout is not None
        try:
            for line in p.stdout:
                logger.debug("[aria2c] %s", line.strip())

                # Parse progress:
                m = progress_re.search(line)
                if m:
                    gd = m.groupdict()
                    downloaded = parse_bytes(gd["downloaded"], gd["dl_unit"])
                    total = parse_bytes(gd["total"], gd["tot_unit"]) if gd["total"] else 0
                    speed = parse_bytes(gd["speed"], gd["speed_unit"])
                    eta = parse_eta(gd["eta"]) if gd["eta"] else None

                    self._hook_progress({
                        "status": "downloading",
                        "downloaded_bytes": downloaded,
                        "total_bytes": total if total > 0 else None,
                        "speed": speed,
                        "eta": eta,
                        "filename": info_dict.get('filepath') or "",
                    }, info_dict)
        except Exception:
            try:
                p.terminate()
                p.wait(timeout=2.0)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
            raise
        finally:
            try:
                p.stdout.close()
            except Exception:
                pass

        p.wait()
        return "", "", p.returncode

_BY_NAME['aria2c'] = DMAAria2cFD


class YTDownloader:
    def __init__(
        self,
        settings: AppSettings,
        loop: asyncio.AbstractEventLoop,
        on_update: Callable[[], None],
    ) -> None:
        self.settings = settings
        self.loop = loop
        self.on_update = on_update

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
            format_spec = task.format_id
        else:
            format_spec = "bv*+ba/b"

        filename_is_generic = False
        if task.filename:
            stem = Path(task.filename).stem
            filename_is_generic = (
                not stem 
                or stem.lower() in GENERIC_STREAM_NAMES 
                or Path(task.filename).name.lower() in GENERIC_STREAM_NAMES
            )

        prefer_title_for_file = task.is_stream or task.is_video

        if task.filename and not filename_is_generic and not prefer_title_for_file:
            import yt_dlp.utils
            stem = Path(task.filename).stem
            ext = Path(task.filename).suffix.lstrip(".")
            sanitized_stem = yt_dlp.utils.sanitize_filename(stem).strip()
            if sanitized_stem and sanitized_stem.lower() not in GENERIC_STREAM_NAMES:
                safe_stem = sanitized_stem.replace("%", "%%")
                if not task.is_video and not task.is_stream and ext:
                    outtmpl = f"{safe_stem}.{ext}"
                else:
                    outtmpl = f"{safe_stem}.%(ext)s"
            else:
                filename_is_generic = True

        if not task.filename or filename_is_generic or prefer_title_for_file:
            import yt_dlp.utils
            sanitized = yt_dlp.utils.sanitize_filename(task.title).strip()
            if sanitized and sanitized != "Pending…":
                safe_stem = sanitized.replace("%", "%%")
                outtmpl = f"{safe_stem}.%(ext)s"
            else:
                outtmpl = "%(title).200B.%(ext)s"

        frag_dir = TMP_DIR / "fragments" / task.task_id
        frag_dir.mkdir(parents=True, exist_ok=True)

        postprocessors: list[JsonObj] = []
        is_media = task.is_stream or task.is_video
        if not is_media:
            ext = ""
            if task.filename:
                ext = os.path.splitext(task.filename)[1].lstrip(".").lower()
            if not ext and task.url:
                try:
                    ext = os.path.splitext(urlparse(task.url).path)[1].lstrip(".").lower()
                except Exception:
                    pass
            if ext in MEDIA_EXTS_SET:
                is_media = True

        if is_media:
            postprocessors.append({"key": "FFmpegMetadata", "add_chapters": True})

        opts: JsonObj = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "merge_output_format": self.settings.merge_output_format,
            "format": format_spec,
            "format_sort": format_sort,
            "concurrent_fragment_downloads": self.settings.concurrent_fragments,
            "compat_opts": ["allow-unsafe-ext"],
            "extractor_args": {"generic": {"impersonate": ["chrome"]}},
            "external_downloader": {
                "m3u8": "native",
                "dash": "native",
            },
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

        if is_media and self.settings.embed_thumbnail:
            opts["writethumbnail"] = True
            postprocessors.append({"key": "EmbedThumbnail"})

        if is_media and self.settings.embed_subtitles:
            opts["writesubtitles"] = True
            opts["writeautomaticsub"] = True
            opts["subtitleslangs"] = [self.settings.subtitle_language]
            postprocessors.append({"key": "FFmpegEmbedSubtitle"})

        ffmpeg_location = find_ffmpeg_location()
        if ffmpeg_location:
            opts["ffmpeg_location"] = ffmpeg_location

        aria2_exe = shutil.which("aria2c")
        if aria2_exe:
            logger.info("aria2c found at '%s', configuring as external downloader.", aria2_exe)
            task.using_aria2c = True
            if isinstance(opts.get("external_downloader"), dict):
                opts["external_downloader"]["default"] = "aria2c"
            else:
                opts["external_downloader"] = {"default": "aria2c", "m3u8": "native", "dash": "native"}
            opts["external_downloader_args"] = {
                "aria2c": [
                    "-x16",
                    "-s16",
                    "-j16",
                    "-k1M",
                    "--min-split-size=1M",
                    "--check-certificate=false",
                ]
            }

        if self.settings.rate_limit_bytes_per_sec > 0:
            opts["ratelimit"] = self.settings.rate_limit_bytes_per_sec

        proxy = self.settings.proxy.strip()
        if proxy:
            opts["proxy"] = proxy

        if task.headers:
            filtered_headers = {k: v for k, v in task.headers.items() if k.lower() != "cookie"}
            if filtered_headers:
                opts["http_headers"] = filtered_headers

        return opts

    def _progress_hook(self, task: DownloadTask, d: JsonObj) -> None:
        if task._cancel.is_set():
            raise DownloadError("Download cancelled by user")
        self.loop.call_soon_threadsafe(self._handle_progress_update, task, dict(d))

    def _handle_progress_update(self, task: DownloadTask, d: JsonObj) -> None:
        status = d.get("status")
        updates = {}
        if status == "downloading":
            updates = self._downloading_updates(task, d)
        elif status == "finished":
            part_bytes = d.get("total_bytes") or d.get("downloaded_bytes") or d.get("info_dict", {}).get("filesize") or 0
            task.prev_parts_bytes += part_bytes
            updates = {
                "downloaded_bytes": task.prev_parts_bytes,
                "total_bytes": max(task.total_bytes, task.prev_parts_bytes),
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
            self.loop.create_task(self.on_update())

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

        overall_downloaded = task.prev_parts_bytes + downloaded
        overall_total = max(task.total_bytes, task.prev_parts_bytes + total)

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
                updates["eta"] = max(0.0, (ref_total - overall_downloaded) / speed)

        filename = d.get("filename")
        if filename:
            updates["filename"] = filename
        return updates

    def _postprocessor_hook(self, task: DownloadTask, d: JsonObj) -> None:
        if task._cancel.is_set():
            raise DownloadError("Download cancelled by user")
        self.loop.call_soon_threadsafe(self._handle_postprocessor_update, task, dict(d))

    def _handle_postprocessor_update(self, task: DownloadTask, d: JsonObj) -> None:
        status = d.get("status")
        if status not in ("started", "finished"):
            return
        pp = d.get("postprocessor") or ""

        if status == "started":
            task.update(status=PP_STATUS.get(pp, TaskStatus.FINALIZING), progress=99.0)
            self.loop.create_task(self.on_update())
        elif status == "finished" and pp in ("Merger", "FFmpegMerger"):
            info = d.get("info_dict") or {}
            filepath = info.get("filepath") or info.get("_filename")
            updates: JsonObj = {"progress": 99.9}
            if filepath:
                updates["final_path"] = filepath
                updates["filename"] = filepath
            task.update(**updates)
            self.loop.create_task(self.on_update())

    def run_download(self, task: DownloadTask, opts: JsonObj) -> None:
        current_opts = dict(opts)
        while True:
            try:
                self._download_once(task, current_opts)
                return
            except DownloadError as exc:
                msg = str(exc).lower()
                has_sub_error = "unable to download video subtitles" in msg or "subtitles" in msg
                has_sub_opts = any(
                    k in current_opts
                    for k in ("writesubtitles", "writeautomaticsub", "subtitleslangs")
                )

                if has_sub_error and has_sub_opts:
                    logger.warning(
                        "Subtitle download failed (%s); disabling subtitles and retrying.",
                        exc,
                    )
                    current_opts = {
                        k: v for k, v in current_opts.items()
                        if k not in ("writesubtitles", "writeautomaticsub", "subtitleslangs")
                    }
                    if "postprocessors" in current_opts:
                        current_opts["postprocessors"] = [
                            pp for pp in current_opts["postprocessors"]
                            if pp.get("key") != "FFmpegEmbedSubtitle"
                        ]
                    continue
                raise

    def _download_once(self, task: DownloadTask, opts: JsonObj) -> None:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl._ies.pop("KnownPiracy", None)
            ydl._ies.pop("KnownDRM", None)
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
