"""Shared, explicit yt-dlp options for probing and downloading."""

from typing import Any, Dict, Optional
import re

from app.utils.http import DEFAULT_USER_AGENT



def build_ytdlp_options(
    *,
    url: Optional[str] = None,
    browser: Optional[str] = None,
    referer: Optional[str] = None,
    ffmpeg_location: Optional[str] = None,
    extra_opts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the common per-operation yt-dlp configuration."""
    opts: Dict[str, Any] = {
        "quiet": False,
        "no_warnings": False,
        "ignoreconfig": True,
        "js_runtimes": {"node": {}, "bun": {}},
        "allow_unplayable_formats": False,
        "allow_multiple_audio_streams": False,
        "allow_multiple_video_streams": False,
        "noplaylist": True,
        "ignore_no_formats_error": False,
        "age_limit": None,
        "geo_bypass": False,
        "nocheckcertificate": False,
        "legacyserverconnect": False,
        "check_formats": "cached",
        "retries": 10,
        "fragment_retries": 10,
        "skip_unavailable_fragments": True,
        "compat_options": ["allow-unsafe-ext", "manifest-filesize-approx"],
        "extractor_args": {
            "youtube": {
                "skip": ["translated_subs"],
            },
        },
        # yt-dlp blocks some previously-supported domains in KnownPiracyIE and
        # KnownDRMIE. Remove those extractors so the generic extractor can still
        # handle the direct media URLs the extension captures.
        "allowed_extractors": ["default", "-Piracy", "-DRM"],
    }
    # Bypasses basic bot protections
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # Resolve Origin dynamically to satisfy site CORS / Origin checks
    origin_source = referer or url
    if origin_source:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(origin_source)
            if parsed.scheme and parsed.netloc:
                headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            pass

    opts.setdefault("http_headers", {}).update(headers)

    if browser and browser.lower() not in ("none", ""):
        opts["cookiesfrombrowser"] = (browser.lower(),)

    if referer and referer.strip():
        opts.setdefault("http_headers", {})["Referer"] = referer.strip()

    if ffmpeg_location and ffmpeg_location.strip():
        opts["ffmpeg_location"] = ffmpeg_location.strip()

    if extra_opts:
        opts.update(extra_opts)

    return opts


# yt-dlp blocks some previously-supported sites in ``KnownPiracyIE`` with a
# hard ``[Piracy]`` error before any network request. When we hit that block,
# fall back to the generic extractor; the caller already passed the real media
# URL or the extension captured the stream directly.
_PIRACY_ERROR_RE = re.compile(r"\[\s*Piracy\s*\]", re.IGNORECASE)


def is_piracy_block_error(exc: BaseException) -> bool:
    """Return True when yt-dlp rejected a URL because it is in KnownPiracyIE."""
    if not exc:
        return False
    return bool(_PIRACY_ERROR_RE.search(str(exc)))
