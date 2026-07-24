import sys
from pathlib import Path
import asyncio
import os
import time
import shutil
import subprocess
import re
import secrets
import mimetypes
import threading
from typing import Any, BinaryIO, Dict, List, Optional, cast

from app.engine import ytdlp_opts
from app.config import get_app_data_dir
from app.domain.exceptions import DownloadPaused
from app.schemas.settings import AppSettings
from app.engine.jobs import jobs_registry
from app.engine.title_extractor import (
    resolve_filename,
    ResolvedFilename,
    _extract_trailing_extension,
    _strip_trailing_extension,
    guess_file_extension,
)
from app.engine.stream_extractor import normalize_numeric_id_url
from app.engine.media_classify import is_direct_file_url
from app.engine.file_types import classify_mime, FILE_TYPE_VIDEO
from app.services.interfaces import IDownloadEngine
from app.utils.http import is_safe_url
from app.utils.logger import get_logger, redact_url

import mmap
import struct

import yt_dlp
from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.downloader.external import Aria2cFD
from yt_dlp.downloader import external as ytdlp_external
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.postprocessor.ffmpeg import FFmpegFixupM3u8PP, FFmpegFixupDuplicateMoovPP
from yt_dlp.utils import prepend_extension

logger = get_logger(__name__)


class YtdlpDownloader(IDownloadEngine):
    """Download video/audio/stream URLs with yt-dlp."""

    def download(
        self,
        job_id: str,
        url: str,
        output_dir: Path,
        **kwargs: Any,
    ) -> str:
        format_id = kwargs.get("format_id", "best")
        loop = kwargs.get("loop")
        event_queue = kwargs.get("event_queue")
        conflict_resolution = kwargs.get("conflict_resolution", "replace")
        referer = kwargs.get("referer")
        settings = kwargs.get("settings")
        if loop is None or event_queue is None or settings is None:
            raise RuntimeError(
                "YtdlpDownloader requires 'loop', 'event_queue', and 'settings'"
            )
        return download_video(
            job_id=job_id,
            url=url,
            format_id=format_id,
            output_dir=str(output_dir),
            loop=loop,
            event_queue=event_queue,
            settings=settings,
            conflict_resolution=conflict_resolution,
            referer=referer,
        )


class DirectDownloader(IDownloadEngine):
    """Download raw direct file URLs without yt-dlp."""

    def download(
        self,
        job_id: str,
        url: str,
        output_dir: Path,
        **kwargs: Any,
    ) -> str:
        # Direct file downloads are currently handled inside download_video.
        # This class provides the IDownloadEngine interface for those paths.
        return YtdlpDownloader().download(job_id, url, output_dir, **kwargs)


def _raise_if_paused(job_id: str) -> None:
    """Abort the current download thread if the job has been paused/removed."""
    if jobs_registry.is_paused(job_id) or jobs_registry.get_job(job_id) is None:
        logger.debug(f"Download job {job_id} is paused or removed; aborting early.")
        raise DownloadPaused()


# Strong signals that a direct or pre-signed URL has expired and needs to be
# refreshed by the user. Generic 403/410 and transport errors are deliberately
# excluded; they usually indicate auth, geo-blocking, or network issues.
_EXPIRED_PHRASES = (
    "has expired",
    "link expired",
    "url expired",
    "download expired",
    "request expired",
    "invalid signature",
    "invalid token",
    "token expired",
    "signature expired",
    "signature does not match",
    "presigned url",
    "presigned request",
)


def is_expired_url_error(error_message: str) -> bool:
    """Return True if an error message strongly suggests the URL expired."""
    lowered = (error_message or "").lower()
    return any(phrase in lowered for phrase in _EXPIRED_PHRASES)


def get_aria2_next_executable_path() -> Optional[Path]:
    # 1. Generate list of directories to search
    possible_dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        possible_dirs.append(Path(sys.executable).parent)
    else:
        possible_dirs.extend(
            [
                Path(__file__).resolve().parent.parent.parent.parent
                / "src-tauri"
                / "binaries"
                / "backend",
                Path(__file__).resolve().parent.parent.parent.parent,
                Path(__file__).resolve().parent.parent.parent,
            ]
        )

    # Priority 1: Search dynamically for any 'aria2-next-*' files matching the platform
    for d in possible_dirs:
        if d.exists():
            try:
                for item in d.iterdir():
                    if item.is_file() and item.name.startswith("aria2-next-"):
                        if sys.platform == "win32" and (
                            item.name.endswith(".exe") or "windows" in item.name
                        ):
                            return item
                        elif sys.platform == "darwin" and (
                            "macos" in item.name or "darwin" in item.name
                        ):
                            return item
                        elif sys.platform not in ("win32", "darwin") and not item.name.endswith(".exe"):
                            return item
            except Exception as exc:
                logger.warning(f"Failed to search {d} for aria2-next binary: {exc}")

    # Priority 2: Check for exact 'aria2-next' / 'aria2-next.exe' in possible dirs.
    next_name = "aria2-next.exe" if sys.platform == "win32" else "aria2-next"
    for d in possible_dirs:
        p = d / next_name
        if p.exists():
            return p

    return None


# Process-wide lock for aria2-next RPC port allocation and startup. This
# eliminates intra-process races where two concurrent downloads could select
# the same ephemeral port before aria2-next binds it.
_ARIA2_PORT_LOCK = threading.Lock()


def _validate_aria2_next_min_split_size(value: Any) -> str:
    """Sanitize the aria2-next --min-split-size argument.

    Accepts a numeric value with an optional K/M/G/T suffix, matching
    aria2-next's documented format. Falls back to the default "1M" for
    anything else so that malformed user settings cannot inject extra
    arguments or crash aria2-next.
    """
    if isinstance(value, str) and re.fullmatch(r"^\d+[KMGTkmgt]?$", value.strip()):
        return value.strip()
    logger.warning(f"Invalid aria2NextMinSplitSize {value!r}; using default '1M'")
    return "1M"


class DMAAria2NextFD(Aria2cFD):
    def report_error(self, msg: str) -> None:
        parent_report_error = getattr(super(), "report_error", None)
        if callable(parent_report_error):
            parent_report_error(msg)

    def _hook_progress(self, status: Dict[str, Any], info_dict: Dict[str, Any]) -> None:
        parent_hook_progress = getattr(super(), "_hook_progress", None)
        if callable(parent_hook_progress):
            parent_hook_progress(status, info_dict)

    def _call_process(
        self, cmd: list[str | bytes], info_dict: Dict[str, Any]
    ) -> tuple[str, str, int]:
        if self.params is None:
            return "", "", 1
        import orjson
        import socket
        import urllib.request

        def _terminate(p: Optional[subprocess.Popen]) -> None:
            if p is None or p.poll() is not None:
                return
            try:
                p.terminate()
                p.wait(timeout=2.0)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

        def _build_rpc_cmd(port: int, secret: str):
            rpc_url = f"http://127.0.0.1:{port}/jsonrpc"

            def rpc(method: str, *params: Any) -> Any:
                body = orjson.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "dma",
                        "method": method,
                        "params": [f"token:{secret}", *params],
                    }
                )
                req = urllib.request.Request(
                    rpc_url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    result = orjson.loads(resp.read())
                if "error" in result:
                    raise RuntimeError(f"aria2-next RPC error: {result['error']}")
                return result["result"]

            STRIP_PREFIX = (
                "--console-log-level=warn",
                "--summary-interval=",
                "--enable-rpc",
                "--rpc-listen-port=",
                "--rpc-secret=",
            )
            filtered_cmd: list[str | bytes] = []
            for arg in cmd:
                s = arg.decode("utf-8") if isinstance(arg, bytes) else arg
                if any(s.startswith(p) for p in STRIP_PREFIX):
                    continue
                filtered_cmd.append(arg)

            try:
                idx = next(
                    i
                    for i, a in enumerate(filtered_cmd)
                    if (a.decode("utf-8") if isinstance(a, bytes) else a) == "--"
                )
            except StopIteration:
                idx = len(filtered_cmd)

            is_bytes = bool(filtered_cmd) and isinstance(filtered_cmd[0], bytes)

            def enc(v: str) -> str | bytes:
                return v.encode("utf-8") if is_bytes else v

            for opt in [
                f"--rpc-secret={secret}",
                f"--rpc-listen-port={port}",
                "--enable-rpc=true",
            ]:
                filtered_cmd.insert(idx, enc(opt))

            str_cmd = [
                a.decode("utf-8") if isinstance(a, bytes) else a for a in filtered_cmd
            ]

            aria2_next_bin = get_aria2_next_executable_path()
            if aria2_next_bin and str_cmd:
                str_cmd[0] = str(aria2_next_bin)

            return str_cmd, rpc

        max_attempts = 3
        for attempt in range(max_attempts):
            p: Optional[subprocess.Popen] = None
            try:
                # Allocate a fresh ephemeral port and spawn aria2-next under a
                # process-wide lock. This removes intra-process races where two
                # concurrent downloads could claim the same port before aria2-next
                # binds it. Cross-process races are still mitigated by the bind
                # check and the post-start RPC verification below.
                with _ARIA2_PORT_LOCK:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _sock:
                        _sock.bind(("127.0.0.1", 0))
                        rpc_port = _sock.getsockname()[1]

                    rpc_secret = f"dma{secrets.token_hex(8)}"
                    str_cmd, rpc = _build_rpc_cmd(rpc_port, rpc_secret)

                    try:
                        p = subprocess.Popen(
                            str_cmd,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    except OSError as err:
                        self.report_error(f"Unable to run external downloader: {err}")
                        return "", "", -1

                # Wait for RPC to become available (up to 5 s)
                rpc_ready = False
                for _ in range(50):
                    if p.poll() is not None:
                        break
                    try:
                        rpc("aria2.getVersion")
                        rpc_ready = True
                        break
                    except Exception:
                        time.sleep(0.1)

                if not rpc_ready:
                    _terminate(p)
                    if attempt < max_attempts - 1:
                        logger.warning(
                            f"[aria2-next RPC] not ready on attempt {attempt + 1}; retrying with a new port"
                        )
                        continue
                    logger.warning(
                        "[aria2-next RPC] Could not connect — no live progress for this stream"
                    )

                # Poll RPC at ~100 ms for realtime progress
                KEYS = ["gid", "completedLength", "totalLength", "downloadSpeed", "status"]
                last_gid: Optional[str] = None
                active_seen = False

                try:
                    while p.poll() is None:
                        job_id = self.params.get("job_id")
                        if job_id:
                            _raise_if_paused(job_id)
                        if rpc_ready:
                            try:
                                active = rpc("aria2.tellActive", KEYS)
                                if active:
                                    active_seen = True
                                    for dl in active:
                                        last_gid = dl.get("gid")
                                        downloaded = int(dl.get("completedLength", 0))
                                        total = int(dl.get("totalLength", 0))
                                        speed = int(dl.get("downloadSpeed", 0))
                                        eta: Optional[int] = (
                                            int((total - downloaded) / speed)
                                            if speed > 0 and total > downloaded
                                            else None
                                        )
                                        self._hook_progress(
                                            {
                                                "status": "downloading",
                                                "downloaded_bytes": downloaded,
                                                "total_bytes": total if total > 0 else None,
                                                "speed": speed,
                                                "eta": eta,
                                                "filename": info_dict.get("filepath") or "",
                                            },
                                            info_dict,
                                        )
                                elif active_seen:
                                    logger.info(
                                        "[aria2-next RPC] Active download finished. Shutting down daemon..."
                                    )
                                    try:
                                        rpc("aria2.shutdown")
                                    except Exception as exc:
                                        logger.warning(f"Failed to shut down aria2-next daemon via RPC: {exc}")
                                    break
                            except DownloadPaused:
                                raise
                            except Exception as poll_err:
                                logger.debug("[aria2-next RPC] poll error: %s", poll_err)

                        time.sleep(0.1)

                    # Emit a final "finished" hook once aria2-next exits
                    if rpc_ready and last_gid:
                        try:
                            stat = rpc("aria2.tellStatus", last_gid, KEYS)
                            downloaded = int(stat.get("completedLength", 0))
                            total = int(stat.get("totalLength", 0))
                            self._hook_progress(
                                {
                                    "status": "finished",
                                    "downloaded_bytes": downloaded,
                                    "total_bytes": total if total > 0 else None,
                                    "speed": 0,
                                    "eta": 0,
                                    "filename": info_dict.get("filepath") or "",
                                },
                                info_dict,
                            )
                        except Exception as exc:
                            logger.warning(f"Failed to emit final aria2 progress hook: {exc}")

                except DownloadPaused:
                    _terminate(p)
                    raise
                except Exception:
                    _terminate(p)
                    raise

                try:
                    returncode = p.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    logger.warning("aria2-next process did not exit after shutdown; terminating it")
                    _terminate(p)
                    try:
                        returncode = p.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        returncode = p.wait()
                return "", "", returncode

            except DownloadPaused:
                _terminate(p)
                raise
            except Exception as exc:
                _terminate(p)
                if attempt < max_attempts - 1:
                    logger.warning(f"aria2-next attempt {attempt + 1} failed: {exc}; retrying")
                    continue
                raise

        # Should never be reached, but keeps the type checker happy.
        return "", "", -1


# Register DMAAria2NextFD into yt-dlp's downloader registry
external_downloader_registry = getattr(ytdlp_external, "_BY_NAME", None)
if isinstance(external_downloader_registry, dict):
    external_downloader_registry["aria2-next"] = DMAAria2NextFD
    try:
        aria2_next_bin = get_aria2_next_executable_path()
        if aria2_next_bin:
            external_downloader_registry[aria2_next_bin.stem] = DMAAria2NextFD
    except Exception as exc:
        logger.warning(f"Failed to register aria2-next downloader: {exc}")


def _probe_http_capabilities(url: str, referer: Optional[str]) -> Dict[str, Any]:
    """Perform a small, authenticated-context-free capability check for aria2."""
    import urllib.error
    import urllib.request

    if not is_safe_url(url):
        return {"status": 0, "supports_ranges": False, "content_length": None}

    headers = {"Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response_headers = response.headers
            status = response.status
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return {"status": 0, "supports_ranges": False, "content_length": None}

    accept_ranges = response_headers.get("Accept-Ranges", "").lower()
    content_type = response_headers.get("Content-Type", "").lower()
    try:
        content_length = int(response_headers.get("Content-Length", ""))
    except (TypeError, ValueError):
        content_length = None
    return {
        "status": status,
        "supports_ranges": "bytes" in accept_ranges,
        "content_length": content_length,
        "content_type": content_type,
    }


def _validate_downloaded_format(
    info: Dict[str, Any],
    requested_format_id: str,
    media_type: Optional[str] = None,
) -> None:
    from app.engine.file_types import ENGINE_STREAM, is_direct_download_type

    if not requested_format_id or requested_format_id == "best":
        return
    if media_type == ENGINE_STREAM or is_direct_download_type(media_type):
        return
    requested_ids = set(requested_format_id.split("+"))
    selected_ids: set[str] = set()
    selected_format_id = info.get("format_id")
    if isinstance(selected_format_id, str):
        selected_ids.update(selected_format_id.split("+"))
    requested_formats = cast(
        List[Dict[str, Any]], info.get("requested_formats") or []
    )
    for requested_format in requested_formats:
        if isinstance(requested_format, dict) and isinstance(
            requested_format.get("format_id"), str
        ):
            selected_ids.add(requested_format["format_id"])
    if not requested_ids.issubset(selected_ids):
        if requested_ids & selected_ids:
            logger.warning(
                f"Format fallback detected: requested {requested_format_id!r}, "
                f"yt-dlp selected {selected_format_id!r}"
            )
            return
        raise RuntimeError(
            "The selected format changed during download; probe again before retrying."
        )


def _source_container_for_remux(target_ext: str) -> str:
    """Return a safe source container that differs from the target mergeFormat.

    Uses the stdlib mimetypes database so no video container extension is
    hard-coded. The source container only needs to be different from the target
    so FFmpegVideoRemuxer actually runs; ffmpeg auto-detects the real content.
    """
    clean_target = (target_ext or "").strip().lstrip(".").lower()
    for mime in ("video/mp4", "video/x-matroska", "video/webm", "video/quicktime"):
        ext = mimetypes.guess_extension(mime, strict=False)
        if ext:
            clean_ext = ext.lstrip(".").lower()
            if clean_ext and clean_ext != clean_target:
                return clean_ext
    # Last resort: ask mimetypes for the most common video extension.
    fallback_ext = mimetypes.guess_extension("video/mp4", strict=False)
    return (fallback_ext or ".mp4").lstrip(".")


class HlsPngTsWrapperStripPP(PostProcessor):
    """Strip PNG wrappers from TikTok-style HLS segments.

    Some HLS CDNs return each segment as a tiny PNG file with the real
    MPEG-TS payload appended after the PNG IEND chunk. yt-dlp's native HLS
    downloader concatenates these files unchanged, so ffmpeg probes the
    result as a PNG image and produces a short black video. This postprocessor
    removes the PNG wrappers and reassembles the raw TS stream, leaving the
    file with its original .mp4 name so FFmpegFixupM3u8PP can then convert the
    TS-in-MP4 to a proper MP4 before the final remuxer runs.
    """

    _PNG_SIG = b"\x89PNG\r\n\x1a\n"
    _IEND = b"IEND"

    @staticmethod
    def _png_end_position(f: BinaryIO, start: int) -> int:
        """Return the byte position right after the PNG IEND chunk."""
        f.seek(start)
        if f.read(8) != HlsPngTsWrapperStripPP._PNG_SIG:
            return start
        pos = start + 8
        while True:
            f.seek(pos)
            length_bytes = f.read(4)
            if len(length_bytes) < 4:
                return pos
            length = struct.unpack(">I", length_bytes)[0]
            chunk_type = f.read(4)
            if chunk_type == HlsPngTsWrapperStripPP._IEND:
                return pos + 12
            pos += 12 + length

    def _strip_to_ts(self, src_path: str, dst_path: str) -> None:
        with open(src_path, "rb") as src, open(dst_path, "wb") as dst, mmap.mmap(
            src.fileno(), 0, access=mmap.ACCESS_READ
        ) as mm:
            png_starts: list[int] = []
            start = 0
            while True:
                pos = mm.find(self._PNG_SIG, start)
                if pos == -1:
                    break
                png_starts.append(pos)
                start = pos + 1

            prev_end = 0
            for png_start in png_starts:
                if png_start > prev_end:
                    dst.write(mm[prev_end:png_start])
                png_end = self._png_end_position(src, png_start)
                prev_end = png_end

            if prev_end < len(mm):
                dst.write(mm[prev_end:])

    def run(self, information: Any) -> Any:
        info: Dict[str, Any] = information
        path = info.get("filepath")
        if not path or not os.path.exists(path):
            return [], info

        protocol = (info.get("protocol") or "").lower()
        if not protocol.startswith("m3u8"):
            return [], info

        with open(path, "rb") as f:
            header = f.read(len(self._PNG_SIG))
        if header != self._PNG_SIG:
            return [], info

        logger.info(f"Stripping PNG wrappers from HLS stream: {path}")
        temp_ts = prepend_extension(path, "ts")
        try:
            self._strip_to_ts(path, temp_ts)
            if os.path.getsize(temp_ts) == 0:
                logger.info("PNG-TS stripper produced empty output; skipping")
                return [], info
        except (OSError, ValueError) as e:
            logger.info(f"PNG-TS stripper failed: {e}")
            return [], info

        # Keep the .mp4 filename: FFmpegFixupM3u8PP will detect the MPEG-TS
        # payload inside and convert it to a valid MP4 before remuxing.
        os.replace(temp_ts, path)
        logger.info(f"PNG-TS stripper reassembled raw TS in {path}")
        return [], info


def _add_stream_fixup_postprocessors(
    ydl: yt_dlp.YoutubeDL,
    info: Dict[str, Any],
) -> None:
    """Add stream fixups so HLS/DASH streams survive the remuxer.

    yt-dlp's process_video_result() makes a copy of the info dict and drops
    the __postprocessors list, so we must add our fixups to ydl's actual
    post_process chain instead. They are inserted at the front so they run
    before any subtitle/thumbnail embedding and before FFmpegVideoRemuxer.
    """
    downloader_cls = get_suitable_downloader(info, ydl.params)
    downloader_name = getattr(downloader_cls, "FD_NAME", None) if downloader_cls else None

    post_process = ydl._pps.get("post_process")
    if not post_process:
        return

    fixups: list[PostProcessor] = []
    if downloader_name == "hlsnative":
        fixups.append(HlsPngTsWrapperStripPP(ydl))
        fixups.append(FFmpegFixupM3u8PP(ydl))
    elif downloader_name == "dashsegments":
        if info.get("is_live") or info.get("is_dash_periods"):
            fixups.append(FFmpegFixupDuplicateMoovPP(ydl))

    for pp in reversed(fixups):
        pp.set_downloader(ydl)
        post_process.insert(0, pp)


def _sanitize_info_extensions(info: Dict[str, Any], preferred_ext: str = "") -> None:
    """Bypasses ffmpeg container errors on unsafe/non-media file extensions (like .php)."""
    UNSAFE_EXTS = {"php", "html", "htm", "js", "txt", "asp", "aspx", "jsp"}
    clean_pref = (preferred_ext or "").strip().lstrip(".").lower()

    def sanitize_dict(d: Dict[str, Any]) -> None:
        ext = d.get("ext")
        if isinstance(ext, str) and ext.lower() in UNSAFE_EXTS:
            d["ext"] = clean_pref

    sanitize_dict(info)
    for fmt in info.get("formats") or []:
        if isinstance(fmt, dict):
            sanitize_dict(fmt)
    for fmt in info.get("requested_formats") or []:
        if isinstance(fmt, dict):
            sanitize_dict(fmt)


def download_video(
    job_id: str,
    url: str,
    format_id: str,
    output_dir: str,
    loop: asyncio.AbstractEventLoop,
    event_queue: asyncio.Queue[dict[str, object]],
    settings: AppSettings,
    conflict_resolution: str = "replace",
    referer: Optional[str] = None,
) -> str:
    """
    Synchronously downloads a video using yt-dlp.
    Natively pulls cookies from the user's selected browser profile.
    Updates progress via progress_hooks by posting events to event_queue.
    Must be run inside a threadpool.
    Returns the absolute path to the downloaded file.
    """
    settings_data = settings

    # Abort immediately if the user already paused/removed this job before the
    # thread started doing any network I/O.
    _raise_if_paused(job_id)

    # Ensure the directory containing the bundled aria2-next is in the PATH so yt-dlp can locate it
    aria2_next_bin = get_aria2_next_executable_path()
    if aria2_next_bin:
        bin_dir = str(aria2_next_bin.parent)
        if bin_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        # Dynamically register the specific executable name in yt-dlp's downloader registry
        if isinstance(external_downloader_registry, dict):
            external_downloader_registry[aria2_next_bin.stem] = DMAAria2NextFD

    from app.engine.file_types import ENGINE_STREAM, is_direct_download_type

    job = jobs_registry.get_job(job_id)
    job_media_type = getattr(job, "media_type", None) if job else None
    is_direct_file = is_direct_download_type(job_media_type)
    is_stream_or_file = job_media_type == ENGINE_STREAM or is_direct_file
    is_non_native = is_direct_file

    # Determine whether this is a video download so the output container can be
    # forced to the UI-selected mergeFormat via ffmpeg remuxing.
    is_video = False
    if job:
        selected_fmt = None
        if job.formats and format_id:
            for fmt in job.formats:
                if isinstance(fmt, dict):
                    fmt_dict = cast(Dict[str, Any], fmt)
                    fmt_id = fmt_dict.get("formatId")
                else:
                    fmt_id = getattr(fmt, "formatId", None)
                if str(fmt_id) == str(format_id):
                    selected_fmt = fmt
                    break
        if not selected_fmt and job.formats:
            selected_fmt = job.formats[0]
        if selected_fmt:
            if isinstance(selected_fmt, dict):
                selected_fmt_dict = cast(Dict[str, Any], selected_fmt)
                height = selected_fmt_dict.get("height") or 0
                codec = selected_fmt_dict.get("codecFamily")
            else:
                height = getattr(selected_fmt, "height", 0)
                codec = getattr(selected_fmt, "codecFamily", None)
            is_video = bool(height and height > 0) or codec == "video"
        if not is_video and classify_mime(getattr(job, "mime", None)) == FILE_TYPE_VIDEO:
            is_video = True
    is_direct_video = is_direct_file and is_video
    skip_postprocessing = is_direct_file and not is_video

    # Overwrite download URL with direct stream/file URL if it's a fallback format
    download_url = url
    if job and is_stream_or_file and job.formats and format_id:
        for fmt in job.formats:
            fmt_id = (
                fmt.get("formatId")
                if isinstance(fmt, dict)
                else getattr(fmt, "formatId", None)
            )
            if fmt_id == format_id:
                fmt_url = (
                    fmt.get("url")
                    if isinstance(fmt, dict)
                    else getattr(fmt, "url", None)
                )
                if fmt_url:
                    download_url = fmt_url
                    logger.debug(f"Using direct format URL for download: {redact_url(download_url)}")
                break
        
        # If format_id is "best" or not matched, fall back to the first format's url
        if download_url == url and format_id == "best" and job.formats:
            first_fmt = job.formats[0]
            fmt_url = (
                first_fmt.get("url")
                if isinstance(first_fmt, dict)
                else getattr(first_fmt, "url", None)
            )
            if fmt_url:
                download_url = fmt_url
                logger.debug(f"Using direct format URL for download (best): {redact_url(download_url)}")

    # Normalize trailing numeric ids with leading zeros (some APIs reject them).
    download_url = normalize_numeric_id_url(download_url)

    # Resolve title/filename through title_extractor so no other module
    # duplicates extraction logic. Existing job hints are used when present;
    # network lookup only runs when local hints are missing or generic.
    _raise_if_paused(job_id)
    resolved_filename = None
    if is_non_native:
        existing_filename = getattr(job, "filename", None) if job else None
        existing_title = job.title if job else None

        if existing_filename:
            # Resume: reuse the existing filename so the partial file continues
            # to the same temp file instead of being renamed on every restart.
            base = os.path.basename(existing_filename)
            ext = _extract_trailing_extension(base)
            title = existing_title or _strip_trailing_extension(base) or "video"
            resolved_filename = ResolvedFilename(
                title=title,
                extension=ext,
                filename=base,
                source="existing",
            )
        else:
            preferred_ext = None
            if job and job.formats and format_id:
                for fmt in job.formats:
                    fmt_id = (
                        fmt.get("formatId")
                        if isinstance(fmt, dict)
                        else getattr(fmt, "formatId", None)
                    )
                    if str(fmt_id) == str(format_id):
                        preferred_ext = (
                            fmt.get("ext")
                            if isinstance(fmt, dict)
                            else getattr(fmt, "ext", None)
                        )
                        break
            resolved_filename = resolve_filename(
                url=download_url or url,
                filename=existing_filename,
                mime=getattr(job, "mime", None) if job else None,
                referer=referer,
                page_title=existing_title,
                preferred_ext=preferred_ext,
                timeout=3.0,
            )
        if job:
            job.title = resolved_filename.title
            job.filename = resolved_filename.filename
            jobs_registry.update_job(
                job_id, title=resolved_filename.title, filename=resolved_filename.filename
            )

    embed_thumbnail = settings_data.embedThumbnail
    embed_subs = settings_data.embedSubs
    merge_format = settings_data.mergeFormat
    browser = settings_data.cookiesFromBrowser
    use_aria2_next = settings_data.useAria2Next

    # aria2-next configurations
    aria2_next_max_connections = settings_data.aria2NextMaxConnections
    aria2_next_split = settings_data.aria2NextSplit
    aria2_next_min_split_size = _validate_aria2_next_min_split_size(settings_data.aria2NextMinSplitSize)
    aria2_next_preallocate = settings_data.aria2NextPreallocate
    aria2_next_check_certificate = settings_data.aria2NextCheckCertificate
    aria2_next_always_resume = settings_data.aria2NextAlwaysResume

    # yt-dlp customization options
    concurrent_fragment_downloads = settings_data.concurrentFragmentDownloads
    download_retries = settings_data.downloadRetries
    fragment_retries = settings_data.fragmentRetries
    ffmpeg_location = settings_data.ffmpegLocation
    rate_limit = settings_data.rateLimit

    # Look up format estimates from job
    video_est = 0
    audio_est = 0
    combined_dl = 0
    combined_total = 0
    is_stream = False
    download_finished = False

    # Check if URL itself is a manifest/stream URL
    lower_url = download_url.lower().split("?")[0]
    if (
        any(lower_url.endswith(ext) for ext in [".m3u8", ".mpd"])
        or "/manifest" in lower_url
    ):
        is_stream = True

    if job and job.formats and format_id:
        for fmt in job.formats:
            fmt_id = (
                fmt.get("formatId")
                if isinstance(fmt, dict)
                else getattr(fmt, "formatId", None)
            )
            if fmt_id == format_id:
                video_est = (
                    fmt.get("videoEstSizeBytes")
                    if isinstance(fmt, dict)
                    else getattr(fmt, "videoEstSizeBytes", None)
                ) or 0
                audio_est = (
                    fmt.get("audioEstSizeBytes")
                    if isinstance(fmt, dict)
                    else getattr(fmt, "audioEstSizeBytes", None)
                ) or 0
                is_stream = (
                    is_stream
                    or (
                        fmt.get("isStream")
                        if isinstance(fmt, dict)
                        else getattr(fmt, "isStream", False)
                    )
                    or False
                )
                break

    # Resolve isolated temp directory for downloading
    app_temp_dir = get_app_data_dir() / "temp" / job_id
    os.makedirs(app_temp_dir, exist_ok=True)

    # Simple format selector (avoiding multiple audio streams where possible)
    format_selector = format_id if format_id else "bestvideo+bestaudio/best"
    if job and is_stream_or_file:
        if not format_id or format_id == "best":
            format_selector = "best"

    # Build the output template from the single source of truth in title_extractor.
    # Escape percent signs so yt-dlp does not interpret them as template markers.
    if resolved_filename:
        clean_title = resolved_filename.title.replace("%", "%%") or "video"
    else:
        clean_title = (job.title if job and job.title else "video").replace("%", "%%")

    # Isolated temp template
    if skip_postprocessing and resolved_filename:
        out_tmpl = os.path.join(
            str(app_temp_dir),
            resolved_filename.filename.replace("%", "%%"),
        )
    else:
        # For streams/yt-dlp, lock the output name to the resolved job title so
        # generic manifest labels like "master" do not leak into the final file.
        out_tmpl = os.path.join(str(app_temp_dir), f"{clean_title}.%(ext)s")

    # Stream phase tracking – mutated from inside progress_hook closure.
    # We detect video vs audio by inspecting info_dict vcodec/acodec.
    # State is: 'single' until we see a video-only stream, then 'video',
    # then once that stream finishes we switch to 'audio'.
    initial_phase = "single"
    initial_video_done = 0.0
    initial_video_total = 0.0
    if job:
        initial_phase = job.stream_phase or "single"
        initial_video_done = job.downloaded_bytes or 0.0
        initial_video_total = job.total_bytes or 0.0

    stream_state: Dict[str, Any] = {
        "phase": initial_phase,  # 'single' | 'video' | 'audio'
        "video_done_bytes": initial_video_done,  # locked-in bytes once video stream finishes
        "video_total_bytes": initial_video_total,  # locked-in total once video stream finishes
    }

    def _detect_phase(d: Dict[str, Any]) -> str:
        """Return 'video', 'audio', or 'single' for the current stream."""
        info = d.get("info_dict") or {}
        vcodec = info.get("vcodec", "")
        acodec = info.get("acodec", "")
        if vcodec and vcodec != "none" and (not acodec or acodec == "none"):
            return "video"
        if acodec and acodec != "none" and (not vcodec or vcodec == "none"):
            return "audio"
        return "single"

    # Define progress hook
    def progress_hook(d: Dict[str, Any]):
        nonlocal combined_dl, combined_total, download_finished, video_est, audio_est
        if jobs_registry.is_paused(job_id) or jobs_registry.get_job(job_id) is None:
            logger.debug(
                f"Pause or removal event detected in progress hook for job {job_id}"
            )
            raise DownloadPaused()

        filename = d.get("filename")
        if filename:
            # Avoid setting file_path to subtitles, thumbnails or metadata info files
            lower_name = filename.lower()
            is_sub_or_meta = False
            for ignored_ext in [
                ".vtt",
                ".srt",
                ".ass",
                ".ssa",
                ".ttml",
                ".sbv",
                ".lrc",
                ".sub",
                ".idx",
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
                ".json",
                ".xml",
            ]:
                if lower_name.endswith(ignored_ext) or f"{ignored_ext}." in lower_name:
                    is_sub_or_meta = True
                    break
            if not is_sub_or_meta:
                jobs_registry.update_job(job_id, file_path=filename)

        status_str = d.get("status")
        downloaded = d.get("downloaded_bytes", 0) or 0
        total = d.get("total_bytes")
        total_est = d.get("total_bytes_estimate")
        current_total = total or total_est or 0

        # --- Phase detection ---
        detected = _detect_phase(d)
        current_phase = stream_state["phase"]

        # Dynamic size estimation from stream if not resolved in probing
        info = d.get("info_dict") or {}
        duration = info.get("duration")
        if duration and duration > 0:
            if detected == "video" and video_est == 0:
                vbr = info.get("vbr") or info.get("tbr") or 0
                if vbr > 0:
                    video_est = int(vbr * 1000 * duration / 8)
            elif detected == "audio" and audio_est == 0:
                abr = info.get("abr") or info.get("tbr") or 0
                if abr > 0:
                    audio_est = int(abr * 1000 * duration / 8)
            elif detected == "single" and video_est == 0:
                tbr = info.get("tbr") or info.get("vbr") or info.get("abr") or 0
                if tbr > 0:
                    video_est = int(tbr * 1000 * duration / 8)

        if current_phase == "single" and detected == "video":
            # Entering separate-stream mode: first stream is video
            stream_state["phase"] = "video"
            current_phase = "video"
        elif current_phase == "video" and detected == "audio":
            # Audio stream started — lock in video bytes
            stream_state["phase"] = "audio"
            current_phase = "audio"
        elif (
            current_phase == "video"
            and status_str == "finished"
            and detected == "video"
        ):
            # Video finished, not yet replaced by audio phase — still lock totals
            stream_state["video_done_bytes"] = downloaded
            stream_state["video_total_bytes"] = current_total

        # When the video stream finishes, lock in its totals so that when
        # audio starts, we can compute a true combined progress.
        if current_phase == "video" and status_str == "finished":
            stream_state["video_done_bytes"] = downloaded
            stream_state["video_total_bytes"] = current_total

        ws_status = "downloading"
        if status_str == "finished":
            if current_phase == "video":
                ws_status = "downloading"
            else:
                ws_status = "completed" if skip_postprocessing else "postprocessing"
                download_finished = True

        existing_job = jobs_registry.get_job(job_id)

        # --- Combined bytes & progress ---
        if current_phase == "video":
            vid_dl = max(downloaded, existing_job.downloaded_bytes if existing_job else 0.0)
            vid_total = total if (total and total > 0.0) else (video_est if video_est > 0.0 else (total_est if total_est and total_est > 0.0 else (existing_job.total_bytes if existing_job and existing_job.total_bytes > 0.0 else 0.0)))
            if vid_total > 0.0 and vid_dl > vid_total:
                vid_total = vid_dl
            aud_dl = 0.0
            aud_total = existing_job.audio_total_bytes if existing_job and existing_job.audio_total_bytes > 0.0 else audio_est
            combined_dl = vid_dl
            combined_total = vid_total + aud_total if aud_total > 0.0 else vid_total
        elif current_phase == "audio":
            vid_dl = (
                stream_state["video_done_bytes"]
                if stream_state["video_done_bytes"] > 0.0
                else (existing_job.downloaded_bytes if existing_job and existing_job.downloaded_bytes > 0.0 else video_est)
            )
            vid_total = (
                stream_state["video_total_bytes"]
                if stream_state["video_total_bytes"] > 0.0
                else (existing_job.total_bytes if existing_job and existing_job.total_bytes > 0.0 else video_est)
            )
            aud_dl = max(downloaded, existing_job.audio_downloaded_bytes if existing_job else 0.0)
            aud_total = total if (total and total > 0.0) else (audio_est if audio_est > 0.0 else (total_est if total_est and total_est > 0.0 else (existing_job.audio_total_bytes if existing_job and existing_job.audio_total_bytes > 0.0 else 0.0)))
            if aud_total > 0.0 and aud_dl > aud_total:
                aud_total = aud_dl
            combined_dl = vid_dl + aud_dl
            combined_total = vid_total + aud_total
        else:
            # Single-stream download
            vid_dl = max(downloaded, existing_job.downloaded_bytes if existing_job else 0.0)
            vid_total = total if (total and total > 0.0) else (video_est if video_est > 0.0 else (total_est if total_est and total_est > 0.0 else (existing_job.total_bytes if existing_job and existing_job.total_bytes > 0.0 else 0.0)))
            if vid_total > 0.0 and vid_dl > vid_total:
                vid_total = vid_dl
            aud_dl = 0.0
            aud_total = 0.0
            combined_dl = max(downloaded, existing_job.combined_downloaded_bytes if existing_job else 0.0)
            combined_total = vid_total if vid_total > 0.0 else (existing_job.combined_total_bytes if existing_job and existing_job.combined_total_bytes > 0.0 else 0.0)
            if combined_total > 0.0 and combined_dl > combined_total:
                combined_total = combined_dl

        progress_pct = 0.0
        if combined_total > 0:
            progress_pct = float(combined_dl) / float(combined_total) * 100.0

        # Calculate combined ETA
        speed = d.get("speed") or 0.0
        combined_eta = d.get("eta")
        if speed > 0 and combined_total > combined_dl:
            combined_eta = int((combined_total - combined_dl) / speed)

        payload = {
            "type": "download_progress",
            "jobId": job_id,
            "status": ws_status,
            "downloadedBytes": vid_dl,
            "totalBytes": vid_total if vid_total > 0 else total,
            "totalBytesEstimate": total_est,
            "audioDownloadedBytes": aud_dl,
            "audioTotalBytes": aud_total,
            "combinedDownloadedBytes": combined_dl,
            "combinedTotalBytes": combined_total,
            "streamPhase": current_phase,
            "speed": speed,
            "eta": combined_eta,
            "fragmentIndex": d.get("fragment_index"),
            "fragmentCount": d.get("fragment_count"),
            "filePath": filename,
        }

        loop.call_soon_threadsafe(event_queue.put_nowait, payload)

        jobs_registry.update_job(
            job_id,
            persist=False,  # high-frequency tick — skip disk write
            progress=progress_pct,
            downloaded_bytes=vid_dl,
            total_bytes=vid_total or (total or total_est or 0),
            audio_downloaded_bytes=aud_dl,
            audio_total_bytes=aud_total,
            combined_downloaded_bytes=combined_dl,
            combined_total_bytes=combined_total,
            stream_phase=current_phase,
            speed=speed,
            eta=combined_eta or 0.0,
            status=ws_status,
            fragment_index=d.get("fragment_index"),
            fragment_count=d.get("fragment_count"),
        )

    # Define postprocessor hook
    def pp_hook(d: Dict[str, Any]):
        if jobs_registry.is_paused(job_id) or jobs_registry.get_job(job_id) is None:
            raise DownloadPaused()

        if skip_postprocessing:
            return

        if not download_finished:
            return

        pp_name = d.get("postprocessor", "unknown")
        pp_status = d.get("status")

        logger.debug(f"PP hook: postprocessor={pp_name}, status={pp_status}")
        jobs_registry.update_job(job_id, persist=False, status="postprocessing")

        job = jobs_registry.get_job(job_id)

        payload = {
            "type": "download_progress",
            "jobId": job_id,
            "status": "postprocessing",
            "downloadedBytes": job.downloaded_bytes if job else None,
            "totalBytes": job.total_bytes if job else None,
            "audioDownloadedBytes": job.audio_downloaded_bytes if job else None,
            "audioTotalBytes": job.audio_total_bytes if job else None,
            "combinedDownloadedBytes": job.combined_downloaded_bytes if job else None,
            "combinedTotalBytes": job.combined_total_bytes if job else None,
            "speed": None,
            "eta": None,
        }
        loop.call_soon_threadsafe(event_queue.put_nowait, payload)

    # Rate limit parsing helper
    def parse_rate_limit(rl: Optional[str]) -> Optional[int]:
        if not rl or rl.strip().lower() == "none" or rl.strip() == "":
            return None
        rl = rl.strip().upper()
        try:
            if rl.endswith("K"):
                return int(float(rl[:-1]) * 1024)
            if rl.endswith("M"):
                return int(float(rl[:-1]) * 1024 * 1024)
            if rl.endswith("G"):
                return int(float(rl[:-1]) * 1024 * 1024 * 1024)
            return int(rl)
        except Exception:
            return None

    opts = ytdlp_opts.build_ytdlp_options(
        url=download_url,
        browser=browser,
        referer=referer,
        ffmpeg_location=ffmpeg_location,
        extra_opts={
            "noprogress": True,
            "concurrent_fragment_downloads": concurrent_fragment_downloads,
            "retries": download_retries,
            "fragment_retries": fragment_retries,
            "continuedl": True,
            "keepvideo": False,
            "windowsfilenames": True,
            "embedmetadata": not skip_postprocessing,
            "allow_unplayable_formats": False,
            "format": format_selector,
            "outtmpl": out_tmpl,
            "merge_output_format": None if skip_postprocessing else merge_format,
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [pp_hook],
            "buffersize": 1024 * 256,
            "socket_timeout": 10,
            "allow_multiple_audio_streams": False,
            # Pass the job id down to the external downloader so it can abort
            # early if the user pauses while aria2-next is still starting up.
            "job_id": job_id,
        },
    )

    parsed_rl = parse_rate_limit(rate_limit)
    if parsed_rl is not None:
        opts["ratelimit"] = parsed_rl

    # Subtitles options
    if embed_subs and not skip_postprocessing:
        opts["writesubtitles"] = True
        opts["embedsubtitles"] = True
        langs_str = settings_data.subtitlesLangs
        langs = [lang.strip() for lang in langs_str.split(",") if lang.strip()]
        opts["subtitleslangs"] = langs if (langs and "all" not in langs) else ["all"]
        opts.setdefault("postprocessors", []).append(
            {"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False}
        )

    # Thumbnail option
    if embed_thumbnail and not skip_postprocessing:
        opts["writethumbnail"] = True
        opts.setdefault("postprocessors", []).append(
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}
        )
        opts.setdefault("postprocessors", []).append(
            {"key": "EmbedThumbnail", "already_have_thumbnail": False}
        )

    # Remux videos to the UI-selected container only when the source container
    # is known to differ. Avoids unnecessary ffmpeg passes and prevents forcing
    # an already-correct container through a redundant remux.
    if is_video and merge_format:
        predicted_source = (
            guess_file_extension(
                filename=getattr(job, "filename", None),
                mime=getattr(job, "mime", None),
                url=download_url,
            )
            or ""
        )
        needs_remux = (
            not predicted_source
            or predicted_source.lower().strip(".") != merge_format.lower().strip(".")
        )
        if needs_remux:
            opts.setdefault("postprocessors", []).append(
                {"key": "FFmpegVideoRemuxer", "preferedformat": merge_format}
            )

    # Embed Metadata postprocessor
    if not skip_postprocessing:
        opts.setdefault("postprocessors", []).append(
            {
                "key": "FFmpegMetadata",
                "add_chapters": True,
                "add_infojson": "if_exists",
                "add_metadata": True,
            }
        )

    # Use aria2-next as yt-dlp's external downloader for every job when enabled.
    # Native yt-dlp download is no longer used.
    if use_aria2_next and aria2_next_bin is not None:
        http_capabilities = _probe_http_capabilities(download_url, referer)
        content_length = http_capabilities.get("content_length")
        opts["external_downloader"] = str(aria2_next_bin)
        logger.debug(
            "Using aria2-next as external downloader: content_length=%s",
            content_length,
        )
        if isinstance(content_length, int) and content_length < 8 * 1024 * 1024:
            connection_limit = 2
        else:
            connection_limit = min(max(int(aria2_next_max_connections), 1), 64)
        split_limit = min(max(int(aria2_next_split), 1), 64)

        aria2_next_args = [
            "-j",
            "1",
            "-x",
            str(connection_limit),
            "-s",
            str(split_limit),
            f"--min-split-size={aria2_next_min_split_size}",
        ]

        # File allocation: falloc on Linux/Windows for fast space reservation;
        # none on macOS (APFS) to avoid startup stalls. Prealloc is the fallback
        # when falloc is unavailable or pre-allocation is disabled.
        if aria2_next_preallocate:
            if sys.platform == "darwin":
                aria2_next_args.append("--file-allocation=none")
            else:
                aria2_next_args.append("--file-allocation=falloc")
        else:
            aria2_next_args.append("--file-allocation=none")

        # High-throughput tuning defaults (see aria2-next performance template).
        aria2_next_args.extend(
            [
                "--disk-cache=64M",
                "--summary-interval=0",
                "--continue=true",
                "--connect-timeout=10",
                "--timeout=10",
                "--retry-wait=2",
                "--max-tries=5",
                "--max-file-not-found=2",
                "--enable-http-pipelining=true",
                "--http-accept-gzip=true",
                "--async-dns=true",
            ]
        )

        if aria2_next_check_certificate:
            aria2_next_args.append("--check-certificate=true")
            try:
                import certifi

                ca_path = certifi.where()
                if ca_path and os.path.exists(ca_path):
                    aria2_next_args.append(f"--ca-certificate={ca_path}")
            except ImportError:
                pass
        else:
            aria2_next_args.append("--check-certificate=false")

        if aria2_next_always_resume:
            aria2_next_args.append("--always-resume=true")
        else:
            aria2_next_args.append("--always-resume=false")

        opts["external_downloader_args"] = {"default": aria2_next_args}

    active_opts = opts.copy()

    try:
        try:
            with yt_dlp.YoutubeDL(active_opts) as ydl:
                _raise_if_paused(job_id)
                try:
                    info = ydl.extract_info(download_url, download=False)
                except Exception as extract_err:
                    if is_direct_file_url(download_url) or is_direct_download_type(getattr(job, "media_type", None)):
                        logger.info(
                            f"yt-dlp extract_info failed for direct file URL {redact_url(download_url)}: {extract_err}; "
                            f"using synthetic direct file info."
                        )
                        ext = guess_file_extension(
                            filename=getattr(job, "filename", None),
                            mime=getattr(job, "mime", None),
                            url=download_url,
                        ) or "bin"
                        info = {
                            "id": job_id,
                            "title": (getattr(job, "title", None) or "file").replace("%", "%%"),
                            "url": download_url,
                            "ext": ext,
                            "extractor": "generic",
                            "extractor_key": "Generic",
                            "protocol": "https" if download_url.startswith("https") else "http",
                            "format_id": "0",
                            "formats": [
                                {
                                    "format_id": "0",
                                    "url": download_url,
                                    "ext": ext,
                                    "protocol": "https" if download_url.startswith("https") else "http",
                                }
                            ],
                        }
                    else:
                        raise extract_err

                if is_direct_video:
                    # Lock the source extension for the temp file so ffmpeg remuxes
                    # from the real container instead of the mergeFormat placeholder.
                    source_ext = (
                        guess_file_extension(
                            filename=None,
                            mime=getattr(job, "mime", None) or info.get("mime"),
                            url=download_url,
                        )
                        or info.get("ext")
                        or _source_container_for_remux(merge_format)
                    )
                    info["ext"] = source_ext
                    # yt-dlp's generic extractor may not set a video codec for raw
                    # file URLs. Make sure the remuxer postprocessor actually runs.
                    if info.get("vcodec") in (None, "none"):
                        info["vcodec"] = "copy"
                _sanitize_info_extensions(info, merge_format)
                if is_video:
                    _add_stream_fixup_postprocessors(ydl, info)
                _raise_if_paused(job_id)
                _ = ydl.process_ie_result(info, download=True)
                _validate_downloaded_format(info, format_id, media_type=getattr(job, "media_type", None))
                final_filepath = ydl.prepare_filename(info)
        except Exception as first_err:
            if isinstance(first_err, DownloadPaused):
                raise
            if "cookiesfrombrowser" in active_opts:
                logger.warning(
                    f"Download failed with native cookies ({browser}): {first_err}. Retrying without cookies..."
                )
                active_opts.pop("cookiesfrombrowser", None)
                with yt_dlp.YoutubeDL(active_opts) as ydl:
                    _raise_if_paused(job_id)
                    info = ydl.extract_info(download_url, download=False)
                    if is_direct_video:
                        source_ext = (
                            guess_file_extension(
                                filename=None,
                                mime=getattr(job, "mime", None) or info.get("mime"),
                                url=download_url,
                            )
                            or info.get("ext")
                            or _source_container_for_remux(merge_format)
                        )
                        info["ext"] = source_ext
                        if info.get("vcodec") in (None, "none"):
                            info["vcodec"] = "copy"
                    _sanitize_info_extensions(info, merge_format)
                    if is_video:
                        _add_stream_fixup_postprocessors(ydl, info)
                    _raise_if_paused(job_id)
                    _ = ydl.process_ie_result(info, download=True)
                    _validate_downloaded_format(info, format_id, media_type=getattr(job, "media_type", None))
                    final_filepath = ydl.prepare_filename(info)
            else:
                raise first_err

        if not os.path.exists(final_filepath):
            base, _ = os.path.splitext(final_filepath)
            merged_path = f"{base}.{merge_format}"
            if os.path.exists(merged_path):
                final_filepath = merged_path
            else:
                dir_files = os.listdir(str(app_temp_dir))
                title_slug = os.path.basename(base)
                for f in dir_files:
                    if f.startswith(title_slug) and not f.endswith(".part"):
                        final_filepath = os.path.join(str(app_temp_dir), f)
                        break

        # Move the completed postprocessed file to the user's selected destination directory
        final_out_dir = output_dir if output_dir else settings.DEFAULT_OUTPUT_DIR
        os.makedirs(final_out_dir, exist_ok=True)

        dest_filepath = os.path.join(final_out_dir, os.path.basename(final_filepath))

        # Resolve filename conflict if conflict_resolution is 'rename'
        if conflict_resolution == "rename" and os.path.exists(dest_filepath):
            base, ext = os.path.splitext(os.path.basename(final_filepath))
            counter = 1
            while True:
                new_filename = f"{base}_{counter}{ext}"
                new_dest = os.path.join(final_out_dir, new_filename)
                if not os.path.exists(new_dest):
                    dest_filepath = new_dest
                    break
                counter += 1

        shutil.move(final_filepath, dest_filepath)
        logger.info(
            f"Download completed successfully. Saved and moved to: {dest_filepath} (resolution: {conflict_resolution})"
        )

        # Clean up isolated app_temp_dir
        try:
            if os.path.exists(app_temp_dir):
                shutil.rmtree(app_temp_dir)
                logger.info(f"Cleaned up isolated temp directory: {app_temp_dir}")
        except Exception as cleanup_err:
            logger.warning(
                f"Failed to clean up temp directory {app_temp_dir}: {cleanup_err}"
            )

        # Get final size of the downloaded file
        final_size = 0
        try:
            final_size = os.path.getsize(dest_filepath)
        except Exception:
            pass

        jobs_registry.update_job(
            job_id,
            status="completed",
            file_path=dest_filepath,
            progress=100.0,
            combined_downloaded_bytes=final_size or combined_dl,
            combined_total_bytes=final_size or combined_total,
        )
        return dest_filepath

    except DownloadPaused:
        logger.info(f"Download job {job_id} was successfully paused.")
        jobs_registry.update_job(job_id, status="paused")
        raise
    except Exception as e:
        error_message = str(e)
        logger.error(f"Download job {job_id} failed: {e}", exc_info=True)
        error_category = "expired_url" if is_expired_url_error(error_message) else None
        jobs_registry.update_job(
            job_id,
            status="failed",
            error=error_message,
            error_category=error_category,
        )
        raise
