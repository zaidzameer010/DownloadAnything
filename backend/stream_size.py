"""Accurate / best-effort file-size resolution for yt-dlp formats.

Handles direct HTTP, HLS/m3u8, DASH/MPD, and yt-dlp's own fragment lists.
All network calls go through the provided :class:`yt_dlp.YoutubeDL` instance so
cookies, impersonation, and proxy settings are respected.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, cast
from urllib.parse import urljoin

import m3u8
import structlog
from yt_dlp.networking.common import Request
from yt_dlp.networking.exceptions import RequestError
from yt_dlp.utils import int_or_none, parse_duration

log = structlog.get_logger()

_SIZE_REQUEST_TIMEOUT = 15.0


def _safe_get_header(response: Any, name: str) -> str | None:
    try:
        return response.get_header(name)
    except Exception:  # noqa: BLE001
        return None


def _local_name(tag: str) -> str:
    return tag.split("}")[-1]


def _bytes_from_tbr(tbr: float, duration: float) -> int:
    """Approximate file size from total bitrate (kbps) and duration (seconds)."""
    return int(duration * tbr * (1000 / 8))


def _parse_http_range_total(content_range: str | None) -> int | None:
    if not content_range:
        return None
    match = re.match(r"bytes \d+-\d+/(\d+|\*)", content_range)
    if not match or match.group(1) == "*":
        return None
    return int(match.group(1))


def _effective_bitrate(fmt: Any) -> float | None:
    tbr = fmt.get("tbr")
    if tbr is not None:
        return float(tbr)
    vbr = fmt.get("vbr") or 0
    abr = fmt.get("abr") or 0
    total = (float(vbr) if vbr else 0) + (float(abr) if abr else 0)
    return total or None


def _estimate_from_bitrate(duration: float | None, fmt: Any) -> int | None:
    if duration is None or duration <= 0:
        return None
    bitrate = _effective_bitrate(fmt)
    if bitrate is None:
        return None
    return _bytes_from_tbr(bitrate, duration)


def _http_content_length(
    ydl: Any,
    url: str,
    headers: dict[str, str] | None,
) -> int | None:
    """Try HEAD, then a tiny Range GET, to learn the resource size."""
    req_headers = {**(headers or {})}

    try:
        req = Request(
            url,
            method="HEAD",
            headers=req_headers,
            extensions={"timeout": _SIZE_REQUEST_TIMEOUT},
        )
        with ydl.urlopen(req) as response:
            if 200 <= response.status < 400:
                cl = _safe_get_header(response, "Content-Length")
                if cl:
                    return int(cl)
    except RequestError:
        pass
    except Exception:  # noqa: BLE001
        log.debug("HEAD size check failed for %s", url)

    try:
        range_headers = {**req_headers, "Range": "bytes=0-0"}
        req = Request(
            url,
            method="GET",
            headers=range_headers,
            extensions={"timeout": _SIZE_REQUEST_TIMEOUT},
        )
        with ydl.urlopen(req) as response:
            if response.status == 206:
                return _parse_http_range_total(_safe_get_header(response, "Content-Range"))
            if 200 <= response.status < 400:
                cl = _safe_get_header(response, "Content-Length")
                if cl:
                    return int(cl)
    except Exception:  # noqa: BLE001
        log.debug("Range size check failed for %s", url)

    return None


def _parse_m3u8_byterange(value: str | None) -> int | None:
    if not value:
        return None
    parts = value.split("@")
    try:
        return int(parts[0])
    except ValueError:
        return None


def _hls_fetch_playlist(ydl: Any, url: str, headers: dict[str, str] | None) -> m3u8.M3U8 | None:
    cache: dict[str, m3u8.M3U8] | None = getattr(ydl, "_stream_size_hls_cache", None)
    if cache is None:
        cache = {}
        ydl._stream_size_hls_cache = cache
    if url in cache:
        return cache[url]

    try:
        req = Request(
            url,
            headers={**(headers or {})},
            extensions={"timeout": _SIZE_REQUEST_TIMEOUT},
        )
        with ydl.urlopen(req) as response:
            text = response.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        log.debug("Could not fetch HLS playlist %s", url)
        return None

    try:
        playlist = m3u8.loads(text, uri=url)
    except Exception:  # noqa: BLE001
        log.debug("Could not parse HLS playlist %s", url)
        return None

    cache[url] = playlist
    return playlist


def _hls_pick_best_variant(playlist: m3u8.M3U8) -> m3u8.Playlist | None:
    variants: list[Any] = list(getattr(playlist, "playlists", None) or [])
    if not variants:
        return None
    try:
        return cast(
            m3u8.Playlist | None,
            max(
                variants,
                key=lambda v: int(getattr(getattr(v, "stream_info", None), "bandwidth", 0) or 0),
            ),
        )
    except Exception:  # noqa: BLE001
        return variants[0]


def _hls_init_size(playlist: m3u8.M3U8) -> int:
    seen: set[tuple[str | None, str | None]] = set()
    total = 0
    inits: list[Any] = list(getattr(playlist, "segment_map", None) or [])
    if playlist.segments and getattr(playlist.segments[0], "init_section", None):
        inits.append(playlist.segments[0].init_section)
    for init in inits:
        uri = getattr(init, "uri", None)
        br = getattr(init, "byterange", None)
        key = (uri, br)
        if key in seen:
            continue
        seen.add(key)
        size = _parse_m3u8_byterange(br)
        if size:
            total += size
    return total


def _hls_size(
    ydl: Any,
    url: str,
    headers: dict[str, str] | None,
    fmt: Any,
    info: Any,
) -> tuple[int, bool] | None:
    playlist = _hls_fetch_playlist(ydl, url, headers)
    if playlist is None:
        return None

    if playlist.is_variant:
        best = _hls_pick_best_variant(playlist)
        if best is None or not best.absolute_uri:
            return None
        return _hls_size(ydl, best.absolute_uri, headers, fmt, info)

    if not playlist.is_endlist and playlist.playlist_type != "VOD":
        return None

    if not playlist.segments:
        return None

    init_size = _hls_init_size(playlist)
    try:
        duration = sum(float(seg.duration or 0) for seg in playlist.segments)
    except Exception:  # noqa: BLE001
        duration = None

    all_have_ranges = all(seg.byterange for seg in playlist.segments)
    if all_have_ranges:
        media_size = sum(_parse_m3u8_byterange(seg.byterange) or 0 for seg in playlist.segments)
        return init_size + media_size, False

    # Fall back to bitrate/duration estimate.
    if duration and duration > 0:
        bitrate = _effective_bitrate(fmt)
        if bitrate is not None:
            return _bytes_from_tbr(bitrate, duration) + init_size, True

        # No bitrate in the format itself; try to parse the master playlist.
        manifest_url = fmt.get("manifest_url") or info.get("manifest_url")
        if manifest_url:
            master = _hls_fetch_playlist(ydl, manifest_url, headers)
            if master and master.is_variant:
                best = _hls_pick_best_variant(master)
                best_bw = getattr(getattr(best, "stream_info", None), "bandwidth", None)
                if best and best_bw:
                    return _bytes_from_tbr(float(best_bw), duration) + init_size, True

    return None


def _dash_duration_from_str(value: str | None) -> float | None:
    if not value:
        return None
    parsed = parse_duration(value)
    return float(parsed) if parsed else None


def _dash_find_all(root: ET.Element, local_name: str) -> list[ET.Element]:
    return root.findall(f".//{{*}}{local_name}")


def _dash_representation_info(
    root: ET.Element,
    fmt: Any,
) -> tuple[ET.Element | None, float | None, str]:
    mpd_type = root.get("type", "static")
    mpd_duration = _dash_duration_from_str(root.get("mediaPresentationDuration"))

    target_id = str(fmt.get("format_id") or "")
    target_bandwidth = None
    tbr = _effective_bitrate(fmt)
    if tbr is not None:
        target_bandwidth = int(tbr * 1000)

    reps = _dash_find_all(root, "Representation")
    if not reps:
        return None, mpd_duration, mpd_type

    # Prefer an explicit id or bandwidth match.
    for rep in reps:
        if target_id and rep.get("id") == target_id:
            return rep, mpd_duration, mpd_type
    for rep in reps:
        if target_bandwidth and int(rep.get("bandwidth") or 0) == target_bandwidth:
            return rep, mpd_duration, mpd_type

    # Fallback: choose a representation whose AdaptationSet content type matches
    # the format's codec profile.
    is_video = fmt.get("vcodec") and fmt.get("vcodec") != "none"
    is_audio = fmt.get("acodec") and fmt.get("acodec") != "none"
    for rep in reps:
        parent = rep.find("..")
        if parent is None:
            continue
        content_type = parent.get("contentType") or parent.get("mimeType", "").split("/")[0]
        if is_video and content_type == "video":
            return rep, mpd_duration, mpd_type
        if is_audio and content_type == "audio":
            return rep, mpd_duration, mpd_type

    return reps[0], mpd_duration, mpd_type


def _dash_nearest_base_url(element: ET.Element, default: str) -> str:
    parents: list[ET.Element | None] = [
        element,
        cast(ET.Element | None, element.find("..")),
        cast(ET.Element | None, element.find("../..")),
    ]
    for parent in parents:
        if parent is None:
            continue
        base = parent.find("{*}BaseURL")
        if base is not None and base.text:
            return urljoin(default, base.text.strip())
    return default


def _dash_range_size(range_str: str) -> int:
    """Parse an HTTP byte range like '863-7113' (inclusive) into a byte count."""
    parts = range_str.split("-")
    if len(parts) != 2:
        return 0
    try:
        start, end = int(parts[0]), int(parts[1])
    except ValueError:
        return 0
    return max(0, end - start + 1)


def _dash_size(
    ydl: Any,
    url: str,
    headers: dict[str, str] | None,
    fmt: Any,
    info: Any,
) -> tuple[int, bool] | None:
    cache: dict[str, str] | None = getattr(ydl, "_stream_size_mpd_cache", None)
    if cache is None:
        cache = {}
        ydl._stream_size_mpd_cache = cache

    text = cache.get(url)
    if text is None:
        try:
            req = Request(
                url,
                headers={**(headers or {})},
                extensions={"timeout": _SIZE_REQUEST_TIMEOUT},
            )
            with ydl.urlopen(req) as response:
                text = response.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            log.debug("Could not fetch DASH MPD %s", url)
            return None
        cache[url] = text

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        log.debug("Could not parse DASH MPD %s", url)
        return None

    rep, duration, mpd_type = _dash_representation_info(root, fmt)
    if rep is None:
        return None

    if mpd_type == "dynamic" and duration is None:
        return None

    base_url = _dash_nearest_base_url(rep, fmt.get("fragment_base_url") or fmt.get("url") or url)
    bandwidth = int_or_none(rep.get("bandwidth"))

    # SegmentList with explicit mediaRange.
    segment_list = rep.find("{*}SegmentList")
    if segment_list is None:
        # Some manifests put SegmentList under AdaptationSet.
        segment_list = _dash_find_all(root, "SegmentList")[0] if _dash_find_all(root, "SegmentList") else None

    if segment_list is not None:
        total = 0
        has_missing = False

        init = segment_list.find("{*}Initialization")
        if init is not None:
            rng = init.get("range")
            if rng:
                total += _dash_range_size(rng)
            src = init.get("sourceURL")
            if src and not rng:
                init_size = _http_content_length(ydl, urljoin(base_url, src), headers)
                if init_size:
                    total += init_size

        for seg in segment_list.findall("{*}SegmentURL"):
            rng = seg.get("mediaRange")
            if rng:
                total += _dash_range_size(rng)
            else:
                has_missing = True

        if total and not has_missing:
            return total, False

    # SegmentBase single-file: HEAD the media file.
    segment_base = rep.find("{*}SegmentBase")
    if segment_base is not None or _dash_find_all(root, "SegmentBase"):
        file_size = _http_content_length(ydl, base_url, headers)
        if file_size:
            return file_size, False

    # SegmentTemplate or any remaining case: estimate from duration × bandwidth.
    if duration and bandwidth:
        return int(duration * bandwidth / 8), True

    return None


def _fragments_size(fragments: list[Any]) -> tuple[int, bool] | None:
    """Sum yt-dlp fragment byte sizes if they are present."""
    total = 0
    missing = False

    for frag in fragments:
        if frag.get("filesize"):
            total += int(frag["filesize"])
            continue

        byte_range = frag.get("byte_range")
        if isinstance(byte_range, dict):
            byte_range = cast(Any, byte_range)
            start = byte_range.get("start")
            end = byte_range.get("end")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
                total += int(end - start)
                continue

        missing = True

    if total:
        return total, missing

    return None


def _protocol_category(fmt: Any) -> str:
    protocol = str(fmt.get("protocol") or "").lower()
    if protocol.startswith("m3u8"):
        return "m3u8"
    if protocol.startswith("http_dash_segments") or protocol.startswith("dash"):
        return "dash"
    if protocol in ("http", "https"):
        return "http"
    if protocol in ("ftp", "ftps"):
        return "ftp"
    return protocol


def _coerce_fragments(fragments: Any) -> list[Any] | None:
    if fragments is None:
        return None
    if isinstance(fragments, (list, tuple)):
        return list(fragments)
    try:
        return list(fragments)
    except Exception:  # noqa: BLE001
        return None


def resolve_format_size(
    ydl: Any,
    fmt: Any,
    info: Any,
) -> tuple[int, bool] | None:
    """Return (bytes, is_estimate) for a single yt-dlp format."""
    if not isinstance(fmt, dict):
        return None
    fmt = cast(Any, fmt)

    # Merged formats (e.g. video+audio) keep their components in requested_formats.
    requested = _coerce_fragments(fmt.get("requested_formats"))
    if requested:
        total = 0
        is_estimate = False
        for sub in requested:
            result = resolve_format_size(ydl, sub, info)
            if result is None:
                return None
            size, estimate = result
            total += size
            is_estimate = is_estimate or estimate
        return total, is_estimate

    if fmt.get("filesize"):
        return int(fmt["filesize"]), False

    if fmt.get("filesize_approx"):
        return int(fmt["filesize_approx"]), True

    fragments = _coerce_fragments(fmt.get("fragments"))
    if fragments:
        result = _fragments_size(fragments)
        if result:
            return result

    url = fmt.get("url") or info.get("url")
    if not url or not isinstance(url, str):
        return None

    headers = fmt.get("http_headers") or info.get("http_headers")
    category = _protocol_category(fmt)

    if category == "m3u8":
        result = _hls_size(ydl, url, headers, fmt, info)
        if result:
            return result

    if category == "dash":
        result = _dash_size(ydl, url, headers, fmt, info)
        if result:
            return result

    if category in ("http", "https", "ftp", "ftps"):
        size = _http_content_length(ydl, url, headers)
        if size:
            return size, False

    duration = info.get("duration") or fmt.get("duration")
    estimate = _estimate_from_bitrate(duration, fmt)
    if estimate:
        return estimate, True

    return None


def resolve_selector_size(
    ydl: Any,
    info: Any,
    selector: str | None,
) -> tuple[int, bool] | None:
    """Resolve a yt-dlp format selector and sum the sizes of the chosen formats."""
    try:
        formats = ydl._get_formats(info)
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not get formats: %s", exc)
        return None

    if not formats:
        return None

    try:
        spec = selector or ydl._default_format_spec(info)
        chosen = ydl._select_formats(formats, ydl.build_format_selector(spec))
        if not chosen and selector:
            # Some selectors (e.g. 'best') may not match DASH/HLS components that are split across tracks.
            chosen = ydl._select_formats(formats, ydl.build_format_selector(ydl._default_format_spec(info)))
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not resolve format selector %r: %s", selector, exc)
        return None

    total = 0
    is_estimate = False
    any_known = False

    for fmt in chosen:
        result = resolve_format_size(ydl, fmt, info)
        if result is None:
            return None
        any_known = True
        size, estimate = result
        total += size
        is_estimate = is_estimate or estimate

    if not any_known:
        return None

    return total, is_estimate
