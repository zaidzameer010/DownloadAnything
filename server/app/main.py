import os
import sys

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

try:
    from app.config import get_app_version, settings
    from app.utils.logger import get_logger, setup_logging
except ModuleNotFoundError as error:
    if error.name != "app":
        raise
    parent_dir = str(Path(__file__).resolve().parent.parent)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    from app.config import get_app_version, settings
    from app.utils.logger import get_logger, setup_logging

from app.api.browse import DirectoryPicker
from app.engine.jobs import jobs_registry
from app.engine.probe import ProbeOrchestrator
from app.engine.torrent import TorrentDownloader, TorrentProber
from app.repositories.categories_repository import CategoriesRepository
from app.repositories.settings_repository import SettingsRepository
from app.services.category_service import CategoryService
from app.services.download_service import DownloadService
from app.services.file_service import FileService
from app.services.job_service import JobService
from app.services.probe_service import ProbeService
from app.services.settings_service import SettingsService
from app.services.torrent_service import TorrentService
from app.ws.dispatcher import MessageDispatcher
from app.ws.manager import ConnectionManager
from app.ws.router import create_ws_router

logger = get_logger("app.main")

if sys.platform == "darwin":
    homebrew_bin = "/opt/homebrew/bin"
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if homebrew_bin not in path_entries:
        os.environ["PATH"] = os.pathsep.join([homebrew_bin, *path_entries])


# Wire dependencies once at startup.  `jobs_registry` is the existing singleton
# used by the engine modules until they are converted to DI.
settings_repository = SettingsRepository()
categories_repository = CategoriesRepository()
directory_picker = DirectoryPicker()
connection_manager = ConnectionManager()

settings_service = SettingsService(settings_repository)
category_service = CategoryService(categories_repository)
job_service = JobService(jobs_registry)
file_service = FileService(
    categories_repository=categories_repository,
    directory_picker=directory_picker,
)

probe_orchestrator = ProbeOrchestrator()
torrent_prober = TorrentProber()
probe_service = ProbeService(
    connection_manager=connection_manager,
    probe_orchestrator=probe_orchestrator,
    torrent_prober=torrent_prober,
    settings_repository=settings_repository,
)

download_service = DownloadService(
    connection_manager=connection_manager,
    job_repository=jobs_registry,
    probe_engine=probe_orchestrator,
    settings_repository=settings_repository,
    file_service=file_service,
)

torrent_service = TorrentService(
    connection_manager=connection_manager,
    job_repository=jobs_registry,
    file_service=file_service,
    settings_repository=settings_repository,
    torrent_downloader=TorrentDownloader(),
)

dispatcher = MessageDispatcher(
    connection_manager=connection_manager,
    settings_service=settings_service,
    category_service=category_service,
    job_service=job_service,
    file_service=file_service,
    probe_service=probe_service,
    download_service=download_service,
    torrent_service=torrent_service,
)

ws_router = create_ws_router(dispatcher, connection_manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("Starting Downloader Backend Service...")
    logger.info(f"Configurations Loaded: Host={settings.HOST}, Port={settings.PORT}")
    yield
    logger.info("Shutting down Downloader Backend Service...")
    for _tab_id, ws in list(connection_manager.active_connections.items()):
        try:
            await ws.close(code=1001, reason="Server shutting down")
        except Exception:
            pass
    logger.info("Downloader Backend Service shutdown complete.")


app = FastAPI(
    title="DownloadAnything",
    description="WebSocket-only service broker for downloading web media using yt-dlp",
    version=get_app_version(),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(tauri://localhost|http://localhost:\d+|http://127\.0\.0\.1:\d+|chrome-extension://.*|moz-extension://.*)$",
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(ws_router)

if __name__ == "__main__":
    is_frozen = getattr(sys, "frozen", False)
    uvicorn.run(
        "main:app" if not is_frozen else app,
        host=settings.HOST,
        port=settings.PORT,
        log_level=settings.LOG_LEVEL.lower(),
        log_config=None,
        reload=not is_frozen,
    )
