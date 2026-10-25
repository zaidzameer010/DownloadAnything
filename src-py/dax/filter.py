"""Single filtering and ranking system for extracted media.

One taxonomy drives every drop and sort decision in the pipeline. The Rust
extractor and the browser extension only discover candidate URLs — what
survives and in which order is decided here, for both stages:

    order_candidates()  candidate URLs → the order yt-dlp probes them in
    rank_formats()      wire formats   → the final rows the UI shows

Roles, best first:

    master       multivariant manifest: .m3u8 whose stem names a master
                 (master/manifest/multivariant/multi/playlist) or that
                 carries a multi= query, plus any .mpd. One probe resolves
                 the full variant ladder — audio renditions included.
    progressive  direct video file (mp4/webm/mkv/mov/m4v/...): complete in
                 one request, no manifest.
    variant      bare HLS playlist (index-v1.m3u8, chunklist_*.m3u8): one
                 rendition, often video-only.
    audio        audio-only file (mp3/m4a/aac/opus/flac/wav).
    soft junk    low-res (≤144p) and short clips (<10s) — kept last so a
                 page whose only media is a clip still downloads.

Dropped outright: previews, thumbnails, trailers, ads, sprites, image
files, stream segments (.ts/.m4s/init chunks), sample clips and DRM rows.

Within a role: height desc, AV1 first, bandwidth desc, VOD before live,
known size before unknown. The final list keeps one row per (audio,
height) slot — the best role wins the slot — and a resolved master
supersedes bare variants from its own directory.
"""

import re
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

MAX_FORMATS = 12

# Candidate list cap for probing — mirrors the format cap.
MAX_CANDIDATES = 12

TIER_MASTER = 0
TIER_PROGRESSIVE = 1

_ROLE_TIER = {
    "master": TIER_MASTER,
    "progressive": TIER_PROGRESSIVE,
    "variant": 2,
    "audio": 3,
}

VIDEO_EXTS = frozenset({"mp4", "webm", "mkv", "mov", "m4v", "ts"})
AUDIO_EXTS = frozenset({"mp3", "m4a", "aac", "opus", "flac", "wav"})
# Containers we intentionally no longer discover or rank. Kept explicit so
# the extensionless fallback in role() cannot reclassify them as progressive.
RETIRED_EXTS = frozenset(
    {"ogg", "oga", "avi", "3gp", "wmv", "flv", "mpg", "mpeg", "wma", "aiff", "m2ts", "alac"}
)
IMAGE_EXTS = frozenset(
    {"jpg", "jpeg", "png", "webp", "gif", "avif", "bmp", "svg", "ico", "tif", "tiff"}
)
HLS_EXTS = frozenset({"m3u8", "m3u"})
DASH_EXTS = frozenset({"mpd"})

# Manifest stems that name a multivariant playlist (KVS master.m3u8, Wowza
# playlist.m3u8, ...). "index-v1.m3u8"-style rendition names stay variants.
_MASTER_STEMS = ("master", "manifest", "multivariant", "multi", "playlist")
_MULTI_QUERY = re.compile(r"(?:^|&)multi=", re.I)
_MANIFEST_QUERY = ("format=m3u8", "format=mpd", "type=m3u8", "type=mpd")

# Junk tokens, matched against whole path-segment words so "road.mp4" never
# trips the "ad" rule. Prefixes catch numbered/decorated compounds:
# preview2, trailer-hd, thumb_01 — "sample"/"promo" stay exact (sampler,
# promoter).
_JUNK_EXACT = {
    "poster": "thumb",
    "screenshot": "thumb",
    "brandmark": "thumb",
    "promo": "trailer",
    "sample": "sample",
    "fb": "preview",
    "ad": "ad",
    "ads": "ad",
    "advert": "ad",
    "advertisement": "ad",
    "commercial": "ad",
    "preroll": "ad",
    "midroll": "ad",
    "postroll": "ad",
    "bumper": "ad",
    "adbreak": "ad",
    "sprite": "sprite",
}
_JUNK_PREFIX = (
    ("preview", "preview"),
    ("thumb", "thumb"),
    ("trailer", "trailer"),
    ("teaser", "trailer"),
    ("storyboard", "sprite"),
    ("logo", "thumb"),
    ("watermark", "thumb"),
)
_ROLE_KEYS = ("type", "role", "kind", "mode", "content")
_HARD_JUNK = frozenset({"preview", "thumb", "trailer", "ad", "sprite"})
_SEGMENT_STEMS = ("seg", "segment", "chunk", "init", "frag", "part")
_ASSET_TOKENS = (
    "logo",
    "watermark",
    "brandmark",
    "favicon",
    "avatar",
    "user_pic",
    "user-pic",
    "user_avatar",
    "profile_pic",
    "author",
    "spinner",
    "loader",
    "loading",
    "spacer",
    "blank",
    "placeholder",
    "default_avatar",
    "default_thumb",
    "nothumb",
    "no_thumb",
    "no-thumb",
    "no_image",
    "no-image",
    "pixel",
    "tracking",
    "analytics",
    "badge",
    "header-logo",
    "site-logo",
    "apple-touch-icon",
    "play_button",
    "play_btn",
    "play-btn",
    "btn_play",
    "button",
    "btn",
    "ad-",
    "advert",
    "sponsor",
    "banner",
)
_JUNK_IMG_EXTS = (".svg", ".ico", ".cur")
_JUNK_DIMS = ("1x1", "16x16", "32x32", "48x48", "64x64")
# One regex pass instead of one substring scan per token — site_asset() runs
# for every candidate thumbnail on a page.
_ASSET_RE = re.compile("|".join(map(re.escape, _ASSET_TOKENS)))
_JUNK_DIM_RE = re.compile("|".join(map(re.escape, _JUNK_DIMS)))


def _parts(url: str) -> tuple[str, str, str, str]:
    """(path, query, last-segment stem, extension) — lowercased stem/ext.
    Empty trailing segments (KVS-style `.../file.mp4/` URLs) are skipped."""
    p = urlsplit(url)
    seg = next((s for s in reversed(p.path.split("/")) if s), "")
    stem, dot, ext = seg.rpartition(".")
    if not dot:
        stem, ext = seg, ""
    return p.path, p.query, stem.lower(), ext.lower()


def _is_master_stem(stem: str, query: str) -> bool:
    return any(tok in stem for tok in _MASTER_STEMS) or bool(_MULTI_QUERY.search(query))


def _is_segment(stem: str, ext: str) -> bool:
    """Stream parts, never standalone media. `init.<ext>` is the DASH
    initialization chunk; `.ts` is a segment only when segment-named
    (numbered stems, seg/segment/chunk prefixes) since raw .ts downloads
    exist."""
    if stem == "init":
        return True
    if ext != "ts":
        return False
    numbered = bool(stem) and all(c.isdigit() or c in "_-" for c in stem)
    return numbered or stem.startswith(_SEGMENT_STEMS)


def role(url: str, kind: str | None = None) -> str | None:
    """Classify a URL into the media taxonomy; None → never media (images,
    non-http, data:)."""
    if not url.startswith(("http://", "https://")):
        return None
    path, query, stem, ext = _parts(url)
    if ext in RETIRED_EXTS:
        return None
    if ext in IMAGE_EXTS or ext == "m4s":
        return None
    # multi=360p,720p,... — one URL serving the whole ladder outranks
    # everything its own extension suggests.
    if _MULTI_QUERY.search(query):
        return "master"
    if ext in HLS_EXTS:
        return "master" if _is_master_stem(stem, query) else "variant"
    if ext in DASH_EXTS:
        return "master"
    if ext in VIDEO_EXTS:
        return None if _is_segment(stem, ext) else "progressive"
    if ext in AUDIO_EXTS:
        return None if _is_segment(stem, ext) else "audio"
    if ext in _SCRIPT_EXTS:
        # A script path serves its media from the query — `/master.php` is
        # not a manifest.
        return {"hls": "variant", "dash": "master", "audio": "audio"}.get(kind or "", "progressive")
    # Extensionless: manifest-ish query/path tokens, then the caller's hint.
    low = f"{path}?{query}".lower()
    if (
        any(tok in low for tok in _MANIFEST_QUERY)
        or "/manifest" in low
        or "/master." in low
        or "/playlist." in low
        or "/hls/" in low
    ):
        return "master"
    return {"hls": "variant", "dash": "master", "audio": "audio"}.get(kind or "", "progressive")


def kind_of(url: str, kind_hint: str | None = None) -> str:
    """Wire kind ('hls' | 'dash' | 'audio' | 'progressive') for a URL."""
    _, query, _, ext = _parts(url)
    # Case-insensitive like role() — signed manifest queries may be uppercase.
    q = query.lower()
    if ext in DASH_EXTS or "format=mpd" in q or "type=mpd" in q:
        return "dash"
    if ext in HLS_EXTS or any(tok in q for tok in ("format=m3u8", "type=m3u8")):
        return "hls"
    if ext in AUDIO_EXTS:
        return "audio"
    if kind_hint:
        return kind_hint
    # Extensionless signed manifests (`/media/hls/?s=…`) carry no ext and no
    # hint — role() already recognizes them as masters.
    if role(url) == "master":
        return "hls"
    return "progressive"


# Script paths that serve a media file named in the query (KVS remote_control.php).
_SCRIPT_EXTS = frozenset({"php", "asp", "aspx", "jsp", "cgi", "pl", "html", "htm"})

_MIME_CONTAINERS = {
    "mp4": "mp4",
    "x-m4v": "mp4",
    "webm": "webm",
    "x-matroska": "mkv",
    "matroska": "mkv",
    "quicktime": "mov",
    "mp2t": "ts",
    "aac": "aac",
    "opus": "opus",
    "flac": "flac",
    "x-flac": "flac",
    "wav": "wav",
    "x-wav": "wav",
}


def container_from_mime(mime: str) -> str | None:
    """File container named by a mime type ("video/mp4" → "mp4")."""
    head = (mime or "").split(";", 1)[0].strip().lower()
    _, _, sub = head.partition("/")
    if not sub:
        return None
    if sub == "mpeg":
        return "mp3" if head.startswith("audio/") else None
    return _MIME_CONTAINERS.get(sub)


def kind_from_mime(mime: str) -> str | None:
    """Wire media kind named by a mime type (None for non-media types)."""
    head = (mime or "").split(";", 1)[0].strip().lower()
    if "mpegurl" in head or "vnd.apple" in head:
        return "hls"
    if "dash+xml" in head:
        return "dash"
    if head.startswith("audio/"):
        return "audio"
    if head.startswith("video/"):
        return "progressive"
    return None


def query_media_ext(query: str) -> str | None:
    """Media container ext hidden in query values — KVS serves the real
    file name as `remote_control.php?file=…_1440p.mp4&acctoken=…`."""
    for _, value in parse_qsl(query, keep_blank_values=True):
        _, dot, ext = unquote(value).rpartition(".")
        if dot and ext.lower() in VIDEO_EXTS | AUDIO_EXTS:
            return ext.lower()
    return None


def container_of(url: str) -> str | None:
    """File extension when the URL names a plain file — manifests and
    extensionless stream URLs carry no container. KVS script paths hide the
    real name in the query (`remote_control.php?file=…_1440p.mp4`)."""
    _, query, _, ext = _parts(url)
    if ext in VIDEO_EXTS or ext in AUDIO_EXTS:
        return ext
    if ext not in _SCRIPT_EXTS:
        return None
    return query_media_ext(query)


def junk(url: str) -> str | None:
    """Junk kind carried by the URL itself — previews, thumbnails, trailers,
    ads, sprites, samples. None when the URL carries no junk signal."""
    path, query, _, _ = _parts(url)
    for seg in path.split("/"):
        low = seg.lower()
        # imgproxy-style transform ops (`vts:434`, `rs:fit:320:180`, `ex:1:no`,
        # `bg:0:0:0`) — derived preview frames/strips, never the source media.
        if re.match(r"^[a-z]{2,6}:", low):
            return "sprite"
        for tok in re.split(r"[^a-z0-9]+", low):
            hit = _JUNK_EXACT.get(tok) or next(
                (j for p, j in _JUNK_PREFIX if tok.startswith(p)), None
            )
            if hit:
                return hit
    for key, value in parse_qsl(query, keep_blank_values=True):
        key = key.lower()
        if key == "preview" and value.lower() not in ("", "0", "false", "no", "off"):
            return "preview"
        if key in _ROLE_KEYS:
            hit = _JUNK_EXACT.get(value.lower())
            if hit:
                return hit
    return None


def site_asset(url: str) -> bool:
    """True when the URL looks like a site logo, watermark, avatar, icon, tracker,
    or banner — never content art, so it must not become an item thumbnail."""
    raw = url.strip()
    if not raw:
        return True
    lower = raw.lower()
    if raw.startswith("data:"):
        return "svg" in lower or "gif" in lower or len(raw) < 128

    path = urlsplit(raw).path.lower()
    if path.endswith(_JUNK_IMG_EXTS):
        return True

    return bool(_ASSET_RE.search(lower) or _JUNK_DIM_RE.search(lower))


def is_downloadable(link: dict[str, Any]) -> bool:
    """A row yt-dlp can actually fetch: http(s), not a DRM sink."""
    url = str(link.get("url") or link.get("manifest_url") or "")
    if not url.startswith(("http://", "https://")):
        return False
    drm = link.get("drm") or {}
    return drm.get("type") not in ("widevine", "play_ready", "fair_play", "unknown")


def has_media(page: dict[str, Any] | None) -> bool:
    """Does an extractor page carry at least one downloadable row?"""
    if not page:
        return False
    return any(
        is_downloadable(f) for item in page.get("items") or [] for f in item.get("formats") or []
    )


def _path_key(url: str) -> str:
    return urlsplit(url)._replace(query="", fragment="").geturl()


def _dedup_key(url: str) -> str:
    """URL identity: query params order-insensitive."""
    p = urlsplit(url)
    pairs = sorted(f"{k}={v}" for k, v in parse_qsl(p.query, keep_blank_values=True))
    return p._replace(query="&".join(pairs), fragment="").geturl()


def _dir(url: str) -> str:
    p = urlsplit(url)
    return f"{p.netloc}{p.path.rsplit('/', 1)[0]}"


def _richness(link: dict[str, Any]) -> tuple[int, int, bool, int]:
    q = link.get("quality") or {}
    return (
        int(q.get("height") or 0),
        int(q.get("bandwidth") or 0),
        bool(q.get("label")),
        int(link.get("probed_size") or 0),
    )


def _merge_missing(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """Fill the winner's gaps from the duplicate it beat."""
    qd = dst.setdefault("quality", {})
    qs = src.get("quality") or {}
    for k in ("height", "width", "bandwidth", "label"):
        if qd.get(k) is None and qs.get(k) is not None:
            qd[k] = qs[k]
    for k in ("container", "codecs", "probed_size", "repr_id"):
        if dst.get(k) is None and src.get(k) is not None:
            dst[k] = src[k]


def _dedupe_links(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per URL, keeping the richest quality metadata. The same URL
    surfaces from a sources array, a sibling JSON key and a raw script."""
    by_url: dict[str, dict] = {}
    order: list[str] = []
    for link in links:
        key = _dedup_key(str(link.get("url") or ""))
        cur = by_url.get(key)
        if cur is None:
            by_url[key] = link
            order.append(key)
        elif _richness(link) > _richness(cur):
            _merge_missing(link, cur)
            by_url[key] = link
        else:
            _merge_missing(cur, link)
    return [by_url[k] for k in order]


def _is_media(link: dict[str, Any]) -> bool:
    """Not an image/segment/non-http URL and not hard junk."""
    url = str(link.get("url") or "")
    return role(url, link.get("kind")) is not None and junk(url) not in _HARD_JUNK


def _tier(link: dict[str, Any]) -> int:
    """Role tier of a row — the manifest it resolved from wins over its own
    URL: a variant row of a master ladder belongs to the master."""
    src = str(link.get("manifest_url") or link.get("url") or "")
    return _ROLE_TIER.get(role(src, link.get("kind")) or "", TIER_PROGRESSIVE)


def _height(link: dict[str, Any]) -> int:
    return int((link.get("quality") or {}).get("height") or 0)


def _bandwidth(link: dict[str, Any]) -> int:
    return int((link.get("quality") or {}).get("bandwidth") or 0)


def _is_audio(link: dict[str, Any]) -> bool:
    return link.get("kind") == "audio"


def _soft(link: dict[str, Any], duration: float | None) -> bool:
    """Low-res or short clip — kept, but ranked last."""
    return (0 < _height(link) <= 144) or (duration is not None and duration < 10)


# Ordering height of a row whose own quality is unknown: a master stands
# for the whole ladder, so it heads the list.
_TOP_HEIGHT = 10_000


def _order_height(link: dict[str, Any]) -> int:
    return _height(link) or (_TOP_HEIGHT if _tier(link) == TIER_MASTER else 0)


def _codec_pref(link: dict[str, Any]) -> int:
    # `codecs` joins video + audio ("vp09.00.10.08, opus"); only the video
    # codec decides compatibility — an Opus audio track must not demote a
    # VP9 row to the HEVC tier.
    vcodec = str(link.get("codecs") or "").lower().split(",", 1)[0]
    if "av01" in vcodec or "av1" in vcodec:
        return 0
    if any(x in vcodec for x in ("hvc1", "hev1", "hevc")):
        return 2
    return 1


def order_candidates(
    links: list[dict[str, Any]], limit: int = MAX_CANDIDATES
) -> list[dict[str, Any]]:
    """Deduped, junk-free, best-first candidate list for yt-dlp probing.
    Masters lead so one probe resolves the full ladder; soft junk trails.
    Tokenized URLs beat their bare siblings — signed CDNs 403 without the
    query."""
    media = [link for link in links if _is_media(link)]
    media = _dedupe_links(media)
    tokened = {
        _path_key(str(link.get("url") or ""))
        for link in media
        if urlsplit(str(link.get("url") or "")).query
    }
    out: list[dict[str, Any]] = []
    for link in media:
        url = str(link.get("url") or "")
        if not urlsplit(url).query and _path_key(url) in tokened:
            continue
        out.append(link)
    out.sort(
        key=lambda link: (
            _tier(link),
            _soft(link, None),
            -_height(link),
            -_bandwidth(link),
        )
    )
    return out[:limit]


def _bare_variant(link: dict[str, Any]) -> bool:
    """A raw variant row — a playlist that resolved no manifest itself."""
    return (
        not link.get("manifest_url")
        and role(str(link.get("url") or ""), link.get("kind")) == "variant"
    )


def _supersede(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A resolved master supersedes bare variant rows from its directory —
    the ladder already carries them with better metadata. Unresolved
    masters supersede nothing: a dead master keeps its variant fallback."""
    resolved = {
        _dir(str(row["manifest_url"]))
        for row in rows
        if row.get("manifest_url") and role(str(row["manifest_url"])) == "master"
    }
    if not resolved:
        return rows
    return [
        row
        for row in rows
        if not (_bare_variant(row) and _dir(str(row.get("url") or "")) in resolved)
    ]


def _slot(link: dict[str, Any]) -> int | str:
    """Quality slot a row competes for — one winner per slot in the final
    list. Height-less rows stay distinct by bandwidth (or URL)."""
    if h := _height(link):
        return h
    return _bandwidth(link) or str(link.get("url") or "")


def _storyboard(link: dict[str, Any]) -> bool:
    return link.get("container") in ("mhtml", "json") or str(link.get("repr_id") or "").startswith(
        "sb"
    )


def rank_formats(
    links: list[dict[str, Any]],
    duration: float | None = None,
    limit: int = MAX_FORMATS,
) -> list[dict[str, Any]]:
    """The final format list: junk and DRM dropped, one row per quality
    slot — best role wins — ordered best quality first, soft junk last.
    Audio-only rows skip the slot race: the single highest-quality audio
    track is pinned at the bottom, below even soft junk."""
    rows = [
        link
        for link in links
        if _is_media(link) and is_downloadable(link) and not _storyboard(link)
    ]
    rows = _dedupe_links(rows)
    auds = [link for link in rows if _is_audio(link)]
    rows = [link for link in rows if not _is_audio(link)]
    # Anonymous progressive rows — a bare file URL with no height or label
    # to name it — drop once the list has an identified format to offer.
    # A page whose formats are all anonymous keeps them (it has nothing
    # better); manifests always carry meaning, labeled or not.
    if any(_height(link) or (link.get("quality") or {}).get("label") for link in rows):
        rows = [
            link
            for link in rows
            if link.get("kind") in ("hls", "dash")
            or _height(link)
            or (link.get("quality") or {}).get("label")
        ]
    rows = _supersede(rows)
    rows.sort(
        key=lambda link: (
            _soft(link, duration),
            _tier(link),
            _codec_pref(link),
            -_height(link),
            -_bandwidth(link),
            bool(link.get("is_live")),
            link.get("probed_size") is None,
        )
    )
    out: list[dict[str, Any]] = []
    seen: set[int | str] = set()
    for link in rows:
        slot = _slot(link)
        if slot not in seen:
            seen.add(slot)
            out.append(link)
    out.sort(
        key=lambda link: (
            _soft(link, duration),
            -_order_height(link),
            -_bandwidth(link),
        )
    )
    # The audio row reserves the last slot, so it survives the cap and
    # always renders as the bottom option.
    out = out[: (limit - 1 if auds else limit)]
    if auds:
        # Bandwidth is the quality signal for audio; size breaks ties.
        out.append(max(auds, key=lambda link: (_bandwidth(link), link.get("probed_size") or 0)))
    return out
