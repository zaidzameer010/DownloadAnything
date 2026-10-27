"""Multiplexed WebSocket JSON-RPC 2.0 client for aria2-next.

Connects to `ws://127.0.0.1:<port>/jsonrpc`, correlates concurrent RPC calls
by request id across worker threads, and wakes per-GID waiters on server-pushed
lifecycle notifications (`aria2.onDownloadStart`, `aria2.onDownloadPause`,
`aria2.onDownloadStop`, `aria2.onDownloadComplete`, `aria2.onDownloadError`,
`aria2.onBtDownloadComplete`) with zero polling delay.
"""

from __future__ import annotations

import contextlib
import itertools
import threading
import time
from collections.abc import Callable
from typing import Any

import msgspec
import structlog
from websockets.exceptions import WebSocketException
from websockets.sync.client import ClientConnection, connect

log = structlog.get_logger(__name__)


class Aria2RpcError(RuntimeError):
    """JSON-RPC error returned by aria2-next."""

    def __init__(self, code: int, message: str, method: str = "") -> None:
        self.code = code
        self.rpc_message = message
        self.method = method
        super().__init__(f"{method}: {message} (code {code})" if method else message)


def format_rpc_options(options: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize an options dict to the wire format aria2-next's optionPairs()
    expects: strings, booleans as 'true'/'false', numbers as decimal strings,
    and lists of strings (e.g. repeatable 'header' entries)."""
    if not options:
        return {}
    out: dict[str, Any] = {}
    for k, v in options.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out[k] = "true" if v else "false"
        elif isinstance(v, (int, float)):
            out[k] = str(v)
        elif isinstance(v, str):
            out[k] = v
        elif isinstance(v, (list, tuple)):
            items = [str(x) for x in v if x is not None and str(x) != ""]
            if items:
                out[k] = items
    return out


class _PendingCall:
    __slots__ = ("error", "event", "result")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: Exception | None = None


class Aria2RpcClient:
    """Thread-safe persistent WebSocket JSON-RPC 2.0 client for aria2-next."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 16802,
        secret: str = "",
        timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.secret = secret
        self.timeout = timeout

        self._ws: ClientConnection | None = None
        self._conn_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, _PendingCall] = {}
        self._id_seq = itertools.count(1)

        self._gid_lock = threading.Lock()
        self._gid_events: dict[str, set[threading.Event]] = {}

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/jsonrpc"

    def configure(self, port: int, secret: str) -> None:
        """Update endpoint credentials; reconnects on next call if changed."""
        if self.port == port and self.secret == secret:
            return
        self.port = port
        self.secret = secret
        self.disconnect()

    def subscribe_gid(self, gid: str) -> tuple[threading.Event, Callable[[], None]]:
        """Register an Event that is set whenever aria2-next pushes a WebSocket
        notification for `gid`."""
        ev = threading.Event()
        with self._gid_lock:
            self._gid_events.setdefault(gid, set()).add(ev)

        def unsubscribe() -> None:
            with self._gid_lock:
                waiters = self._gid_events.get(gid)
                if waiters is not None:
                    waiters.discard(ev)
                    if not waiters:
                        self._gid_events.pop(gid, None)

        return ev, unsubscribe

    def _ensure_connected(self) -> ClientConnection:
        with self._conn_lock:
            if self._ws is not None:
                return self._ws
            ws = connect(
                self.ws_url,
                open_timeout=min(self.timeout, 5.0),
                close_timeout=2.0,
                # tellStatus/inspectTorrent on a large torrent returns a
                # file entry per member — tens of thousands is realistic.
                max_size=64 * 1024 * 1024,
                compression=None,
            )
            self._ws = ws
            threading.Thread(
                target=self._reader_loop,
                args=(ws,),
                daemon=True,
                name="aria2-next-ws-reader",
            ).start()
            return ws

    def _reader_loop(self, ws: ClientConnection) -> None:
        try:
            while True:
                raw = ws.recv()
                raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
                try:
                    msg = msgspec.json.decode(raw_bytes)
                except msgspec.DecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "id" in msg and msg["id"] is not None:
                    self._handle_response(msg)
                elif "method" in msg:
                    self._handle_notification(msg)
        except Exception as e:
            transport = isinstance(e, (WebSocketException, OSError, EOFError))
            if not transport:
                log.debug("aria2-next ws reader exited", error=str(e))
            should_fail = False
            with self._conn_lock:
                if self._ws is ws:
                    self._ws = None
                    should_fail = True
            if should_fail:
                err = ConnectionError(f"aria2-next WebSocket closed: {e}") if transport else e
                self._fail_all_pending(err)
        finally:
            with self._conn_lock:
                if self._ws is ws:
                    self._ws = None

    def _handle_response(self, msg: dict[str, Any]) -> None:
        rid = msg.get("id")
        if not isinstance(rid, int):
            try:
                rid = int(rid)
            except (TypeError, ValueError):
                return
        with self._pending_lock:
            slot = self._pending.pop(rid, None)
        if slot is None:
            return
        err = msg.get("error")
        if isinstance(err, dict):
            code = int(err.get("code") or 1)
            message = str(err.get("message") or "RPC error")
            slot.error = Aria2RpcError(code, message)
        else:
            slot.result = msg.get("result")
        slot.event.set()

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        params = msg.get("params")
        gid = ""
        if isinstance(params, list) and params and isinstance(params[0], dict):
            gid = str(params[0].get("gid") or "")
        if not gid:
            return
        with self._gid_lock:
            waiters = list(self._gid_events.get(gid) or ())
        for ev in waiters:
            ev.set()

    def _fail_all_pending(self, exc: Exception) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for slot in pending:
            slot.error = exc
            slot.event.set()
        with self._gid_lock:
            for waiters in self._gid_events.values():
                for ev in waiters:
                    ev.set()

    def disconnect(self) -> None:
        with self._conn_lock:
            ws = self._ws
            self._ws = None
        if ws is not None:
            with contextlib.suppress(Exception):
                ws.close()
        self._fail_all_pending(ConnectionError("aria2-next RPC disconnected"))

    def _auth_params(self, params: list[Any] | None) -> list[Any]:
        args = list(params) if params else []
        if self.secret:
            return [f"token:{self.secret}", *args]
        return args

    def call(
        self,
        method: str,
        params: list[Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Execute a JSON-RPC 2.0 call over the persistent WebSocket."""
        wait_timeout = timeout if timeout is not None else self.timeout
        last_err: Exception | None = None
        for attempt in range(2):
            rid = next(self._id_seq)
            slot = _PendingCall()
            with self._pending_lock:
                self._pending[rid] = slot
            payload = msgspec.json.encode(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": method,
                    "params": self._auth_params(params),
                }
            ).decode("utf-8")
            try:
                ws = self._ensure_connected()
                with self._send_lock:
                    ws.send(payload)
            except Exception as e:
                with self._pending_lock:
                    self._pending.pop(rid, None)
                self.disconnect()
                last_err = e
                if attempt == 0:
                    continue
                raise

            if not slot.event.wait(wait_timeout):
                with self._pending_lock:
                    self._pending.pop(rid, None)
                raise TimeoutError(f"aria2-next RPC {method} timed out after {wait_timeout}s")

            if slot.error is not None:
                if isinstance(slot.error, Aria2RpcError):
                    raise Aria2RpcError(slot.error.code, slot.error.rpc_message, method)
                if attempt == 0 and isinstance(
                    slot.error, (ConnectionError, OSError, WebSocketException)
                ):
                    last_err = slot.error
                    continue
                raise slot.error
            return slot.result

        if last_err is not None:
            raise last_err
        raise ConnectionError(f"aria2-next RPC {method} failed")

    # ---- typed aria2-next RPC wrappers --------------------------------------

    def get_version(self, *, timeout: float = 3.0) -> dict[str, Any]:
        res = self.call("aria2.getVersion", [], timeout=timeout)
        return res if isinstance(res, dict) else {}

    def add_uri(
        self,
        uris: list[str],
        options: dict[str, Any] | None = None,
        position: int | None = None,
    ) -> str:
        params: list[Any] = [uris, format_rpc_options(options)]
        if position is not None:
            params.append(position)
        return str(self.call("aria2.addUri", params))

    def add_torrent(
        self,
        torrent_b64: str,
        uris: list[str] | None = None,
        options: dict[str, Any] | None = None,
        position: int | None = None,
    ) -> str:
        """Add a .torrent (base64 bencode); `uris` are web seeds."""
        params: list[Any] = [torrent_b64, list(uris or []), format_rpc_options(options)]
        if position is not None:
            params.append(position)
        return str(self.call("aria2.addTorrent", params))

    def change_option(self, gid: str, options: dict[str, Any]) -> str:
        return str(self.call("aria2.changeOption", [gid, format_rpc_options(options)]))

    def inspect_torrent(self, torrent_b64: str) -> dict[str, Any]:
        """Describe a .torrent without adding it (name/files/lengths)."""
        res = self.call("aria2.inspectTorrent", [torrent_b64])
        return res if isinstance(res, dict) else {}

    def tell_status(self, gid: str, keys: list[str] | None = None) -> dict[str, Any]:
        params: list[Any] = [gid]
        if keys:
            params.append(keys)
        res = self.call("aria2.tellStatus", params)
        return res if isinstance(res, dict) else {}

    def pause(self, gid: str, *, force: bool = False) -> str:
        method = "aria2.forcePause" if force else "aria2.pause"
        return str(self.call(method, [gid]))

    def pause_all(self, *, force: bool = False) -> str:
        method = "aria2.forcePauseAll" if force else "aria2.pauseAll"
        return str(self.call(method, []))

    def unpause(self, gid: str) -> str:
        return str(self.call("aria2.unpause", [gid]))

    def unpause_all(self) -> str:
        return str(self.call("aria2.unpauseAll", []))

    def remove(self, gid: str, *, force: bool = False) -> str:
        method = "aria2.forceRemove" if force else "aria2.remove"
        return str(self.call(method, [gid]))

    def remove_download_result(self, gid: str) -> str:
        return str(self.call("aria2.removeDownloadResult", [gid]))

    def purge_download_result(self) -> str:
        return str(self.call("aria2.purgeDownloadResult", []))

    def evict(self, gid: str) -> None:
        """forceRemove + removeDownloadResult, retrying while the task is
        still stopping: a torrent pauses, saves resume data and leaves the
        session asynchronously, and its result is not removable until then.
        Without the retry the gid stays occupied and a re-add fails. Gids
        absent from the session bail fast — stale fragment ids would
        otherwise burn the full retry budget each."""
        with contextlib.suppress(Aria2RpcError):
            self.remove(gid, force=True)
        try:
            self.tell_status(gid, ["status"])
        except Aria2RpcError:
            with contextlib.suppress(Aria2RpcError):
                self.remove_download_result(gid)
            return
        for _ in range(10):
            try:
                self.remove_download_result(gid)
                return
            except Aria2RpcError:
                time.sleep(0.2)

    def change_global_option(self, options: dict[str, Any]) -> str:
        return str(self.call("aria2.changeGlobalOption", [format_rpc_options(options)]))

    def get_global_option(self) -> dict[str, Any]:
        res = self.call("aria2.getGlobalOption", [])
        return res if isinstance(res, dict) else {}

    def finish_media(self, gid: str) -> str:
        """End an active or paused live HLS/DASH recording and finalize output."""
        return str(self.call("aria2.finishMedia", [gid]))

    def retry_media(self, gid: str, options: dict[str, Any] | None = None) -> str:
        """Requeue a failed media task with the same GID and retained segments."""
        params: list[Any] = [gid]
        if options:
            params.append(format_rpc_options(options))
        return str(self.call("aria2.retryMedia", params))

    def shutdown(self, *, force: bool = False) -> str:
        method = "aria2.forceShutdown" if force else "aria2.shutdown"
        return str(self.call(method, [], timeout=3.0))
