"""
main.py — Media Acquisition Engine Entrypoint
=============================================
FastAPI server entrypoint. Wraps modularized engine package.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

import orjson
import yt_dlp
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, ORJSONResponse
from fastapi.staticfiles import StaticFiles

from engine.config import (
    BASE_DIR,
    STATIC_DIR,
    ensure_system_path,
    load_settings,
)
from engine.manager import Client, DownloadManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("dma-engine")

try:
    _YT_DLP_VERSION: str = yt_dlp.version.__version__
except Exception:  # noqa: BLE001
    _YT_DLP_VERSION = "unknown"

# ──────────────────────────────────────────────
#  FastAPI application
# ──────────────────────────────────────────────

manager = DownloadManager(load_settings())


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_system_path()
    manager.load_tasks()
    dispatcher = asyncio.create_task(manager.run())
    app.state.dispatcher = dispatcher
    try:
        yield
    finally:
        await manager.shutdown()
        with suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(dispatcher, timeout=2.0)


app = FastAPI(
    title="Media Acquisition Engine",
    version="3.0.0",
    lifespan=lifespan,
    default_response_class=ORJSONResponse,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:8000",
        "tauri://localhost",
        "https://tauri.localhost",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def disable_static_cache(request: Request, call_next: Callable[[Request], Awaitable[Any]]):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> Any:
    index_html = BASE_DIR / "dist" / "index.html"
    if index_html.exists():
        return FileResponse(str(index_html))
    return JSONResponse(
        {
            "status": "ok",
            "service": "Media Acquisition Engine",
            "mode": "sidecar" if getattr(sys, "frozen", False) else "api-only",
            "message": (
                "Frontend UI files (index.html) not found. "
                "Use the desktop app or run 'fastapi run' from the project root."
            ),
        }
    )


@app.websocket("/ws/progress")
async def websocket_progress(websocket: WebSocket) -> None:
    await websocket.accept()
    client = Client(websocket)
    manager.add_client(client)
    try:
        await client.send_text(manager.payload())
        while True:
            raw = await websocket.receive_text()
            try:
                msg = orjson.loads(raw)
            except orjson.JSONDecodeError as exc:
                await client.send_json(
                    {
                        "type": "response",
                        "action": None,
                        "request_id": None,
                        "ok": False,
                        "error": f"Invalid JSON payload: {exc}",
                    }
                )
                continue
            action = msg.get("action")
            if not action:
                continue
            task = asyncio.create_task(
                manager.handle_action(
                    client, action, msg.get("request_id"), msg.get("payload") or {}
                )
            )
            client.pending.add(task)
            task.add_done_callback(client.pending.discard)
    except WebSocketDisconnect:
        pass
    finally:
        manager.remove_client(client)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
