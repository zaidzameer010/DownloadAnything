"""Aria2-next external downloader with WebSocket JSON-RPC progress.

yt-dlp delegates HTTP/FTP and fragmented HLS/DASH downloads to the bundled
``aria2-next`` binary.  This module replaces the previous console-readout
parser with a clean event loop that polls aria2's JSON-RPC over a WebSocket,
aggregates per-download progress, and pushes it through yt-dlp's
``progress_hooks``.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import socket
import stat
import sys
import time
from pathlib import Path
from typing import Any, cast

import aiohttp
import structlog
import yt_dlp.downloader.external as _external
from yt_dlp.utils import DownloadCancelled

log = structlog.get_logger("da.aria2next")

POLL_INTERVAL = 0.2
_RPC_STARTUP_TIMEOUT = 10.0
_RPC_CALL_TIMEOUT = 10.0
_RPC_SHUTDOWN_TIMEOUT = 10.0
_RPC_KEYS = [
    "gid",
    "status",
    "completedLength",
    "totalLength",
    "downloadSpeed",
    "errorCode",
    "errorMessage",
]


def _find_free_port() -> int:
    """Return an ephemeral TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _find_aria2_next_binary() -> Path | None:
    """Return the bundled aria2-next executable for the current platform, if any."""
    env_path = os.environ.get("ARIA2_NEXT_PATH")
    if env_path:
        return Path(env_path)

    system = platform.system().lower()
    is_windows = system == "windows"

    roots = [Path(sys.executable).resolve().parent]
    if not getattr(sys, "frozen", False):
        roots.append(Path(__file__).resolve().parent.parent)

    for root in roots:
        try:
            candidates = [p for p in root.iterdir() if p.is_file() and p.name.startswith("aria2-next")]
        except OSError:
            continue
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
                        log.warning("Found aria2-next binary but it is not executable: %s", path)
                        continue
            return path
    return None


ARIA2_NEXT_BINARY = _find_aria2_next_binary()


class Aria2RPCError(Exception):
    """JSON-RPC call to aria2 failed or timed out."""


class _Aria2RPCClient:
    """Minimal JSON-RPC client over an :class:`aiohttp.ClientWebSocketResponse`."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse, timeout: float = _RPC_CALL_TIMEOUT) -> None:
        self.ws = ws
        self._timeout = timeout
        self._counter = 0
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._receiver = asyncio.create_task(self._receive())

    async def _receive(self) -> None:
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        self._inbox.put_nowait(json.loads(msg.data))
                    except json.JSONDecodeError:
                        log.debug("Ignored non-JSON WebSocket message", data=msg.data[:200])
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("WebSocket receiver ended", error=str(exc))

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        self._counter += 1
        request_id = self._counter
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": list(params or []),
        }
        await self.ws.send_str(json.dumps(payload))

        deadline = time.monotonic() + self._timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                data = await asyncio.wait_for(self._inbox.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if data.get("id") == request_id:
                if "error" in data:
                    raise Aria2RPCError(f"{method}: {data['error']}")
                return data.get("result")
        raise Aria2RPCError(f"Timeout waiting for {method}")

    async def close(self) -> None:
        self._receiver.cancel()
        try:
            await self._receiver
        except asyncio.CancelledError:
            pass
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001
            pass


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


def _is_disk_space_error(message: str) -> bool:
    normalized = message.lower()
    return any(
        marker in normalized
        for marker in ("no space left on device", "enospc", "errnum=28", "errorcode=28")
    )


def _aggregate_progress(
    progress_by_gid: dict[str, dict[str, Any]],
) -> tuple[int, int, int, int]:
    """Return (downloaded, total, speed, completed_count)."""
    downloaded = 0
    total = 0
    speed = 0
    completed = 0
    for entry in progress_by_gid.values():
        completed_length = _int(entry.get("completedLength"))
        total_length = _int(entry.get("totalLength"))
        downloaded += completed_length
        total += total_length
        speed += _int(entry.get("downloadSpeed"))
        if (
            entry.get("status") == "complete"
            or (total_length > 0 and completed_length >= total_length)
        ):
            completed += 1
    return downloaded, total, speed, completed


async def _read_stdout(
    proc: asyncio.subprocess.Process,
    output: list[str],
) -> None:
    if proc.stdout is None:
        return
    while True:
        try:
            line = await proc.stdout.readline()
        except Exception:  # noqa: BLE001
            break
        if not line:
            break
        try:
            output.append(line.decode("utf-8", errors="replace").rstrip("\n"))
        except Exception:  # noqa: BLE001
            pass


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


class Aria2NextFD(_external.Aria2cFD):
    """yt-dlp external downloader that uses the bundled aria2-next binary.

    Adds WebSocket JSON-RPC monitoring so real-time progress is pushed through
    the normal yt-dlp ``progress_hooks`` without parsing console output.
    """

    SUPPORTED_PROTOCOLS = (
        "http",
        "https",
        "ftp",
        "ftps",
    )

    # Set by the FileDownloader base class; declared for the type checker.
    ydl: Any
    _last_error_output: str

    @classmethod
    def get_basename(cls) -> str:
        return "aria2-next"

    def report_error(self, text: str, *args: Any, **kwargs: Any) -> None:
        detail = getattr(self, "_last_error_output", "")
        if _is_disk_space_error(detail):
            text = (
                "Insufficient disk space for aria2-next. Free space on the "
                "destination volume or set File allocation to 'none' and retry; "
                "the full download must still fit on disk."
            )
        self.ydl.report_error(text, *args, **kwargs)

    @staticmethod
    def _aria2c_filename(filename: str) -> str:
        return filename if os.path.isabs(filename) else f".{os.path.sep}{filename}"

    def _call_process(self, cmd: list[str], info_dict: dict[str, Any]) -> tuple[str, str, int]:
        self._last_error_output = ""
        try:
            stdout, stderr, returncode = asyncio.run(self._run_rpc(cmd, info_dict))
            if returncode:
                self._last_error_output = stderr
            return stdout, stderr, returncode
        except DownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            self._last_error_output = str(exc)
            log.exception("aria2-next RPC download failed", error=str(exc))
            return "", str(exc), 1

    async def _run_rpc(
        self,
        cmd: list[str],
        info_dict: dict[str, Any],
    ) -> tuple[str, str, int]:
        port = _find_free_port()
        rpc_cmd = [
            cmd[0],
            "--enable-rpc",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={port}",
            "--rpc-allow-origin-all=false",
            *cmd[1:],
        ]

        output: list[str] = []
        proc: asyncio.subprocess.Process | None = None
        reader: asyncio.Task[Any] | None = None
        session: aiohttp.ClientSession | None = None
        client: _Aria2RPCClient | None = None

        try:
            proc = await asyncio.create_subprocess_exec(
                *rpc_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            reader = asyncio.create_task(_read_stdout(proc, output))

            session = aiohttp.ClientSession()
            ws: aiohttp.ClientWebSocketResponse | None = None
            deadline = time.monotonic() + _RPC_STARTUP_TIMEOUT
            while time.monotonic() < deadline:
                try:
                    ws = await session.ws_connect(f"ws://127.0.0.1:{port}/jsonrpc")
                    break
                except Exception:  # noqa: BLE001
                    if proc.returncode is not None:
                        break
                    await asyncio.sleep(0.1)

            if ws is None:
                raise Aria2RPCError("aria2 JSON-RPC WebSocket did not become available")

            async with ws:
                client = _Aria2RPCClient(ws)
                await client.call("aria2.getVersion")

                # yt-dlp's FileDownloader base is untyped; cast so basedpyright
                # does not flag _hook_progress as an unknown attribute.
                _self: Any = cast(Any, self)

                fragments = list(info_dict.get("fragments") or [])
                expected_gids = max(1, len(fragments))

                _self._hook_progress({
                    "status": "downloading",
                    "downloaded_bytes": 0,
                    "total_bytes": None,
                    "speed": None,
                    "eta": None,
                }, info_dict)

                progress_by_gid: dict[str, dict[str, Any]] = {}
                max_total = 0

                while True:
                    await asyncio.sleep(POLL_INTERVAL)

                    stat = await client.call("aria2.getGlobalStat")
                    stat_dict = cast(dict[str, Any], stat) if isinstance(stat, dict) else {}

                    num_active = _int(stat_dict.get("numActive"))
                    num_waiting = _int(stat_dict.get("numWaiting"))
                    num_stopped_total = _int(stat_dict.get("numStoppedTotal"))

                    active = await _list_downloads(client, "aria2.tellActive", num_active, _RPC_KEYS)
                    waiting = await _list_downloads(client, "aria2.tellWaiting", num_waiting, _RPC_KEYS)
                    stopped = await _list_downloads(client, "aria2.tellStopped", num_stopped_total, _RPC_KEYS)

                    for entry in active + waiting + stopped:
                        parsed = _parse_entry(entry)
                        if parsed["gid"]:
                            progress_by_gid[parsed["gid"]] = parsed

                    downloaded, observed_total, speed, completed = _aggregate_progress(progress_by_gid)
                    max_total = max(max_total, observed_total)

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
                        status["segment_count"] = expected_gids
                        status["segments_done"] = completed

                    _self._hook_progress(status, info_dict)

                    if (
                        num_active == 0
                        and num_waiting == 0
                        and num_stopped_total >= expected_gids
                        and len(progress_by_gid) >= expected_gids
                    ):
                        break

                errors: list[dict[str, Any]] = []
                for entry in progress_by_gid.values():
                    if entry.get("status") == "error":
                        errors.append(entry)
                skip_unavailable = _self.params.get("skip_unavailable_fragments", True)
                returncode = 0 if (not errors or skip_unavailable) else 1

                if errors and not skip_unavailable:
                    log.warning(
                        "aria2-next finished with errors",
                        gids=[e["gid"] for e in errors],
                        messages=[e.get("errorMessage") for e in errors],
                    )

                try:
                    await asyncio.wait_for(
                        client.call("aria2.shutdown"),
                        timeout=_RPC_SHUTDOWN_TIMEOUT,
                    )
                except Exception:  # noqa: BLE001
                    if proc.returncode is None:
                        proc.terminate()

                await client.close()

            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_RPC_SHUTDOWN_TIMEOUT)
                except asyncio.TimeoutError:
                    proc.terminate()
                    await proc.wait()

            return "", "\n".join(output), returncode

        finally:
            if client is not None:
                await client.close()
            if session is not None:
                await session.close()
            if reader is not None and not reader.done():
                reader.cancel()
                try:
                    await reader
                except asyncio.CancelledError:
                    pass
            if proc is not None and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()


if ARIA2_NEXT_BINARY:
    cast(Any, _external)._BY_NAME["aria2-next"] = Aria2NextFD
    log.info("Using bundled external downloader: %s", ARIA2_NEXT_BINARY)
