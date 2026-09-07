"""DownloadAnything FastAPI websocket backend.

Single websocket server shared by the Chrome extension and the React
dashboard. JSON messages, one per frame:

Client -> server:
    {"type": "hello", "client": "extension" | "dashboard"}
    {"type": "probe", "reqId": str, "url": str, "fallbackUrl"?: str,
     "fallbackUrls"?: [str], "mediaType"?: str}
    {"type": "probe_cancel", "reqId": str, "probeReqId": str}
    {"type": "download", "reqId": str, "url": str, "formatId"?: str,
     "directory"?: str, "filename"?: str, "title"?: str, "thumbnail"?: str,
     "engine"?: str, "mediaType"?: str, "referer"?: str,
     "downloadPlaylist"?: bool, "selectedEntryUrls"?: [str],
     "duplicateAction"?: "override" | "rename" | "skip" | "resume"}
    {"type": "job_action", "reqId"?: str, "jobId": str,
     "action": "cancel" | "pause" | "resume" | "remove"}
    {"type": "reveal", "reqId"?: str, "jobId": str}
    {"type": "clear_finished"}
    {"type": "settings_get"}
    {"type": "settings_set", "settings": {...partial}}
    {"type": "browsers_list"}
    {"type": "install_extension", "browserId": str}

Server -> client:
    {"type": "hello", "ok": true, "version", "settings", "jobs"}
    {"type": "probe_result", "reqId", "ok", "engine", "result"? | "error"?}
    {"type": "probe_cancel_result", "reqId", "ok", "error"?}
    {"type": "download_started", "reqId", "ok", "jobId"? | "error"?,
     "skipped"?: bool, "duplicate"?: {"type": "job"|"file", "existing": ..., "filename": str, "suggestedName": str}}
    {"type": "reveal_result", "reqId", "ok", "error"?}
    {"type": "job_update", "job": {...}}           (broadcast)
    {"type": "job_removed", "jobId"}               (broadcast)
    {"type": "settings", "settings": {...}}        (broadcast)
    {"type": "error", "reqId"?, "error": str}

Run with: conda run -n downloadanything python backend/server.py
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import logging
import orjson
import os
import re
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import structlog
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, WebSocketException
import uvicorn


try:
    from .browsers import install_extension, extension_dir, list_browsers
    from .db import Database
    from .engine import DownloadManager
    from .log import configure_logging
    from .settings import SettingsStore
except ImportError:
    from browsers import install_extension, extension_dir, list_browsers
    from db import Database
    from engine import DownloadManager
    from log import configure_logging
    from settings import SettingsStore

HOST = os.environ.get("DA_HOST", "127.0.0.1")
PORT = int(os.environ.get("DA_PORT", "8765"))
IS_BUNDLED = "__compiled__" in globals() or bool(getattr(sys, "frozen", False))
RELOAD = os.environ.get("DA_RELOAD", "false" if IS_BUNDLED else "true").lower() in {"1", "true", "yes"}
PROBE_TIMEOUT_SECONDS = 90
_ALLOWED_ORIGIN_RE = re.compile(
    r"^(?:chrome-extension://[a-p]{32}|https?://(?:localhost|127\.0\.0\.1)(?::\d+)?|"
    r"tauri://localhost|https?://tauri\.localhost)$"
)


def _is_allowed_websocket_origin(origin: str | None, client_host: str | None) -> bool:
    if not client_host:
        return False
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = client_host == "localhost"
    if not is_loopback:
        return False
    return not origin or _ALLOWED_ORIGIN_RE.fullmatch(origin) is not None


configure_logging(level=logging.INFO)
log = structlog.get_logger("da.server")

CLIENTS: set[WebSocket] = set()
IN_FLIGHT: dict[WebSocket, dict[str, dict[str, Any]]] = {}
SEND_LOCKS: dict[WebSocket, asyncio.Lock] = {}


async def _send_text(ws: WebSocket, text: str) -> None:
    """Send a raw text frame to a WebSocket connection under a per-socket async lock."""
    lock = SEND_LOCKS.setdefault(ws, asyncio.Lock())
    async with lock:
        try:
            await ws.send_text(text)
        except Exception:  # noqa: BLE001 - client may vanish mid-send
            log.debug("Failed to send to client", exc_info=True)


async def send(ws: WebSocket, payload: dict[str, Any]) -> None:
    """Serialize payload to JSON and send to a specific WebSocket client."""
    await _send_text(ws, orjson.dumps(payload).decode("utf-8"))


async def broadcast(payload: dict[str, Any]) -> None:
    """Broadcast a JSON message payload concurrently to all connected WebSocket clients."""
    if not CLIENTS:
        return
    text = orjson.dumps(payload).decode("utf-8")
    await asyncio.gather(
        *(_send_text(ws, text) for ws in CLIENTS),
        return_exceptions=True,
    )


async def handle_message(
    ws: WebSocket,
    message: dict[str, Any],
    manager: DownloadManager,
    settings_store: SettingsStore,
) -> None:
    """Dispatch and process incoming WebSocket command messages from dashboard or extension clients."""
    msg_type = message.get("type")
    req_id = message.get("reqId")

    if msg_type == "ping":
        await send(ws, {"type": "pong"})

    elif msg_type == "probe":
        if not isinstance(req_id, str) or not req_id:
            await send(ws, {"type": "probe_result", "reqId": req_id, "ok": False,
                            "engine": "none", "error": "Missing request ID"})
            return
        raw_url = message.get("url")
        url = raw_url.strip().strip("'\"") if isinstance(raw_url, str) else ""
        if not url:
            await send(ws, {"type": "probe_result", "reqId": req_id, "ok": False,
                            "engine": "none", "error": "Missing URL"})
            return
        fallback_urls: list[str] = []
        fallback_sources: list[dict[str, str]] = []
        raw_sources = message.get("fallbackSources")
        if isinstance(raw_sources, list):
            for source in raw_sources:
                if not isinstance(source, dict) or not isinstance(source.get("url"), str):
                    continue
                source_url = source["url"].strip()
                if source_url:
                    source_entry: dict[str, str] = {"url": source_url, "label": str(source.get("label") or "")}
                    if source.get("referer"):
                        source_entry["referer"] = str(source["referer"]).strip()
                    if source.get("origin"):
                        source_entry["origin"] = str(source["origin"]).strip()
                    fallback_sources.append(source_entry)
        raw_list = message.get("fallbackUrls")
        if isinstance(raw_list, list):
            fallback_urls.extend(u.strip() for u in raw_list if isinstance(u, str) and u.strip())
        single = message.get("fallbackUrl")
        if isinstance(single, str) and single.strip():
            fallback_urls.insert(0, single.strip())
        media_type = str(message.get("mediaType") or "")

        cancel_event = threading.Event()
        async_cancel_event = asyncio.Event()
        in_flight = IN_FLIGHT.setdefault(ws, {})
        # Cancel any existing in-flight probe for this connection immediately; a client can
        # only usefully consume one probe at a time.
        for existing in list(in_flight.values()):
            existing["cancel_event"].set()
            if existing.get("async_cancel_event") is not None:
                existing["async_cancel_event"].set()
            if existing.get("future") is not None:
                existing["future"].cancel()
        probe_handle: dict[str, Any] = {
            "cancel_event": cancel_event,
            "async_cancel_event": async_cancel_event,
            "future": None,
        }
        in_flight[req_id] = probe_handle

        loop = asyncio.get_running_loop()
        timed_out = False
        was_cancelled = False
        cancel_task = None
        probe_task = None
        try:
            probe_executor = getattr(manager, "_probe_executor", None)
            executor = probe_executor if isinstance(probe_executor, ThreadPoolExecutor) else None
            future = loop.run_in_executor(
                executor,
                lambda: manager.probe(url, fallback_urls, media_type, fallback_sources, cancel_event),
            )
            probe_handle["future"] = future

            probe_task = asyncio.ensure_future(future)
            cancel_task = asyncio.create_task(async_cancel_event.wait())

            done, pending = await asyncio.wait(
                [probe_task, cancel_task],
                timeout=PROBE_TIMEOUT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if cancel_task in done:
                was_cancelled = True
                cancel_event.set()
                probe_task.cancel()
                result = {
                    "ok": False,
                    "engine": "none",
                    "error": "Probe cancelled",
                }
            elif probe_task in done:
                cancel_task.cancel()
                if probe_task.cancelled():
                    was_cancelled = True
                    result = {
                        "ok": False,
                        "engine": "none",
                        "error": "Probe cancelled",
                    }
                else:
                    exc = probe_task.exception()
                    if exc is not None:
                        raise exc
                    result = probe_task.result()
            else:
                timed_out = True
                cancel_event.set()
                cancel_task.cancel()
                probe_task.cancel()
                log.warning("Probe timed out", url=url, timeout_seconds=PROBE_TIMEOUT_SECONDS)
                result = {
                    "ok": False,
                    "engine": "none",
                    "error": "Probe timed out; backend connection is still available",
                }
        except asyncio.CancelledError:
            cancel_event.set()
            async_cancel_event.set()
            was_cancelled = True
            result = {
                "ok": False,
                "engine": "none",
                "error": "Probe cancelled",
            }
        except Exception as exc:  # noqa: BLE001
            cancel_event.set()
            log.exception("Probe failed unexpectedly", url=url, error=str(exc))
            result = {
                "ok": False,
                "engine": "none",
                "error": str(exc).strip() or exc.__class__.__name__,
            }
        finally:
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()
            current = IN_FLIGHT.get(ws, {})
            if current.get(req_id) is probe_handle:
                current.pop(req_id, None)

        # If a cancel request came in after the worker had already finished a
        # successful probe, override the response so the client sees a clean
        # cancellation. Timeouts and real failures keep their original messages.
        if (was_cancelled or cancel_event.is_set()) and not timed_out and result.get("ok"):
            result = {
                "ok": False,
                "engine": "none",
                "error": "Probe cancelled",
            }

        await send(ws, {"type": "probe_result", "reqId": req_id, **result})

    elif msg_type == "probe_cancel":
        target_req_id = message.get("probeReqId") or message.get("reqId")
        in_flight = IN_FLIGHT.get(ws, {})
        handles_to_cancel = []
        if target_req_id and target_req_id in in_flight:
            handles_to_cancel.append(in_flight[target_req_id])
        elif in_flight:
            handles_to_cancel.extend(in_flight.values())

        if handles_to_cancel:
            for handle in handles_to_cancel:
                handle["cancel_event"].set()
                if handle.get("async_cancel_event") is not None:
                    handle["async_cancel_event"].set()
                if handle.get("future") is not None:
                    handle["future"].cancel()
            await send(ws, {
                "type": "probe_cancel_result",
                "reqId": req_id,
                "ok": True,
            })
        else:
            await send(ws, {
                "type": "probe_cancel_result",
                "reqId": req_id,
                "ok": False,
                "error": "Probe not found or already completed",
            })

    elif msg_type == "download":
        raw_url = message.get("url")
        url = raw_url.strip().strip("'\"") if isinstance(raw_url, str) else ""
        if not url:
            await send(ws, {"type": "download_started", "reqId": req_id,
                            "ok": False, "error": "Missing URL"})
            return
        raw_directory = message.get("directory")
        directory = (
            raw_directory.strip()
            if isinstance(raw_directory, str) and raw_directory.strip()
            else settings_store.get()["downloadDir"]
        )
        format_selector = message.get("formatId")
        if not isinstance(format_selector, str) or not format_selector.strip():
            format_selector = None
        filename = message.get("filename")
        if not isinstance(filename, str) or not filename.strip():
            filename = None
        title = str(message.get("title") or "")
        thumbnail = str(message.get("thumbnail") or "")
        engine = str(message.get("engine") or "ytdlp")
        media_type = str(message.get("mediaType") or "")
        referer = str(message.get("referer") or "")
        download_playlist = bool(message.get("downloadPlaylist"))
        selected_entry_urls = message.get("selectedEntryUrls")
        if not isinstance(selected_entry_urls, list) or not selected_entry_urls or not all(isinstance(u, str) and u.strip() for u in selected_entry_urls):
            selected_entry_urls = None
        selected_files = message.get("selectedFiles")
        if not isinstance(selected_files, list) or not selected_files or not all(isinstance(path, str) and path.strip() for path in selected_files):
            selected_files = None
        duplicate_action = str(message.get("duplicateAction") or "")
        if duplicate_action not in ("override", "rename", "skip", "resume"):
            duplicate_action = None

        loop = asyncio.get_running_loop()

        def resolve() -> dict[str, Any]:
            resolve_media_type = "playlist" if download_playlist else media_type
            return manager.resolve_duplicate(
                url,
                directory,
                format_selector,
                filename,
                title,
                resolve_media_type,
                duplicate_action,
            )

        try:
            result = await loop.run_in_executor(None, resolve)
        except Exception as exc:  # noqa: BLE001
            log.exception("Duplicate resolution failed", url=url)
            await send(ws, {"type": "download_started", "reqId": req_id,
                            "ok": False, "error": str(exc) or "Could not start download"})
            return
        if result["status"] == "duplicate":
            duplicate_payload = {
                "type": result["type"],
                "existing": result["existing"],
                "filename": result["filename"],
                "suggestedName": result["suggestedName"],
            }
            await send(ws, {
                "type": "download_started",
                "reqId": req_id,
                "ok": False,
                "error": "duplicate",
                "duplicate": duplicate_payload,
            })
            return
        if result.get("skipped"):
            await send(ws, {"type": "download_started", "reqId": req_id,
                            "ok": True, "skipped": True})
            return

        resolved_filename = result.get("filename") or filename

        def do_start():
            if download_playlist:
                return manager.start_playlist(
                    url,
                    directory,
                    format_selector=format_selector,
                    title=title,
                    thumbnail=thumbnail,
                    selected_entry_urls=selected_entry_urls,
                    referer=referer,
                )
            return manager.start(
                url,
                directory,
                format_selector=format_selector,
                filename=resolved_filename,
                title=title,
                thumbnail=thumbnail,
                engine=engine,
                media_type=media_type,
                selected_files=selected_files,
                referer=referer,
            )

        try:
            job = await loop.run_in_executor(None, do_start)
        except Exception as exc:  # noqa: BLE001
            log.exception("Download start failed", url=url)
            await send(ws, {"type": "download_started", "reqId": req_id,
                            "ok": False, "error": str(exc) or "Could not start download"})
            return
        await send(ws, {"type": "download_started", "reqId": req_id, "ok": True, "jobId": job.id})

    elif msg_type == "reveal":
        job_id = str(message.get("jobId") or "")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, manager.reveal, job_id)
        await send(ws, {"type": "reveal_result", "reqId": req_id, **result})

    elif msg_type == "job_action":
        job_id = str(message.get("jobId") or "")
        action = message.get("action")
        ok = False
        loop = asyncio.get_running_loop()
        if action == "cancel":
            ok = await loop.run_in_executor(None, manager.cancel, job_id)
        elif action == "pause":
            ok = await loop.run_in_executor(None, manager.pause, job_id)
        elif action == "resume":
            ok = await loop.run_in_executor(None, manager.resume, job_id)
        elif action == "remove":
            ok = await loop.run_in_executor(None, manager.remove, job_id)
            if ok:
                await broadcast({"type": "job_removed", "jobId": job_id})
        if not ok:
            await send(ws, {"type": "error", "reqId": req_id,
                            "error": f"Unknown job or action: {action} {job_id}"})
        elif req_id:
            await send(ws, {"type": "job_action_result", "reqId": req_id, "ok": True})

    elif msg_type == "clear_finished":
        loop = asyncio.get_running_loop()
        for job_id in await loop.run_in_executor(None, manager.clear_finished):
            await broadcast({"type": "job_removed", "jobId": job_id})
        if req_id:
            await send(ws, {"type": "clear_finished_result", "reqId": req_id, "ok": True})

    elif msg_type == "settings_get":
        await send(ws, {"type": "settings", "reqId": req_id, "settings": settings_store.get()})

    elif msg_type == "settings_set":
        partial = message.get("settings")
        if not isinstance(partial, dict):
            await send(ws, {"type": "error", "reqId": req_id, "error": "settings must be an object"})
            return
        settings_store.update(partial)  # broadcast happens via subscriber
        if req_id:
            await send(ws, {"type": "settings_result", "reqId": req_id, "ok": True})

    elif msg_type == "browsers_list":
        loop = asyncio.get_running_loop()
        browsers = await loop.run_in_executor(None, list_browsers)
        await send(ws, {
            "type": "browsers_list_result",
            "reqId": req_id,
            "ok": True,
            "browsers": browsers,
            "extensionPath": str(extension_dir()),
        })

    elif msg_type == "install_extension":
        browser_id = str(message.get("browserId") or "")
        log.info("install_extension requested", browser_id=browser_id)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, install_extension, browser_id)
        await send(ws, {"type": "install_extension_result", "reqId": req_id, **result})

    elif msg_type == "log":
        level = str(message.get("level") or "info").lower()
        if level not in {"debug", "info", "warning", "error", "critical"}:
            level = "info"
        msg = str(message.get("message") or "")
        context = message.get("context") or {}
        client = str(message.get("client") or "extension")
        if not isinstance(context, dict):
            context = {"raw": context}
        getattr(log, level)(msg, client=client, context=context)

    else:
        log.warning("Unknown websocket message type", msg_type=msg_type)
        await send(ws, {"type": "error", "reqId": req_id,
                        "error": f"Unknown message type: {msg_type}"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    db = Database()
    await db.connect()
    settings_store = SettingsStore(db, loop)
    await settings_store.load()
    manager = DownloadManager(settings_store, db, loop)
    manager.add_listener(lambda job: broadcast({"type": "job_update", "job": job}))
    await manager.restore()
    settings_broadcast_tasks: set[asyncio.Task[None]] = set()

    def _on_settings(snapshot: dict[str, Any]) -> None:
        task = asyncio.create_task(broadcast({"type": "settings", "settings": snapshot}))
        settings_broadcast_tasks.add(task)

        def finish(done: asyncio.Task[None]) -> None:
            settings_broadcast_tasks.discard(done)
            if not done.cancelled() and (error := done.exception()) is not None:
                log.error("Settings broadcast failed", error=str(error))

        task.add_done_callback(finish)

    settings_store.subscribe(_on_settings)

    app.state.settings_store = settings_store
    app.state.manager = manager
    log.info("DownloadAnything backend listening", host=HOST, port=PORT)
    try:
        yield
    finally:
        log.info("Shutting down backend")
        await manager.shutdown()
        if settings_broadcast_tasks:
            await asyncio.gather(*settings_broadcast_tasks, return_exceptions=True)
        await settings_store.shutdown()
        await db.close()
        log.info("Backend shutdown complete")


app = FastAPI(lifespan=lifespan)


@app.websocket("/")
async def websocket_endpoint(websocket: WebSocket):
    origin = websocket.headers.get("origin")
    peer = websocket.client
    peer_host = peer[0] if peer else None
    if not _is_allowed_websocket_origin(origin, peer_host):
        log.warning("Rejected websocket origin", origin=origin, peer=peer_host)
        await websocket.close(code=1008)
        return

    await websocket.accept()
    CLIENTS.add(websocket)
    peer_str = f"{peer[0]}:{peer[1]}" if peer else "unknown"
    probe_tasks: set[asyncio.Task[None]] = set()

    def finish_probe(done: asyncio.Task[None]) -> None:
        probe_tasks.discard(done)
        if not done.cancelled() and (error := done.exception()) is not None:
            log.error("Probe handler failed", peer=peer_str, error=str(error))

    log.info("Client connected", peer=peer_str, total_clients=len(CLIENTS))
    try:
        manager = app.state.manager
        settings_store = app.state.settings_store
        while True:
            raw = await websocket.receive_text()
            try:
                message = orjson.loads(raw)
            except orjson.JSONDecodeError:
                await send(websocket, {"type": "error", "error": "Invalid JSON"})
                continue
            if not isinstance(message, dict):
                await send(websocket, {"type": "error", "error": "Message must be a JSON object"})
                continue
            message = cast(dict[str, Any], message)

            if message.get("type") == "hello":
                loop = asyncio.get_running_loop()
                jobs = await loop.run_in_executor(None, manager.list_jobs)
                await send(websocket, {
                    "type": "hello",
                    "ok": True,
                    "client": message.get("client"),
                    "settings": settings_store.get(),
                    "jobs": jobs,
                })
                continue

            if message.get("type") == "probe":
                # Probes can be long-running; run them as a task so the receive
                # loop stays free to handle a probe_cancel.
                task = asyncio.create_task(handle_message(websocket, message, manager, settings_store))
                probe_tasks.add(task)
                task.add_done_callback(finish_probe)
                continue

            try:
                await handle_message(websocket, message, manager, settings_store)
            except Exception:  # noqa: BLE001
                log.exception("Handler failed", msg_type=message.get("type"))
                await send(websocket, {"type": "error", "reqId": message.get("reqId"),
                                "error": "Request could not be processed"})
    except WebSocketDisconnect:
        log.info("Client disconnected", peer=peer_str)
    except (WebSocketException, RuntimeError) as exc:
        log.info("WebSocket closed", peer=peer_str, error=str(exc))
    finally:
        CLIENTS.discard(websocket)
        for handle in IN_FLIGHT.pop(websocket, {}).values():
            handle["cancel_event"].set()
            if handle.get("async_cancel_event") is not None:
                handle["async_cancel_event"].set()
            if handle.get("future") is not None:
                handle["future"].cancel()
        for task in probe_tasks:
            task.cancel()
        if probe_tasks:
            await asyncio.gather(*probe_tasks, return_exceptions=True)
        SEND_LOCKS.pop(websocket, None)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    module = "backend.server" if __package__ else "server"
    try:
        if RELOAD:
            uvicorn.run(f"{module}:app", host=HOST, port=PORT, reload=True)
        else:
            uvicorn.run(app, host=HOST, port=PORT)

    except KeyboardInterrupt:
        pass
