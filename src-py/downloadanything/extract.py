"""Extraction chain: yt-dlp → Rust extractor (PyO3) → yt-dlp → extension-sniffed URLs.

Per input URL:
1. Python yt-dlp probes the page/site URL the caller passed.
2. No downloadable media → the custom Rust extractor (downloadanything_rs,
   built from src-rs) scrapes candidate media URLs + page metadata from the
   page and sends the extracted media URLs to Python yt-dlp for full format
   and manifest probing (falling back to the raw link if yt-dlp cannot probe it).
3. Still no downloadable links → the extension's captured media URLs
   (`options.contexts`) are probed through yt-dlp (or synthesized as direct
   links).

Every drop/order/rank decision is downloadanything.filter's (filter.py) — the
Rust extractor and the extension only discover candidate URLs.
extractor_engine: auto (1→2→3) | python_ytdlp (1→3) | custom (2→3).
auto skips step 1 for pages yt-dlp has no dedicated extractor for when
`ytdlp_generic` is off — the Rust extractor replaces GenericIE.
"""

import asyncio
import contextlib
import copy
import functools
import os
import re
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from typing import Any
from urllib.parse import urljoin, urlsplit

import msgspec
import orjson
import structlog
from cachebox import TTLCache
from yt_dlp import YoutubeDL
from yt_dlp.utils import UnsupportedError

from .filter import (
    AUDIO_EXTS,
    MAX_CANDIDATES,
    VIDEO_EXTS,
    container_from_mime,
    container_of,
    has_media,
    kind_from_mime,
    kind_of,
    order_candidates,
    rank_formats,
    role,
    site_asset,
)
from .log import YDL_LOGGER, clean_text
from .models import ExtractOptions
from .settings import DEFAULT_UA

log = structlog.get_logger(__name__)


# yt-dlp marks a video's original audio track '(original)' in format_note;
# dubbed/descriptive tracks never carry it. Selector rungs prefer it then
# fall back to plain ba — single-language videos have no marker. Shared by
# the probe's pairing estimate and downloads' merge rungs.
ORIG_AUDIO = "ba[format_note*=original]"

DEFAULT_ACCEPT_LANG = "en-US,en;q=0.9"

# Cache budgets. Signed format URLs outlive the result TTL by hours; the
# info cache is longer-lived because a download replays it.
_BROKEN_IE_MAX = 1024
_BROKEN_IE_TTL = 1800
_COOKIE_JAR_MAX = 8
_COOKIE_JAR_TTL = 1800
_RESULT_CACHE_MAX = 512
_RESULT_CACHE_TTL = 300
_INFO_CACHE_MAX = 512
_INFO_CACHE_TTL = 1800
_STAT_CACHE_MAX = 2048
_STAT_CACHE_TTL = 600


@functools.cache
def impersonate_target():
    """curl_cffi TLS impersonation target — python-urllib's TLS fingerprint
    gets 403'd by anti-bot CDNs while the headers claim a Chrome UA. None
    when curl_cffi is not installed (yt-dlp would reject the target)."""
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return None
    from yt_dlp.networking.impersonate import ImpersonateTarget

    return ImpersonateTarget.from_str("chrome")


def youtube_client_args(player_client: str | None) -> list[str]:
    """youtube:player_client value shared by extract and download params.
    'default'/unset expands to yt-dlp's own client list; web_embedded is
    excluded unless picked explicitly — its embedded ytcfg currently lacks
    INNERTUBE_CONTEXT and crashes _fix_embedded_ytcfg (unfixed upstream;
    tv_downgraded+web cover the same formats)."""
    pick = player_client if player_client and player_client != "default" else "default"
    return [pick] if pick == "web_embedded" else [pick, "-web_embedded"]


# Error codes mirror downloadanything_rs::ExtractError::exit_code. Python emits 1 and 3;
# code 4 (DRM) arrives only inside Rust PageResults, passed through as-is.
CODE_FAIL = 1
CODE_NO_MEDIA = 3


def host_matches(host: str, domain: str) -> bool:
    """subdomains included — same matching the extractor applies to contexts."""
    return host == domain or host.endswith("." + domain)


def _clamp(v: int | None, default: int) -> int:
    return max(1, min(32, v if v is not None else default))


def replay_ctx_headers(
    headers: dict[str, str],
    contexts: Iterable[tuple[str, Iterable[tuple[str, str]]]],
    url: str,
    *,
    collect_cookies: bool = False,
    ua_from_ctx: bool = False,
    custom: bool = False,
) -> list[tuple[str, str]]:
    """Replay captured browser headers onto same-host requests and,
    optionally, harvest `Cookie:` headers into (host, header) jar seeds.
    `contexts` yields (ctx_url, [(name, value)]) pairs normalized by the
    caller (msgspec Structs or raw request dicts). Only presentation
    headers replay — captured authorization/origin headers are scoped to
    the request that carried them and poison API calls when replayed
    globally (a stale SAPISIDHASH broke youtube extraction). `custom` —
    set only for media targets, where every request is the manifest or a
    segment — also replays x-* headers: signed CDN tokens bind the
    request to device/session headers like x-did and 403 without them."""
    host = (urlsplit(url).hostname or "").lower()
    cookies: list[tuple[str, str]] = []
    for ctx_url, ctx_headers in contexts:
        try:
            ctx_host = (urlsplit(ctx_url).hostname or "").lower()
        except ValueError:
            continue
        if not ctx_host:
            continue
        for name_raw, value in ctx_headers:
            if not (name_raw and value):
                continue
            name = name_raw.lower()
            if name == "cookie":
                if collect_cookies:
                    cookies.append((ctx_host, value))
                continue
            if not (host and host_matches(host, ctx_host)):
                continue
            if name == "referer":
                headers.setdefault("Referer", value)
            elif name == "user-agent":
                if ua_from_ctx:
                    headers["User-Agent"] = value
            elif name == "accept-language":
                headers["Accept-Language"] = value
            elif custom and name.startswith("x-"):
                headers[name] = value
    return cookies


def apply_common_ydl_opts(
    params: dict[str, Any],
    check_certificate: bool | None,
    player_client: str | None,
) -> None:
    """Shared yt-dlp wiring for probe and download params: cert check,
    youtube player_client, the log bridge, and TLS impersonation. The generic
    extractor opts OUT of the global impersonate target (yt-dlp#11335) — its
    webpage fetch needs the extractor-arg or it 403s on TLS-fingerprint-gated
    CDNs with a chrome UA."""
    if check_certificate is False:
        params["nocheckcertificate"] = True
    # yt-dlp sends each progress tick to the logger's debug hook — quiet the
    # progress printer or every chunk becomes a log line.
    params["noprogress"] = True
    params["logger"] = YDL_LOGGER
    args = params.setdefault("extractor_args", {})
    args["youtube"] = {"player_client": youtube_client_args(player_client)}
    if t := impersonate_target():
        params["impersonate"] = t
        args["generic"] = {"impersonate": ["chrome"]}


def _ydl_params(opts: ExtractOptions, url: str) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Params + captured cookies. Cookies never ride in http_headers —
    yt-dlp 2026 deprecates header cookies (a redirect could leak them to a
    foreign host); they're seeded into the cookiejar scoped to the domain
    the browser captured them on. Returns (params, [(ctx_host, cookie_hdr)])."""
    headers: dict[str, str] = {
        "User-Agent": opts.user_agent or DEFAULT_UA,
        "Accept-Language": DEFAULT_ACCEPT_LANG,
    }
    if opts.referer:
        headers["Referer"] = opts.referer
    # Per-origin contexts: replay captured headers onto matching-host requests.
    cookies = replay_ctx_headers(
        headers,
        ((c.url, ((h.name, h.value) for h in c.headers)) for c in opts.contexts),
        url,
        collect_cookies=True,
        ua_from_ctx=not opts.user_agent,
        custom=_is_media_url(url),
    )
    params: dict[str, Any] = {
        "quiet": False,
        "http_headers": headers,
        # `is not None`, not truthiness — 0 retries / 0 timeout are valid
        # yt-dlp values ("no retry" / fail fast), not "unset".
        "socket_timeout": opts.timeout_secs if opts.timeout_secs is not None else 30,
        "retries": opts.retries if opts.retries is not None else 3,
        "extractor_retries": opts.retries if opts.retries is not None else 3,
        "ignore_no_formats_error": True,
        "skip_download": True,
        # Playlist listings come back as cheap url stubs — the probe's
        # Playlist tab renders them, and every entry still resolves fully
        # when its download runs. Video pages are unaffected.
        "extract_flat": "in_playlist",
    }
    if not opts.playlist:
        # The video pass: a "video AND playlist" URL (watch?v=…&list=…) must
        # yield only its video — the Playlist tab's own flat probe asks for
        # the list separately (opts.playlist).
        params["noplaylist"] = True
    if opts.proxy:
        params["proxy"] = opts.proxy
    # Cookies are NOT passed via cookiefile/cookiesfrombrowser — those load
    # per YoutubeDL instance, and a probe run spawns one per candidate.
    # _ytdlp_extract seeds each instance from _browser_jar's shared jar.
    if opts.max_items:
        params["playlistend"] = opts.max_items
    if opts.rate_limit_rps:
        params["sleep_interval_requests"] = 1.0 / opts.rate_limit_rps
    apply_common_ydl_opts(params, opts.check_certificate, opts.player_client)
    return params, cookies


def _seed_cookies(ydl: YoutubeDL, seeds: list[tuple[str, str]]) -> None:
    """Parse captured `Cookie:` headers into the jar, scoped to the domain
    the browser sent them on — yt-dlp's own unscoped→scoped fixup shape
    (`.{host}`, leading dot, path `/`)."""
    import http.cookiejar

    for host, header in seeds:
        domain = f".{host}"
        for part in header.split(";"):
            name, sep, value = part.strip().partition("=")
            if not sep or not name:
                continue
            ydl.cookiejar.set_cookie(
                http.cookiejar.Cookie(
                    0,
                    name,
                    value,
                    None,
                    False,
                    domain,
                    True,
                    True,
                    "/",
                    True,
                    False,
                    None,
                    False,
                    None,
                    None,
                    {},
                )
            )


def _cookie_seeds(contexts: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(host, Cookie-header) pairs from stored request contexts — seeded
    into the cookiejar so downloads replay the extractor's session
    (premium format lists depend on it)."""
    seeds: list[tuple[str, str]] = []
    for ctx in contexts:
        try:
            host = (urlsplit(ctx.get("url") or "").hostname or "").lower()
        except ValueError:
            continue
        for h in (ctx.get("headers") or []) if host else []:
            if (h.get("name") or "").lower() == "cookie" and h.get("value"):
                seeds.append((host, h["value"]))
    return seeds


def _kind(fmt: dict[str, Any]) -> str:
    # Audio-only rows carry no video codec — vcodec 'none' marks them; a
    # missing vcodec with a real acodec (or an audio container ext, for
    # codecs yt-dlp can't parse, e.g. iamf) is audio too. Checked before
    # the protocol so m3u8/mpd audio renditions don't read as video streams.
    vc, ac = fmt.get("vcodec"), fmt.get("acodec")
    if vc in (None, "none") and (
        vc == "none" or ac not in (None, "none") or str(fmt.get("ext") or "").lower() in AUDIO_EXTS
    ):
        return "audio"
    proto = fmt.get("protocol") or ""
    if proto.startswith("m3u8"):
        return "hls"
    if "dash" in proto:
        return "dash"
    return "progressive"


def _join_codecs(*parts: str | None) -> str | None:
    """Normalize codec fields into one comma-separated list, video first.
    Drops yt-dlp's `none`/`unknown` placeholders and blank entries."""
    codecs: list[str] = []
    for part in parts:
        for codec in (part or "").split(","):
            codec = codec.strip()
            if codec and len(codec) <= 64 and codec.lower() not in ("none", "unknown"):
                codecs.append(codec)
    return ", ".join(codecs[:8]) or None


def _codecs(fmt: dict[str, Any]) -> str | None:
    return _join_codecs(fmt.get("vcodec"), fmt.get("acodec"))


def _drm(fmt: dict[str, Any]) -> dict[str, Any] | None:
    if fmt.get("has_drm"):
        return {"type": "unknown", "scheme": "drm"}
    aes = fmt.get("hls_aes")
    if isinstance(aes, dict) and aes.get("uri"):
        return {"type": "aes_128", "key_uri": aes["uri"]}
    return None


def _best_audio(
    formats: list[dict[str, Any]], duration: float | None, pick: Any = None
) -> tuple[int, float]:
    """(bytes, kbps) of the audio track `+ba` would pair a video-only row
    with — size from the format itself or estimated from bitrate × duration.
    `pick` is yt-dlp's own selector (build_format_selector('ba')) so the
    pairing mirrors the download's merge — yt-dlp prefers e.g. opus over a
    higher-tbr mp4a, which a plain bitrate sort would not reproduce."""
    auds = [
        f
        for f in formats
        if f.get("vcodec") == "none"
        and f.get("acodec") not in (None, "none")
        and (f.get("url") or f.get("manifest_url"))
        and not f.get("has_drm")
    ]
    if not auds:
        return 0, 0.0
    best = None
    if pick is not None:
        try:
            best = next((f for f in pick({"formats": formats}) if f in auds), None)
        except Exception:
            best = None
    if best is None:
        best = max(auds, key=lambda f: f.get("tbr") or f.get("abr") or 0)
    tbr = best.get("tbr") or best.get("abr") or 0.0
    size = best.get("filesize") or best.get("filesize_approx")
    if not size and tbr and duration:
        size = tbr * 1000 * duration / 8
    return int(size or 0), float(tbr)


def _media_container(fmt: dict[str, Any]) -> str | None:
    """Real media container of a probed row. A script-path URL
    (`remote_control.php?file=…mp4`) names the file in its query, and an
    unsafe URL ext must never reach the UI or the output filename."""
    ext = str(fmt.get("ext") or "").lower()
    if ext in VIDEO_EXTS or ext in AUDIO_EXTS:
        return ext
    return container_of(str(fmt.get("url") or fmt.get("manifest_url") or ""))


def _link(
    fmt: dict[str, Any],
    live: bool,
    referer: str | None,
    audio: tuple[int, float] = (0, 0.0),
) -> dict[str, Any] | None:
    url = fmt.get("url") or fmt.get("manifest_url")
    if not url or url.startswith("data:"):
        return None
    # Storyboards and other metadata rows aren't media.
    if fmt.get("ext") in ("mhtml", "json") or str(fmt.get("format_id", "")).startswith("sb"):
        return None
    # Video-only rows download merged with the best audio — stats show
    # the pair, not the silent half.
    pair = fmt.get("vcodec") not in (None, "none") and fmt.get("acodec") in (None, "none")
    link: dict[str, Any] = {"url": url, "kind": _kind(fmt), "source": "extractor"}
    height, width = fmt.get("height"), fmt.get("width")
    tbr = fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr")
    if pair:
        tbr = (tbr or 0) + audio[1]
    quality: dict[str, Any] = {}
    if height:
        quality["height"] = height
    if width:
        quality["width"] = width
    if tbr:
        quality["bandwidth"] = int(tbr * 1000)
    label = fmt.get("format_note") or fmt.get("format")
    if label:
        quality["label"] = str(label)
    if quality:
        link["quality"] = quality
    if container := _media_container(fmt):
        link["container"] = container
    if codecs := _codecs(fmt):
        link["codecs"] = codecs
    ref = (fmt.get("http_headers") or {}).get("Referer") or referer
    if ref:
        link["headers"] = {"referer": ref}
    if live:
        link["is_live"] = True
    if drm := _drm(fmt):
        link["drm"] = drm
    if fmt.get("format_id"):
        link["repr_id"] = str(fmt["format_id"])
    if fmt.get("manifest_url"):
        link["manifest_url"] = fmt["manifest_url"]
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if size and pair:
        size += audio[0]
    if size:
        link["probed_size"] = int(size)
    return link


def _item(info: dict[str, Any], referer: str | None, pick: Any = None) -> dict[str, Any] | None:
    live = bool(info.get("is_live")) or info.get("live_status") == "is_live"
    audio = _best_audio(info.get("formats") or [], info.get("duration"), pick)
    formats = [lk for f in info.get("formats") or [] if (lk := _link(f, live, referer, audio))]
    deduped = rank_formats(formats, info.get("duration"))
    if not deduped:
        return None
    item: dict[str, Any] = {
        "id": str(info.get("id") or info.get("webpage_url") or ""),
        "page_url": info.get("webpage_url") or info.get("original_url") or "",
        "formats": deduped,
    }
    if info.get("title"):
        item["title"] = info["title"]
    if info.get("duration"):
        item["duration_secs"] = float(info["duration"])
    if info.get("thumbnail") and not site_asset(str(info["thumbnail"])):
        item["thumbnail"] = info["thumbnail"]
    return item


def _entry_thumb(e: dict[str, Any]) -> str | None:
    """A flat playlist entry's thumbnail — the plain `thumbnail` field, else
    the largest URL the extractor's thumbnails list carries."""
    if t := e.get("thumbnail"):
        return str(t)
    for t in reversed(e.get("thumbnails") or []):
        if url := t.get("url"):
            return str(url)
    return None


def _playlist_meta(info: dict[str, Any], entries: Iterable) -> dict[str, Any]:
    """A playlist result → the Playlist tab's listing. Flat entries arrive
    as url stubs ({url, id, title, ie_key}); resolved entries carry the same
    fields. `index` is yt-dlp's 1-based playlist_index — DownloadReq picks
    and the download's playlist_items spec select by it."""
    items: list[dict[str, Any]] = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            continue  # deleted/private slots arrive as None
        it: dict[str, Any] = {
            "index": int(e.get("playlist_index") or i + 1),
            "id": str(e.get("id") or ""),
            "url": str(e.get("url") or e.get("webpage_url") or ""),
            "title": str(e.get("title") or ""),
        }
        if key := e.get("ie_key"):
            it["ie_key"] = str(key)
        if e.get("duration"):
            it["duration_secs"] = float(e["duration"])
        if thumb := _entry_thumb(e):
            it["thumbnail"] = thumb
        items.append(it)
    return {
        "id": str(info.get("id") or ""),
        "title": str(info.get("title") or ""),
        "url": str(info.get("webpage_url") or info.get("original_url") or ""),
        "count": int(info.get("playlist_count") or len(items)),
        "entries": items,
    }


def _clean_error(e: BaseException) -> str:
    msg = str(e)
    msg = re.sub(r"^\[.*?\]\s*", "", msg)  # drop [extractor] tag
    msg = msg.removeprefix("ERROR: ").strip()
    return clean_text(msg)


def _is_unsupported(e: BaseException) -> bool:
    """yt-dlp has no extractor for this URL — the Rust pipeline's job."""
    if isinstance(e, UnsupportedError):
        return True
    msg = str(e)
    return "Unsupported URL" in msg or "No suitable extractor" in msg


def _is_extractor_bug(e: BaseException) -> bool:
    """yt-dlp tags extractor-code bugs (site changed upstream) with the
    'please report this issue' suffix — unlike geo/network failures these
    crash on every page of the host."""
    return "please report this issue" in str(e)


# Hosts whose dedicated extractor is broken upstream — the auto chain skips
# the doomed yt-dlp page pass for 30 min; candidate probing is unaffected.
_broken_ie: TTLCache[str, bool] = TTLCache(_BROKEN_IE_MAX, _BROKEN_IE_TTL)


def _mark_ie_broken(url: str) -> None:
    try:
        if host := (urlsplit(url).hostname or "").lower():
            _broken_ie.insert(host, True)
    except ValueError:
        pass


def _ie_broken(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and _broken_ie.get(host) is not None


def _ytdlp_extract(opts: ExtractOptions, raw: str, probe: bool = False) -> dict[str, Any]:
    try:
        params, cookie_seeds = _ydl_params(opts, raw)
        with YoutubeDL(params) as ydl:
            if jar := _browser_jar(opts):
                # Copies — an extractor mutating its jar must not leak into
                # the shared cache entry.
                for c in jar:
                    ydl.cookiejar.set_cookie(copy.copy(c))
            # A selected browser is the authoritative cookie source —
            # captured Cookie headers only fill in when none was chosen.
            if cookie_seeds and not (opts.cookies_browser and opts.cookies_browser != "off"):
                _seed_cookies(ydl, cookie_seeds)
            info = ydl.extract_info(raw, download=False)
            info = ydl.sanitize_info(info)
            pick = ydl.build_format_selector(f"{ORIG_AUDIO}/ba")
    except Exception as e:
        bug = _is_extractor_bug(e)
        if bug:
            _mark_ie_broken(raw)
        # A failed candidate probe keeps its raw fallback row — expected,
        # not an error. Upstream extractor bugs skip the traceback: the
        # app log can't fix yt-dlp.
        (log.warning if probe or bug else log.error)(
            "extract failed",
            url=raw,
            error=_clean_error(e),
            exc_info=not probe and not bug and not _is_unsupported(e),
        )
        page: dict[str, Any] = {
            "input": raw,
            "items": [],
            "error": _clean_error(e),
            "error_code": CODE_FAIL,
        }
        if _is_unsupported(e):
            page["_unsupported"] = True
        return page
    if not info:
        return {
            "input": raw,
            "items": [],
            "error": "no media found on page",
            "error_code": CODE_NO_MEDIA,
        }
    entries = (
        list(info["entries"])
        if info.get("_type") == "playlist" and info.get("entries") is not None
        else None
    )
    sources = entries if entries else [info]
    pairs = [(e, it) for e in sources if e and (it := _item(e, opts.referer, pick))]
    if opts.max_items:
        pairs = pairs[: opts.max_items]
    items = [it for _, it in pairs]
    # Probe rows show what the download gets: HEAD fills size, a bounded
    # range read sniffs codec tags from the moov box.
    _stat_missing([f for it in items for f in it["formats"]], opts)
    page = {"input": raw, "items": items}
    ie = (info.get("extractor_key") or info.get("extractor") or "").lower()
    page["extractor"] = "yt-dlp:generic" if ie == "generic" else "yt-dlp"
    if info.get("webpage_url"):
        page["final_url"] = info["webpage_url"]
    if entries is not None:
        # The Playlist tab's listing — flat stubs carry title/index/url,
        # enough for picking; per-entry formats resolve at download time.
        page["playlist"] = _playlist_meta(info, entries)
    if items or entries is not None:
        # Downloads reuse this extraction via process_ie_result — pairs
        # carry each entry's own info so a playlist item's page_url
        # resolves to that video's dict, not the whole list; the playlist's
        # own dict lets a playlist task replay without a second listing.
        _register_info(opts, raw, info, pairs)
    if not items and entries is None:
        page["error"] = "no media found on page"
        page["error_code"] = CODE_NO_MEDIA
    log.info(
        "extract complete",
        url=raw,
        items=len(items),
        formats=sum(len(i["formats"]) for i in items),
        error=page.get("error"),
    )
    return page


_cookie_jar_cache: TTLCache[tuple[str, str, float], CookieJar] = TTLCache(
    _COOKIE_JAR_MAX, _COOKIE_JAR_TTL
)


def _browser_jar(opts: ExtractOptions) -> CookieJar | None:
    """Browser/cookie-file jar shared by every yt-dlp instance — loading
    decrypts the whole browser store, so it runs once per (browser, file,
    mtime) instead of once per probed candidate."""
    browser = opts.cookies_browser if opts.cookies_browser and opts.cookies_browser != "off" else ""
    cfile = os.path.expanduser(opts.cookies_file) if opts.cookies_file else ""
    if cfile and not os.path.isfile(cfile):
        log.warning("cookies file not found — ignored", path=opts.cookies_file)
        cfile = ""
    if not browser and not cfile:
        return None
    # A cookies file edited on disk invalidates immediately — the TTL alone
    # would serve the old session for up to 30 minutes.
    try:
        stamp = os.path.getmtime(cfile) if cfile else 0.0
    except OSError:
        stamp = 0.0
    key = (browser, cfile, stamp)
    jar = _cookie_jar_cache.get(key)
    if jar is None:
        try:
            yopts: dict[str, Any] = {"logger": YDL_LOGGER}
            if browser:
                # yt-dlp decrypts the browser's store (chrome, firefox,
                # safari, edge, brave, chromium, opera, vivaldi, whale).
                yopts["cookiesfrombrowser"] = (browser,)
            if cfile:
                yopts["cookiefile"] = cfile
            with YoutubeDL(yopts) as ydl:
                jar = ydl.cookiejar
                # Loading must not write back: close() saves the whole jar
                # (decrypted browser cookies included) into the user's file.
                ydl.params["cookiefile"] = None
            _cookie_jar_cache.insert(key, jar)
        except Exception as e:
            log.warning("cookie load failed", error=e)
            return None
    return jar


def _cookie_header_for_url(opts: ExtractOptions, url: str) -> tuple[str, str] | None:
    """Resolve cookies from `cookies_browser` or `cookies_file` in memory for
    `url`'s host so the Rust page fetcher receives authenticated session
    cookies via `contexts` without writing decrypted cookies to disk."""
    host = (urlsplit(url).hostname or "").lower()
    jar = _browser_jar(opts) if host else None
    if jar is None:
        return None
    pairs = [
        f"{c.name}={c.value}"
        for c in jar
        if c.name and c.value and c.domain and host_matches(host, c.domain.lstrip(".").lower())
    ]
    if not pairs:
        return None
    return f"https://{host}", "; ".join(pairs)


def _ctx_title(opts: ExtractOptions) -> str | None:
    """Fallback title for the pushed tab: the single top-level title first,
    then the legacy per-context copies older extensions sent. Rust's page
    title is preferred over this wherever it exists."""
    if title := (opts.title or "").strip():
        return title
    return next((t for c in opts.contexts if (t := (c.title or "").strip())), None)


# A shell <title> or a generic JSON title often names the app, not the video.
LOW_TITLE_SOURCES = frozenset({"document_title", "json_generic"})


def _page_title(
    page_meta: dict[str, Any] | None,
    opts: ExtractOptions,
    page_url: str | None = None,
) -> str | None:
    """Rust's title unless its source is low-confidence — then the live DOM
    title wins, unless the live title is just the site name."""
    meta = page_meta or {}
    rust_title = (meta.get("title") or "").strip()
    source = (meta.get("title_source") or "").strip().lower()
    ext_title = _ctx_title(opts)
    if rust_title and source not in LOW_TITLE_SOURCES:
        return rust_title
    if not ext_title:
        return rust_title or None
    if rust_title:
        host = (urlsplit(page_url).hostname or "").lower() if page_url else ""
        label = host.split(".")[0]
        ext_norm = re.sub(r"[^a-z0-9]", "", ext_title.lower())
        if label and ext_norm and (ext_norm == label or ext_norm in label):
            return rust_title
        rust_l, ext_l = rust_title.lower(), ext_title.lower()
        if rust_l != ext_l and (rust_l.startswith(ext_l) or rust_l.endswith(ext_l)):
            return rust_title
    return ext_title


# Codec fourccs sniffed from the moov box — video then audio.
_CODEC_TAGS = (
    b"avc1",
    b"avc3",
    b"hvc1",
    b"hev1",
    b"vp09",
    b"vp08",
    b"av01",
    b"mp4v",
    b"mp4a",
    b"ac-3",
    b"ec-3",
    b"Opus",
    b"fLaC",
)
_STAT_BYTES = 64 * 1024

# HEAD/range results keyed by (url, referer) — re-probing the same row in a
# later batch reuses them. Signed URLs make the key unique per session.
_stat_cache: TTLCache[tuple[str, str, str], tuple[int | None, str | None]] = TTLCache(
    _STAT_CACHE_MAX, _STAT_CACHE_TTL
)
_stat_local = threading.local()


def _stat_session() -> Any:
    """Per-thread curl_cffi session — the HEAD + range read share one TLS
    connection instead of a handshake per request."""
    session = getattr(_stat_local, "session", None)
    if session is None:
        from curl_cffi import requests as creq

        session = creq.Session(impersonate="chrome")
        _stat_local.session = session
    return session


def _probe_headers(source: dict[str, Any], opts: ExtractOptions, url: str) -> dict[str, str]:
    """Headers for a direct probe: the row's own, plus the extraction jar's
    Cookie header when the row carries none. Signed CDN URLs bind the key to
    the session, so a HEAD/range/master probe 403s on a wrong key otherwise."""
    headers = {k: v for k, v in (source.get("headers") or {}).items() if isinstance(v, str)}
    if not any(k.lower() == "cookie" for k in headers) and (
        ck := _cookie_header_for_url(opts, url)
    ):
        headers["cookie"] = ck[1]
    return headers


def _stat_row(fmt: dict[str, Any], opts: ExtractOptions) -> None:
    """Fill `probed_size`/`codecs` on a direct-file row — HEAD for
    content-length, a bounded range read for moov-box codec tags.
    Best-effort: failures leave the fields unset."""
    url = str(fmt.get("url") or "")
    if not url.startswith(("http://", "https://")):
        return
    headers = _probe_headers(fmt, opts, url)
    # Cookies ride in the key: two sessions on one cookie-gated URL must
    # not share a stat entry — a signed URL differs per session anyway, so
    # this only extends the same guarantee to stable private URLs.
    key = (url, headers.get("referer") or "", headers.get("cookie") or "")
    if (hit := _stat_cache.get(key)) is not None:
        size, codecs = hit
        if fmt.get("probed_size") is None and size is not None:
            fmt["probed_size"] = size
        if not fmt.get("codecs") and codecs:
            fmt["codecs"] = codecs
        return
    try:
        session = _stat_session()
    except ImportError:
        return
    try:
        r = session.head(url, headers=headers, timeout=10)
        size: int | None = None
        # An error body's content-length is not the file size — xhpingcdn
        # answers bad-key probes with a 9-byte 403 stub.
        if r.status_code < 400 and (cl := r.headers.get("content-length")):
            with contextlib.suppress(ValueError):
                size = int(cl)
        codecs: str | None = None
        # The range GET doubles as the size source for HEAD-rejecting
        # servers: 206 carries the total in Content-Range. Only run it
        # when something is still missing.
        if size is None or (
            not fmt.get("codecs") and "video" in str(r.headers.get("content-type") or "")
        ):
            resp = session.get(
                url,
                headers={**headers, "Range": f"bytes=0-{_STAT_BYTES - 1}"},
                timeout=10,
                stream=True,
            )
            try:
                if size is None:
                    if resp.status_code == 206 and (
                        m := re.search(
                            r"/(\d+)\s*$",
                            str(resp.headers.get("content-range") or ""),
                        )
                    ):
                        size = int(m.group(1))
                    elif resp.status_code == 200 and (cl := resp.headers.get("content-length")):
                        with contextlib.suppress(ValueError):
                            size = int(cl)
                if (
                    not fmt.get("codecs")
                    and resp.status_code < 400
                    and "video" in str(resp.headers.get("content-type") or "")
                ):
                    buf = bytearray()
                    for chunk in resp.iter_content():
                        buf.extend(chunk)
                        if len(buf) >= _STAT_BYTES:
                            break
                    if tags := dict.fromkeys(t for t in _CODEC_TAGS if t in buf):
                        codecs = ", ".join(t.decode() for t in tags)
            finally:
                resp.close()
        if fmt.get("probed_size") is None and size is not None:
            fmt["probed_size"] = size
        if codecs is not None:
            fmt["codecs"] = codecs
        if size is not None or codecs is not None:
            _stat_cache.insert(key, (size, codecs))
    except Exception as e:
        # Best-effort probe: the row keeps its unset fields. Log the host,
        # never the signed URL (its query carries the access token).
        host = (urlsplit(url).hostname or "").lower()
        log.debug("stat probe failed", host=host, error=clean_text(str(e)))
        return


# Candidate probes per item are network-bound and independent — cap the pool
# so a page with many candidates cannot spawn one thread per row.
_PROBE_WORKERS = 8


def _stat_missing(formats: list[dict[str, Any]], opts: ExtractOptions) -> None:
    """HEAD+range fill for progressive rows missing size or codec tags —
    the UI's size chip reads `probed_size`, so every shown row stats."""
    stat = [
        f
        for f in formats
        if f.get("kind") == "progressive" and (f.get("probed_size") is None or not f.get("codecs"))
    ]
    if stat:
        with ThreadPoolExecutor(max_workers=min(len(stat), _PROBE_WORKERS)) as pool:
            list(pool.map(lambda f: _stat_row(f, opts), stat))


_HLS_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _parse_hls_master(text: str, master_url: str) -> list[dict[str, Any]]:
    """Variant rows from an HLS master playlist — every `EXT-X-STREAM-INF`
    carries its own RESOLUTION/CODECS/BANDWIDTH. A media playlist yields none."""
    rows: list[dict[str, Any]] = []
    pending: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = {
                key: value.strip().strip('"')
                for key, value in _HLS_ATTR_RE.findall(line.partition(":")[2])
            }
            continue
        if not line or line.startswith("#") or pending is None:
            continue
        attrs, pending = pending, None
        url = urljoin(master_url, line)
        if not url.startswith(("http://", "https://")):
            continue
        quality: dict[str, Any] = {}
        if resolution := attrs.get("RESOLUTION"):
            width, _, height = resolution.lower().partition("x")
            if width.isdigit() and height.isdigit():
                quality["width"] = int(width)
                quality["height"] = int(height)
        if (bandwidth := attrs.get("BANDWIDTH") or attrs.get("AVERAGE-BANDWIDTH")) and (
            bandwidth.isdigit()
        ):
            quality["bandwidth"] = int(bandwidth)
        row: dict[str, Any] = {
            "url": url,
            "kind": "hls",
            "manifest_url": master_url,
            "page_url": url,
        }
        if quality:
            row["quality"] = quality
        if codecs := _join_codecs(attrs.get("CODECS")):
            row["codecs"] = codecs
        rows.append(row)
    return rows


def _hls_master_rows(cand: dict[str, Any], opts: ExtractOptions) -> list[dict[str, Any]]:
    """Fetch a master playlist and list every rendition — the fallback when a
    yt-dlp probe resolved one or no format. Best-effort: failures keep the
    candidate's own fallback row."""
    url = str(cand.get("url") or "")
    if not url.startswith(("http://", "https://")):
        return []
    headers = _probe_headers(cand, opts, url)
    try:
        resp = _stat_session().get(url, headers=headers, timeout=10)
        if resp.status_code >= 400:
            return []
        rows = _parse_hls_master(resp.text, url)
    except Exception as e:
        host = (urlsplit(url).hostname or "").lower()
        log.debug("hls master probe failed", host=host, error=clean_text(str(e)))
        return []
    referer = (cand.get("headers") or {}).get("referer")
    source = cand.get("source") or "direct"
    for row in rows:
        row["source"] = source
        if referer:
            row["headers"] = {"referer": referer}
    return rows


def _probe_rust_item(
    sub_opts: ExtractOptions,
    raw_item: dict[str, Any],
    page_ref: str,
    ctx_title: str | None,
    page_thumbnail: str | None = None,
    page_duration: float | None = None,
) -> dict[str, Any] | None:
    """Probe one Rust item's candidate URLs through yt-dlp and shape its rows."""
    cands = order_candidates(raw_item.get("formats") or [])
    if not cands:
        return None
    merged_formats: list[dict[str, Any]] = []
    probed_meta: dict[str, Any] = {}
    probed_page_url: str | None = None
    # Fallback playlist reads per item — a page of per-quality masters would
    # otherwise pay one extra fetch for every failed/single-format probe.
    hls_budget = 4

    def probe(cand: dict[str, Any]) -> dict[str, Any]:
        referer = (cand.get("headers") or {}).get("referer") or page_ref
        probe_opts = (
            msgspec.structs.replace(sub_opts, referer=referer)
            if referer and referer != sub_opts.referer
            else sub_opts
        )
        return _ytdlp_extract(probe_opts, str(cand["url"]), probe=True)

    def fold(cand: dict[str, Any], probed: dict[str, Any]) -> int:
        """Merge a probe result into the format list; return the row
        count it yielded. Failed probes keep the raw candidate as a
        direct-link fallback row."""
        nonlocal probed_meta, probed_page_url, hls_budget
        cand_referer = (cand.get("headers") or {}).get("referer") or page_ref
        got = 0
        if has_media(probed):
            for p_item in probed.get("items") or []:
                if not probed_meta:
                    probed_meta = p_item
                if not probed_page_url and p_item.get("page_url"):
                    probed_page_url = p_item["page_url"]
                for fmt in p_item.get("formats") or []:
                    fmt.setdefault("headers", {})["referer"] = cand_referer
                    # Each row keeps its own probe URL — one item can merge
                    # rows from several manifest probes, and the download
                    # replays that row's cached info dict by this URL.
                    if p_page := p_item.get("page_url"):
                        fmt["page_url"] = p_page
                    # A resolved redirect loses the candidate's metadata —
                    # `remote_control.php` has no height/container while the
                    # candidate's `_1440p.mp4` name carried both.
                    if q := cand.get("quality"):
                        fq = fmt.setdefault("quality", {})
                        for k in ("height", "width", "label", "bandwidth"):
                            if fq.get(k) is None and q.get(k) is not None:
                                fq[k] = q[k]
                    if not fmt.get("codecs") and (cand_codecs := cand.get("codecs")):
                        fmt["codecs"] = cand_codecs
                    if (cand_container := container_of(str(cand.get("url") or ""))) and (
                        str(fmt.get("container") or "") not in VIDEO_EXTS | AUDIO_EXTS
                    ):
                        fmt["container"] = cand_container
                    merged_formats.append(fmt)
                    got += 1
        # A master that resolved one/no format hides the rest of its ladder —
        # read the playlist itself. Variant playlists yield no rows.
        url = str(cand.get("url") or "")
        if (
            got <= 1
            and hls_budget > 0
            and cand.get("kind") == "hls"
            and (got == 0 or role(url, "hls") == "master")
        ):
            hls_budget -= 1
            variants = _hls_master_rows(cand, sub_opts)
            if len(variants) > got:
                for row in variants:
                    row.setdefault("headers", {}).setdefault("referer", cand_referer)
                    merged_formats.append(row)
                got = len(variants)
        if got == 0:
            cand.setdefault("headers", {}).setdefault("referer", page_ref)
            # A failed probe stays a direct row: its own URL is the
            # authoritative download/probe identity.
            cand.setdefault("page_url", cand.get("url"))
            merged_formats.append(cand)
        return got

    # Manifest candidates probe in parallel — per-quality masters each
    # resolve to a single rendition, so serial probing costs one
    # round-trip per row. A manifest resolving a multi-format ladder
    # already carried every variant: the remaining direct-file
    # fallbacks then stay unprobed.
    manifest = [c for c in cands if c.get("kind") in ("hls", "dash")]
    rest = [c for c in cands if c.get("kind") not in ("hls", "dash")]
    ladder = False
    workers = min(len(cands), _PROBE_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for cand, probed in zip(manifest, pool.map(probe, manifest), strict=True):
            if fold(cand, probed) > 1:
                ladder = True
        if not ladder:
            for cand, probed in zip(rest, pool.map(probe, rest), strict=True):
                fold(cand, probed)
    shaped = rank_formats(merged_formats, raw_item.get("duration_secs"))
    if not shaped:
        return None
    _stat_missing(shaped, sub_opts)
    item: dict[str, Any] = {
        "id": str(raw_item.get("id") or probed_meta.get("id") or page_ref),
        "page_url": probed_page_url or str(raw_item.get("page_url") or page_ref),
        "formats": shaped,
    }
    if title := (raw_item.get("title") or probed_meta.get("title") or ctx_title):
        item["title"] = title
    if dur := (raw_item.get("duration_secs") or probed_meta.get("duration_secs") or page_duration):
        item["duration_secs"] = float(dur)
    if thumb := next(
        (
            str(t)
            for t in (raw_item.get("thumbnail"), probed_meta.get("thumbnail"), page_thumbnail)
            if t and not site_asset(str(t))
        ),
        None,
    ):
        item["thumbnail"] = thumb
    return item


def _probe_rust_items(
    opts: ExtractOptions,
    raw_items: list[dict[str, Any]],
    page_ref: str,
    page_meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Send candidate media URLs extracted by the Rust page scraper into Python
    yt-dlp so manifests (HLS/DASH) and streams are expanded, while preserving
    the page metadata (title, thumbnail, duration, referer) scraped by Rust.
    Candidates are ordered and the result ranked by downloadanything.filter —
    the single filtering system. Items probe in parallel (bounded) so a
    multi-video page doesn't serialize one round-trip per item."""
    sub_opts = msgspec.structs.replace(opts, referer=page_ref)
    ctx_title = _page_title(page_meta, opts, page_ref)
    page_thumbnail = opts.thumbnail or (page_meta or {}).get("thumbnail")
    page_duration = (page_meta or {}).get("duration_secs")
    source = ((page_meta or {}).get("title_source") or "").strip().lower()
    if source in LOW_TITLE_SOURCES and ctx_title:
        for item in raw_items:
            item["title"] = ctx_title
    workers = min(len(raw_items), 4) or 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(
            lambda ri: _probe_rust_item(
                sub_opts, ri, page_ref, ctx_title, page_thumbnail, page_duration
            ),
            raw_items,
        )
        return [item for item in results if item is not None]


def _rust_extract(opts: ExtractOptions, raw: str) -> dict[str, Any] | None:
    """Custom Rust page extractor (in-process via PyO3) → extracts candidate
    media URLs + page metadata from the webpage and sends those URLs to Python
    yt-dlp for full format/manifest probing."""
    try:
        from . import downloadanything_rs
    except Exception as e:
        log.warning(
            "downloadanything_rs module missing — run `bun run backend:bundle`; "
            "rust fallback skipped",
            error=e,
        )
        return None
    o = msgspec.to_builtins(opts)
    if cookie_ctx := _cookie_header_for_url(opts, raw):
        origin, cookie_hdr = cookie_ctx
        # msgspec.Struct doesn't validate at construction — a caller-side
        # contexts=None serializes to null and .append would crash on it.
        o["contexts"] = (o.get("contexts") or []) + [
            {"url": origin, "headers": [{"name": "Cookie", "value": cookie_hdr}]}
        ]
    try:
        out = orjson.loads(downloadanything_rs.extract(orjson.dumps(o).decode(), [raw]))
    except Exception as e:
        log.exception("rust extract failed", url=raw, error=e)
        return None
    page = out[0] if out else None
    if page is None:
        return None
    page["extractor"] = "rust"
    raw_items = page.get("items") or []
    if raw_items:
        page_ref = str(page.get("final_url") or opts.referer or raw)
        page["items"] = _probe_rust_items(opts, raw_items, page_ref, page)
        if not page["items"]:
            page["error"] = "no media found on page"
            page["error_code"] = CODE_NO_MEDIA
    log.info(
        "rust extract complete",
        url=raw,
        items=len(page.get("items") or []),
        error=page.get("error"),
    )
    return page


def _ctx_kind(c: Any) -> str | None:
    """Wire kind from the extension's capture classification — the only
    signal for an extensionless stream (`/stream/abc` served as video/mp4)."""
    kind = (c.kind or "").lower()
    if kind.startswith("hls"):
        return "hls"
    if kind == "dash":
        return "dash"
    return kind_from_mime(c.mime or "")


def _sniffed_extract(
    opts: ExtractOptions, raw: str, page_meta: dict[str, Any] | None = None
) -> list[dict[str, Any]] | None:
    """Extension-captured media URLs → candidate media links, classified and
    ordered by downloadanything.filter (manifests first — the extension's own
    capture order carries no meaning here). Rust's page metadata names the
    item when it found any; the extension title is only the last resort."""
    links: list[dict[str, Any]] = []
    seen = {raw}
    for c in opts.contexts:
        if not c.url or c.url in seen:
            continue
        seen.add(c.url)
        kind = _ctx_kind(c)
        if kind is None:
            kind = kind_of(c.url)
            if kind == "progressive" and container_of(c.url) is None:
                continue  # not media-looking — the extension captured a page/API URL
        link: dict[str, Any] = {"url": c.url, "kind": kind, "source": "direct"}
        if container := container_of(c.url) or container_from_mime(c.mime or ""):
            link["container"] = container
        quality: dict[str, Any] = {}
        if c.height:
            quality["height"] = int(c.height)
        if c.width:
            quality["width"] = int(c.width)
        if c.label:
            quality["label"] = c.label
        if quality:
            link["quality"] = quality
        if codecs := _join_codecs(c.codecs):
            link["codecs"] = codecs
        referer = next(
            (h.value for h in c.headers if h.name.lower() == "referer"),
            None,
        ) or opts.referer
        if referer:
            link["headers"] = {"referer": referer}
        links.append(link)
        log.info("sniffed fallback candidate", url=c.url)

    if not (ordered := order_candidates(links, limit=MAX_CANDIDATES)):
        return None
    meta = page_meta or {}
    item: dict[str, Any] = {
        "id": raw,
        "page_url": meta.get("final_url") or opts.referer or raw,
        "formats": ordered,
    }
    if title := _page_title(page_meta, opts, raw):
        item["title"] = title
    if thumbnail := opts.thumbnail or meta.get("thumbnail"):
        item["thumbnail"] = thumbnail
    if duration := meta.get("duration_secs"):
        item["duration_secs"] = float(duration)
    return [item]


def _sniffed_page(
    opts: ExtractOptions, raw: str, page_meta: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Extension-sniffed URLs probed through yt-dlp (or kept as direct links) —
    the last hop. Rust's page metadata, when present, is reused instead of
    the extension's DOM title/thumbnail."""
    if raw_items := _sniffed_extract(opts, raw, page_meta):
        page_ref = (page_meta or {}).get("final_url") or opts.referer or raw
        items = _probe_rust_items(opts, raw_items, page_ref, page_meta)
        if items:
            page: dict[str, Any] = {"input": raw, "items": items, "extractor": "extension"}
            if opts.referer:
                page["final_url"] = opts.referer
            return page
    return None


def _is_media_url(url: str) -> bool:
    """True for a direct file or manifest URL — media URLs are probed by
    yt-dlp directly, never treated as a page the Rust extractor must fetch."""
    return container_of(url) is not None or role(url) in ("master", "variant", "audio")


@functools.lru_cache(maxsize=512)
def _has_dedicated_extractor(url: str) -> bool:
    """Does yt-dlp have a site-specific extractor for this URL? Its GenericIE
    ('generic') does not count — that is the slow fallback the Rust extractor
    replaces. Unparseable URLs keep the yt-dlp-first order."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return True
    if not host:
        return True
    try:
        from yt_dlp.extractor import gen_extractor_classes
    except Exception:
        return True  # unknown yt-dlp layout — keep the yt-dlp-first order
    for ie in gen_extractor_classes():
        if ie.IE_NAME == "generic":
            continue
        try:
            if ie.suitable(url):
                return True
        except Exception:
            continue
    return False


@functools.lru_cache(maxsize=512)
def _claiming_keys(url: str) -> frozenset[str]:
    """Dedicated extractor keys claiming this URL — a "watch?v=…&list=…"
    page is claimed by the tab/playlist IE while the plain video URL it
    resolves to is claimed by the video IE. That difference marks a URL
    whose video pass suppressed a listing worth probing."""
    try:
        from yt_dlp.extractor import gen_extractor_classes
    except Exception:
        return frozenset()
    keys = set()
    for ie in gen_extractor_classes():
        if ie.IE_NAME == "generic":
            continue
        try:
            if ie.suitable(url):
                keys.add(ie.ie_key())
        except Exception:
            continue
    return frozenset(keys)


async def _flat_listing_probe(
    opts: ExtractOptions, raw: str, page: dict[str, Any]
) -> dict[str, Any] | None:
    """Recover a playlist the video pass suppressed. Two cases pay for a
    second extraction: a "video AND playlist" URL resolved to its video
    (the claiming IE set differs from the resolved URL's — watch?v+list is
    a tab-IE URL, the video a video-IE one), or the pass found nothing on a
    URL a dedicated extractor claims (a dead video inside a live list)."""
    # Iterating every yt-dlp extractor is CPU work — keep it off the loop.
    claims = await asyncio.to_thread(_claiming_keys, raw)
    if not claims:
        return None
    if has_media(page):
        resolved = page.get("final_url") or raw
        if not (claims - await asyncio.to_thread(_claiming_keys, resolved)):
            return None
    pl_opts = msgspec.structs.replace(opts, playlist=True)
    listing = await asyncio.to_thread(_ytdlp_extract, pl_opts, raw)
    pl = listing.get("playlist")
    if pl:
        log.info("playlist listing", url=raw, items=pl.get("count"))
    return pl


async def _extract_one(opts: ExtractOptions, raw: str) -> dict[str, Any]:
    """The probe chain:
    - auto: Python yt-dlp first → Rust page extractor (feeding extracted URLs
      into yt-dlp) → extension-sniffed URLs. With `ytdlp_generic=False`,
      pages without a dedicated yt-dlp extractor skip the generic pass and
      go straight to the Rust extractor.
    - python_ytdlp: Python yt-dlp → extension-sniffed URLs.
    - custom: Rust page extractor (feeding extracted URLs into yt-dlp) →
      extension-sniffed URLs.

    The extension hop receives the Rust page dict, so it reuses Rust's
    title/thumbnail/duration and only falls back to its own DOM title when
    Rust found none."""
    engine = (opts.extractor_engine or "auto").lower()

    if engine == "custom":
        page = await asyncio.to_thread(_rust_extract, opts, raw)
        if has_media(page):
            return page
        return (
            await asyncio.to_thread(_sniffed_page, opts, raw, page)
            or page
            or {"input": raw, "items": [], "extractor": "rust"}
        )

    if engine == "python_ytdlp":
        page = await asyncio.to_thread(_ytdlp_extract, opts, raw)
        if has_media(page) or page.get("playlist"):
            return page
        return await asyncio.to_thread(_sniffed_page, opts, raw) or page

    # auto with the generic page pass disabled: only dedicated yt-dlp
    # extractors run first. Generic-only pages are the Rust extractor's job —
    # its extracted URLs are still probed through yt-dlp for format ladders.
    if (
        opts.ytdlp_generic is False
        and not _is_media_url(raw)
        and not await asyncio.to_thread(_has_dedicated_extractor, raw)
    ):
        rust = await asyncio.to_thread(_rust_extract, opts, raw)
        if has_media(rust):
            return rust
        if rust and rust.get("items"):
            return rust
        return (
            await asyncio.to_thread(_sniffed_page, opts, raw, rust)
            or rust
            or {"input": raw, "items": [], "extractor": "rust"}
        )

    # auto: Python yt-dlp first; Rust custom extractor fallback on failure/empty.
    # A host whose dedicated extractor is broken upstream skips the pass.
    page = None if _ie_broken(raw) else await asyncio.to_thread(_ytdlp_extract, opts, raw)
    if has_media(page) or (page or {}).get("playlist"):
        return page
    rust = await asyncio.to_thread(_rust_extract, opts, raw)
    if has_media(rust):
        return rust
    if rust and rust.get("items"):
        return rust
    return (
        await asyncio.to_thread(_sniffed_page, opts, raw, rust)
        or page
        or rust
        or {
            "input": raw,
            "items": [],
            "error": "extraction failed",
            "error_code": CODE_FAIL,
        }
    )


# Extract results are cached briefly: re-opening the probe sheet or retrying
# after tweaking options re-pays the full yt-dlp network probe otherwise.
# Signed format URLs outlive this TTL by hours — failures are never cached.
_result_cache: TTLCache[tuple[Any, ...], dict[str, Any]] = TTLCache(
    _RESULT_CACHE_MAX, _RESULT_CACHE_TTL
)

# Successful probes' info dicts — the download worker feeds them to
# ydl.process_ie_result instead of re-extracting the page (a second
# webpage+player+JS-challenge+cookie round-trip per download otherwise).
# 30 minutes: signed format URLs outlive it by hours, and a failed replay
# drops the entry and extracts fresh.
_info_cache: TTLCache[tuple[Any, ...], dict[str, Any]] = TTLCache(_INFO_CACHE_MAX, _INFO_CACHE_TTL)


def _ctx_key(opts: ExtractOptions) -> tuple[Any, ...]:
    """Contexts fingerprint — session cookies change what a page yields."""
    return tuple(
        (c.url, c.title, tuple((h.name, h.value) for h in c.headers)) for c in opts.contexts
    )


def _opts_key(opts: ExtractOptions) -> tuple[Any, ...]:
    """Cache identity of an options struct — contexts included (session
    cookies change what a page yields), so a fresh push with new captures
    still gets a fresh probe."""
    return (
        opts.extractor_engine,
        opts.ytdlp_generic,
        opts.cookies_file,
        opts.cookies_browser,
        opts.referer,
        opts.title,
        opts.thumbnail,
        opts.user_agent,
        opts.proxy,
        opts.timeout_secs,
        opts.retries,
        opts.rate_limit_rps,
        opts.max_items,
        opts.player_client,
        opts.check_certificate,
        opts.playlist,
        _ctx_key(opts),
    )


def _info_sig(opts: ExtractOptions) -> tuple[Any, ...]:
    """Extraction identity for download reuse — only what changes the media
    a page yields AND survives into the download request: session sources
    (cookie file/browser, captured contexts), egress (proxy, UA), and the
    player-client pick. Deliberately excludes referer (the download
    legitimately carries the picked row's own referer), the engine, and
    probe-only knobs (timeouts, retries, item caps)."""
    return (
        opts.cookies_file,
        opts.cookies_browser,
        opts.proxy,
        opts.user_agent,
        opts.player_client,
        opts.check_certificate,
        opts.playlist,
        _ctx_key(opts),
    )


def _register_info(
    opts: ExtractOptions,
    raw: str,
    info: dict[str, Any],
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
) -> None:
    """Index a successful probe's info dict under every URL a download can
    arrive with: the input URL, the canonical webpage_url (a repr row's task
    URL is the item's page_url), plus each playlist entry's own page URL —
    per-entry dicts, so an entry add downloads that video, not the list."""
    sig = _info_sig(opts)
    for k in (raw, info.get("webpage_url"), info.get("original_url")):
        if k:
            _info_cache.insert((k, sig), info)
    for e, _ in pairs:
        for k in (e.get("webpage_url"), e.get("original_url")):
            if k:
                _info_cache.insert((k, sig), e)


def cached_info(opts: ExtractOptions, url: str) -> dict[str, Any] | None:
    """A copy of the probed info dict for `url` under matching session
    options — process_ie_result mutates its input, so callers get a private
    copy and the entry stays reusable for another format pick. sanitize_info
    output is JSON-safe, so an orjson round-trip is the copy (~10x deepcopy)."""
    key = (url, _info_sig(opts))
    hit = _info_cache.get(key)
    if hit is None:
        return None
    try:
        return orjson.loads(orjson.dumps(hit))
    except Exception:
        _info_cache.pop(key, None)
        return None


def drop_cached_info(opts: ExtractOptions, url: str) -> None:
    """Drop the entry and every alias it was registered under — one stale
    signed URL poisons all of them, not just the key that was hit. The same
    info object is inserted under each alias, so identity finds them all;
    this only runs on the rare stale-replay path."""
    hit = _info_cache.pop((url, _info_sig(opts)), None)
    if hit is None:
        return
    doomed = [k for k, v in _info_cache.items() if v is hit]
    for k in doomed:
        _info_cache.pop(k, None)


# In-flight extractions keyed like _result_cache — two callers probing the
# same URL at once share one network round-trip (the UI dedupes per input
# box, not per backend call).
_inflight: dict[tuple[Any, ...], asyncio.Task[dict[str, Any]]] = {}


async def extract(opts: ExtractOptions, urls: list[str]) -> list[dict[str, Any]]:
    """Extract URLs in parallel (bounded, order-preserving); per-URL failures
    are carried in the result, not thrown."""
    sem = asyncio.Semaphore(_clamp(opts.extract_concurrency, 8))
    sig = _opts_key(opts)

    async def run(u: str, key: tuple[Any, ...]) -> dict[str, Any]:
        async with sem:
            try:
                page = await _extract_one(opts, u)
            except Exception as e:
                # A bad URL or a broken extractor must not sink the batch.
                log.exception("extract failed", url=u)
                return {
                    "input": u,
                    "items": [],
                    "error": clean_text(str(e)),
                    "error_code": CODE_FAIL,
                }
            if not page.get("playlist"):
                try:
                    if pl := await _flat_listing_probe(opts, u, page):
                        page["playlist"] = pl
                except Exception:
                    # The listing is additive — a failure must not sink a
                    # perfectly good video result.
                    log.warning("playlist listing probe failed", url=u)
        if has_media(page) or page.get("playlist"):
            _result_cache.insert(key, page)
        return page

    async def one(u: str) -> dict[str, Any]:
        key = (u, sig)
        if (hit := _result_cache.get(key)) is not None:
            return hit
        task = _inflight.get(key)
        if task is None:
            task = asyncio.create_task(run(u, key))
            _inflight[key] = task
            task.add_done_callback(lambda _: _inflight.pop(key, None))
        # Shield: awaiting a shared Task propagates cancellation into it —
        # one caller dropping mid-probe must not cancel the extraction for
        # every other waiter on the same URL.
        return await asyncio.shield(task)

    return list(await asyncio.gather(*(one(u) for u in urls)))
