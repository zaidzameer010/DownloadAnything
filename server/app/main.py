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
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

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
    description="FastAPI service broker for downloading web media using yt-dlp",
    version=get_app_version(),
    lifespan=lifespan
)

# Configure CORS Middleware
# Allows localhost web applications and chrome/firefox extension contexts
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000", 
        "http://localhost:5173", 
        "http://127.0.0.1:3000", 
        "http://127.0.0.1:5173"
    ],
    allow_origin_regex=r"^(chrome|moz)-extension://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Wire Routes
app.include_router(ws_router)

# Serve Frontend static files if the dist folder is built
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

dist_path = Path(__file__).resolve().parent.parent.parent / "dist"

if dist_path.exists() and dist_path.is_dir():
    logger.info(f"Serving static frontend build from {dist_path}")
    app.mount("/assets", StaticFiles(directory=dist_path / "assets"), name="assets")
    
    @app.get("/{catchall:path}")
    async def serve_frontend(catchall: str):
        # Exclude API endpoints to let FastAPI route them natively
        if catchall.startswith("api/") or catchall.startswith("ws"):
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(dist_path / "index.html")
else:
    logger.warning(f"Static frontend build path {dist_path} does not exist. Frontend dashboard will not be served.")

if __name__ == "__main__":
    is_frozen = getattr(sys, "frozen", False)
    uvicorn.run(
        "main:app" if not is_frozen else app, 
        host=settings.HOST, 
        port=settings.PORT, 
        log_level=settings.LOG_LEVEL.lower(),
        reload=not is_frozen
    )
