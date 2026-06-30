"""
title_extractor.py — Title and filename sanitization pipeline.
"""
import os
from urllib.parse import urlparse
from engine.constants import GENERIC_STREAM_NAMES


def sanitise_title(
    raw: str,
    url: str,
    page_title: str | None = None,
    *,
    prefer_page: bool = False,
) -> str:
    stripped = raw.strip()
    
    # Extract the base stem to check against generic names (e.g. "master.m3u8" -> "master")
    name_part = stripped.split("?")[0]
    stem_part = os.path.splitext(name_part)[0].strip()
    
    is_generic = (
        not stem_part 
        or stem_part.lower() in GENERIC_STREAM_NAMES 
        or stripped.lower() in GENERIC_STREAM_NAMES
    )

    if prefer_page and page_title and page_title.strip():
        return page_title.strip()
    if stripped and not is_generic:
        return stripped
    if page_title and page_title.strip():
        return page_title.strip()
    parsed = urlparse(url)
    host = parsed.hostname or ""
    stem = os.path.splitext(os.path.basename(parsed.path.rstrip("/")))[0]
    # Check if URL stem is also generic
    url_stem_part = stem.split("?")[0]
    url_stem = os.path.splitext(url_stem_part)[0].strip()
    url_is_generic = (
        not url_stem 
        or url_stem.lower() in GENERIC_STREAM_NAMES 
        or stem.lower() in GENERIC_STREAM_NAMES
    )
    if stem and not url_is_generic:
        return f"{host} – {stem}" if host else stem
    return host or stripped or "Stream"
