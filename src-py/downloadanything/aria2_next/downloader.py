"""yt-dlp external downloader backed by the persistent aria2-next WebSocket RPC daemon.

`Aria2NextFD` plugs directly into `yt_dlp.downloader.external` (registered as
`aria2_next`, `aria2-next`, and `aria2next` — completely distinct from legacy
`aria2c`). Every download leg is submitted to and controlled through the
`aria2-next` JSON-RPC 2.0 WebSocket interface as a `media=file` task:

- Direct HTTP/HTTPS files & progressive streams → multi-connection adaptive
  range splitting (`--split`, `--max-connection-per-server`, `--min-split-size`,
  `--file-allocation`).
- Pre-sliced segment lists (`m3u8_frag_urls`, `dash_frag_urls`) → parallel
  per-fragment `media=file` tasks, then concatenated for yt-dlp's post-merger.

Manifest protocols (`m3u8`, `m3u8_native`, `http_dash_segments`,
`http_dash_segments_generator`) are deliberately NOT claimed: yt-dlp's `HlsFD`
and `DashSegmentsFD` already parse manifests (AES-128 keys, EXT-X-MAP init
segments, byte ranges, media sequences, live detection) and delegate the
fragment downloads back to this downloader via the `*_frag_urls` protocols.
aria2-next's own `media=hls`/`media=dash`/`media=collection` pipeline is also
skipped on purpose: its HTTP transport multiplexes every segment request over
a single HTTP/2 connection (`CURL_HTTP_VERSION_2TLS`), which caps throughput
at one connection's share, whereas parallel `media=file` tasks each open their
own TCP connection.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import threading
import time
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from yt_dlp.downloader import external as ytdlp_external
from yt_dlp.downloader.external import ExternalFD
from yt_dlp.networking import Request
from yt_dlp.utils import DownloadCancelled, update_url_query, urljoin

from ..settings import AppSettings
from .daemon import Aria2NextDaemon, get_daemon, global_options_from_settings
from .rpc import Aria2RpcClient, Aria2RpcError

if TYPE_CHECKING:
    from ..downloads import Task

log = structlog.get_logger(__name__)


def derive_leg_gid(task_gid: str, leg_name: str) -> str:
    """Deterministic 16-hex-digit non-zero GID for an aria2-next task leg."""
    digest = hashlib.blake2b(f"{task_gid}:{leg_name}".encode(), digest_size=8).hexdigest()
    return digest if digest != "0" * 16 else "0000000000000001"


def _resolve_fragment_entries(info_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """Materialize `info_dict['fragments']` with resolved absolute URLs.

    The original fragment fields (`byte_range`, `decrypt_info`,
    `media_sequence`, ...) are preserved because yt-dlp's decrypter and the
    Range-header logic below read them."""
    raw_frags = info_dict.get("fragments")
    if callable(raw_frags) or not isinstance(raw_frags, list) or not raw_frags:
        return []

    base_url = info_dict.get("fragment_base_url")
    extra_query = None
    if extra_param := info_dict.get("extra_param_to_segment_url"):
        extra_query = urllib.parse.parse_qs(extra_param)

    resolved: list[dict[str, Any]] = []
    for idx, frag in enumerate(raw_frags):
        if not isinstance(frag, dict):
            continue
        f_url = frag.get("url")
        if not f_url and base_url and frag.get("path"):
            f_url = urljoin(base_url, frag["path"])
        if not f_url:
            continue
        if extra_query:
            f_url = update_url_query(f_url, extra_query)
        entry = {**frag, "index": idx, "url": str(f_url)}
        entry.setdefault("media_sequence", idx)
        resolved.append(entry)
    return resolved


class Aria2NextFD(ExternalFD):
    """yt-dlp external downloader that executes transfers through aria2-next's
    WebSocket JSON-RPC server (`ws://127.0.0.1:<port>/jsonrpc`)."""

    AVAILABLE_OPT = "--version"
    EXE_NAME = "aria2-next"
    FD_NAME = "aria2_next"
    SUPPORTED_PROTOCOLS = (
        "http",
        "https",
        "m3u8_frag_urls",
        "dash_frag_urls",
    )
    SUPPORTED_FEATURES = ()

    @classmethod
    def get_basename(cls) -> str:
        return "aria2_next"

    @classmethod
    def available(cls, path: str | None = None) -> str | bool:
        if path and path not in ("aria2_next", "aria2-next", "aria2next"):
            p = Path(os.path.expanduser(path))
            if p.is_file():
                cls.exe = str(p)
                return str(p)
            return False
        # Name-only probe (delegated fragment FDs pass the downloader name):
        # the UI-configured path is stamped on `cls.exe` by
        # `configure_ydl_params` before any download runs.
        exe = cls.exe
        return exe if isinstance(exe, str) and Path(exe).is_file() else False

    @classmethod
    def supports(cls, info_dict: dict[str, Any]) -> bool:
        if info_dict.get("to_stdout"):
            return False
        proto = str(info_dict.get("protocol") or "")
        if "+" in proto:
            return False
        return all(p in cls.SUPPORTED_PROTOCOLS for p in proto.split("+") if p)

    @classmethod
    def supports_manifest(cls, manifest: str) -> bool:
        """Accept every manifest so HlsFD delegates VOD fragment downloads."""
        return True

    def _context(self) -> tuple[Task | None, AppSettings, Aria2NextDaemon]:
        task: Task | None = self.params.get("_downloadanything_task")
        settings: AppSettings = self.params.get("_downloadanything_settings") or AppSettings()
        daemon: Aria2NextDaemon = (
            self.params.get("_downloadanything_aria2_daemon") or get_daemon()
        )
        return task, settings, daemon

    def _cookie_header(self, url: str) -> str:
        try:
            return self.ydl.cookiejar.get_cookie_header(url) or ""
        except Exception:
            return ""

    def _base_http_options(
        self,
        url: str,
        info_dict: dict[str, Any],
        s: AppSettings,
    ) -> dict[str, Any]:
        opts: dict[str, Any] = global_options_from_settings(s)
        # aria2's max-tries counts consecutive per-slot failures without
        # progress, not whole-download retries like yt-dlp's --retries. Its
        # fixed 8s stall window aborts a leg on a short CDN pause, so the
        # yt-dlp retry count alone is too small a budget. 0 means unlimited
        # in aria2, so an explicit "no retries" maps to a single try.
        retries = s.retries if s.retries is not None else 3
        opts["max-tries"] = str(max(5, retries) if retries else 1)
        opts["allow-overwrite"] = "true"
        opts["auto-file-renaming"] = "false"
        opts["force-save"] = "false"
        opts["continue"] = "true" if self.params.get("continuedl", True) else "false"
        opts["remove-control-file"] = "false" if self.params.get("continuedl", True) else "true"

        headers_map: dict[str, str] = {}
        raw_headers = info_dict.get("http_headers") or self.params.get("http_headers") or {}
        for k, v in raw_headers.items():
            if v is not None:
                headers_map[str(k)] = str(v)

        ua = headers_map.pop("User-Agent", None) or headers_map.pop("user-agent", None)
        ref = headers_map.pop("Referer", None) or headers_map.pop("referer", None)
        if ua:
            opts["user-agent"] = ua
        if ref:
            opts["referer"] = ref

        cookie_hdr = self._cookie_header(url)
        if cookie_hdr and "Cookie" not in headers_map and "cookie" not in headers_map:
            headers_map["Cookie"] = cookie_hdr

        header_lines = [f"{k}: {v}" for k, v in headers_map.items() if v]
        if header_lines:
            opts["header"] = header_lines

        if proxy := self.params.get("proxy"):
            opts["all-proxy"] = str(proxy)
        return opts

    def _adopt_progress_hooks(self) -> None:
        """HlsFD/DashSegmentsFD create this delegated FD without copying the
        caller's progress hooks (yt-dlp TODO) — adopt the ydl's hooks so
        DownloadAnything's task progress reporter sees fragment updates.
        Identity-dedup keeps the top-level-fd path (hooks already attached by
        ydl.dl) from reporting twice."""
        for ph in getattr(self.ydl, "_progress_hooks", ()):
            if ph not in self._progress_hooks:
                self._progress_hooks.append(ph)

    def _call_downloader(self, tmpfilename: str, info_dict: dict[str, Any]) -> int:
        task, s, daemon = self._context()
        if task is not None and task.cancel.is_set():
            raise DownloadCancelled("aborted by user")

        self._adopt_progress_hooks()

        rpc = daemon.ensure_running(s)
        abs_tmp = os.path.abspath(tmpfilename)
        os.makedirs(os.path.dirname(abs_tmp), exist_ok=True)

        # Fragmented streams delegated by HlsFD/DashSegmentsFD or supplied
        # directly (m3u8_frag_urls / dash_frag_urls).
        fragments = _resolve_fragment_entries(info_dict)
        if fragments:
            return self._download_http_fragments(
                rpc, abs_tmp, tmpfilename, info_dict, fragments, task, s
            )

        # Direct / progressive HTTP(S) download via an aria2-next HttpTask.
        url = str(info_dict.get("url") or "")
        ok, err = False, ""
        for _ in range(3):
            if task is not None and task.cancel.is_set():
                raise DownloadCancelled("aborted by user")
            try:
                ok, err = self._download_http_single(
                    rpc, abs_tmp, tmpfilename, url, info_dict, task, s
                )
            except DownloadCancelled:
                raise
            except Exception as e:
                # A dropped RPC socket is a leg failure like any other —
                # it must reach the native fallback, not abort the download.
                ok, err = False, str(e)
            if ok:
                break
        if not ok:
            # aria2-next aborts a leg after its fixed 8s stall window; a CDN
            # that only paused still answers yt-dlp's impersonated stack.
            # shortcut: the fallback restarts the leg at byte 0, resume it
            # once aria2-next can hand off a partial .inc file.
            log.warning("aria2-next leg failed — native fallback", error=err)
            try:
                self._fetch_ranged_fragment(url, 0, 0, abs_tmp, info_dict, task)
            except DownloadCancelled:
                raise
            except Exception as e:
                self.report_error(f"aria2-next download failed: {err}; native fetch: {e}")
                return 1
        return 0

    def _download_http_single(
        self,
        rpc: Aria2RpcClient,
        abs_tmp: str,
        tmpfilename: str,
        url: str,
        info_dict: dict[str, Any],
        task: Task | None,
        s: AppSettings,
    ) -> tuple[bool, str]:
        leg_gid = derive_leg_gid(
            task.gid if task else "downloadanything", os.path.basename(abs_tmp)
        )
        if task is not None:
            with task.lock:
                task.aria2_gid = leg_gid
                task.aria2_gids.add(leg_gid)

        opts = self._base_http_options(url, info_dict, s)
        opts["dir"] = os.path.dirname(abs_tmp)
        opts["out"] = os.path.basename(abs_tmp)
        opts["media"] = "file"
        opts["incomplete-suffix"] = ".inc"
        opts["gid"] = leg_gid

        self._submit_or_resume(rpc, leg_gid, [url], opts)
        ok, actual_path, err = self._monitor_gid(
            rpc, leg_gid, abs_tmp, tmpfilename, info_dict, task
        )
        if (
            ok
            and actual_path
            and os.path.abspath(actual_path) != abs_tmp
            and os.path.exists(actual_path)
        ):
            shutil.move(actual_path, abs_tmp)
        return ok, err

    def _download_http_fragments(
        self,
        rpc: Aria2RpcClient,
        abs_tmp: str,
        tmpfilename: str,
        info_dict: dict[str, Any],
        fragments: list[dict[str, Any]],
        task: Task | None,
        s: AppSettings,
    ) -> int:
        """Download segments in parallel batches via aria2-next HTTP tasks over
        WebSocket RPC, decrypting AES-128 if needed, and assembling `abs_tmp`."""
        total_frags = len(fragments)
        started = time.time()
        frag_dir = f"{abs_tmp}.frags"
        os.makedirs(frag_dir, exist_ok=True)
        concurrency = max(1, min(32, s.parallel_segments or s.aria2_split or 16))
        skip_unavailable = self.params.get("skip_unavailable_fragments", True)
        decrypt_fragment = self.decrypter(info_dict)
        # Encrypted fragments decrypt inside the worker pool — a serial
        # decrypt pass after the last byte arrives wastes the whole tail of
        # the download on one core. The durable marker is the decrypted
        # `.dec` file so a resume never double-decrypts.
        needs_decrypt = any(
            (f.get("decrypt_info") or {}).get("METHOD") == "AES-128" for f in fragments
        )

        def finalize(frag: dict[str, Any], raw_path: str) -> tuple[str, int]:
            """Return the durable output (dec_path, size) for a finished frag."""
            if not needs_decrypt:
                return raw_path, os.path.getsize(raw_path)
            dec_path = f"{raw_path}.dec"
            if not (os.path.isfile(dec_path) and os.path.getsize(dec_path) > 0):
                with open(raw_path, "rb") as f:
                    plain = decrypt_fragment(frag, f.read())
                tmp = f"{dec_path}.tmp"
                with open(tmp, "wb") as f:
                    f.write(plain)
                os.replace(tmp, dec_path)
                with contextlib.suppress(OSError):
                    os.remove(raw_path)
            return dec_path, os.path.getsize(dec_path)

        completed_paths: dict[int, str] = {}
        bytes_acc = 0
        done_count = 0
        # Live in-flight stats — each worker's monitor piggybacks its ~1s
        # tellStatus into `inflight`; the reporter thread folds them into a
        # steady aggregate so the UI updates even between completions.
        stats_lock = threading.Lock()
        emit_lock = threading.Lock()
        inflight: dict[str, tuple[int, float]] = {}
        snapshot = {"bytes": 0, "count": 0}
        stop_report = threading.Event()
        dest_filename = self.undo_temp_name(tmpfilename)

        def fetch_one(frag: dict[str, Any]) -> tuple[int, str, int]:
            if task is not None and task.cancel.is_set():
                raise DownloadCancelled("aborted by user")
            idx = int(frag["index"])
            frag_out = os.path.join(frag_dir, f"frag-{idx:06d}.part")
            # Partial bytes live at `frag_out + .inc` (incomplete-suffix below)
            # and aria2-next renames to `frag_out` only on completion, so a
            # non-empty file at the final name is definitionally finished.
            # With AES-128 the finished artifact is the decrypted `.dec`.
            done_path = f"{frag_out}.dec" if needs_decrypt else frag_out
            if os.path.isfile(done_path) and os.path.getsize(done_path) > 0:
                return idx, done_path, os.path.getsize(done_path)
            if needs_decrypt and os.path.isfile(frag_out) and os.path.getsize(frag_out) > 0:
                # Download finished but the worker died before decrypting —
                # finish the crypto, not the network.
                path, size = finalize(frag, frag_out)
                return idx, path, size

            f_url = frag["url"]
            br = frag.get("byte_range") or {}
            start_b = int(br.get("start") or 0)
            end_b = int(br.get("end") or 0)
            if end_b > start_b:
                # A byte-range slice of a larger resource: aria2-next's planner
                # can only model filling [0, total) of one resource (its probe
                # requests from offset 0 and a bounded 206 then reads as
                # "server ignores ranges", looping forever), so fetch it
                # through yt-dlp's own HTTP stack — the same path yt-dlp's
                # native fragment downloader uses.
                self._fetch_ranged_fragment(f_url, start_b, end_b, frag_out, info_dict, task)
                path, size = finalize(frag, frag_out)
                return idx, path, size

            fgid = derive_leg_gid(
                task.gid if task else "downloadanything",
                f"{os.path.basename(abs_tmp)}:f{idx}",
            )
            if task is not None:
                with task.lock:
                    task.aria2_gids.add(fgid)

            opts = self._base_http_options(f_url, info_dict, s)
            opts["dir"] = frag_dir
            opts["out"] = os.path.basename(frag_out)
            opts["media"] = "file"
            opts["incomplete-suffix"] = ".inc"
            # HttpTask's probing worker always negotiates HTTP/2
            # (`CURL_HTTP_VERSION_2TLS`), and curl_multi pools that connection
            # across tasks — every queued fragment would multiplex onto one
            # shared TCP connection (measured ~53 KB/s per stream). FORBID_REUSE
            # (keep-alive off) makes every transfer open its own connection, and
            # `split >= 2` + `min-split-size=64K` spawns the HTTP/1.1 workers
            # (`CURL_HTTP_VERSION_1_1`) on separate TCP connections even for
            # ~0.5-1.5 MiB HLS/DASH segments. Measured: 3.9-5.1 MB/s vs
            # 2.9-3.4 MB/s with pooled connections. Split is held at 2: every
            # in-flight fragment costs `split` sockets, and the daemon's
            # concurrency cap is the parallelism that matters — a few hundred
            # queued connections get SYN-dropped and stall everything.
            opts["enable-http-keep-alive"] = "false"
            opts["min-split-size"] = "64K"
            opts["split"] = "2"
            opts["max-connection-per-server"] = opts["split"]
            opts["max-tries"] = "10"
            opts["gid"] = fgid

            def sink(st: dict[str, Any], _fgid: str = fgid) -> None:
                with stats_lock:
                    inflight[_fgid] = (
                        int(st.get("completedLength") or 0),
                        float(st.get("downloadSpeed") or 0),
                    )

            err = ""
            ok = False
            actual_path = ""
            try:
                for attempt in range(3):
                    if attempt > 0:
                        with contextlib.suppress(OSError):
                            os.remove(f"{frag_out}.inc")
                    try:
                        self._submit_or_resume(rpc, fgid, [f_url], opts)
                        ok, actual_path, err = self._monitor_gid(
                            rpc,
                            fgid,
                            frag_out,
                            frag_out,
                            info_dict,
                            task,
                            emit_progress=False,
                            status_sink=sink,
                        )
                    except DownloadCancelled:
                        raise
                    except Exception as e:
                        # An RPC hiccup (socket reset, gid race) is retried like a
                        # transfer failure instead of sinking the fragment.
                        ok, actual_path, err = False, "", str(e)
                    if ok:
                        break
                if not ok:
                    # TLS-gated CDNs (Cloudflare-fronted phncdn nodes) 410
                    # aria2-next's plain-curl fingerprint — fetch through
                    # yt-dlp's impersonated stack instead.
                    try:
                        self._fetch_ranged_fragment(f_url, 0, 0, frag_out, info_dict, task)
                    except DownloadCancelled:
                        raise
                    except Exception as native_err:
                        raise RuntimeError(
                            f"{err or f'fragment {idx} failed'}; native fetch: {native_err}"
                        ) from native_err
                    path, size = finalize(frag, frag_out)
                    return idx, path, size
                if (
                    actual_path
                    and os.path.exists(actual_path)
                    and os.path.abspath(actual_path) != os.path.abspath(frag_out)
                ):
                    shutil.move(actual_path, frag_out)
                path, size = finalize(frag, frag_out)
                return idx, path, size
            finally:
                with stats_lock:
                    inflight.pop(fgid, None)
                # A finished fragment's gid is permanently stale (its result
                # is already removed server-side) — drop it so task controls
                # don't sweep dead ids.
                if task is not None:
                    with task.lock:
                        task.aria2_gids.discard(fgid)

        def emit_progress() -> None:
            with stats_lock:
                extra = sum(c for c, _ in inflight.values())
                live_speed = sum(sp for _, sp in inflight.values())
                b = snapshot["bytes"]
                cnt = snapshot["count"]
            elapsed = max(0.001, time.time() - started)
            downloaded = b + extra
            speed = live_speed if live_speed > 0 else (downloaded / elapsed)
            payload: dict[str, Any] = {
                "filename": dest_filename,
                "tmpfilename": tmpfilename,
                "status": "downloading",
                "downloaded_bytes": downloaded,
                "fragment_index": cnt,
                "fragment_count": total_frags,
                "speed": speed,
                "elapsed": elapsed,
            }
            if cnt > 0 and b > 0:
                payload["total_bytes_estimate"] = max(downloaded, int((b / cnt) * total_frags))
            with emit_lock:
                self._hook_progress(payload, info_dict)

        def live_reporter() -> None:
            # The hook raises DownloadCancelled when the task is cancelled —
            # the main loop owns the unwind; the reporter just stops.
            while not stop_report.wait(0.8):
                try:
                    emit_progress()
                except DownloadCancelled:
                    return

        reporter = threading.Thread(target=live_reporter, daemon=True)
        reporter.start()

        try:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {pool.submit(fetch_one, f): f for f in fragments}
                try:
                    for fut in as_completed(futures):
                        if task is not None and task.cancel.is_set():
                            raise DownloadCancelled("aborted by user")
                        frag = futures[fut]
                        idx = int(frag["index"])
                        try:
                            _, fpath, fsize = fut.result()
                        except DownloadCancelled:
                            raise
                        except Exception as e:
                            if skip_unavailable and idx > 0:
                                self.report_skip_fragment(idx, e)
                                continue
                            for pending in futures:
                                pending.cancel()
                            self.report_error(f"aria2-next fragment {idx} failed: {e}")
                            return 1
                        completed_paths[idx] = fpath
                        bytes_acc += fsize
                        done_count += 1
                        with stats_lock:
                            snapshot["bytes"] = bytes_acc
                            snapshot["count"] = done_count
                        emit_progress()
                except BaseException:
                    for pending in futures:
                        pending.cancel()
                    raise
        finally:
            stop_report.set()
            reporter.join(timeout=2)

        with open(abs_tmp, "wb") as dest:
            for frag in fragments:
                idx = int(frag["index"])
                fpath = completed_paths.get(idx)
                if not fpath or not os.path.isfile(fpath):
                    if skip_unavailable and idx > 0:
                        continue
                    return 1
                # Workers already decrypted AES-128 fragments — concat only.
                with open(fpath, "rb") as src:
                    shutil.copyfileobj(src, dest, 1 << 20)
        shutil.rmtree(frag_dir, ignore_errors=True)
        return 0

    def _fetch_ranged_fragment(
        self,
        url: str,
        start: int,
        end: int,
        frag_out: str,
        info_dict: dict[str, Any],
        task: Task | None,
    ) -> None:
        """Fetch via yt-dlp's own HTTP stack (cookies, impersonation, proxy
        ride along), atomically replacing `frag_out` on success. end <= start
        means the whole resource; otherwise `bytes=start-(end-1)`."""
        headers = dict(info_dict.get("http_headers") or {})
        if end > start:
            headers["Range"] = f"bytes={start}-{end - 1}"
        # Request-level target — the whole point of this path is passing
        # TLS-fingerprint-gated CDNs the aria2 fetch could not.
        from ..extract import impersonate_target

        ext: dict[str, Any] = {}
        if t := impersonate_target():
            ext["impersonate"] = t
        last_err: Exception | None = None
        for _ in range(3):
            if task is not None and task.cancel.is_set():
                raise DownloadCancelled("aborted by user")
            try:
                tmp = f"{frag_out}.tmp"
                with (
                    self.ydl.urlopen(Request(url, headers=headers, extensions=ext)) as resp,
                    open(tmp, "wb") as f,
                ):
                    while chunk := resp.read(1 << 20):
                        if task is not None and task.cancel.is_set():
                            raise DownloadCancelled("aborted by user")
                        f.write(chunk)
                os.replace(tmp, frag_out)
                return
            except DownloadCancelled:
                raise
            except Exception as e:
                last_err = e
                with contextlib.suppress(OSError):
                    os.remove(tmp)
        raise RuntimeError(f"native fetch failed: {last_err}")

    def _submit_or_resume(
        self,
        rpc: Aria2RpcClient,
        gid: str,
        uris: list[str],
        opts: dict[str, Any],
    ) -> None:
        """Resume an existing aria2-next task for `gid` if present, or add it."""
        try:
            st = rpc.tell_status(gid, ["gid", "status", "files"])
        except Aria2RpcError:
            st = {}

        status = st.get("status")
        if status in ("active", "waiting"):
            return
        if status == "paused":
            try:
                rpc.unpause(gid)
                return
            except Aria2RpcError:
                pass
        if status in ("complete", "error", "removed", "paused"):
            try:
                if status == "paused":
                    rpc.remove(gid, force=True)
                rpc.remove_download_result(gid)
            except Aria2RpcError:
                pass

        rpc.add_uri(uris, opts)

    def _monitor_gid(
        self,
        rpc: Aria2RpcClient,
        gid: str,
        abs_tmp: str,
        tmpfilename: str,
        info_dict: dict[str, Any],
        task: Task | None,
        *,
        emit_progress: bool = True,
        status_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[bool, str, str]:
        """Monitor `gid` via WebSocket notifications + status snapshots until completion."""
        ev, unsub = rpc.subscribe_gid(gid)
        started = time.time()
        dest_filename = self.undo_temp_name(tmpfilename)
        try:
            while True:
                if task is not None and task.cancel.is_set():
                    self._abort_gid_on_cancel(rpc, gid, task)
                    raise DownloadCancelled("aborted by user")

                st = rpc.tell_status(gid)
                if status_sink is not None:
                    status_sink(st)
                aria_status = str(st.get("status") or "")
                files = st.get("files") if isinstance(st.get("files"), list) else []
                actual_path = str(
                    (files[0].get("path") if files and isinstance(files[0], dict) else "") or ""
                )

                if emit_progress and aria_status in ("active", "waiting"):
                    payload: dict[str, Any] = {
                        "filename": dest_filename,
                        "tmpfilename": tmpfilename,
                        "status": "downloading",
                        "downloaded_bytes": int(st.get("completedLength") or 0),
                        "speed": float(st.get("downloadSpeed") or 0.0),
                        "elapsed": time.time() - started,
                    }
                    total_bytes = int(st.get("totalLength") or 0)
                    if total_bytes > 0:
                        payload["total_bytes"] = total_bytes
                    self._hook_progress(payload, info_dict)

                if aria_status == "complete":
                    with contextlib.suppress(Aria2RpcError):
                        rpc.remove_download_result(gid)
                    return True, actual_path or abs_tmp, ""

                if aria_status == "error":
                    err_msg = (
                        str(st.get("errorMessage") or "")
                        or f"aria2-next error (code {st.get('errorCode') or '1'})"
                    )
                    with contextlib.suppress(Aria2RpcError):
                        rpc.remove_download_result(gid)
                    return False, actual_path, err_msg

                if aria_status == "removed":
                    raise DownloadCancelled("removed in aria2-next")

                if aria_status == "paused":
                    user_paused = False
                    if task is not None:
                        with task.lock:
                            user_paused = task.cancel.is_set() or task.status == "paused"
                    if user_paused:
                        raise DownloadCancelled("paused by user")
                    with contextlib.suppress(Aria2RpcError):
                        rpc.unpause(gid)

                # Terminal notifications wake the wait; the timeout only
                # bounds how often we re-check cancel + status.
                ev.wait(timeout=0.2 if emit_progress else 1.0)
                ev.clear()
        finally:
            unsub()

    @staticmethod
    def _abort_gid_on_cancel(rpc: Aria2RpcClient, gid: str, task: Task) -> None:
        with task.lock:
            is_pause = task.status == "paused"
        try:
            if is_pause:
                rpc.pause(gid, force=True)
            else:
                with contextlib.suppress(Aria2RpcError):
                    rpc.remove(gid, force=True)
                rpc.remove_download_result(gid)
        except Exception:
            pass


def register_ytdlp_downloader() -> None:
    """Register `Aria2NextFD` into `yt_dlp.downloader.external` so yt-dlp
    resolves `external_downloader='aria2_next'` (and `aria2-next`) to our
    WebSocket RPC downloader class."""
    ytdlp_external.Aria2NextFD = Aria2NextFD
    by_name = getattr(ytdlp_external, "_BY_NAME", None)
    if isinstance(by_name, dict):
        by_name["aria2_next"] = Aria2NextFD
        by_name["aria2-next"] = Aria2NextFD
        by_name["aria2next"] = Aria2NextFD


register_ytdlp_downloader()
