import asyncio
import logging
import mimetypes
import os
import re
from typing import Any
from urllib.parse import unquote, urlparse

import aiohttp
import m3u8

from engine.constants import DEFAULT_UA, MIME_TO_EXT

logger = logging.getLogger("dma-engine")


def guess_extension_from_mime(mime: str) -> str | None:
    mime = mime.split(";")[0].strip().lower()
    # Check standard library first
    ext = mimetypes.guess_extension(mime)
    if ext:
        return ext.lstrip(".")
    # Fallback for custom stream/media mime types not in standard registry
    return MIME_TO_EXT.get(mime)


def parse_content_disposition(header_val: str) -> str | None:
    if not header_val:
        return None
    # 1. Look for filename* parameter (RFC 6266 / RFC 5987)
    match_star = re.search(r"filename\*=\s*([^;]+)", header_val, re.IGNORECASE)
    if match_star:
        val = match_star.group(1).strip()
        parts = val.split("'", 2)
        if len(parts) == 3:
            charset, _, encoded_name = parts
            try:
                return unquote(encoded_name, encoding=charset or "utf-8")
            except Exception:
                pass
        elif len(parts) == 1:
            try:
                return unquote(parts[0])
            except Exception:
                pass

    # 2. Look for standard filename parameter
    match_fn = re.search(r"filename\s*=\s*((['\"])(.*?)\2|([^;\s]+))", header_val, re.IGNORECASE)
    if match_fn:
        name = match_fn.group(3) or match_fn.group(4)
        if name:
            return os.path.basename(name.strip())
    return None


async def probe_direct_link(url: str, headers: dict[str, str] | None) -> dict[str, Any] | None:
    """Asynchronously probe direct links using aiohttp.
    
    First tries a HEAD request, falling back to a range-limited GET request
    if HEAD is blocked or returns an error.
    """
    req_headers = {"User-Agent": DEFAULT_UA, **(headers or {})}
    timeout = aiohttp.ClientTimeout(total=10)

    # 1. Try HEAD request
    try:
        async with aiohttp.ClientSession(headers=req_headers) as session:
            async with session.head(url, timeout=timeout, allow_redirects=True) as resp:
                if resp.status == 200:
                    return {
                        "status": resp.status,
                        "url": str(resp.url),
                        "headers": dict(resp.headers),
                    }
    except Exception:
        pass

    # 2. Try GET request with Range: bytes=0-0
    try:
        get_headers = {**req_headers, "Range": "bytes=0-0"}
        async with aiohttp.ClientSession(headers=get_headers) as session:
            async with session.get(url, timeout=timeout, allow_redirects=True) as resp:
                if resp.status in (200, 206):
                    headers_dict = dict(resp.headers)
                    resp.close()
                    return {
                        "status": resp.status,
                        "url": str(resp.url),
                        "headers": headers_dict,
                    }
    except Exception as exc:
        logger.debug("GET probe failed for %s: %s", url, exc)

    return None


async def probe_url_metadata(url: str, headers: dict[str, str] | None) -> dict[str, Any] | None:
    """Probes a direct URL link to extract metadata (filename, size, is_stream).
    
    Returns None if the URL is an HTML page or probe fails.
    """
    probe_res = await probe_direct_link(url, headers)
    if not probe_res:
        return None

    resp_url = probe_res["url"]
    resp_headers = probe_res["headers"]
    ct = resp_headers.get("Content-Type", "").lower()

    if "text/html" in ct or "application/xhtml+xml" in ct:
        return None

    # Extract filename from Content-Disposition
    filename = parse_content_disposition(resp_headers.get("Content-Disposition", ""))
    if not filename:
        path = urlparse(resp_url).path.rstrip("/")
        filename = os.path.basename(path) or "download"
        filename = unquote(filename)

    filename = os.path.basename(filename).strip()
    if not os.path.splitext(filename)[1]:
        ext = guess_extension_from_mime(ct)
        if ext:
            filename = f"{filename}.{ext}"

    # Extract filesize
    filesize = None
    cr = resp_headers.get("Content-Range", "")
    if "/" in cr:
        total_str = cr.split("/")[-1].strip()
        if total_str.isdigit():
            filesize = int(total_str)
    if filesize is None:
        cl = resp_headers.get("Content-Length", "")
        if cl.isdigit():
            filesize = int(cl)

    is_stream = "mpegurl" in ct or "dash+xml" in ct or resp_url.lower().endswith((".m3u8", ".mpd"))

    return {
        "filename": filename,
        "filesize": filesize if filesize and filesize > 0 else None,
        "is_stream": is_stream,
        "mime_type": ct,
        "url": resp_url,
    }


async def estimate_stream_size(url: str, headers: dict[str, str] | None) -> int | None:
    """Best-effort total-byte estimate for an HLS stream by sampling segments.

    Returns ``None`` if size estimation fails.
    """
    req_headers = {"User-Agent": DEFAULT_UA, **(headers or {})}
    try:
        async with aiohttp.ClientSession(headers=req_headers) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                manifest = await resp.text()

            playlist = m3u8.loads(manifest, uri=url)
            if playlist.is_variant:
                best = max(
                    playlist.playlists,
                    key=lambda p: getattr(p.stream_info, "bandwidth", 0) or 0,
                    default=None,
                )
                if best and best.absolute_uri:
                    async with session.get(best.absolute_uri, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        if r.status != 200:
                            return None
                        playlist = m3u8.loads(await r.text(), uri=best.absolute_uri)

            segments = [s for s in playlist.segments if s.uri]
            if not segments:
                return None

            total = len(segments)
            if total <= 5:
                sample_urls = [s.absolute_uri for s in segments]
            else:
                indices = {0, total // 4, total // 2, 3 * total // 4, total - 1}
                sample_urls = [segments[i].absolute_uri for i in sorted(indices)]

            async def head_size(seg_url: str) -> int:
                try:
                    async with session.head(seg_url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                        return int(r.headers.get("Content-Length", 0)) if r.status == 200 else 0
                except Exception:
                    return 0

            sizes = await asyncio.gather(*(head_size(u) for u in sample_urls))
            valid = [s for s in sizes if s > 0]
            if not valid:
                return None
            return int(sum(valid) / len(valid) * total)
    except Exception as exc:
        logger.debug("Stream size estimation failed for %s: %s", url, exc)
        return None
