import asyncio
import os
import shutil
from typing import Any, Dict, Optional
import yt_dlp
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, TypeAdapter
from app.config import settings, get_app_version
from app.utils.logger import logger
from app.ws.manager import ws_manager
from app.engine.jobs import jobs_registry, DownloadPaused
from app.engine.probe import probe_video, determine_probe_error_suggestion, is_natively_supported
from app.engine.codec_filter import filter_and_summarize_formats
from app.engine.downloader import download_video, get_app_support_dir
from app.schemas.messages import (
    ClientMessage, ClientHelloMessage, ClientProbeMessage, ClientChooseMessage,
    ClientRevealFileMessage, ClientPingMessage, ClientGetJobsMessage,
    ClientGetCategoriesMessage, ClientSaveCategoriesMessage,
    ClientBrowseDirectoryMessage, ClientGetSettingsMessage, ClientSaveSettingsMessage,
    ClientPauseMessage, ClientResumeMessage, ClientRemoveJobMessage, ClientDeleteFileMessage,
    ClientCheckFileExistsMessage, ClientCancelProbeMessage
)

router = APIRouter()

# Setup Pydantic TypeAdapter for dynamic parsing
message_adapter = TypeAdapter(ClientMessage)

# Memory store for temporary probes before they are queued as jobs
pending_probes: Dict[str, Any] = {}

# Active probe tasks that can be cancelled
active_probe_tasks: Dict[str, asyncio.Task] = {}

# Active downloader tasks that can be cancelled/aborted
active_downloader_tasks: Dict[str, asyncio.Task] = {}

async def run_downloader_task(
    tab_id: int,
    job_id: str,
    url: str,
    format_id: str,
    output_dir: str,
    conflict_resolution: str = "replace",
    referer: Optional[str] = None
):
    task = asyncio.current_task()
    active_downloader_tasks[job_id] = task
    event_queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    
    # Update status to downloading immediately to prevent "Waiting in queue" UI flicker
    jobs_registry.update_job(job_id, status="downloading")
    
    # Broadcast updated jobs list to clients
    jobs = list(jobs_registry.list_jobs().values())
    await ws_manager.broadcast({
        "type": "jobs_list",
        "jobs": [j.model_dump() for j in jobs]
    })
    
    # Send download_queued message
    out_dir = output_dir if output_dir else settings.DEFAULT_OUTPUT_DIR
    job = jobs_registry.get_job(job_id)
    await ws_manager.broadcast({
        "type": "download_queued",
        "jobId": job_id,
        "outputPath": out_dir,
        "url": url,
        "title": job.title if job else None,
        "duration": job.duration if job else None,
        "thumbnail": job.thumbnail if job else None,
        "uploader": job.uploader if job else None
    })
    
    async def consumer():
        while True:
            try:
                event = await event_queue.get()
                await ws_manager.broadcast(event)
                event_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in progress hook ws forwarder: {e}")

    consumer_task = asyncio.create_task(consumer())
    
    try:
        # Run blocking download in worker thread
        filepath = await asyncio.to_thread(
            download_video,
            job_id=job_id,
            url=url,
            format_id=format_id,
            output_dir=out_dir,
            loop=loop,
            event_queue=event_queue,
            conflict_resolution=conflict_resolution,
            referer=referer
        )
        
        job = jobs_registry.get_job(job_id)
        size_bytes = (job.combined_total_bytes if job else None) or (job.total_bytes if job else None)
        
        await ws_manager.broadcast({
            "type": "download_completed",
            "jobId": job_id,
            "filePath": filepath,
            "sizeBytes": size_bytes,
            "durationMs": None
        })
    except DownloadPaused:
        logger.info(f"Download job {job_id} paused cleanly.")
        jobs = list(jobs_registry.list_jobs().values())
        await ws_manager.broadcast({
            "type": "jobs_list",
            "jobs": [j.model_dump() for j in jobs]
        })
    except yt_dlp.utils.DownloadCancelled:
        await ws_manager.broadcast({
            "type": "download_canceled",
            "jobId": job_id
        })
    except Exception as e:
        stage = "downloading"
        job = jobs_registry.get_job(job_id)
        if job and job.status == "postprocessing":
            stage = "postprocessing"
            
        await ws_manager.broadcast({
            "type": "download_failed",
            "jobId": job_id,
            "error": str(e),
            "stage": stage
        })
    finally:
        active_downloader_tasks.pop(job_id, None)
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass

def extract_fallback_title_from_url(url: str) -> str:
    try:
        from urllib.parse import urlparse
        import re
        path = urlparse(url).path
        if path.endswith('/'):
            path = path[:-1]
        segments = [s for s in path.split('/') if s]
        if not segments:
            return "video"
        
        last = segments[-1]
        generic_names = {"index", "master", "playlist", "manifest", "video", "chunk", "stream"}
        
        name_part = last.rsplit('.', 1)[0].lower()
        is_generic_part = (
            name_part in generic_names 
            or len(name_part) < 3
            or (re.match(r'^[a-zA-Z0-9\-_\s]+$', name_part) and any(res in name_part for res in ["720p", "1080p", "480p", "360p", "240p", "2160p", "4k"]))
            or re.match(r'^[0-9\-_\s]+$', name_part)
        )
        
        if is_generic_part:
            for s in reversed(segments[:-1]):
                s_part = s.lower()
                is_s_generic = (
                    s_part in generic_names 
                    or len(s_part) < 3
                    or (re.match(r'^[a-zA-Z0-9\-_\s]+$', s_part) and any(res in s_part for res in ["720p", "1080p", "480p", "360p", "240p", "2160p", "4k"]))
                    or re.match(r'^[0-9\-_\s]+$', s_part)
                )
                if not is_s_generic:
                    return s
            return "video"
        return last.rsplit('.', 1)[0]
    except Exception:
        return "video"

async def run_probe_task(tab_id: int, job_id: str, url_to_probe: str, page_title: Optional[str] = None):
    task = asyncio.current_task()
    active_probe_tasks[job_id] = task
    try:
        # Probe in threadpool
        info = await asyncio.to_thread(
            probe_video,
            job_id=job_id,
            url=url_to_probe
        )
        
        # Override generic stream title with page_title or URL path segment if needed
        import re
        extracted_title = info.get("title") or ""
        title_to_check = extracted_title
        if "." in title_to_check:
            title_to_check = title_to_check.rsplit('.', 1)[0]
            
        media_type = info.get("mediaType") or "video"
        generic_names = ["index", "master", "playlist", "manifest", "video", "chunk", "stream"]
        
        is_generic = (
            not title_to_check 
            or any(g == title_to_check.lower() for g in generic_names) 
            or len(title_to_check) < 3
            or (re.match(r'^[a-zA-Z0-9\-_\s]+$', title_to_check) and any(res in title_to_check.lower() for res in ["720p", "1080p", "480p", "360p", "240p", "2160p", "4k"]))
            or re.match(r'^[0-9\-_\s]+$', title_to_check)
        )
        
        if media_type == "stream" or is_generic:
            # Only use page_title if it isn't the generic dashboard page title
            if page_title and not any(d in page_title.lower() for d in ["downloadanything", "download anything"]):
                info["title"] = page_title
            elif is_generic:
                info["title"] = extract_fallback_title_from_url(url_to_probe)
        
        # Process formats
        formats_list = filter_and_summarize_formats(
            info.get("formats", []), 
            info.get("duration")
        )
        
        formats_json = [f.model_dump() for f in formats_list]
        
        # Save to pending_probes memory dict instead of jobs_registry!
        pending_probes[job_id] = {
            "url": url_to_probe,
            "title": info.get("title"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader"),
            "formats": formats_json,
            "mediaType": info.get("mediaType", "video")
        }
        
        # Send result only to the initiating tab
        await ws_manager.send_message(tab_id, {
            "type": "probe_result",
            "jobId": job_id,
            "title": info.get("title", "Unknown Title"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader"),
            "formats": formats_json,
            "mediaType": info.get("mediaType", "video")
        })
    except asyncio.CancelledError:
        logger.info(f"Probe task for job {job_id} was cancelled.")
        pending_probes.pop(job_id, None)
    except Exception as err:
        logger.error(f"Probe failed for {url_to_probe}: {err}")
        
        is_unsupported = not is_natively_supported(url_to_probe)
        
        # Send failure only to the initiating tab
        suggestion = determine_probe_error_suggestion(str(err))
        await ws_manager.send_message(tab_id, {
            "type": "probe_failed",
            "jobId": job_id,
            "error": str(err),
            "suggestion": suggestion,
            "isUnsupportedUrl": is_unsupported
        })
    finally:
        active_probe_tasks.pop(job_id, None)

@router.websocket("/ws")
@router.websocket("/ws/progress")
async def websocket_endpoint(websocket: WebSocket):
    tab_id = None
    try:
        await websocket.accept()
        
        # 1. Handshake
        initial_data = await websocket.receive_json()
        try:
            handshake = message_adapter.validate_python(initial_data)
            if not isinstance(handshake, ClientHelloMessage):
                await websocket.close(code=4001, reason="Handshake expected")
                return
            tab_id = handshake.tabId
        except Exception as e:
            logger.error(f"Handshake validation failed: {e}")
            await websocket.close(code=4002, reason="Invalid handshake format")
            return
            
        # Re-register under connection manager
        ws_manager.active_connections[tab_id] = websocket
        logger.info(f"Handshake success for tab {tab_id}. Client version: {handshake.clientVersion}")
        
        # Send Hello reply
        try:
            from yt_dlp.version import __version__ as yt_dlp_ver
            yt_dlp_version = yt_dlp_ver
        except ImportError:
            yt_dlp_version = getattr(yt_dlp, "__version__", "unknown") or "unknown"
        ffmpeg_available = shutil.which("ffmpeg") is not None
        
        await ws_manager.send_message(tab_id, {
            "type": "hello",
            "serverVersion": get_app_version(),
            "ytDlpVersion": yt_dlp_version,
            "ffmpegAvailable": ffmpeg_available,
            "heartbeatIntervalMs": 20000
        })

        # 2. Main message loop
        while True:
            data = await websocket.receive_json()
            try:
                msg = message_adapter.validate_python(data)
            except Exception as e:
                logger.error(f"Invalid message format received: {e}")
                continue
                
            if isinstance(msg, ClientPingMessage):
                await ws_manager.send_message(tab_id, {"type": "pong", "ts": msg.ts})
                
            elif isinstance(msg, ClientProbeMessage):
                import uuid
                job_id = f"job_{uuid.uuid4().hex[:8]}"
                
                # Check duplicate job (any status)
                existing_jobs = jobs_registry.list_jobs()
                duplicate_job = None
                for j_id, j_info in existing_jobs.items():
                    if j_info.url == msg.url:
                        duplicate_job = j_info
                        break
                        
                if duplicate_job:
                    logger.info(f"Duplicate probe requested for {msg.url}. Alerting client. JobId: {duplicate_job.job_id}")
                    await ws_manager.send_message(tab_id, {
                        "type": "duplicate_job_alert",
                        "jobId": duplicate_job.job_id,
                        "url": duplicate_job.url,
                        "title": duplicate_job.title or "Unknown Title",
                        "status": duplicate_job.status
                    })
                    continue
                
                # Setup job ID for probing
                url_to_probe = msg.url
                await ws_manager.send_message(tab_id, {
                    "type": "probe_started",
                    "jobId": job_id,
                    "url": url_to_probe
                })
                
                asyncio.create_task(run_probe_task(tab_id, job_id, url_to_probe, page_title=msg.title))

            elif isinstance(msg, ClientCancelProbeMessage):
                job_id = msg.jobId
                logger.info(f"Cancel probe requested for jobId: {job_id}")
                task = active_probe_tasks.get(job_id)
                if task:
                    task.cancel()
                    logger.info(f"Successfully cancelled probe task for jobId: {job_id}")

            elif isinstance(msg, ClientChooseMessage):
                job_id = msg.jobId
                
                # Retrieve from pending_probes memory cache
                metadata = pending_probes.pop(job_id, None)
                if not metadata:
                    if msg.url:
                        metadata = {
                            "url": msg.url,
                            "title": msg.title or extract_fallback_title_from_url(msg.url),
                            "duration": None,
                            "thumbnail": None,
                            "uploader": None,
                            "formats": [],
                            "mediaType": "file"
                        }
                    else:
                        logger.warning(f"Choose requested for untracked/missing probed job {job_id}")
                        continue
                
                # Now create the actual job in the registry
                jobs_registry.create_job(job_id, metadata["url"], status="downloading")
                jobs_registry.update_job(
                    job_id,
                    title=metadata["title"],
                    duration=metadata["duration"],
                    thumbnail=metadata["thumbnail"],
                    uploader=metadata["uploader"],
                    formats=metadata["formats"],
                    format_id=msg.formatId,
                    output_dir=msg.outputDir,
                    combined_total_bytes=msg.fileSize or 0.0,
                    total_bytes=msg.fileSize or 0.0,
                    referer=msg.referer,
                    media_type=metadata.get("mediaType")
                )
                
                conflict_res = getattr(msg, "conflictResolution", "replace") or "replace"
                
                asyncio.create_task(
                    run_downloader_task(
                        tab_id=tab_id,
                        job_id=job_id,
                        url=metadata["url"],
                        format_id=msg.formatId,
                        output_dir=msg.outputDir,
                        conflict_resolution=conflict_res,
                        referer=msg.referer
                    )
                )

            elif isinstance(msg, ClientCheckFileExistsMessage):
                exists = False
                try:
                    full_path = os.path.join(msg.path, msg.filename)
                    exists = os.path.exists(full_path)
                except Exception as e:
                    logger.error(f"Failed to check file existence: {e}")
                
                await ws_manager.send_message(tab_id, {
                    "type": "file_exists_result",
                    "exists": exists,
                    "filename": msg.filename,
                    "path": msg.path,
                    "jobId": msg.jobId
                })

            elif isinstance(msg, ClientPauseMessage):
                job_id = msg.jobId
                logger.info(f"Pause request received for job {job_id}")
                success = jobs_registry.trigger_pause(job_id)
                if success:
                    jobs = list(jobs_registry.list_jobs().values())
                    await ws_manager.broadcast({
                        "type": "jobs_list",
                        "jobs": [j.model_dump() for j in jobs]
                    })
                else:
                    logger.warning(f"Failed to pause job {job_id}")

            elif isinstance(msg, ClientResumeMessage):
                job_id = msg.jobId
                logger.info(f"Resume request received for job {job_id}")
                success = jobs_registry.trigger_resume(job_id)
                if success:
                    job = jobs_registry.get_job(job_id)
                    if job:
                        # Broadcast queued status first
                        jobs = list(jobs_registry.list_jobs().values())
                        await ws_manager.broadcast({
                            "type": "jobs_list",
                            "jobs": [j.model_dump() for j in jobs]
                        })
                        # Start downloader task again
                        asyncio.create_task(
                            run_downloader_task(
                                tab_id=tab_id,
                                job_id=job_id,
                                url=job.url,
                                format_id=job.format_id or "best",
                                output_dir=job.output_dir or "",
                                referer=job.referer
                            )
                        )
                else:
                    logger.warning(f"Failed to resume job {job_id}")

            elif isinstance(msg, ClientRemoveJobMessage):
                job_id = msg.jobId
                logger.info(f"Remove request received for job {job_id}")
                
                # Remove from registry so progress hook triggers abort
                jobs_registry.remove_job(job_id)
                
                # Cancel task and wait for it to stop cleanly
                task = active_downloader_tasks.get(job_id)
                if task:
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=1.5)
                    except Exception:
                        pass
                # Safe temp cleanup
                app_temp_dir = get_app_support_dir() / "temp" / job_id
                try:
                    if os.path.exists(app_temp_dir):
                        shutil.rmtree(app_temp_dir)
                        logger.info(f"Cleaned up temp folder on job removal: {app_temp_dir}")
                except Exception as e:
                    logger.error(f"Failed to remove temp folder {app_temp_dir}: {e}")
                jobs = list(jobs_registry.list_jobs().values())
                await ws_manager.broadcast({
                    "type": "jobs_list",
                    "jobs": [j.model_dump() for j in jobs]
                })

            elif isinstance(msg, ClientRevealFileMessage):
                job_id = msg.jobId
                logger.info(f"Reveal file request received for job {job_id}")
                job = jobs_registry.get_job(job_id)
                if job and job.file_path:
                    if os.path.exists(job.file_path):
                        import subprocess
                        import sys
                        try:
                            if sys.platform == "darwin":
                                subprocess.run(["open", "-R", job.file_path], check=True)
                            elif sys.platform == "win32":
                                subprocess.run(["explorer", "/select,", os.path.normpath(job.file_path)], check=True)
                            else:
                                subprocess.run(["xdg-open", os.path.dirname(job.file_path)], check=True)
                            logger.info(f"Successfully revealed file {job.file_path}")
                        except Exception as e:
                            logger.error(f"Failed to reveal file {job.file_path}: {e}")
                    else:
                        logger.warning(f"Cannot reveal file, path does not exist: {job.file_path}")

            elif isinstance(msg, ClientDeleteFileMessage):
                job_id = msg.jobId
                logger.info(f"Delete file request received for job {job_id}")
                job = jobs_registry.get_job(job_id)
                if job and job.file_path:
                    try:
                        if os.path.exists(job.file_path):
                            os.remove(job.file_path)
                            logger.info(f"Deleted file: {job.file_path}")
                        else:
                            part_path = f"{job.file_path}.part"
                            if os.path.exists(part_path):
                                os.remove(part_path)
                                logger.info(f"Deleted partial file: {part_path}")
                    except Exception as e:
                        logger.error(f"Failed to delete file {job.file_path}: {e}")
                jobs_registry.remove_job(job_id)
                jobs = list(jobs_registry.list_jobs().values())
                await ws_manager.broadcast({
                    "type": "jobs_list",
                    "jobs": [j.model_dump() for j in jobs]
                })

            elif isinstance(msg, ClientGetJobsMessage):
                jobs = list(jobs_registry.list_jobs().values())
                await ws_manager.send_message(tab_id, {
                    "type": "jobs_list",
                    "jobs": [j.model_dump() for j in jobs]
                })

            elif isinstance(msg, ClientGetCategoriesMessage):
                from app.api.categories import load_categories
                categories = load_categories()
                await ws_manager.send_message(tab_id, {
                    "type": "categories_list",
                    "categories": [c.model_dump() for c in categories]
                })

            elif isinstance(msg, ClientSaveCategoriesMessage):
                from app.api.categories import save_categories_to_file
                save_categories_to_file(msg.categories)
                await ws_manager.broadcast({
                    "type": "categories_list",
                    "categories": [c.model_dump() for c in msg.categories]
                })

            elif isinstance(msg, ClientBrowseDirectoryMessage):
                from app.api.browse import pick_directory_system
                try:
                    selected_path = await pick_directory_system(initial_dir=msg.path)
                    if selected_path:
                        await ws_manager.send_message(tab_id, {
                            "type": "directory_selected",
                            "path": selected_path,
                            "forField": msg.forField
                        })
                except Exception as e:
                    await ws_manager.send_message(tab_id, {
                        "type": "browse_failed",
                        "error": str(e)
                    })

            elif isinstance(msg, ClientGetSettingsMessage):
                from app.api.settings import load_settings
                settings_data = load_settings()
                await ws_manager.send_message(tab_id, {
                    "type": "settings_data",
                    "settings": settings_data.model_dump()
                })

            elif isinstance(msg, ClientSaveSettingsMessage):
                from app.api.settings import save_settings_to_file
                save_settings_to_file(msg.settings)
                await ws_manager.broadcast({
                    "type": "settings_data",
                    "settings": msg.settings.model_dump()
                })
                    
    except WebSocketDisconnect:
        logger.info(f"WebSocket connection closed for tab {tab_id}")
    except Exception as e:
        logger.error(f"WS error: {e}", exc_info=True)
    finally:
        if tab_id is not None:
            ws_manager.disconnect(tab_id)


