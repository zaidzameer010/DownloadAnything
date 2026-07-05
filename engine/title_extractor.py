"""
title_extractor.py — Title and filename sanitization pipeline.
"""
import os
import re
from urllib.parse import urlparse
from engine.constants import GENERIC_STREAM_NAMES, TITLE_SUFFIXES


def _strip_site_suffixes(value: str) -> str:
    lowered = value.lower()
    for suffix in TITLE_SUFFIXES:
        dash_token = f" - {suffix}".lower()
        if lowered.endswith(dash_token):
            return value[: -len(dash_token)].strip()
        pipe_token = f" | {suffix}".lower()
        if lowered.endswith(pipe_token):
            return value[: -len(pipe_token)].strip()
    return re.sub(
        r"\s*[-|·•–—]\s*(YouTube|Vimeo|Twitch|Dailymotion|Twitter|X|Facebook|Instagram|TikTok|Reddit|Bilibili|Rumble|Odysee|PeerTube|Niconico|SoundCloud|Spotify|Netflix|Prime Video|Disney\+|Apple TV)\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()


def _strip_query_fragment(value: str) -> str:
    return value.split("?", 1)[0].split("#", 1)[0].strip()


def _clean_name(value: str) -> str:
    if not value:
        return ""
    text = _strip_query_fragment(value)
    text = os.path.basename(text)
    stem, ext = os.path.splitext(text)
    if ext and 2 <= len(ext.lstrip(".")) <= 5:
        text = stem or text
    return _strip_site_suffixes(text).strip()


def _name_token(value: str) -> str:
    cleaned = _clean_name(value)
    if not cleaned:
        return ""
    return os.path.splitext(os.path.basename(cleaned))[0].strip().lower()


def _is_generic_name(value: str) -> bool:
    token = _name_token(value)
    return not token or token in GENERIC_STREAM_NAMES


def sanitise_title(
    raw: str,
    url: str,
    page_title: str | None = None,
    *,
    prefer_page: bool = False,
) -> str:
    stripped = _clean_name(raw)

    # Extract the base stem to check against generic names (e.g. "master.m3u8" -> "master")
    is_generic = _is_generic_name(stripped)

    if prefer_page and page_title and page_title.strip():
        cleaned_page = _clean_name(page_title)
        return cleaned_page or page_title.strip()
    if stripped and not is_generic:
        return stripped
    if page_title and page_title.strip():
        cleaned_page = _clean_name(page_title)
        if cleaned_page and not _is_generic_name(cleaned_page):
            return cleaned_page
    parsed = urlparse(url)
    host = parsed.hostname or ""
    stem = _clean_name(os.path.basename(parsed.path.rstrip("/")))
    url_is_generic = _is_generic_name(stem)
    if stem and not url_is_generic:
        return f"{host} – {stem}" if host else stem
    return host or stripped or "Stream"
