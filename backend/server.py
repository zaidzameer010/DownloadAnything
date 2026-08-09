"""DownloadAnything FastAPI websocket backend.

Single websocket server shared by the Chrome extension and the React
dashboard. JSON messages, one per frame:

Client -> server:
    {"type": "hello", "client": "extension" | "dashboard"}
    {"type": "probe", "reqId": str, "url": str, "fallbackUrl"?: str,
     "fallbackUrls"?: [str], "mediaType"?: str}
    {"type": "download", "reqId": str, "url": str, "formatId"?: str,
     "directory"?: str, "filename"?: str, "title"?: str, "thumbnail"?: str,
     "engine"?: str, "mediaType"?: str,
     "downloadPlaylist"?: bool, "selectedEntryUrls"?: [str],
     "duplicateAction"?: "override" | "rename" | "skip"}
    {"type": "job_action", "reqId"?: str, "jobId": str,
     "action": "cancel" | "pause" | "resume" | "remove"}
    {"type": "reveal", "reqId"?: str, "jobId": str}
    {"type": "clear_finished"}
    {"type": "settings_get"}
    {"type": "settings_set", "settings": {...partial}}

Server -> client:
    {"type": "hello", "ok": true, "version", "settings", "jobs"}
    {"type": "probe_result", "reqId", "ok", "engine", "result"? | "error"?}
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
import logging
import orjson
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import structlog
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, WebSocketException
import uvicorn

try:
    from .db import Database
    from .engine import DownloadManager
    from .log import configure_logging
    from .settings import SettingsStore
except ImportError:
    from db import Database
    from engine import DownloadManager
    from log import configure_logging
    from settings import SettingsStore

HOST = os.environ.get("DA_HOST", "127.0.0.1")
PORT = int(os.environ.get("DA_PORT", "8765"))
IS_BUNDLED = "__compiled__" in globals() or bool(getattr(sys, "frozen", False))
RELOAD = os.environ.get("DA_RELOAD", "false" if IS_BUNDLED else "true").lower() in {"1", "true", "yes"}
VERSION = "1.0.0"
PROBE_TIMEOUT_SECONDS = 90

configure_logging(level=logging.INFO)
log = structlog.get_logger("da.server")

CLIENTS: list[WebSocket] = []


async def send(ws: WebSocket, payload: dict[str, Any]) -> None:
    try:
        await ws.send_text(orjson.dumps(payload).decode("utf-8"))
    except Exception:  # noqa: BLE001 - client may vanish mid-send
        log.debug("Failed to send to client", exc_info=True)


async def broadcast(payload: dict[str, Any]) -> None:
    if not CLIENTS:
        return
    message = orjson.dumps(payload).decode("utf-8")
    await asyncio.gather(
        *(ws.send_text(message) for ws in CLIENTS),
        return_exceptions=True,
    )


async def handle_message(
    ws: WebSocket,
    message: dict[str, Any],
    manager: DownloadManager,
    settings_store: SettingsStore,
) -> None:
    msg_type = message.get("type")
    req_id = message.get("reqId")

    if msg_type == "ping":
        await send(ws, {"type": "pong"})

    elif msg_type == "probe":
        log.debug("Handling probe", req_id=req_id)
        raw_url = message.get("url")
        url = raw_url.strip() if isinstance(raw_url, str) else ""
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
                    fallback_sources.append({"url": source_url, "label": str(source.get("label") or "")})
        raw_list = message.get("fallbackUrls")
        if isinstance(raw_list, list):
            fallback_urls.extend(u.strip() for u in raw_list if isinstance(u, str) and u.strip())
        single = message.get("fallbackUrl")
        if isinstance(single, str) and single.strip():
            fallback_urls.insert(0, single.strip())
        loop = asyncio.get_running_loop()
        media_type = str(message.get("mediaType") or "")
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    manager.probe,
                    url,
                    fallback_urls,
                    media_type,
                    fallback_sources,
                ),
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            log.warning("Probe timed out", url=url, timeout_seconds=PROBE_TIMEOUT_SECONDS)
            result = {
                "ok": False,
                "engine": "none",
                "error": "Probe timed out; backend connection is still available",
            }
        except Exception as exc:  # noqa: BLE001
            log.exception("Probe failed unexpectedly", url=url, error=str(exc))
            result = {
                "ok": False,
                "engine": "none",
                "error": str(exc).strip() or exc.__class__.__name__,
            }
        await send(ws, {"type": "probe_result", "reqId": req_id, **result})

    elif msg_type == "download":
        log.debug("Handling download", req_id=req_id)
        raw_url = message.get("url")
        url = raw_url.strip() if isinstance(raw_url, str) else ""
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
        download_playlist = bool(message.get("downloadPlaylist"))
        selected_entry_urls = message.get("selectedEntryUrls")
        if not isinstance(selected_entry_urls, list) or not selected_entry_urls or not all(isinstance(u, str) and u.strip() for u in selected_entry_urls):
            selected_entry_urls = None
        selected_files = message.get("selectedFiles")
        if not isinstance(selected_files, list) or not selected_files or not all(isinstance(path, str) and path.strip() for path in selected_files):
            selected_files = None
        duplicate_action = str(message.get("duplicateAction") or "")
        if duplicate_action not in ("override", "rename", "skip"):
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

        result = await loop.run_in_executor(None, resolve)
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
            )

        job = await loop.run_in_executor(None, do_start)
        await send(ws, {"type": "download_started", "reqId": req_id, "ok": True, "jobId": job.id})

    elif msg_type == "reveal":
        log.debug("Handling reveal", req_id=req_id, job_id=message.get("jobId"))
        job_id = str(message.get("jobId") or "")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, manager.reveal, job_id)
        await send(ws, {"type": "reveal_result", "reqId": req_id, **result})

    elif msg_type == "job_action":
        log.debug("Handling job_action", req_id=req_id)
        job_id = str(message.get("jobId") or "")
        action = message.get("action")
        ok = False
        if action == "cancel":
            ok = manager.cancel(job_id)
        elif action == "pause":
            ok = manager.pause(job_id)
        elif action == "resume":
            ok = manager.resume(job_id)
        elif action == "remove":
            ok = manager.remove(job_id)
            if ok:
                await broadcast({"type": "job_removed", "jobId": job_id})
        if not ok:
            await send(ws, {"type": "error", "reqId": req_id,
                            "error": f"Unknown job or action: {action} {job_id}"})
        elif req_id:
            await send(ws, {"type": "job_action_result", "reqId": req_id, "ok": True})

    elif msg_type == "clear_finished":
        for job_id in manager.clear_finished():
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
        log.info("settings_set", keys=list(partial.keys()))
        settings_store.update(partial)  # broadcast happens via subscriber
        if req_id:
            await send(ws, {"type": "settings_result", "reqId": req_id, "ok": True})

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

    def _on_settings(snapshot: dict[str, Any]) -> None:
        asyncio.ensure_future(broadcast({"type": "settings", "settings": snapshot}))

    settings_store.subscribe(_on_settings)

    app.state.settings_store = settings_store
    app.state.manager = manager
    log.info("DownloadAnything backend listening", host=HOST, port=PORT)
    try:
        yield
    finally:
        log.info("Shutting down backend")
        await manager.shutdown()
        await db.close()
        log.info("Backend shutdown complete")


app = FastAPI(lifespan=lifespan)


@app.websocket("/")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    CLIENTS.append(websocket)
    peer = websocket.client
    peer_str = f"{peer[0]}:{peer[1]}" if peer else "unknown"
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
                await send(websocket, {
                    "type": "hello",
                    "ok": True,
                    "version": VERSION,
                    "client": message.get("client"),
                    "settings": settings_store.get(),
                    "jobs": manager.list_jobs(),
                })
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
        if websocket in CLIENTS:
            CLIENTS.remove(websocket)


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
