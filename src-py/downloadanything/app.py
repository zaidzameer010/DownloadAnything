"""Litestar app: one WebSocket channel, static UI and a thumbnail proxy.

Everything the backend exposes rides a single /ws socket:
  commands   {"id":n,"method":…,"params":{…}} → {"id":n,"result"|"error":…}
  events     {"type":"downloadanything-push"|"downloads-snapshot"|"engine-status",
              "payload":{…}} — pushed, never polled

The one HTTP route is `/thumb`: the UI cannot load hotlink-protected cover
art with its own Referer, so the backend fetches it with the page referer.

The extension connects the same way (token in the query string) and uses
`push`/`ping` methods; the UI uses the full method set. Download state is
broadcast while clients are connected — no client-side polling.

When ui/dist exists it is served same-origin, with the api token injected
into index.html — the app UI needs no manual pairing while the socket
itself stays token-guarded.
"""

import asyncio
import base64
import hmac
import inspect
import mimetypes
import sqlite3
from collections.abc import Coroutine, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import msgspec
import structlog
from cachebox import TTLCache
from litestar import Litestar, Request, Response, WebSocket, get
from litestar.datastructures import State
from litestar.di import Provide
from litestar.exceptions import NotFoundException, WebSocketDisconnect
from litestar.handlers import websocket_listener
from litestar.response import File

from . import aria2_next, downloads, extract
from .db import TaskDb
from .downloads import Registry
from .log import clean_text, init_log
from .models import DownloadReq, ExtractOptions
from .settings import DATA_DIR, AppSettings, resolve_ffmpeg
from .thumbs import fetch_thumbnail

log = structlog.get_logger(__name__)

UI_DIST = Path(__file__).resolve().parents[2] / "ui" / "dist"
UI_DIST_RESOLVED = UI_DIST.resolve()


class Shared:
    """App state."""

    def __init__(self, data_dir: Path) -> None:
        self.db = TaskDb.open(data_dir / "downloadanything.db")
        self._settings = AppSettings.load(self.db)
        self.registry = Registry(self.db, self._settings)
        self.ws_clients: set[WebSocket] = set()
        # Strong refs to fire-and-forget tasks — a task nobody holds can be
        # collected mid-run, and its exception would never be retrieved.
        self._bg: set[asyncio.Task] = set()
        # Last broadcast payloads — the loop skips identical resends.
        self.last_snap: bytes | None = None
        self.last_engine: bytes | None = None

    def settings(self) -> AppSettings:
        return self._settings

    def set_settings(self, s: AppSettings) -> None:
        self._settings = s
        self.registry.set_max_concurrent(s.max_concurrent_downloads)


def _shared(state: State) -> Shared:
    return state.shared


def _spawn(shared: Shared, coro: Coroutine[Any, Any, Any]) -> None:
    """create_task that keeps a ref until the coroutine finishes."""
    t = asyncio.create_task(coro)
    shared._bg.add(t)
    t.add_done_callback(shared._bg.discard)


# ---- shared logic -----------------------------------------------------------


async def _apply_settings(shared: Shared, data: dict[str, Any]) -> str | None:
    """Persist settings. `data` is the full settings object the UI sends —
    unmentioned fields reset to defaults by design."""
    new = AppSettings.from_dict(data, fallback_token=shared.settings().api_token)
    try:
        new.save(shared.db)
    except sqlite3.Error as e:
        return str(e)
    shared.set_settings(new)
    await asyncio.to_thread(aria2_next.get_daemon().sync_settings, new)
    log.info("settings saved")
    await _broadcast_state(shared)
    return None


async def _regenerate(shared: Shared) -> tuple[AppSettings | None, str | None]:
    new = replace(shared.settings())
    new.rotate_tokens()
    shared.set_settings(new)
    try:
        new.save(shared.db)
    except sqlite3.Error as e:
        log.warning("rotated tokens not persisted — db keeps old", error=e)
    return new, None


# ---- websocket RPC + push --------------------------------------------------


async def _send_text(shared: Shared, sockets: Iterable[WebSocket], body: bytes) -> None:
    """Send pre-encoded bytes to each socket; a dead socket is evicted early
    (its disconnect handler would drop it anyway)."""

    async def one(ws: WebSocket) -> None:
        try:
            await ws.send_text(body)
        except Exception as e:
            # A dead socket must not fail the broadcast for everyone else;
            # the disconnect handler would evict it on the next tick.
            log.debug("websocket send failed", error=str(e))
            shared.ws_clients.discard(ws)

    await asyncio.gather(*(one(ws) for ws in list(sockets)))


async def _send_msg(shared: Shared, sockets: Iterable[WebSocket], msg: dict[str, Any]) -> None:
    """Encode once, then fan out — send_json would re-serialize per socket."""
    await _send_text(shared, sockets, msgspec.json.encode(msg))


async def _broadcast(shared: Shared, msg: dict[str, Any]) -> None:
    await _send_msg(shared, shared.ws_clients, msg)


async def _push_state(shared: Shared, sockets: list[WebSocket], *, force: bool = False) -> Any:
    """Gather snapshot + engine status once, push to `sockets`. Payloads
    identical to the last broadcast are skipped — the idle tick otherwise
    resends the whole registry every second. `force` sends to a specific
    newly-connected client without mutating the broadcast cache for others.
    """
    snap = await downloads.downloads_snapshot(shared)
    body = msgspec.json.encode({"type": "downloads-snapshot", "payload": snap})
    if force or body != shared.last_snap:
        await _send_text(shared, sockets, body)
    if not force:
        shared.last_snap = body
    estat = msgspec.json.encode(
        {"type": "engine-status", "payload": _engine_status_dict(shared.settings())}
    )
    if force or estat != shared.last_engine:
        await _send_text(shared, sockets, estat)
    if not force:
        shared.last_engine = estat
    return snap


async def _broadcast_state(shared: Shared) -> None:
    """Broadcasts a fresh snapshot to all connected clients."""
    if shared.ws_clients:
        await _push_state(shared, list(shared.ws_clients))


async def _ws_accept_guard(socket: WebSocket) -> None:
    """Runs at handshake: raising denies the upgrade — a sent close() would
    still let the listener run and accept() after close."""
    shared = socket.scope["app"].state.shared
    token = (socket.query_params or {}).get("token", "")
    if not hmac.compare_digest(str(token).encode(), shared.settings().api_token.encode()):
        raise WebSocketDisconnect(detail="bad token", code=4401)
    await socket.accept()
    shared.ws_clients.add(socket)
    _spawn(shared, _push_state(shared, [socket], force=True))


async def _ws_on_disconnect(socket: WebSocket) -> None:
    socket.scope["app"].state.shared.ws_clients.discard(socket)


async def _state_broadcast_loop(shared: Shared) -> None:
    """Real-time server-side state streaming: pushes the downloads snapshot
    and engine status to connected clients with 250ms cadence during downloads
    and 1.0s when idle."""
    while True:
        if not shared.ws_clients:
            await asyncio.sleep(0.5)
            continue
        has_active = False
        try:
            snap = await _push_state(shared, list(shared.ws_clients))
            if isinstance(snap, dict):
                if snap.get("active") or snap.get("waiting"):
                    has_active = True
                stat = snap.get("stat")
                if isinstance(stat, dict) and (
                    int(stat.get("numActive") or 0) > 0 or int(stat.get("downloadSpeed") or 0) > 0
                ):
                    has_active = True
        except Exception:
            log.exception("state broadcast failed")
            # A persistent failure must not spin-log at tick cadence.
            await asyncio.sleep(5.0)
        await asyncio.sleep(0.25 if has_active else 1.0)


# ---- method dispatch ---------------------------------------------------------


async def _m_extract(shared: Shared, p: dict[str, Any]) -> Any:
    urls = p.get("urls") or []
    # A bare string would be iterated char-by-char downstream.
    if not isinstance(urls, list) or not urls or not all(isinstance(u, str) for u in urls):
        raise ValueError("no urls")
    s = shared.settings()
    merged = s.to_downloadanything_options()
    incoming = msgspec.to_builtins(msgspec.convert(p.get("options") or {}, ExtractOptions))
    for k, v in incoming.items():
        if v is not None and v != [] and v != {}:
            merged[k] = v
    opts = msgspec.convert(merged, ExtractOptions)
    if p.get("referer"):
        opts.referer = p["referer"]
    return await extract.extract(opts, urls)


async def _m_get_settings(shared: Shared, p: dict[str, Any]) -> Any:
    return shared.settings().to_dict()


async def _m_save_settings(shared: Shared, p: dict[str, Any]) -> Any:
    if e := await _apply_settings(shared, p.get("settings") or {}):
        raise RuntimeError(e)
    return {"ok": True}


async def _m_regenerate(shared: Shared, p: dict[str, Any]) -> Any:
    new, e = await _regenerate(shared)
    if e or new is None:
        raise RuntimeError(e or "token rotation failed")
    return new.to_dict()


def _engine_status_dict(s: AppSettings) -> dict[str, Any]:
    """The backend IS the engine — running is always true; the version is
    the yt-dlp in use (plus aria2-next when active) and the features list
    what extraction and downloading can use."""
    import yt_dlp

    try:
        from . import downloadanything_rs  # noqa: F401

        rust = True
    except Exception as e:
        log.debug("rust extractor unavailable", error=str(e))
        rust = False
    features = ["python-ytdlp"]
    if rust:
        features.append("rust-extractor")
    daemon = aria2_next.get_daemon()
    aria2_bin = aria2_next.resolve_aria2_next(s)
    if aria2_bin:
        features.append("aria2-next")
    ver = f"yt-dlp {yt_dlp.version.__version__}"
    if s.downloader_engine == "aria2_next" and aria2_bin:
        a2_ver = daemon.version
        ver = f"{ver} · aria2-next {a2_ver}" if a2_ver else f"{ver} · aria2-next"
    return {
        "running": True,
        "version": ver,
        "mediaFeatures": features,
        "downloaderEngine": s.downloader_engine,
        "aria2NextRunning": daemon.is_running(),
    }


async def _m_engine_status(shared: Shared, p: dict[str, Any]) -> Any:
    return _engine_status_dict(shared.settings())


async def _m_control(shared: Shared, p: dict[str, Any]) -> Any:
    r, e = await downloads.control_task(shared, p.get("gid") or "", p.get("action") or "")
    if e is not None:
        raise RuntimeError(e)
    _spawn(shared, _broadcast_state(shared))
    return r


async def _m_add_download(shared: Shared, p: dict[str, Any]) -> Any:
    req = msgspec.convert(p.get("req") or {}, DownloadReq)
    gid, e = await downloads.add_download(shared, req)
    if e is not None:
        raise RuntimeError(e)
    _spawn(shared, _broadcast_state(shared))
    return {"gid": gid}


def _m_check_name(shared: Shared, p: dict[str, Any]) -> Any:
    req = msgspec.convert(p.get("req") or {}, DownloadReq)
    return downloads.check_name_conflict(shared, req)


def _m_log(shared: Shared, p: dict[str, Any]) -> Any:
    structlog.get_logger("ui").error(clean_text(str(p.get("message") or "")))
    return {"ok": True}


async def _m_push(shared: Shared, p: dict[str, Any]) -> Any:
    """Extension → broadcast."""
    url = p.get("url")
    if not url:
        raise ValueError("no url")
    log.info(
        "push received",
        url=url,
        referer=p.get("referer") or None,
        title=p.get("title"),
        contexts=len(p.get("contexts") or []),
        intercepted=(p.get("download") or {}).get("name") or None,
    )
    await _broadcast(
        shared,
        {
            "type": "downloadanything-push",
            "payload": {
                "url": url,
                "referer": p.get("referer"),
                "title": p.get("title"),
                "thumbnail": p.get("thumbnail"),
                "contexts": p.get("contexts") or [],
                "download": p.get("download"),
            },
        },
    )
    return {"ok": True}


async def _m_ping(shared: Shared, p: dict[str, Any]) -> Any:
    """Extension health check."""
    return {"ok": True, "engine": _engine_status_dict(shared.settings())}


async def _m_inspect_torrent(shared: Shared, p: dict[str, Any]) -> Any:
    """Describe a .torrent (URL or base64 bytes) without adding it — the
    file picker previews name/files and echoes `torrent` back on submit."""
    s = shared.settings()
    data = str(p.get("data") or "").strip()
    if not data:
        url = str(p.get("url") or "").strip()
        if not aria2_next.is_torrent_url(url):
            raise ValueError("not a .torrent URL")
        req = {
            "url": url,
            "referer": p.get("referer"),
            "contexts": p.get("contexts") or [],
        }
        raw = await asyncio.to_thread(
            aria2_next.fetch_torrent,
            url,
            aria2_next.torrent_headers(req),
            verify=s.check_certificate,
        )
        data = base64.b64encode(raw).decode()

    def _describe() -> dict[str, Any]:
        rpc = aria2_next.get_daemon().ensure_running(s)
        return rpc.inspect_torrent(data)

    info = await asyncio.to_thread(_describe)
    info["torrent"] = data
    return info


async def _m_set_torrent_files(shared: Shared, p: dict[str, Any]) -> Any:
    """Apply the picker's selection to a magnet parked by pause-metadata and
    start its download."""
    gid = str(p.get("gid") or "")
    task = shared.registry.get(gid)
    if task is None:
        raise ValueError("task not found")
    files = list(task.torrent_files)
    if not files:
        raise ValueError("torrent metadata not ready")
    valid = aria2_next.valid_selection(p.get("indices") or [], files)
    if not valid:
        raise ValueError("no files selected")
    sel = aria2_next.format_selection(valid)
    s = shared.settings()

    def _apply() -> None:
        rpc = aria2_next.get_daemon().ensure_running(s)
        rpc.change_option(gid, {"select-file": sel})
        rpc.unpause(gid)

    await asyncio.to_thread(_apply)
    with task.lock:
        if task.status == "selecting":
            task.status = "active"
    # The worker parks while awaiting selection (no download slot held) —
    # restart it now that the torrent is running again.
    await asyncio.to_thread(shared.registry.launch, task, s)
    log.info("torrent files selected", gid=gid, select_file=sel)
    return {"ok": True, "selectFile": sel}


async def _m_ytdlp_info(shared: Shared, p: dict[str, Any]) -> Any:
    """In-process yt-dlp + external ffmpeg & aria2-next resolution."""
    import yt_dlp

    s = shared.settings()
    ffmpeg, aria2_st = await asyncio.gather(
        asyncio.to_thread(resolve_ffmpeg, s),
        asyncio.to_thread(aria2_next.get_daemon().status_dict, s),
    )
    return {
        "available": ffmpeg is not None,
        "ytdlp_path": f"yt-dlp {yt_dlp.version.__version__} (in-process)",
        "ffmpeg_path": ffmpeg,
        "aria2_next_available": aria2_st["available"],
        "aria2_next_running": aria2_st["running"],
        "aria2_next_path": aria2_st["binary_path"],
        "aria2_next_version": aria2_st["version"],
        "aria2_next_rpc_port": aria2_st["rpc_port"],
    }


_WS_METHODS = {
    "push": _m_push,
    "ping": _m_ping,
    "extract_media": _m_extract,
    "get_app_settings": _m_get_settings,
    "save_app_settings": _m_save_settings,
    "regenerate_tokens": _m_regenerate,
    "engine_status": _m_engine_status,
    "download_control": _m_control,
    "add_download": _m_add_download,
    "check_download_name": _m_check_name,
    "inspect_torrent": _m_inspect_torrent,
    "set_torrent_files": _m_set_torrent_files,
    "ytdlp_info": _m_ytdlp_info,
    "log_ui_error": _m_log,
}


async def _ws_handler(socket: WebSocket, data: Any) -> dict[str, Any]:
    """{id,method,params} → {id,result|error}. Anything else is ignored."""
    if not isinstance(data, dict):
        return {"id": None, "error": "bad frame"}
    rid, method = data.get("id"), data.get("method")
    params = data.get("params") or {}
    if not isinstance(params, dict):
        return {"id": rid, "error": "params must be an object"}
    fn = _WS_METHODS.get(method or "")
    if fn is None:
        return {"id": rid, "error": f"unknown method {method!r}"}
    shared = socket.scope["app"].state.shared
    try:
        result = fn(shared, params)
        if inspect.isawaitable(result):
            result = await result
        return {"id": rid, "result": result}
    except Exception as e:
        log.exception("ws method failed", method=method, req_id=rid)
        return {"id": rid, "error": clean_text(str(e))}


ws = websocket_listener(
    "/ws",
    connection_accept_handler=_ws_accept_guard,
    on_disconnect=_ws_on_disconnect,
)(_ws_handler)


# ---- thumbnail proxy -------------------------------------------------------

_thumb_cache: TTLCache[tuple[str, str], tuple[bytes, str]] = TTLCache(128, 3600)
# Short negative cache — a dead/expired thumb URL otherwise re-pays the
# upstream timeout on every render pass.
_thumb_neg: TTLCache[tuple[str, str], None] = TTLCache(256, 60)
# Concurrent misses for the same image share one upstream fetch — a grid of
# rows after the same cover must not fan out to one request each.
_thumb_inflight: dict[tuple[str, str], asyncio.Task] = {}


@get("/thumb", include_in_schema=False)
async def thumbnail_proxy(shared: Shared, request: Request) -> Response:
    token = request.query_params.get("token") or ""
    if not hmac.compare_digest(token, shared.settings().api_token):
        return Response("forbidden", status_code=403, media_type="text/plain")
    url = request.query_params.get("u") or ""
    referer = request.query_params.get("r") or ""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return Response("bad url", status_code=400, media_type="text/plain")
    key = (url, referer)
    hit = _thumb_cache.get(key)
    if hit is None:
        if key in _thumb_neg:
            return Response("thumbnail unavailable", status_code=502, media_type="text/plain")
        task = _thumb_inflight.get(key)
        if task is None:
            task = asyncio.create_task(asyncio.to_thread(fetch_thumbnail, url, referer))
            _thumb_inflight[key] = task
            task.add_done_callback(lambda _t, k=key: _thumb_inflight.pop(k, None))
        try:
            hit = await asyncio.shield(task)
        except Exception:
            _thumb_neg.insert(key, None)
            return Response("thumbnail unavailable", status_code=502, media_type="text/plain")
        _thumb_cache.insert(key, hit)
    body, content_type = hit
    return Response(
        body,
        media_type=content_type,
        headers={"cache-control": "public, max-age=86400"},
    )


# ---- static UI -------------------------------------------------------------


def _resolve_ui_target(path: str) -> Path:
    """Join under UI_DIST and refuse escapes. resolve() normalizes "..";
    is_relative_to (not a str prefix) keeps a ui/dist2 sibling outside.
    The catch-all arrives with a leading slash — joining it as-is would make
    pathlib treat the request as absolute and every asset would fall back to
    index.html."""
    t = (UI_DIST / path.lstrip("/")).resolve()
    if not t.is_relative_to(UI_DIST_RESOLVED):
        return UI_DIST / "index.html"
    return t


_index_cache: dict[str, tuple[float, str]] = {}


def _read_index(path: Path) -> str:
    """index.html text keyed by mtime — read once per build, not per hit."""
    mtime = path.stat().st_mtime
    if (cached := _index_cache.get(str(path))) and cached[0] == mtime:
        return cached[1]
    text = path.read_text()
    _index_cache[str(path)] = (mtime, text)
    return text


@get(["/", "/{path:path}"], include_in_schema=False)
async def static_ui(shared: Shared, request: Request, path: str = "") -> Response | File:
    # Litestar hands the catch-all its leading slash: path is "/api/…".
    if path.lstrip("/").startswith(("api", "ws")):
        raise NotFoundException()
    if not UI_DIST.is_dir():
        return Response(
            "DownloadAnything backend running — ui/dist not built", media_type="text/plain"
        )
    target = _resolve_ui_target(path) if path else UI_DIST / "index.html"
    if not target.is_file():
        target = UI_DIST / "index.html"  # SPA fallback
    if target.name == "index.html":
        # Same-origin token injection — the app UI pairs without pasting.
        # Inject only for our own page loads: a foreign-origin fetch of this
        # index must never see the token or it could drive the socket.
        # The listener only ever binds 127.0.0.1, so a non-loopback Host is
        # a forged header (DNS-rebind), not our own page. no-store keeps
        # bundles (and tokens) from going stale in cache.
        origin = request.headers.get("origin") or ""
        host = request.headers.get("host") or ""
        hostname = host.rsplit(":", 1)[0].strip("[]")
        same_origin = hostname in ("127.0.0.1", "localhost", "::1") and (
            not origin or origin in (f"http://{host}", f"https://{host}")
        )
        html = await asyncio.to_thread(_read_index, target)
        if same_origin:
            token = shared.settings().api_token
            inject = f'<script>window.__DOWNLOADANYTHING_TOKEN__="{token}"</script>'
            html = html.replace("</head>", f"{inject}</head>", 1)
        return Response(
            html,
            media_type="text/html",
            headers={"cache-control": "no-store"},
        )
    # File derives the type from `filename` only; without it every bundle
    # lands as application/octet-stream and module scripts are refused.
    return File(
        path=target,
        media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
    )


# ---- lifecycle ---------------------------------------------------------------


def _pause_restored_torrents(shared: Shared) -> None:
    """aria2-next restores its session (torrents come back active) while the
    app restores every live task paused — pause the torrents to match, so no
    task downloads or seeds invisibly until the user resumes it."""
    daemon = aria2_next.get_daemon()
    if not daemon.is_running():
        return
    for t in shared.registry.all():
        if not aria2_next.is_torrent_req(t.req):
            continue
        try:
            daemon.rpc.pause(t.gid)
        except Exception as e:
            # Not in the session (complete/purged) — nothing to pause.
            log.debug("torrent not paused", gid=t.gid, error=str(e))


async def _on_startup(app: Litestar) -> None:
    shared = app.state.shared
    # Restored tasks enter the registry paused — the user resumes explicitly.
    await asyncio.to_thread(downloads.restore_pending, shared)
    s = shared.settings()
    if aria2_next.use_aria2_next(s):
        try:
            await asyncio.to_thread(aria2_next.get_daemon().ensure_running, s)
            await asyncio.to_thread(_pause_restored_torrents, shared)
        except Exception as e:
            log.warning("aria2-next startup warm-up failed", error=str(e))
    _spawn(shared, _state_broadcast_loop(shared))


async def _on_shutdown(app: Litestar) -> None:
    shared = app.state.shared
    try:
        # Pause every live task before the daemon dies — workers unwind via
        # cancel, rows persist 'paused', and no .part is left mid-write.
        await asyncio.to_thread(downloads._control_local, shared, None, "pauseAll")
    except Exception as e:
        log.warning("shutdown pause-all failed", error=str(e))
    try:
        await asyncio.to_thread(aria2_next.get_daemon().shutdown)
    finally:
        await asyncio.to_thread(shared.db.close)


def create_app() -> Litestar:
    data_dir = DATA_DIR
    init_log(data_dir / "downloadanything.log")
    data_dir.mkdir(parents=True, exist_ok=True)
    shared = Shared(data_dir)
    try:
        shared.settings().save(shared.db)
    except sqlite3.Error as e:
        log.warning("settings save failed", error=e)
    return Litestar(
        route_handlers=[ws, thumbnail_proxy, static_ui],
        state=State({"shared": shared}),
        dependencies={"shared": Provide(_shared, sync_to_thread=False)},
        on_startup=[_on_startup],
        on_shutdown=[_on_shutdown],
        openapi_config=None,  # type: ignore[call-arg]
        # litestar's default installs a QueueHandler on root that pre-empts
        # our structlog renderers — logging is owned by log.init_log.
        logging_config=None,  # type: ignore[call-arg]
    )


APP = create_app()
