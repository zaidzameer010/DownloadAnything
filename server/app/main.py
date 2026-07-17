import os
import sys

# Apply global SSL monkeypatch to ignore unexpected EOF protocol violations
try:
    import ssl
    if hasattr(ssl, "OP_IGNORE_UNEXPECTED_EOF"):
        orig_create_default_context = ssl.create_default_context
        def patched_create_default_context(*args, **kwargs):
            ctx = orig_create_default_context(*args, **kwargs)
            ctx.options |= ssl.OP_IGNORE_UNEXPECTED_EOF
            return ctx
        ssl.create_default_context = patched_create_default_context
        
        if hasattr(ssl, "_create_default_https_context"):
            orig_create_default_https_context = ssl._create_default_https_context
            def patched_create_default_https_context(*args, **kwargs):
                ctx = orig_create_default_https_context(*args, **kwargs)
                ctx.options |= ssl.OP_IGNORE_UNEXPECTED_EOF
                return ctx
            ssl._create_default_https_context = patched_create_default_https_context
except Exception:
    pass

# Disable yt-dlp's security block on unusual extensions (e.g. .php) when run as a library
try:
    from yt_dlp.utils._utils import _UnsafeExtensionError
    _UnsafeExtensionError._enabled = False
except Exception:
    pass

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

try:
    from app.config import get_app_version, settings
    from app.utils.logger import logger
    from app.ws.manager import ws_manager
    from app.ws.router import router as ws_router
except ModuleNotFoundError as error:
    if error.name != "app":
        raise
    # Add the parent directory of 'app' to the Python path to support running from any directory
    parent_dir = str(Path(__file__).resolve().parent.parent)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    from app.config import get_app_version, settings
    from app.utils.logger import logger
    from app.ws.manager import ws_manager
    from app.ws.router import router as ws_router

# Expose homebrew binaries to the running process PATH
if sys.platform == "darwin":
    homebrew_bin = "/opt/homebrew/bin"
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if homebrew_bin not in path_entries:
        os.environ["PATH"] = os.pathsep.join([homebrew_bin, *path_entries])


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Downloader Backend Service...")
    logger.info(f"Configurations Loaded: Host={settings.HOST}, Port={settings.PORT}")
    yield
    logger.info("Shutting down Downloader Backend Service...")
    # Clean up active websocket connections
    for _tab_id, ws in list(ws_manager.active_connections.items()):
        try:
            await ws.close(code=1001, reason="Server shutting down")
        except Exception:
            pass
    logger.info("Downloader Backend Service shutdown complete.")


app = FastAPI(
    title="yt-dlp Powered Browser Media Downloader",
    description="WebSocket-only service broker for downloading web media using yt-dlp",
    version=get_app_version(),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
        reload=not is_frozen,
    )
