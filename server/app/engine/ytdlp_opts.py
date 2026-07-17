"""Shared, explicit yt-dlp options for probing and downloading."""

from typing import Any, Dict, Optional


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
        "quiet": True,
        "no_warnings": True,
        "ignoreconfig": True,
        "js_runtimes": {"node": {}, "bun": {}},
        "allow_unplayable_formats": True,
        "allow_multiple_audio_streams": True,
        "allow_multiple_video_streams": True,
        "ignore_no_formats_error": False,
        "age_limit": None,
        "geo_bypass": False,
        "nocheckcertificate": True,
        "legacyserverconnect": True,
        "check_formats": "cached",
        "retries": 10,
        "fragment_retries": 10,
        "skip_unavailable_fragments": True,
        "compat_options": ["allow-unsafe-ext"],
        "extractor_args": {
            "youtube": {
                "skip": ["translated_subs"],
            },
        },
    }
    # Bypasses basic bot protections
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
