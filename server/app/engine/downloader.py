import sys
from pathlib import Path
import asyncio
import os
import time
import shutil
import subprocess
import re
import secrets
from typing import Any, Dict, List, Optional

from app.engine import ytdlp_opts
from app.config import get_app_data_dir, settings
from app.api.settings import load_settings
from app.engine.jobs import jobs_registry, DownloadPaused
from app.engine.title_extractor import resolve_filename
from app.utils.logger import logger, redact_url
import yt_dlp
from yt_dlp.downloader.external import Aria2cFD
from yt_dlp.downloader import external as ytdlp_external
from yt_dlp.utils import classproperty


def get_aria2_executable_path() -> Optional[Path]:
    if sys.platform not in ["win32", "darwin"]:
        return None

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

    # Priority 1: Search dynamically for any 'aria2-next-*' or 'aria2c-next-*' files matching the platform
    for d in possible_dirs:
        if d.exists():
            try:
                for item in d.iterdir():
                    if item.is_file() and (
                        item.name.startswith("aria2-next-")
                        or item.name.startswith("aria2c-next-")
                    ):
                        if sys.platform == "win32" and (
                            item.name.endswith(".exe") or "windows" in item.name
                        ):
                            return item
                        elif sys.platform == "darwin" and (
                            "macos" in item.name or "darwin" in item.name
                        ):
                            return item
            except Exception:
                pass

    # Priority 2: Check for exact 'aria2-next' / 'aria2c-next' / 'aria2-next.exe' / 'aria2c-next.exe' in possible dirs
    next_names = (
        ["aria2c-next.exe", "aria2-next.exe"]
        if sys.platform == "win32"
        else ["aria2c-next", "aria2-next"]
    )
    for d in possible_dirs:
        for name in next_names:
            p = d / name
            if p.exists():
                return p

    # Priority 3: Check for the exact 'aria2-next' / 'aria2-next.exe' alias
    alias_name = "aria2-next.exe" if sys.platform == "win32" else "aria2-next"
    for d in possible_dirs:
        alias_path = d / alias_name
        if alias_path.exists():
            return alias_path

    return None


def _validate_aria2_min_split_size(value: Any) -> str:
    """Sanitize the aria2 --min-split-size argument.

    Accepts a numeric value with an optional K/M/G/T suffix, matching
    aria2c's documented format. Falls back to the default "1M" for
    anything else so that malformed user settings cannot inject extra
    arguments or crash aria2c.
    """
    if isinstance(value, str) and re.fullmatch(r"^\d+[KMGTkmgt]?$", value.strip()):
        return value.strip()
    logger.warning(f"Invalid aria2MinSplitSize {value!r}; using default '1M'")
    return "1M"


class DMAAria2cFD(Aria2cFD):
    def report_error(self, msg: str) -> None:
        parent_report_error = getattr(super(), "report_error", None)
        if callable(parent_report_error):
            parent_report_error(msg)

    def _hook_progress(self, status: Dict[str, Any], info_dict: Dict[str, Any]) -> None:
        parent_hook_progress = getattr(super(), "_hook_progress", None)
        if callable(parent_hook_progress):
            parent_hook_progress(status, info_dict)

    def _call_process(
        self, cmd: List[Any], info_dict: Dict[str, Any]
    ) -> tuple[str, str, int]:
        import json
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
                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "dma",
                        "method": method,
                        "params": [f"token:{secret}", *params],
                    }
                ).encode()
                req = urllib.request.Request(
                    rpc_url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    result = json.loads(resp.read())
                if "error" in result:
                    raise RuntimeError(f"aria2 RPC error: {result['error']}")
                return result["result"]

            STRIP_EXACT = {"--quiet", "-q"}
            STRIP_PREFIX = (
                "--console-log-level=",
                "--summary-interval=",
                "--enable-rpc",
                "--rpc-listen-port=",
                "--rpc-secret=",
            )
            filtered_cmd = []
            for arg in cmd:
                s = arg.decode("utf-8") if isinstance(arg, bytes) else arg
                if s in STRIP_EXACT or any(s.startswith(p) for p in STRIP_PREFIX):
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

            def enc(v):
                return v.encode("utf-8") if is_bytes else v

            for opt in [
                "--quiet=true",
                "--console-log-level=error",
                f"--rpc-secret={secret}",
                f"--rpc-listen-port={port}",
                "--enable-rpc=true",
            ]:
                filtered_cmd.insert(idx, enc(opt))

            str_cmd = [
                a.decode("utf-8") if isinstance(a, bytes) else a for a in filtered_cmd
            ]

            aria2_bin = get_aria2_executable_path()
            if aria2_bin and str_cmd:
                str_cmd[0] = str(aria2_bin)

            return str_cmd, rpc

        max_attempts = 3
        last_error: Optional[Exception] = None
        for attempt in range(max_attempts):
            p: Optional[subprocess.Popen] = None
            try:
                # Allocate a fresh ephemeral port each attempt to avoid races
                # where another process grabs the port between bind and aria2c start.
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
                            f"[aria2 RPC] not ready on attempt {attempt + 1}; retrying with a new port"
                        )
                        continue
                    logger.warning(
                        "[aria2 RPC] Could not connect — no live progress for this stream"
                    )

                # Poll RPC at ~100 ms for realtime progress
                KEYS = ["gid", "completedLength", "totalLength", "downloadSpeed", "status"]
                last_gid: Optional[str] = None
                active_seen = False

                try:
                    while p.poll() is None:
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
                                        "[aria2 RPC] Active download finished. Shutting down daemon..."
                                    )
                                    try:
                                        rpc("aria2.shutdown")
                                    except Exception:
                                        pass
                                    break
                            except DownloadPaused:
                                raise
                            except Exception as poll_err:
                                logger.debug("[aria2 RPC] poll error: %s", poll_err)

                        time.sleep(0.1)

                    # Emit a final "finished" hook once aria2c exits
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
                        except Exception:
                            pass

                except DownloadPaused:
                    _terminate(p)
                    raise
                except Exception:
                    _terminate(p)
                    raise

                try:
                    returncode = p.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    logger.warning("aria2 process did not exit after shutdown; terminating it")
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
                last_error = exc
                _terminate(p)
                if attempt < max_attempts - 1:
                    logger.warning(f"aria2 attempt {attempt + 1} failed: {exc}; retrying")
                    continue
                raise

        # Should never be reached, but keeps the type checker happy.
        return "", "", -1


# Register DMAAria2cFD into yt-dlp's downloader registry
external_downloader_registry = getattr(ytdlp_external, "_BY_NAME", None)
if isinstance(external_downloader_registry, dict):
    external_downloader_registry["aria2-next"] = DMAAria2cFD
    try:
        aria2_bin = get_aria2_executable_path()
        if aria2_bin:
            external_downloader_registry[aria2_bin.stem] = DMAAria2cFD
    except Exception:
        pass


def _probe_http_capabilities(url: str, referer: Optional[str]) -> Dict[str, Any]:
    """Perform a small, authenticated-context-free capability check for aria2."""
    import urllib.error
    import urllib.request

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
    if not requested_format_id or requested_format_id == "best":
        return
    if media_type in ("stream", "file"):
        return
    requested_ids = set(requested_format_id.split("+"))
    selected_ids = set()
    selected_format_id = info.get("format_id")
    if isinstance(selected_format_id, str):
        selected_ids.update(selected_format_id.split("+"))
    for requested_format in info.get("requested_formats") or []:
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


def _sanitize_info_extensions(info: Dict[str, Any]) -> None:
    """Bypasses ffmpeg container errors on unsafe/non-media file extensions (like .php)."""
    UNSAFE_EXTS = {"php", "html", "htm", "js", "txt", "asp", "aspx", "jsp"}

    def sanitize_dict(d: Dict[str, Any]) -> None:
        ext = d.get("ext")
        if isinstance(ext, str) and ext.lower() in UNSAFE_EXTS:
            vcodec = d.get("vcodec")
            acodec = d.get("acodec")
            if acodec and acodec != "none" and (not vcodec or vcodec == "none"):
                d["ext"] = "m4a"
            else:
                d["ext"] = "mp4"

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
    # Load settings from settings.json
    settings_data = load_settings()

    # Ensure the directory containing the bundled aria2c is in the PATH so yt-dlp can locate it
    aria2_bin = get_aria2_executable_path()
    if aria2_bin:
        bin_dir = str(aria2_bin.parent)
        if bin_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        # Dynamically register the specific executable name in yt-dlp's downloader registry
        if isinstance(external_downloader_registry, dict):
            external_downloader_registry[aria2_bin.stem] = DMAAria2cFD

    job = jobs_registry.get_job(job_id)

    is_chrome_intercept = bool(job and getattr(job, "media_type", None) == "file")
    skip_postprocessing = is_chrome_intercept

    # Overwrite download URL with direct stream/file URL if it's a fallback format
    download_url = url
    if job and getattr(job, "media_type", None) in ("stream", "file") and job.formats and format_id:
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
                    logger.info(f"Using direct format URL for download: {redact_url(download_url)}")
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
                logger.info(f"Using direct format URL for download (best): {redact_url(download_url)}")

    # Resolve title/filename through title_extractor so no other module
    # duplicates extraction logic. Existing job hints are used when present;
    # network lookup only runs when local hints are missing or generic.
    resolved_filename = resolve_filename(
        url=download_url or url,
        filename=getattr(job, "filename", None) if job else None,
        mime=getattr(job, "mime", None) if job else None,
        referer=referer,
        page_title=job.title if job else None,
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
    use_aria2 = settings_data.useAria2

    # aria2 configurations
    aria2_max_connections = settings_data.aria2MaxConnections
    aria2_split = settings_data.aria2Split
    aria2_min_split_size = _validate_aria2_min_split_size(settings_data.aria2MinSplitSize)
    aria2_preallocate = settings_data.aria2Preallocate
    aria2_check_certificate = settings_data.aria2CheckCertificate
    aria2_always_resume = settings_data.aria2AlwaysResume

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
    if job and getattr(job, "media_type", None) in ("stream", "file"):
        if not format_id or format_id == "best":
            format_selector = "best"

    # Build the output template from the single source of truth in title_extractor.
    # Escape percent signs so yt-dlp does not interpret them as template markers.
    clean_title = resolved_filename.title.replace("%", "%%") or "video"

    # Isolated temp template
    if skip_postprocessing:
        out_tmpl = os.path.join(
            str(app_temp_dir),
            resolved_filename.filename.replace("%", "%%"),
        )
    else:
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
            logger.info(
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
        opts.setdefault("postprocessors", []).append(
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}
        )
        opts.setdefault("postprocessors", []).append(
            {"key": "EmbedThumbnail", "already_have_thumbnail": False}
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

    # aria2 is useful for validated, range-capable direct files. It is not
    # forced onto manifests, HTML pages, or servers that do not advertise ranges.
    http_capabilities = (
        _probe_http_capabilities(download_url, referer) if use_aria2 and not is_stream else {}
    )
    content_type = str(http_capabilities.get("content_type") or "")
    aria2_eligible = (
        use_aria2
        and not is_stream
        and aria2_bin is not None
        and 200 <= int(http_capabilities.get("status") or 0) < 300
        and bool(http_capabilities.get("supports_ranges"))
        and not content_type.startswith("text/html")
    )
    if aria2_eligible:
        opts["external_downloader"] = str(aria2_bin)
        logger.debug(
            "Using adaptive aria2 acceleration: ranges=%s content_length=%s",
            http_capabilities.get("supports_ranges"),
            http_capabilities.get("content_length"),
        )
        content_length = http_capabilities.get("content_length")
        if isinstance(content_length, int) and content_length < 8 * 1024 * 1024:
            connection_limit = 2
        else:
            connection_limit = min(max(int(aria2_max_connections), 1), 16)
        split_limit = min(max(int(aria2_split), 1), connection_limit)

        aria2_args = [
            "-j",
            "1",
            "-x",
            str(connection_limit),
            "-s",
            str(split_limit),
            f"--min-split-size={aria2_min_split_size}",
        ]

        if aria2_preallocate:
            aria2_args.append("--file-allocation=prealloc")
        else:
            aria2_args.append("--file-allocation=none")

        if aria2_check_certificate:
            aria2_args.append("--check-certificate=true")
            try:
                import certifi

                ca_path = certifi.where()
                if ca_path and os.path.exists(ca_path):
                    aria2_args.append(f"--ca-certificate={ca_path}")
            except ImportError:
                pass
        else:
            aria2_args.append("--check-certificate=false")

        if aria2_always_resume:
            aria2_args.append("--always-resume=true")
        else:
            aria2_args.append("--always-resume=false")

        opts["external_downloader_args"] = {"default": aria2_args}
    elif use_aria2 and not is_stream:
        logger.debug("Using native downloader because HTTP range capability was not verified")

    active_opts = opts.copy()

    try:
        try:
            with yt_dlp.YoutubeDL(active_opts) as ydl:
                info = ydl.extract_info(download_url, download=False)
                _sanitize_info_extensions(info)
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
                    info = ydl.extract_info(download_url, download=False)
                    _sanitize_info_extensions(info)
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
        logger.error(f"Download job {job_id} failed: {e}", exc_info=True)
        jobs_registry.update_job(job_id, status="failed", error=str(e))
        raise
    finally:
        # Isolated temp directory cleanup for failed jobs
        snapshot = jobs_registry.get_job_snapshot(job_id)
        if snapshot:
            status, _ = snapshot
            if status == "failed":
                try:
                    if os.path.exists(app_temp_dir):
                        shutil.rmtree(app_temp_dir)
                        logger.info(
                            f"Successfully cleaned up isolated temp folder: {app_temp_dir}"
                        )
                except Exception as rmtree_err:
                    logger.warning(
                        f"Failed to clean up isolated temp folder {app_temp_dir}: {rmtree_err}"
                    )
