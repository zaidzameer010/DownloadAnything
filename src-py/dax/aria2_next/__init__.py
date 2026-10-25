"""aria2-next external downloader and WebSocket JSON-RPC integration for dax."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from .daemon import (
    Aria2NextDaemon,
    get_daemon,
    global_options_from_settings,
    resolve_aria2_next,
)
from .downloader import Aria2NextFD, derive_leg_gid, register_ytdlp_downloader
from .rpc import Aria2RpcClient, Aria2RpcError, format_rpc_options
from .torrent import (
    fetch_torrent,
    format_selection,
    is_magnet,
    is_torrent_req,
    is_torrent_url,
    root_path,
    task_options,
    torrent_headers,
    valid_selection,
)

if TYPE_CHECKING:
    from ..downloads import Task
    from ..settings import AppSettings

log = structlog.get_logger(__name__)

# All yt-dlp simplified protocol keys routed through Aria2NextFD.
EXTERNAL_DOWNLOADER_MAP: dict[str, str] = {
    "default": "aria2_next",
    "http": "aria2_next",
    "m3u8": "aria2_next",
    "dash": "aria2_next",
}


def use_aria2_next(s: AppSettings) -> bool:
    """True when aria2-next is selected as the downloader engine and available."""
    return s.downloader_engine == "aria2_next" and resolve_aria2_next(s) is not None


def configure_ydl_params(
    params: dict[str, Any],
    task: Task,
    s: AppSettings,
    daemon: Aria2NextDaemon | None = None,
) -> bool:
    """Wire Aria2NextFD into yt-dlp's download params when aria2-next is active."""
    if not use_aria2_next(s):
        return False
    register_ytdlp_downloader()
    # yt-dlp probes `Aria2NextFD.available("aria2_next")` (name only) when
    # routing delegated fragment downloads — stamp the configured binary so
    # the probe resolves the same executable the daemon spawns.
    Aria2NextFD.exe = resolve_aria2_next(s)
    active_daemon = daemon or get_daemon()
    params["external_downloader"] = dict(EXTERNAL_DOWNLOADER_MAP)
    params["_dax_task"] = task
    params["_dax_settings"] = s
    params["_dax_aria2_daemon"] = active_daemon
    return True


def prepare_info_for_external(info: dict[str, Any], s: AppSettings) -> None:
    """Clear curl_cffi `impersonate` markers from resolved format dicts right
    before download (`when='before_dl'`) so yt-dlp's `_get_suitable_downloader`
    does not bypass `external_downloader` when `info_dict.get('impersonate')`
    was set during extraction."""
    if not use_aria2_next(s):
        return
    info.pop("impersonate", None)
    info.pop("_has_impersonate", None)
    for key in ("requested_formats", "requested_downloads", "formats"):
        items = info.get(key)
        if isinstance(items, list):
            for fmt in items:
                if isinstance(fmt, dict):
                    fmt.pop("impersonate", None)
                    fmt.pop("_has_impersonate", None)


def control_task_rpc(
    daemon: Aria2NextDaemon,
    task: Task | None,
    action: str,
) -> None:
    """Execute task lifecycle controls against aria2-next over WebSocket RPC."""
    if not daemon.is_running():
        return
    rpc = daemon.rpc
    try:
        match action:
            case "pauseAll" | "forcePauseAll":
                rpc.pause_all(force=(action == "forcePauseAll"))
                return
            case "unpauseAll":
                rpc.unpause_all()
                return
            case "purge":
                rpc.purge_download_result()
                return
    except Exception as e:
        log.debug("aria2-next global control RPC notice", action=action, error=str(e))
        return

    if task is None:
        return

    with task.lock:
        gids = list(task.aria2_gids)
        if task.aria2_gid and task.aria2_gid not in gids:
            gids.append(task.aria2_gid)

    if not gids:
        return

    for agid in gids:
        try:
            match action:
                case "pause" | "forcePause":
                    rpc.pause(agid, force=(action == "forcePause"))
                case "unpause" | "resume":
                    rpc.unpause(agid)
                case "finishMedia":
                    rpc.finish_media(agid)
                case "remove" | "forceRemove" | "delete" | "download":
                    rpc.evict(agid)
                case "retry":
                    try:
                        rpc.retry_media(agid)
                    except Aria2RpcError:
                        # Not a media task (HTTP/BT) — clear the result so the
                        # worker can re-add the same gid.
                        rpc.evict(agid)
        except Exception as e:
            log.debug(
                "aria2-next task control RPC notice",
                gid=agid,
                action=action,
                error=str(e),
            )


__all__ = [
    "EXTERNAL_DOWNLOADER_MAP",
    "Aria2NextDaemon",
    "Aria2NextFD",
    "Aria2RpcClient",
    "Aria2RpcError",
    "configure_ydl_params",
    "control_task_rpc",
    "derive_leg_gid",
    "fetch_torrent",
    "format_rpc_options",
    "format_selection",
    "get_daemon",
    "global_options_from_settings",
    "is_magnet",
    "is_torrent_req",
    "is_torrent_url",
    "prepare_info_for_external",
    "register_ytdlp_downloader",
    "resolve_aria2_next",
    "root_path",
    "task_options",
    "torrent_headers",
    "use_aria2_next",
    "valid_selection",
]
