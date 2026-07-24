"""WebSocket message dispatcher delegating to application services."""

import asyncio
import shutil
import urllib.parse
import uuid
from typing import Any, Optional, Union, cast

import yt_dlp
from pydantic import TypeAdapter

from app.config import get_app_version, settings as app_settings
from app.engine.file_types import classify_mime
from app.engine.media_classify import classify_download_item
from app.engine.title_extractor import _is_unusable_stem, resolve_filename
from app.engine.torrent import is_magnet_url
from app.schemas.messages import (
    ClientBrowseDirectoryMessage,
    ClientCancelDownloadMessage,
    ClientCancelProbeMessage,
    ClientCheckFileExistsMessage,
    ClientChooseMessage,
    ClientDeleteFileMessage,
    ClientDownloadUrlMessage,
    ClientGetCategoriesMessage,
    ClientGetJobsMessage,
    ClientGetSettingsMessage,
    ClientHelloMessage,
    ClientMessage,
    ClientPauseMessage,
    ClientPingMessage,
    ClientProbeMessage,
    ClientRefreshUrlMessage,
    ClientRemoveJobMessage,
    ClientRevealFileMessage,
    ClientResumeMessage,
    ClientSaveCategoriesMessage,
    ClientSaveSettingsMessage,
)
from app.services.category_service import CategoryService
from app.services.download_service import DownloadService
from app.services.file_service import FileService
from app.services.interfaces import IConnectionManager
from app.services.job_service import JobService
from app.services.probe_service import ProbeService
from app.services.settings_service import SettingsService
from app.services.torrent_service import TorrentService
from app.utils.logger import bind_contextvars, clear_contextvars, get_logger, redact_url

logger = get_logger(__name__)


def _is_unusable_title(title: Optional[str]) -> bool:
    return not title or _is_unusable_stem(title)


class MessageDispatcher:
    """Maps validated client messages to async handler methods."""

    def __init__(
        self,
        connection_manager: IConnectionManager,
        settings_service: SettingsService,
        category_service: CategoryService,
        job_service: JobService,
        file_service: FileService,
        probe_service: ProbeService,
        download_service: DownloadService,
        torrent_service: TorrentService,
    ) -> None:
        self._connection_manager = connection_manager
        self._settings_service = settings_service
        self._category_service = category_service
        self._job_service = job_service
        self._file_service = file_service
        self._probe_service = probe_service
        self._download_service = download_service
        self._torrent_service = torrent_service

        self._message_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)

    async def handle_message(self, tab_id: int, data: Any) -> None:
        try:
            msg = self._message_adapter.validate_python(data)
        except Exception as e:
            logger.error(f"Invalid message format received: {e}")
            await self._connection_manager.send_message(
                tab_id,
                {"type": "error", "error": f"Invalid message format: {e}"},
            )
            return

        if isinstance(msg, ClientHelloMessage):
            await self.handle_hello(tab_id, msg)
            return

        handler_name = f"handle_{msg.type}"
        handler = getattr(self, handler_name, None)
        if handler is None:
            logger.warning(f"No handler for message type: {msg.type}")
            return

        try:
            bind_contextvars(tab_id=tab_id)
            await handler(tab_id, msg)
        except Exception as e:
            logger.error(f"Error handling message {msg.type}: {e}", exc_info=True)
        finally:
            clear_contextvars()

    async def handle_disconnect(self, tab_id: int) -> None:
        await self._probe_service.cancel_probes_for_tab(tab_id)

    async def handle_hello(self, tab_id: int, msg: ClientHelloMessage) -> None:
        logger.debug(
            f"Handshake success for tab {tab_id}. Client version: {msg.clientVersion}"
        )
        try:
            from yt_dlp.version import __version__ as yt_dlp_ver

            yt_dlp_version = yt_dlp_ver
        except ImportError:
            yt_dlp_version = getattr(yt_dlp, "__version__", "unknown") or "unknown"

        ffmpeg_available = shutil.which("ffmpeg") is not None

        await self._connection_manager.send_message(
            tab_id,
            {
                "type": "hello",
                "serverVersion": get_app_version(),
                "ytDlpVersion": yt_dlp_version,
                "ffmpegAvailable": ffmpeg_available,
                "heartbeatIntervalMs": 20000,
            },
        )

    async def handle_ping(self, tab_id: int, msg: ClientPingMessage) -> None:
        await self._connection_manager.send_message(
            tab_id, {"type": "pong", "ts": msg.ts}
        )

    async def handle_probe(self, tab_id: int, msg: ClientProbeMessage) -> None:
        job_id = msg.jobId or f"job_{uuid.uuid4().hex[:8]}"
        logger.info(f"Probe requested for job {job_id}: {redact_url(msg.url)}")

        duplicate = await self._job_service.find_duplicate(msg.url)
        if duplicate:
            logger.info(
                f"Duplicate probe requested for {msg.url}. JobId: {duplicate.job_id}"
            )
            await self._connection_manager.send_message(
                tab_id,
                {
                    "type": "duplicate_job_alert",
                    "jobId": duplicate.job_id,
                    "url": duplicate.url,
                    "title": duplicate.title or "Unknown Title",
                    "status": duplicate.status,
                },
            )
            return

        await self._probe_service.start_probe(
            tab_id=tab_id,
            job_id=job_id,
            url=msg.url,
            page_title=msg.title,
            referer=msg.referer,
        )

    async def handle_cancel_probe(
        self, tab_id: int, msg: ClientCancelProbeMessage
    ) -> None:
        _ = tab_id
        logger.info(f"Cancel probe requested for jobId: {msg.jobId}")
        await self._probe_service.cancel_probe(msg.jobId)

    async def _resolve_filename_in_thread(
        self, **kwargs: Any
    ) -> Any:
        return await asyncio.to_thread(resolve_filename, **kwargs)

    async def handle_choose(self, tab_id: int, msg: ClientChooseMessage) -> None:
        _ = tab_id
        job_id = msg.jobId
        await self._probe_service.prune_pending_probes()

        metadata_raw = await self._probe_service.pop_pending_probe(job_id)
        format_ids: list[Any] = []

        if metadata_raw is None:
            if msg.url:
                resolved = await self._resolve_filename_in_thread(
                    url=msg.url,
                    filename=msg.filename,
                    mime=msg.mime,
                    referer=msg.referer,
                    page_title=msg.title,
                    timeout=3.0,
                )
                intercept_type = classify_download_item(msg.url, msg.mime)
                file_type = classify_mime(msg.mime) if msg.mime else None
                metadata_raw = {
                    "url": msg.url,
                    "title": resolved.title,
                    "duration": None,
                    "thumbnail": None,
                    "uploader": None,
                    "formats": [],
                    "mediaType": intercept_type or "file",
                    "fileType": file_type,
                    "mime": msg.mime,
                    "filename": resolved.filename,
                }
            else:
                logger.warning(
                    f"Choose requested for untracked/missing probed job {job_id}"
                )
                return
        else:
            format_ids = cast(list[Any], metadata_raw.get("formatIds", []) or [])
            available_format_ids = {str(format_id) for format_id in format_ids}
            if (
                msg.formatId != "best"
                and available_format_ids
                and msg.formatId not in available_format_ids
            ):
                await self._probe_service.restore_pending_probe(job_id, metadata_raw)
                await self._connection_manager.send_message(
                    tab_id,
                    {
                        "type": "probe_failed",
                        "jobId": job_id,
                        "error": "The selected format is no longer available. Please probe again.",
                        "errorCategory": "stale_probe",
                        "suggestion": "reprobe_required",
                    },
                )
                return

        metadata_url = cast(str, metadata_raw["url"])
        metadata_title = cast(Optional[str], metadata_raw.get("title"))
        metadata_duration = cast(Optional[float], metadata_raw.get("duration"))
        metadata_thumbnail = cast(Optional[str], metadata_raw.get("thumbnail"))
        metadata_uploader = cast(Optional[str], metadata_raw.get("uploader"))
        metadata_formats = cast(list[Any], metadata_raw.get("formats") or [])
        metadata_media_type = cast(Optional[str], metadata_raw.get("mediaType"))
        metadata_torrent = cast(
            Optional[dict[str, object]], metadata_raw.get("torrent")
        )
        metadata_mime = cast(Optional[str], metadata_raw.get("mime")) or msg.mime
        metadata_filename = msg.filename or cast(
            Optional[str], metadata_raw.get("filename")
        )

        probe_referer = cast(Optional[str], metadata_raw.get("probeReferer"))
        effective_referer = msg.referer or probe_referer

        needs_title_resolve = (
            not metadata_filename
            or not metadata_title
            or _is_unusable_stem(metadata_title)
        )
        if needs_title_resolve and (metadata_title or msg.title or metadata_filename):
            page_title_hint = metadata_title
            if _is_unusable_title(page_title_hint):
                page_title_hint = msg.title
            if _is_unusable_title(page_title_hint):
                page_title_hint = None

            preferred_ext: Optional[str] = None
            for fmt in metadata_formats or []:
                if isinstance(fmt, dict):
                    fmt_id = fmt.get("formatId")
                    fmt_ext = fmt.get("ext")
                else:
                    fmt_id = getattr(fmt, "formatId", None)
                    fmt_ext = getattr(fmt, "ext", None)
                if str(fmt_id) == str(msg.formatId):
                    preferred_ext = fmt_ext
                    break

            resolved = await self._resolve_filename_in_thread(
                url=metadata_url,
                filename=metadata_filename,
                mime=metadata_mime,
                referer=effective_referer,
                page_title=page_title_hint,
                preferred_ext=preferred_ext,
                timeout=3.0,
                allow_network=False,
            )
            metadata_title = resolved.title
            metadata_filename = resolved.filename

        probe_format_ids = [str(format_id) for format_id in format_ids]
        probe_timestamp = cast(Optional[float], metadata_raw.get("probeTimestamp"))

        chosen_output_dir = msg.outputDir or app_settings.DEFAULT_OUTPUT_DIR
        if not await self._file_service.is_path_allowed(chosen_output_dir):
            logger.warning(
                f"Rejected choose request for disallowed output directory: {chosen_output_dir}"
            )
            await self._connection_manager.send_message(
                tab_id,
                {
                    "type": "download_failed",
                    "jobId": job_id,
                    "error": "Selected output directory is not allowed.",
                    "stage": "queued",
                },
            )
            return

        resolved_output_dir = await self._file_service.resolve_output_dir(
            chosen_output_dir
        )

        await self._job_service.create_job(job_id, metadata_url, status="queued")
        await self._job_service.update_job(
            job_id,
            title=metadata_title,
            duration=metadata_duration,
            thumbnail=metadata_thumbnail,
            uploader=metadata_uploader,
            formats=metadata_formats,
            format_id=msg.formatId,
            output_dir=resolved_output_dir,
            total_bytes=msg.fileSize or 0.0,
            referer=effective_referer,
            page_url=msg.pageUrl or effective_referer,
            probe_format_ids=probe_format_ids or None,
            probe_timestamp=probe_timestamp,
            probe_referer=probe_referer,
            media_type=metadata_media_type,
            mime=metadata_mime,
            filename=metadata_filename,
            torrent_files=(metadata_torrent or {}).get("files"),
            torrent_selected_file_indices=msg.torrentSelectedFileIndices,
            torrent_piece_length=(metadata_torrent or {}).get("pieceLength"),
            torrent_piece_count=(metadata_torrent or {}).get("pieceCount"),
            torrent_info_hash=(metadata_torrent or {}).get("infoHash"),
        )

        conflict_res = getattr(msg, "conflictResolution", "replace") or "replace"

        if metadata_media_type == "torrent" or is_magnet_url(metadata_url):
            await self._torrent_service.start_download(
                tab_id=tab_id,
                job_id=job_id,
                url=metadata_url,
                output_dir=resolved_output_dir,
                selected_files=msg.torrentSelectedFileIndices,
            )
        else:
            await self._download_service.start_download(
                tab_id=tab_id,
                job_id=job_id,
                url=metadata_url,
                format_id=msg.formatId,
                output_dir=resolved_output_dir,
                conflict_resolution=conflict_res,
                referer=effective_referer,
                media_type=metadata_media_type,
            )

    async def handle_check_file_exists(
        self, tab_id: int, msg: ClientCheckFileExistsMessage
    ) -> None:
        result = await self._file_service.check_file_exists(
            path=msg.path,
            job_id=msg.jobId,
            filename=msg.filename,
            title=msg.title,
            ext=msg.ext,
            url=msg.url,
            mime=msg.mime,
        )
        await self._connection_manager.send_message(tab_id, result)

    async def _broadcast_jobs_list(self) -> None:
        jobs = list((await self._job_service.list_jobs()).values())
        await self._connection_manager.broadcast(
            {"type": "jobs_list", "jobs": [j.model_dump() for j in jobs]}
        )

    async def handle_pause(self, tab_id: int, msg: ClientPauseMessage) -> None:
        _ = tab_id
        logger.info(f"Pause request received for job {msg.jobId}")
        success = await self._job_service.pause(msg.jobId)
        if success:
            await self._broadcast_jobs_list()
        else:
            logger.warning(f"Failed to pause job {msg.jobId}")

    async def handle_resume(self, tab_id: int, msg: ClientResumeMessage) -> None:
        _ = tab_id
        logger.info(f"Resume request received for job {msg.jobId}")
        success = await self._job_service.resume(msg.jobId)
        if not success:
            logger.warning(f"Failed to resume job {msg.jobId}")
            return

        job = await self._job_service.get_job(msg.jobId)
        if not job:
            return

        output_dir = job.output_dir or app_settings.DEFAULT_OUTPUT_DIR
        if not await self._file_service.is_path_allowed(output_dir):
            logger.warning(
                f"Refusing to resume job {msg.jobId} because output directory is not allowed: {output_dir}"
            )
            await self._job_service.update_job(
                msg.jobId,
                status="failed",
                error="Selected output directory is no longer allowed.",
            )
            await self._broadcast_jobs_list()
            return

        resolved_output_dir = await self._file_service.resolve_output_dir(output_dir)
        await self._broadcast_jobs_list()

        is_torrent = job.media_type == "torrent" or is_magnet_url(job.url)
        if is_torrent:
            if self._torrent_service.is_active(msg.jobId):
                logger.debug(
                    f"Torrent download task for job {msg.jobId} is already running; resumed in-place."
                )
                return
            await self._torrent_service.start_download(
                tab_id=tab_id,
                job_id=msg.jobId,
                url=job.url,
                output_dir=resolved_output_dir,
                selected_files=job.torrent_selected_file_indices,
            )
        else:
            if self._download_service.is_active(msg.jobId):
                logger.debug(
                    f"HTTP download task for job {msg.jobId} is already running; resumed in-place."
                )
                return
            await self._download_service.start_download(
                tab_id=tab_id,
                job_id=msg.jobId,
                url=job.url,
                format_id=job.format_id or "best",
                output_dir=resolved_output_dir,
                referer=job.referer,
                media_type=job.media_type,
            )

    async def handle_refresh_url(
        self, tab_id: int, msg: ClientRefreshUrlMessage
    ) -> None:
        _ = tab_id
        logger.info(f"Refresh URL request received for job {msg.jobId}")
        job = await self._job_service.get_job(msg.jobId)
        if not job:
            logger.warning(f"Job {msg.jobId} not found for refresh URL")
            return

        parsed_url = urllib.parse.urlparse(msg.url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            logger.warning(f"Invalid refresh URL received for job {msg.jobId}")
            await self._connection_manager.send_message(
                tab_id,
                {
                    "type": "download_failed",
                    "jobId": msg.jobId,
                    "error": "The provided URL is not valid.",
                    "stage": "queued",
                },
            )
            return

        await self._job_service.pause(msg.jobId)
        await self._download_service.cancel(msg.jobId)
        await self._torrent_service.cancel(msg.jobId)
        await self._job_service.resume(msg.jobId)

        await self._job_service.update_job(
            msg.jobId,
            url=msg.url,
            referer=msg.referer or job.referer,
            status="queued",
            error=None,
            error_category=None,
        )
        await self._broadcast_jobs_list()

        output_dir = job.output_dir or app_settings.DEFAULT_OUTPUT_DIR
        if not await self._file_service.is_path_allowed(output_dir):
            logger.warning(
                f"Refusing to refresh URL for job {msg.jobId} because output directory is not allowed: {output_dir}"
            )
            await self._job_service.update_job(
                msg.jobId,
                status="failed",
                error="Selected output directory is no longer allowed.",
                error_category=None,
            )
            await self._broadcast_jobs_list()
            return

        resolved_output_dir = await self._file_service.resolve_output_dir(output_dir)

        is_torrent = job.media_type == "torrent" or is_magnet_url(msg.url)
        if is_torrent:
            await self._torrent_service.start_download(
                tab_id=tab_id,
                job_id=msg.jobId,
                url=msg.url,
                output_dir=resolved_output_dir,
                selected_files=job.torrent_selected_file_indices,
            )
        else:
            await self._download_service.start_download(
                tab_id=tab_id,
                job_id=msg.jobId,
                url=msg.url,
                format_id=job.format_id or "best",
                output_dir=resolved_output_dir,
                referer=msg.referer or job.referer,
                media_type=job.media_type,
            )

    async def handle_download_url(
        self, tab_id: int, msg: ClientDownloadUrlMessage
    ) -> None:
        parsed_url = urllib.parse.urlparse(msg.url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            logger.warning(f"Invalid download URL received for job {msg.jobId}")
            return

        logger.info(
            f"Download URL captured for job {msg.jobId}: {redact_url(msg.url)}"
        )

    async def _handle_remove_or_cancel(
        self, tab_id: int, msg: Union[ClientRemoveJobMessage, ClientCancelDownloadMessage]
    ) -> None:
        _ = tab_id
        is_cancel = isinstance(msg, ClientCancelDownloadMessage)
        job_id = msg.jobId
        logger.info(
            f"{'Cancel' if is_cancel else 'Remove'} request received for job {job_id}"
        )

        job_before_remove = await self._job_service.get_job(job_id)
        await self._job_service.remove_job(job_id)

        await self._download_service.cancel(job_id)
        await self._torrent_service.cancel(job_id)

        if job_before_remove and job_before_remove.file_path and job_before_remove.progress < 100.0:
            await self._file_service.maybe_trash_incomplete(
                job_before_remove.file_path, job_before_remove.progress
            )

        # Only clean the temp folder when the worker wrapper has actually finished.
        if not self._download_service.is_active(
            job_id
        ) and not self._torrent_service.is_active(job_id):
            await self._file_service.remove_temp_dir(job_id)

        if is_cancel:
            await self._connection_manager.broadcast(
                {"type": "download_canceled", "jobId": job_id}
            )

        await self._broadcast_jobs_list()

    async def handle_remove_job(
        self, tab_id: int, msg: ClientRemoveJobMessage
    ) -> None:
        await self._handle_remove_or_cancel(tab_id, msg)

    async def handle_cancel(
        self, tab_id: int, msg: ClientCancelDownloadMessage
    ) -> None:
        await self._handle_remove_or_cancel(tab_id, msg)

    async def handle_reveal_file(
        self, tab_id: int, msg: ClientRevealFileMessage
    ) -> None:
        job = await self._job_service.get_job(msg.jobId)
        if not job or not job.file_path:
            return

        logger.info(f"Reveal file request received for job {msg.jobId}")
        success = await self._file_service.reveal_file(job.file_path)
        if success:
            logger.info(f"Successfully revealed file {job.file_path}")
        else:
            logger.warning(f"Cannot reveal file, path does not exist: {job.file_path}")

    async def handle_delete_file(
        self, tab_id: int, msg: ClientDeleteFileMessage
    ) -> None:
        job = await self._job_service.get_job(msg.jobId)
        logger.info(f"Delete file request received for job {msg.jobId}")

        if job and job.file_path:
            await self._file_service.delete_file(job.file_path)

        await self._job_service.remove_job(msg.jobId)
        await self._broadcast_jobs_list()

    async def handle_get_jobs(self, tab_id: int, msg: ClientGetJobsMessage) -> None:
        _ = msg
        jobs = list((await self._job_service.list_jobs()).values())
        await self._connection_manager.send_message(
            tab_id,
            {"type": "jobs_list", "jobs": [j.model_dump() for j in jobs]},
        )

    async def handle_get_categories(
        self, tab_id: int, msg: ClientGetCategoriesMessage
    ) -> None:
        _ = msg
        categories = await self._category_service.get_categories()
        await self._connection_manager.send_message(
            tab_id,
            {
                "type": "categories_list",
                "categories": [c.model_dump() for c in categories],
            },
        )

    async def handle_save_categories(
        self, tab_id: int, msg: ClientSaveCategoriesMessage
    ) -> None:
        _ = tab_id
        logger.info(f"Categories saved ({len(msg.categories)} categories)")
        categories = await self._category_service.save_categories(msg.categories)
        await self._connection_manager.broadcast(
            {
                "type": "categories_list",
                "categories": [c.model_dump() for c in categories],
            }
        )

    async def handle_browse_directory(
        self, tab_id: int, msg: ClientBrowseDirectoryMessage
    ) -> None:
        try:
            selected_path = await self._file_service.browse_directory(msg.path)
            if selected_path:
                await self._connection_manager.send_message(
                    tab_id,
                    {
                        "type": "directory_selected",
                        "path": selected_path,
                        "forField": msg.forField,
                    },
                )
        except Exception as e:
            logger.warning(f"Directory picker failed: {e}")
            await self._connection_manager.send_message(
                tab_id, {"type": "browse_failed", "error": str(e)}
            )

    async def handle_get_settings(
        self, tab_id: int, msg: ClientGetSettingsMessage
    ) -> None:
        _ = msg
        settings = await self._settings_service.get_settings()
        await self._connection_manager.send_message(
            tab_id,
            {"type": "settings_data", "settings": settings.model_dump()},
        )

    async def handle_save_settings(
        self, tab_id: int, msg: ClientSaveSettingsMessage
    ) -> None:
        _ = tab_id
        await self._settings_service.save_settings(msg.settings)
        logger.info("Settings saved")
        await self._connection_manager.broadcast(
            {"type": "settings_data", "settings": msg.settings.model_dump()}
        )
        await self._download_service.notify_limit_changed()
        await self._torrent_service.notify_limit_changed()
