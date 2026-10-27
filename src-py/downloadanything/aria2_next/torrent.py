"""Torrent support for the aria2-next libtorrent engine.

Detection (magnet links, .torrent URLs, request kind), magnet parsing, the
.torrent fetch carrying the request's captured browser headers, per-task BT
option mapping from AppSettings, and select-file formatting for the file
picker. The download worker itself lives in downloads.py.
"""

from __future__ import annotations

import os
import ssl
import urllib.request
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import structlog

from ..settings import DEFAULT_UA
from .rpc import format_rpc_options

if TYPE_CHECKING:
    from ..settings import AppSettings

log = structlog.get_logger(__name__)

MAGNET_SCHEME = "magnet:"
# .torrent files are KBs to a few MB of bencode — anything larger is not a
# torrent and must not be pulled into memory.
MAX_TORRENT_BYTES = 20 * 1024 * 1024


def is_magnet(url: str) -> bool:
    return url.strip().lower().startswith(MAGNET_SCHEME)


def is_torrent_url(url: str) -> bool:
    """An http(s) URL that names a .torrent file."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and parts.path.lower().endswith(".torrent")


def is_torrent_req(req: dict[str, Any]) -> bool:
    """True when a task dict routes to the libtorrent path."""
    return (
        req.get("kind") == "torrent"
        or bool(req.get("torrent"))
        or is_magnet(str(req.get("url") or ""))
    )


def format_selection(indices: list[int]) -> str:
    """1-based indices → aria2's select-file syntax ("1-3,5")."""
    nums = sorted({int(i) for i in indices if int(i) > 0})
    parts: list[str] = []
    start = prev = 0
    for n in nums:
        if start == 0:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            parts.append(str(start) if start == prev else f"{start}-{prev}")
            start = prev = n
    if start:
        parts.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(parts)


def valid_selection(indices: list[int], files: list[dict[str, Any]]) -> list[int]:
    """The picked 1-based indices that exist in the torrent's file list
    (pad files are absent from it, so a plain 1..N bound is wrong)."""
    known = {int(f["index"]) for f in files if str(f.get("index") or "").isdigit()}
    return sorted({int(i) for i in indices} & known)


def torrent_headers(req: dict[str, Any]) -> dict[str, str]:
    """Fetch headers for a .torrent URL — the request's UA/referer plus the
    captured cookies for that host (private trackers hand the file only to
    the session that saw the page)."""
    from ..extract import host_matches

    url = str(req.get("url") or "")
    headers = {"User-Agent": DEFAULT_UA}
    if ref := req.get("referer"):
        headers["Referer"] = str(ref)
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        host = ""
    cookies: list[str] = []
    for ctx in req.get("contexts") or []:
        if not isinstance(ctx, dict):
            continue
        try:
            ctx_host = (urlsplit(str(ctx.get("url") or "")).hostname or "").lower()
        except ValueError:
            continue
        if host and ctx_host and not host_matches(host, ctx_host):
            continue
        for h in ctx.get("headers") or []:
            if not isinstance(h, dict):
                continue
            if str(h.get("name") or "").lower() == "cookie" and h.get("value"):
                cookies.append(str(h["value"]))
    if cookies:
        headers["Cookie"] = "; ".join(cookies)
    return headers


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


class _ScopedRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the captured session on the host it was captured for — a
    tracker redirecting to another host must not receive its cookies or the
    page URL (which may carry a passkey) as the Referer."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _host(newurl) != _host(req.full_url):
            new.remove_header("Cookie")
            new.remove_header("Referer")
        return new


def fetch_torrent(url: str, headers: dict[str, str], *, verify: bool = True) -> bytes:
    """Fetch .torrent bytes over http(s) with the request's headers."""
    try:
        scheme = urlsplit(url).scheme
    except ValueError as e:
        raise ValueError("invalid torrent URL") from e
    if scheme not in ("http", "https"):
        raise ValueError("torrent URL must be http(s)")
    req = urllib.request.Request(url, headers=headers)
    handlers: list[urllib.request.BaseHandler] = [_ScopedRedirect()]
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)
    with opener.open(req, timeout=30) as resp:
        data = resp.read(MAX_TORRENT_BYTES + 1)
    if not data:
        raise ValueError("empty torrent file")
    if len(data) > MAX_TORRENT_BYTES:
        raise ValueError("torrent file too large")
    return data


def task_options(
    s: AppSettings, req: dict[str, Any], *, dir_path: str, magnet: bool
) -> dict[str, Any]:
    """Per-task BT option pairs for aria2.addUri / aria2.addTorrent."""
    opts: dict[str, Any] = {
        "dir": dir_path,
        "bt-max-peers": max(0, int(s.bt_max_peers)),
        "seed-ratio": s.bt_seed_ratio,
        "enable-dht": s.bt_enable_dht,
        "enable-peer-exchange": s.bt_enable_pex,
        "bt-enable-lpd": s.bt_enable_lpd,
        "force-sequential": s.bt_sequential,
    }
    if s.bt_seed_time > 0:
        opts["seed-time"] = s.bt_seed_time
    if s.bt_trackers.strip():
        opts["bt-tracker"] = s.bt_trackers.strip()
    if sel := str(req.get("selectFiles") or "").strip():
        opts["select-file"] = sel
    elif magnet:
        # Pause once metadata arrives so the file picker decides what to
        # fetch; the selection (or the single-file auto-continue) unpauses.
        opts["pause-metadata"] = True
    return format_rpc_options(opts)


def root_path(files: list[dict[str, Any]]) -> str:
    """The path a torrent task opens: the common root of its files — the
    torrent folder for multi-file torrents, the file itself for single."""
    paths = [str(f["path"]) for f in files if isinstance(f, dict) and f.get("path")]
    if not paths:
        return ""
    if len(paths) == 1:
        return paths[0]
    try:
        return os.path.commonpath(paths)
    except ValueError:
        return paths[0]
