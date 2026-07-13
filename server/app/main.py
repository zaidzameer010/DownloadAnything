import sys
import os
from pathlib import Path

# Add the parent directory of 'app' to the Python path to support running from any directory
parent_dir = str(Path(__file__).resolve().parent.parent)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Expose homebrew binaries to the running process PATH
os.environ["PATH"] = "/opt/homebrew/bin:" + os.environ.get("PATH", "")

from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI

from app.config import settings, get_app_version
from app.utils.logger import logger
from app.ws.manager import ws_manager
from app.ws.router import router as ws_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Downloader Backend Service...")
    logger.info(f"Configurations Loaded: Host={settings.HOST}, Port={settings.PORT}")
    yield
    logger.info("Shutting down Downloader Backend Service...")
    # Clean up active websocket connections
    for tab_id, ws in list(ws_manager.active_connections.items()):
        try:
            await ws.close(code=1001, reason="Server shutting down")
        except Exception:
            pass
    logger.info("Downloader Backend Service shutdown complete.")

app = FastAPI(
    title="yt-dlp Powered Browser Media Downloader",
    description="WebSocket-only service broker for downloading web media using yt-dlp",
    version=get_app_version(),
    lifespan=lifespan
)

# Wire WebSocket route only
app.include_router(ws_router)

if __name__ == "__main__":
    is_frozen = getattr(sys, "frozen", False)
    uvicorn.run(
        "main:app" if not is_frozen else app, 
        host=settings.HOST, 
        port=settings.PORT, 
        log_level=settings.LOG_LEVEL.lower(),
        reload=not is_frozen
    )
