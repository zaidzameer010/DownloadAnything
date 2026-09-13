"""Aria2-next external downloader with HTTP JSON-RPC progress.

yt-dlp delegates HTTP and fragmented HLS/DASH downloads to the bundled
``aria2-next`` binary.  This module replaces the previous console-readout
parser with a clean event loop that polls aria2's JSON-RPC over HTTP,
aggregates per-download progress, and pushes it through yt-dlp's
``progress_hooks``.
"""

from __future__ import annotations

import asyncio
from collections import deque
import json
import os
import platform
import re
import secrets
import socket
import stat
import sys
import time
from pathlib import Path
from typing import Any, cast

import aiohttp
import structlog
import yt_dlp.downloader.external as _external
from yt_dlp.utils import DownloadCancelled, parse_m3u8_attributes

log = structlog.get_logger("da.aria2next")

try:
    from .constants import RPC_KEYS
except ImportError:  # flat (non-package) execution
    from constants import RPC_KEYS

POLL_INTERVAL = 0.2
_RPC_STARTUP_TIMEOUT = 10.0
_RPC_STARTUP_ATTEMPTS = 3
_RPC_CALL_TIMEOUT = 5.0
_RPC_SHUTDOWN_TIMEOUT = 5.0
_RPC_FAIL_LIMIT = 8
# Safety net for a wedged aria2 process: if there are active downloads but the
# aggregate byte count has not moved for this long, fail instead of polling
# forever. aria2's own --timeout/--max-tries normally error stalled transfers
# long before this.
_RPC_STALL_TIMEOUT = 300.0
_MAX_OUTPUT_BYTES = 64 * 1024


def _find_free_port() -> int:
    """Return an ephemeral TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _find_free_peer_port(excluded_port: int) -> int:
    while True:
        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as tcp_sock,
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_sock,
        ):
            tcp_sock.bind(("0.0.0.0", 0))
            port = int(tcp_sock.getsockname()[1])
            if port == excluded_port:
                continue
            try:
                udp_sock.bind(("0.0.0.0", port))
            except OSError:
                continue
            return port


def _find_aria2_next_binary() -> Path | None:
    """Return the bundled aria2-next executable for the current platform, if any."""
    env_path = os.environ.get("ARIA2_NEXT_PATH")
    if env_path:
        candidate = Path(env_path)
        if candidate.is_file():
            return candidate
        log.warning("ARIA2_NEXT_PATH is set but does not exist; falling back to discovery", path=str(candidate))

    system = platform.system().lower()
    is_windows = system == "windows"

    roots = [Path(sys.executable).resolve().parent]
    if not getattr(sys, "frozen", False):
        roots.append(Path(__file__).resolve().parent.parent)

    for root in roots:
        try:
            candidates = [
                p
                for p in root.iterdir()
                if p.is_file() and re.fullmatch(r"aria2-next(?:-\d.*)?(?:\.exe)?", p.name)
            ]
        except OSError:
            continue
        candidates.sort(
            key=lambda p: [int(c) if c.isdigit() else c for c in re.split(r"(\d+)", p.name)],
            reverse=True,
        )
        for path in candidates:
            name = path.name.lower()
            if is_windows:
                if not name.endswith(".exe"):
                    continue
            else:
                if name.endswith(".exe"):
                    continue
                if not os.access(path, os.X_OK):
                    try:
                        path.chmod(path.stat().st_mode | stat.S_IXUSR)
                    except OSError:
                        log.warning("Found aria2-next binary but it is not executable", path=str(path))
                        continue
            return path
    return None


ARIA2_NEXT_BINARY = _find_aria2_next_binary()


class Aria2RPCError(Exception):
    """JSON-RPC call to aria2 failed or timed out."""


class _Aria2RPCClient:
    """Minimal JSON-RPC client over an :class:`aiohttp.ClientSession`."""

    def __init__(self, session: aiohttp.ClientSession, url: str, secret: str | None = None) -> None:
        self._session = session
        self._url = url
        self._secret = secret
        self._counter = 0

    async def __aenter__(self) -> _Aria2RPCClient:
        return self

    async def __aexit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        return

    def _params(self, params: list[Any] | None) -> list[Any]:
        call_params: list[Any] = []
        if self._secret:
            call_params.append(f"token:{self._secret}")
        if params:
            call_params.extend(params)
        return call_params

    async def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._session.post(self._url, json=payload) as response:
                if response.status != 200:
                    body = await response.text()
                    raise Aria2RPCError(f"{method}: HTTP {response.status}: {body[:200]}")
                try:
                    data = await response.json(content_type=None)
                except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError) as exc:
                    raise Aria2RPCError(f"{method}: invalid JSON-RPC response") from exc
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise Aria2RPCError(f"Timeout waiting for {method}") from exc
        except aiohttp.ClientError as exc:
            raise Aria2RPCError(f"Could not reach aria2 RPC for {method}: {exc}") from exc

        if not isinstance(data, dict) or data.get("id") != payload["id"]:
            raise Aria2RPCError(f"{method}: invalid JSON-RPC response")
        if "error" in data:
            raise Aria2RPCError(f"{method}: {data['error']}")
        return data

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        self._counter += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._counter,
            "method": method,
            "params": self._params(params),
        }
        return (await self._post(method, payload)).get("result")

    async def multicall(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        """Execute a batch of methods in a single JSON-RPC system.multicall request."""
        if not calls:
            return []
        self._counter += 1
        methods = [
            {"methodName": method, "params": self._params(params)}
            for method, params in calls
        ]
        payload = {
            "jsonrpc": "2.0",
            "id": self._counter,
            "method": "system.multicall",
            "params": [methods],
        }
        data = await self._post("system.multicall", payload)
        raw_results = data.get("result") or []
        results: list[Any] = []
        for item in raw_results:
            if isinstance(item, list) and item:
                results.append(item[0])
            elif isinstance(item, dict) and "faultString" in item:
                raise Aria2RPCError(f"multicall item error: {item['faultString']}")
            else:
                results.append(item)
        return results


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "gid": str(entry.get("gid") or ""),
        "status": str(entry.get("status") or ""),
        "completedLength": _int(entry.get("completedLength")),
        "totalLength": _int(entry.get("totalLength")),
        "downloadSpeed": _int(entry.get("downloadSpeed")),
        "errorCode": str(entry.get("errorCode") or "0"),
        "errorMessage": str(entry.get("errorMessage") or ""),
    }


def is_disk_space_error(message: str) -> bool:
    normalized = message.lower()
    return any(
        marker in normalized
        for marker in ("no space left on device", "enospc", "errnum=28", "errorcode=28")
    )


def _entry_finished(entry: dict[str, Any]) -> bool:
    total_length = _int(entry.get("totalLength"))
    return (
        entry.get("status") == "complete"
        or (total_length > 0 and _int(entry.get("completedLength")) >= total_length)
    )


def _entry_settled(entry: dict[str, Any]) -> bool:
    return entry.get("status") in {"complete", "error", "removed"}


def _absorb_entry(
    progress_by_gid: dict[str, dict[str, Any]],
    agg: dict[str, int],
    entry: dict[str, Any],
) -> None:
    """Fold a parsed entry into ``progress_by_gid``, updating running totals.

    Entries always come from :func:`_parse_entry`, so numeric fields are
    already ints and need no re-coercion.
    """
    gid = entry.get("gid")
    if not gid:
        return
    prev = progress_by_gid.get(gid)
    if prev is not None:
        agg["downloaded"] -= prev["completedLength"]
        agg["total"] -= prev["totalLength"]
        agg["speed"] -= prev["downloadSpeed"]
        if _entry_finished(prev):
            agg["completed"] -= 1
        if _entry_settled(prev):
            agg["settled"] -= 1
    progress_by_gid[gid] = entry
    agg["downloaded"] += entry["completedLength"]
    agg["total"] += entry["totalLength"]
    agg["speed"] += entry["downloadSpeed"]
    if _entry_finished(entry):
        agg["completed"] += 1
    if _entry_settled(entry):
        agg["settled"] += 1


async def _read_stdout(
    proc: asyncio.subprocess.Process,
    output: deque[bytes],
    output_size: list[int],
) -> None:
    if proc.stdout is None:
        return
    while True:
        try:
            line = await proc.stdout.readline()
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, asyncio.IncompleteReadError):
            break
        if not line:
            break
        if len(line) > _MAX_OUTPUT_BYTES:
            line = line[-_MAX_OUTPUT_BYTES:]
        output.append(line)
        output_size[0] += len(line)
        while output_size[0] > _MAX_OUTPUT_BYTES:
            output_size[0] -= len(output.popleft())


async def _list_downloads(
    client: _Aria2RPCClient,
    method: str,
    total: int,
    keys: list[str],
) -> list[dict[str, Any]]:
    """Paginate ``aria2.tellWaiting``/``tellStopped`` calls.

    ``aria2.tellActive`` only accepts a ``keys`` array and returns the full
    active list, so it is handled as a single request.
    """
    results: list[dict[str, Any]] = []

    if method == "aria2.tellActive":
        if total <= 0:
            return results
        batch = await client.call(method, [keys])
        return cast(list[dict[str, Any]], batch) if isinstance(batch, list) else results

    offset = 0
    chunk = 1000
    while offset < total:
        params = [offset, min(chunk, total - offset), keys]
        batch = await client.call(method, params)
        if not isinstance(batch, list):
            break
        results.extend(cast(list[dict[str, Any]], batch))
        if len(batch) < min(chunk, total - offset):
            break
        offset += chunk
    return results


def _purge_resume_state(tmpfilename: str) -> bool:
    """Delete a stale partial download and its aria2 control file.

    Returns True when the resume state is gone (or was never there), so a
    retry cannot accidentally resume bytes from a mismatched resource.
    """
    if not tmpfilename:
        return False
    partial = Path(tmpfilename)
    clean = True
    for stale in (partial, Path(f"{partial}.aria2")):
        try:
            stale.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            clean = False
    return clean


def _build_fragment_calls(
    fragments: list[Any],
    fragment_output: str,
) -> list[tuple[str, list[Any]]]:
    """Build one addUri RPC call per fragment.

    Every fragment is (re-)enqueued on every run. aria2-next retired the
    .aria2 control file (v2.6.0) for SQLite-backed stream state, so a
    partial fragment from an interrupted session is indistinguishable from
    a complete one by stat() alone — completeness cannot be detected on
    disk. Re-enqueueing is safe and resume-correct: aria2-next resumes
    fragments with valid persisted state and re-downloads untracked ones
    under allow-overwrite.
    """
    built_calls: list[tuple[str, list[Any]]] = []
    for fragment_index, fragment in enumerate(fragments):
        if not isinstance(fragment, dict) or not isinstance(fragment.get("url"), str):
            raise Aria2RPCError(f"Fragment {fragment_index} has no downloadable URL")

        fragment_options: dict[str, Any] = {
            "out": f"{fragment_output}-Frag{fragment_index}",
            "allow-overwrite": "true",
            "auto-file-renaming": "false",
            "force-save": "false",
        }
        built_calls.append(("aria2.addUri", [[fragment["url"]], fragment_options]))
    return built_calls


class Aria2NextFD(_external.Aria2cFD):
    """yt-dlp external downloader that uses the bundled aria2-next binary.

    Adds HTTP JSON-RPC monitoring so real-time progress is pushed through
    the normal yt-dlp ``progress_hooks`` without parsing console output.
    """

    SUPPORTED_PROTOCOLS = (
        "http",
        "https",
        "m3u8_frag_urls",
        "dash_frag_urls",
    )

    EXE_NAME = "aria2-next"

    @property
    def exe(self) -> str:
        return str(ARIA2_NEXT_BINARY) if ARIA2_NEXT_BINARY else self.EXE_NAME

    # Set by the FileDownloader base class; declared for the type checker.
    ydl: Any = None
    _last_error_output: str = ""

    @classmethod
    def get_basename(cls) -> str:
        return "aria2-next"

    # Legacy aria2 options yt-dlp still emits but aria2-next retired or
    # owns natively. They are stripped, never translated — the engine passes
    # the maintained native flags (--state-save-interval,
    # --stream-max-connections, --optimize-concurrent-downloads) itself.
    _LEGACY_OPTIONS = {
        "--always-resume",
        "--max-resume-failure-tries",
        "--auto-save-interval",
        "--min-split-size",
        "--remove-control-file",
    }

    # Legacy flags that take no value; a bare occurrence must not swallow
    # the argument that follows.
    _LEGACY_VALUELESS = {"--always-resume", "--remove-control-file"}

    def _make_cmd(self, tmpfilename: str, info_dict: Any) -> list[str]:
        cmd = cast(list[str], cast(Any, super())._make_cmd(tmpfilename, info_dict))
        filtered: list[str] = []
        skip_next = False
        for arg in cmd:
            if skip_next:
                skip_next = False
                continue
            name = arg.split("=", 1)[0]
            is_combined = len(arg) > 2 and arg[:2] in {"-x", "-s"} and arg[2:].isdigit()
            is_short_conn = arg in {"-x", "-s"} or is_combined
            if name in self._LEGACY_OPTIONS or is_short_conn:
                # Legacy aria2 inputs the native engine owns or retired:
                # never pass them on (they only produce legacy-input
                # warnings, or exit 28 for removed options). The engine
                # passes the native equivalents explicitly. A standalone
                # "-x"/"-s" also consumes its following value.
                skip_next = (
                    "=" not in arg
                    and not is_combined
                    and name not in self._LEGACY_VALUELESS
                    and (name in self._LEGACY_OPTIONS or arg in {"-x", "-s"})
                )
                continue
            filtered.append(arg)
        return filtered

    @classmethod
    def supports(cls, info_dict: Any) -> bool:
        # The base class refuses formats carrying extra_param_to_segment_url,
        # which token-signed HLS masters always have (the manifest query must
        # ride on every segment). yt-dlp bakes that query into every fragment
        # URL before delegation (hls.py/dash.py update_url_query), so
        # aria2-next can honor it — refusing it just forces slow native HLS
        # for every token-signed stream. AES key URLs stay refused: aria2-next
        # cannot fetch/decrypt keys (and supports_manifest already rejects
        # encrypted manifests).
        if "extra_param_to_segment_url" in info_dict:
            info_dict = {k: v for k, v in info_dict.items() if k != "extra_param_to_segment_url"}
        return (
            not info_dict.get("has_drm")
            and not info_dict.get("request_data")
            and super().supports(info_dict)
        )

    @staticmethod
    def supports_manifest(manifest: str) -> bool:
        for raw_line in manifest.splitlines():
            line = raw_line.strip().upper()
            # Byterange fragments need the manifest-mandated Range honored
            # exactly. aria2-next's native range planner treats a ranged
            # response as partial progress toward the full resource and
            # re-fetches the remainder, corrupting the fragment. Let yt-dlp's
            # native HlsFD (which ranges in-process) take these manifests.
            if line.startswith("#EXT-X-BYTERANGE"):
                return False
            if not line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
                continue
            if parse_m3u8_attributes(line.partition(":")[2]).get("METHOD") != "NONE":
                return False
        return True

    def real_download(self, filename: str, info_dict: Any) -> bool:
        # Needed by the resume-mismatch recovery in _call_process: the stale
        # partial and its aria2 control file live at this exact path.
        self._current_tmpfilename = self.temp_name(filename)
        # Wire the app's progress hooks for every download, not just fragment
        # jobs: the RPC loop is the only live progress source here (yt-dlp's
        # ExternalFD base reports nothing between start and finish), so a
        # plain HTTP download would otherwise sit at 0% until completion.
        progress_hooks = getattr(self, "_progress_hooks", ())
        for hook in getattr(self.ydl, "_progress_hooks", ()):
            if hook not in progress_hooks:
                self.add_progress_hook(hook)
        return bool(super().real_download(filename, info_dict))

    def report_error(self, text: str, *args: Any, **kwargs: Any) -> None:
        if is_disk_space_error(getattr(self, "_last_error_output", "")):
            text = (
                "Insufficient disk space for aria2-next. Free space on the "
                "destination volume or set File allocation to 'none' and retry; "
                "the full download must still fit on disk."
            )
        self.ydl.report_error(text, *args, **kwargs)

    def _call_process(self, cmd: list[str], info_dict: dict[str, Any]) -> tuple[str, str, int]:
        self._last_error_output = ""
        try:
            stdout, stderr, returncode = asyncio.run(self._run_rpc(cmd, info_dict))
        except DownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            self._last_error_output = str(exc)
            log.exception("aria2-next RPC download failed", error=str(exc))
            stdout, stderr, returncode = "", str(exc), 1

        # A control file left by a previous attempt (different signed URL or
        # token) makes aria2 abort with "The remote resource changed while
        # resuming the download". The partial can never be resumed; purge it
        # and retry once from scratch instead of failing the whole download.
        if (
            returncode
            and not info_dict.get("fragments")
            and "remote resource changed" in stderr.lower()
            and _purge_resume_state(getattr(self, "_current_tmpfilename", ""))
        ):
            log.warning("aria2-next resume mismatch; cleared stale partial and retrying from scratch")
            try:
                return asyncio.run(self._run_rpc(cmd, info_dict))
            except DownloadCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error_output = str(exc)
                log.exception("aria2-next retry after resume mismatch failed", error=str(exc))
                return "", str(exc), 1

        return stdout, stderr, returncode

    @staticmethod
    def _output_text(output: deque[bytes]) -> str:
        return b"".join(output).decode("utf-8", errors="replace")

    @staticmethod
    async def _stop_process(proc: asyncio.subprocess.Process | None) -> None:
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=_RPC_SHUTDOWN_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                return
            await proc.wait()

    def _process_failed(self, proc: asyncio.subprocess.Process, output: deque[bytes]) -> Aria2RPCError:
        detail = self._output_text(output).strip()
        code = proc.returncode
        if detail:
            return Aria2RPCError(f"aria2-next exited with code {code}: {detail[-500:]}")
        return Aria2RPCError(f"aria2-next exited with code {code}")

    async def _run_rpc(
        self,
        cmd: list[str],
        info_dict: dict[str, Any],
    ) -> tuple[str, str, int]:
        fragments = list(info_dict.get("fragments") or [])
        fragment_output = ""
        aria2_cmd = cmd
        if fragments:
            try:
                separator = cmd.index("--")
                aria2_cmd = cmd[:separator]
                output_index = aria2_cmd.index("--out")
                fragment_output = aria2_cmd[output_index + 1]
            except (ValueError, IndexError) as exc:
                raise Aria2RPCError("Fragment command is missing -- or --out") from exc
            del aria2_cmd[output_index:output_index + 2]
            # fragment_output is a basename; aria2 resolves it against --dir.

        secret = secrets.token_hex(16)
        output: deque[bytes] = deque()
        output_size = [0]
        proc: asyncio.subprocess.Process | None = None
        reader: asyncio.Task[Any] | None = None
        session: aiohttp.ClientSession | None = None
        client: _Aria2RPCClient | None = None

        try:
            timeout = aiohttp.ClientTimeout(
                total=_RPC_CALL_TIMEOUT,
                sock_connect=2.0,
                sock_read=_RPC_CALL_TIMEOUT,
            )
            session = aiohttp.ClientSession(
                timeout=timeout,
                connector=aiohttp.TCPConnector(limit=4, ttl_dns_cache=60),
            )
            rpc_ready = False
            # The free-port probe can lose the race to another process between
            # probe and bind; if aria2-next exits before its RPC socket is
            # reachable, retry with fresh ports before surfacing the failure.
            for attempt in range(_RPC_STARTUP_ATTEMPTS):
                def _pick_ports() -> tuple[int, int]:
                    port = _find_free_port()
                    return port, _find_free_peer_port(port)

                port, peer_port = await asyncio.to_thread(_pick_ports)
                rpc_cmd = [
                    aria2_cmd[0],
                    "--enable-rpc",
                    f"--rpc-secret={secret}",
                    "--rpc-listen-all=false",
                    f"--rpc-listen-port={port}",
                    "--rpc-allow-origin-all=false",
                    "--enable-dht=false",
                    "--enable-peer-exchange=false",
                    "--bt-enable-lpd=false",
                    f"--listen-port={peer_port}",
                    "--stop-with-process",
                    str(os.getpid()),
                    *aria2_cmd[1:],
                ]
                if fragments:
                    rpc_cmd.append(f"--max-download-result={len(fragments)}")

                proc = await asyncio.create_subprocess_exec(
                    *rpc_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                reader = asyncio.create_task(_read_stdout(proc, output, output_size))
                client = _Aria2RPCClient(session, f"http://127.0.0.1:{port}/jsonrpc", secret=secret)

                deadline = time.monotonic() + _RPC_STARTUP_TIMEOUT
                while time.monotonic() < deadline:
                    if proc.returncode is not None:
                        break
                    try:
                        await client.call("aria2.getVersion")
                        rpc_ready = True
                        break
                    except Aria2RPCError:
                        await asyncio.sleep(0.1)

                if rpc_ready or proc.returncode is None:
                    break
                log.debug(
                    "aria2-next exited before RPC was ready; retrying",
                    attempt=attempt + 1,
                    code=proc.returncode,
                )
                await self._stop_process(proc)
                if reader is not None and not reader.done():
                    reader.cancel()
                    try:
                        await reader
                    except asyncio.CancelledError:
                        pass
                proc = reader = client = None

            if not rpc_ready:
                if proc is not None and proc.returncode is not None:
                    raise self._process_failed(proc, output)
                raise Aria2RPCError("aria2 JSON-RPC HTTP endpoint did not become available")

            async with client:
                # yt-dlp's FileDownloader base is untyped; cast so basedpyright
                # does not flag _hook_progress as an unknown attribute.
                _self: Any = cast(Any, self)

                calls: list[tuple[str, list[Any]]] = []
                if fragments:
                    calls = _build_fragment_calls(fragments, fragment_output)

                expected_gids = len(fragments) if fragments else 1

                if calls:
                    # Dispatch in batches of 100 via system.multicall for blazing fast enqueue
                    chunk_size = 100
                    for i in range(0, len(calls), chunk_size):
                        batch = calls[i:i + chunk_size]
                        gids = await client.multicall(batch)
                        for fragment_offset, gid in enumerate(gids):
                            actual_index = i + fragment_offset
                            if not isinstance(gid, str) or not gid:
                                raise Aria2RPCError(f"aria2 did not return a GID for fragment {actual_index}")

                progress_by_gid: dict[str, dict[str, Any]] = {}
                agg = {"downloaded": 0, "total": 0, "speed": 0, "completed": 0, "settled": 0}
                max_total = 0
                stopped_seen = 0
                rpc_failures = 0
                round_done = False
                last_progress: tuple[int, int, int] | None = None
                last_progress_at = time.monotonic()

                while not round_done:
                    if proc.returncode is not None:
                        raise self._process_failed(proc, output)
                    await asyncio.sleep(POLL_INTERVAL)

                    try:
                        gstat = await client.call("aria2.getGlobalStat")
                        stat_dict = cast(dict[str, Any], gstat) if isinstance(gstat, dict) else {}
                        num_active = _int(stat_dict.get("numActive"))
                        num_waiting = _int(stat_dict.get("numWaiting"))
                        num_stopped_total = _int(stat_dict.get("numStoppedTotal"))
                        new_stopped = max(0, num_stopped_total - stopped_seen)
                        # The three tell* queries are independent; run them concurrently.
                        poll_coros: list[Any] = [
                            _list_downloads(client, "aria2.tellActive", num_active, RPC_KEYS),
                            _list_downloads(client, "aria2.tellWaiting", num_waiting, RPC_KEYS),
                        ]
                        if new_stopped > 0:
                            poll_coros.append(client.call("aria2.tellStopped", [-1, new_stopped, RPC_KEYS]))
                        poll_results = await asyncio.gather(*poll_coros)
                        active = cast(list[dict[str, Any]], poll_results[0])
                        waiting = cast(list[dict[str, Any]], poll_results[1])
                        stopped: list[dict[str, Any]] = []
                        if new_stopped > 0 and isinstance(poll_results[2], list):
                            stopped = cast(list[dict[str, Any]], poll_results[2])
                    except Aria2RPCError:
                        rpc_failures += 1
                        if proc.returncode is not None:
                            raise self._process_failed(proc, output) from None
                        if rpc_failures >= _RPC_FAIL_LIMIT:
                            raise Aria2RPCError(
                                f"aria2 JSON-RPC became unreachable after {rpc_failures} failures"
                            ) from None
                        # Back off before retrying so a struggling aria2 is not
                        # hammered at the full poll rate.
                        await asyncio.sleep(min(1.0, POLL_INTERVAL * rpc_failures))
                        continue

                    rpc_failures = 0
                    stopped_seen = num_stopped_total

                    for entry in active:
                        _absorb_entry(progress_by_gid, agg, _parse_entry(entry))
                    for entry in waiting:
                        _absorb_entry(progress_by_gid, agg, _parse_entry(entry))
                    for entry in stopped:
                        _absorb_entry(progress_by_gid, agg, _parse_entry(entry))

                    downloaded = agg["downloaded"]
                    observed_total = agg["total"]
                    speed = agg["speed"]
                    completed = agg["completed"]
                    max_total = max(max_total, observed_total)

                    progress_now = (num_active, num_waiting, downloaded)
                    if progress_now != last_progress:
                        last_progress = progress_now
                        last_progress_at = time.monotonic()
                    elif num_active and time.monotonic() - last_progress_at > _RPC_STALL_TIMEOUT:
                        raise Aria2RPCError(
                            f"aria2-next made no progress for {int(_RPC_STALL_TIMEOUT)}s "
                            f"({num_active} active download(s) stalled)"
                        )

                    total = max_total or None
                    eta = None
                    if total is not None and total > downloaded and speed > 0:
                        eta = (total - downloaded + speed - 1) // speed

                    status: dict[str, Any] = {
                        "status": "downloading",
                        "downloaded_bytes": downloaded,
                        "total_bytes": total,
                        "speed": speed or None,
                        "eta": eta,
                    }
                    if "fragments" in info_dict:
                        status["segment_count"] = len(fragments)
                        status["segments_done"] = completed

                    _self._hook_progress(status, info_dict)

                    if num_active == 0 and num_waiting == 0:
                        if (
                            num_stopped_total >= expected_gids
                            and len(progress_by_gid) >= expected_gids
                        ):
                            round_done = True
                        elif progress_by_gid and agg["settled"] >= len(progress_by_gid):
                            round_done = True

                errors: list[dict[str, Any]] = []
                for entry in progress_by_gid.values():
                    if entry.get("status") == "error":
                        errors.append(entry)
                # skip_unavailable_fragments only applies to fragment downloads;
                # a failed single-file download must report failure so yt-dlp
                # does not treat the missing output file as success.
                skip_unavailable = _self.params.get("skip_unavailable_fragments", True)
                if fragments and errors and agg["completed"] <= 0:
                    # Every fragment failed: the output would be empty or
                    # corrupt. Never report success here — fail the download
                    # outright so the engine's error path handles it.
                    skip_unavailable = False
                returncode = 0 if (not errors or (fragments and skip_unavailable)) else 1

                if errors:
                    log.warning(
                        "aria2-next finished with errors",
                        gids=[e["gid"] for e in errors],
                        messages=[e.get("errorMessage") for e in errors],
                    )

                try:
                    await client.call("aria2.shutdown")
                except Aria2RPCError:
                    log.debug("aria2.shutdown call failed; process stop will clean up")

            error_summary = "\n".join(
                f"error_code={entry.get('errorCode')} message={entry.get('errorMessage')}"
                for entry in errors
            )
            stderr = "\n".join(part for part in (self._output_text(output), error_summary) if part)
            return "", stderr, returncode

        finally:
            if session is not None:
                await session.close()
            if reader is not None and not reader.done():
                reader.cancel()
                try:
                    await reader
                except asyncio.CancelledError:
                    pass
            await self._stop_process(proc)


if ARIA2_NEXT_BINARY:
    by_name = cast(Any, _external)._BY_NAME
    by_name["aria2-next"] = Aria2NextFD
    by_name[ARIA2_NEXT_BINARY.name] = Aria2NextFD
    by_name[ARIA2_NEXT_BINARY.stem] = Aria2NextFD
    log.info("Using bundled external downloader", path=str(ARIA2_NEXT_BINARY))
