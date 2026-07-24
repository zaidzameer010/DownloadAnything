from __future__ import annotations

from typing import Any, Mapping, cast
from urllib.parse import urlparse

from yt_dlp.utils import DownloadError

from app.domain.exceptions import ProbeError

# Backwards-compatible alias for existing callers.
ProbeFailure = ProbeError


def _has_drm_signal(info: Mapping[str, Any]) -> bool:
    if any(
        bool(info.get(key))
        for key in ("drm", "has_drm", "drm_fairplay", "is_drm")
    ):
        return True

    raw_formats = info.get("formats")
    formats = (
        [cast(Mapping[str, Any], fmt) for fmt in raw_formats if isinstance(fmt, Mapping)]
        if isinstance(raw_formats, list)
        else []
    )
    for fmt in formats:
        if any(
            bool(fmt.get(key))
            for key in ("drm", "has_drm", "drm_fairplay", "is_drm")
        ):
            return True
        format_note = str(fmt.get("format_note") or "").lower()
        if "drm" in format_note or "encrypted" in format_note:
            return True
    return False


def _is_valid_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_probe_info(url: str, info: Mapping[str, Any]) -> None:
    """Reject probe results that cannot represent a verified downloadable asset."""
    if not _is_valid_url(url):
        raise ProbeError("invalid_url", "Only valid HTTP(S) URLs can be probed.")

    if _has_drm_signal(info):
        raise ProbeError(
            "drm_protected",
            "The detected media is DRM-protected and cannot be downloaded as clear media.",
            "drm_protected",
        )

    raw_formats = info.get("formats")
    formats = (
        [cast(Mapping[str, Any], fmt) for fmt in raw_formats if isinstance(fmt, Mapping)]
        if isinstance(raw_formats, list)
        else []
    )
    direct_url = info.get("url")
    if not formats:
        if not isinstance(direct_url, str) or not _is_valid_url(direct_url):
            raise ProbeError(
                "no_media_found",
                "No downloadable media formats were found at this URL.",
                "no_media_found",
            )

    valid_formats = [
        fmt
        for fmt in formats
        if isinstance(fmt.get("format_id"), str)
        and bool(fmt.get("url") or fmt.get("manifest_url") or fmt.get("protocol"))
    ]
    if raw_formats and not valid_formats and not direct_url:
        raise ProbeError(
            "no_media_found",
            "The probe returned metadata but no usable media resource.",
            "no_media_found",
        )


def classify_probe_exception(error: BaseException) -> ProbeError:
    """Convert yt-dlp failures into stable, user-facing categories."""
    if isinstance(error, ProbeError):
        return error

    message = str(error).strip() or "Media probing failed."
    lowered = message.lower()
    if "drm" in lowered or "encrypted" in lowered or "widevine" in lowered:
        return ProbeError("drm_protected", message, "drm_protected")
    if "login" in lowered or "sign in" in lowered or "authentication" in lowered:
        return ProbeError("authentication_required", message, "cookies_required")
    if "geo-restricted" in lowered or "not available in your country" in lowered:
        return ProbeError("geo_restricted", message, "geo_blocked")
    if "unsupported url" in lowered:
        return ProbeError("unsupported", message, "unsupported_url")
    if isinstance(error, DownloadError):
        return ProbeError("extractor_error", message)
    return ProbeError("probe_failed", message)

