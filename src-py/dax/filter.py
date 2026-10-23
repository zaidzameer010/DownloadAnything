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
    audio        audio-only file (mp3/m4a/aac/ogg/opus/flac/wav/...).
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

VIDEO_EXTS = frozenset(
    {"mp4", "webm", "mkv", "mov", "m4v", "avi", "flv", "ts", "m2ts", "3gp", "mpg", "mpeg", "wmv"}
)
AUDIO_EXTS = frozenset(
    {"mp3", "m4a", "aac", "ogg", "oga", "opus", "flac", "wav", "wma", "aiff"}
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
    "poster": "thumb", "screenshot": "thumb", "brandmark": "thumb",
    "promo": "trailer", "sample": "sample",
    "fb": "preview",
    "ad": "ad", "ads": "ad", "advert": "ad", "advertisement": "ad",
    "commercial": "ad", "preroll": "ad", "midroll": "ad",
    "postroll": "ad", "bumper": "ad", "adbreak": "ad",
    "sprite": "sprite",
}
_JUNK_PREFIX = (
    ("preview", "preview"), ("thumb", "thumb"), ("trailer", "trailer"),
    ("teaser", "trailer"), ("storyboard", "sprite"), ("logo", "thumb"),
    ("watermark", "thumb"),
)
_ROLE_KEYS = ("type", "role", "kind", "mode", "content")
_HARD_JUNK = frozenset({"preview", "thumb", "trailer", "ad", "sprite"})
_SEGMENT_STEMS = ("seg", "segment", "chunk", "init", "frag", "part")
_ASSET_TOKENS = (
    "logo", "watermark", "brandmark", "favicon", "avatar", "user_pic", "user-pic",
    "user_avatar", "profile_pic", "author", "spinner", "loader", "loading",
    "spacer", "blank", "placeholder", "default_avatar", "default_thumb",
    "nothumb", "no_thumb", "no-thumb", "no_image", "no-image", "pixel",
    "tracking", "analytics", "badge", "header-logo", "site-logo",
    "apple-touch-icon", "play_button", "play_btn", "play-btn", "btn_play",
    "button", "btn", "ad-", "advert", "sponsor", "banner",
)
_JUNK_IMG_EXTS = (".svg", ".ico", ".cur")
_JUNK_DIMS = ("1x1", "16x16", "32x32", "48x48", "64x64")


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
    if ext in DASH_EXTS or "format=mpd" in query or "type=mpd" in query:
        return "dash"
    if ext in HLS_EXTS or any(tok in query for tok in ("format=m3u8", "type=m3u8")):
        return "hls"
    if ext in AUDIO_EXTS:
        return "audio"
    return kind_hint or "progressive"


# Script paths that serve a media file named in the query (KVS remote_control.php).
_SCRIPT_EXTS = frozenset({"php", "asp", "aspx", "jsp", "cgi", "pl", "html", "htm"})


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

    return any(tok in lower for tok in _ASSET_TOKENS) or any(dim in lower for dim in _JUNK_DIMS)


def is_downloadable(link: dict) -> bool:
    """A row yt-dlp can actually fetch: http(s), not a DRM sink."""
    url = str(link.get("url") or link.get("manifest_url") or "")
    if not url.startswith(("http://", "https://")):
        return False
    drm = link.get("drm") or {}
    return drm.get("type") not in ("widevine", "play_ready", "fair_play", "unknown")


def has_media(page: dict | None) -> bool:
    """Does an extractor page carry at least one downloadable row?"""
    if not page:
        return False
    return any(
        is_downloadable(f)
        for item in page.get("items") or []
        for f in item.get("formats") or []
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


def _is_media(link: dict) -> bool:
    """Not an image/segment/non-http URL and not hard junk."""
    url = str(link.get("url") or "")
    return role(url, link.get("kind")) is not None and junk(url) not in _HARD_JUNK


def _tier(link: dict) -> int:
    """Role tier of a row — the manifest it resolved from wins over its own
    URL: a variant row of a master ladder belongs to the master."""
    src = str(link.get("manifest_url") or link.get("url") or "")
    return _ROLE_TIER.get(role(src, link.get("kind")) or "", TIER_PROGRESSIVE)


def _height(link: dict) -> int:
    return int((link.get("quality") or {}).get("height") or 0)


def _bandwidth(link: dict) -> int:
    return int((link.get("quality") or {}).get("bandwidth") or 0)


def _is_audio(link: dict) -> bool:
    return link.get("kind") == "audio"


def _soft(link: dict, duration: float | None) -> bool:
    """Low-res or short clip — kept, but ranked last."""
    return (0 < _height(link) <= 144) or (duration is not None and duration < 10)


# Ordering height of a row whose own quality is unknown: a master stands
# for the whole ladder, so it heads the list.
_TOP_HEIGHT = 10_000


def _order_height(link: dict) -> int:
    return _height(link) or (_TOP_HEIGHT if _tier(link) == TIER_MASTER else 0)


def _codec_pref(link: dict) -> int:
    codecs = (link.get("codecs") or "").lower()
    if "av01" in codecs or "av1" in codecs:
        return 0
    if any(x in codecs for x in ("hvc1", "hevc", "vorbis")):
        return 2
    return 1


def order_candidates(links: list[dict], limit: int = MAX_CANDIDATES) -> list[dict]:
    """Deduped, junk-free, best-first candidate list for yt-dlp probing.
    Masters lead so one probe resolves the full ladder; soft junk trails.
    Tokenized URLs beat their bare siblings — signed CDNs 403 without the
    query."""
    links = [l for l in links if _is_media(l)]
    tokened = {_path_key(str(l.get("url") or "")) for l in links if urlsplit(str(l.get("url") or "")).query}
    out: list[dict] = []
    seen: set[str] = set()
    for l in links:
        url = str(l.get("url") or "")
        if not urlsplit(url).query and _path_key(url) in tokened:
            continue
        key = _dedup_key(url)
        if key not in seen:
            seen.add(key)
            out.append(l)
    out.sort(key=lambda l: (_tier(l), _soft(l, None), -_height(l), -_bandwidth(l)))
    return out[:limit]


def _bare_variant(link: dict) -> bool:
    """A raw variant row — a playlist that resolved no manifest itself."""
    return not link.get("manifest_url") and role(
        str(link.get("url") or ""), link.get("kind")
    ) == "variant"


def _supersede(rows: list[dict]) -> list[dict]:
    """A resolved master supersedes bare variant rows from its directory —
    the ladder already carries them with better metadata. Unresolved
    masters supersede nothing: a dead master keeps its variant fallback."""
    resolved = {
        _dir(str(l["manifest_url"]))
        for l in rows
        if l.get("manifest_url") and role(str(l["manifest_url"])) == "master"
    }
    if not resolved:
        return rows
    return [
        l
        for l in rows
        if not (_bare_variant(l) and _dir(str(l.get("url") or "")) in resolved)
    ]


def _slot(link: dict) -> tuple:
    """Quality slot a row competes for — one winner per slot in the final
    list. Height-less rows stay distinct by bandwidth (or URL)."""
    if h := _height(link):
        return (False, h)
    return (False, _bandwidth(link) or str(link.get("url") or ""))


def _storyboard(link: dict) -> bool:
    return link.get("container") in ("mhtml", "json") or str(
        link.get("repr_id") or ""
    ).startswith("sb")


def rank_formats(
    links: list[dict], duration: float | None = None, limit: int = MAX_FORMATS
) -> list[dict]:
    """The final format list: junk and DRM dropped, one row per quality
    slot — best role wins — ordered best quality first, soft junk last.
    Audio-only rows skip the slot race: the single highest-quality audio
    track is pinned at the bottom, below even soft junk."""
    rows = [
        l
        for l in links
        if _is_media(l) and is_downloadable(l) and not _storyboard(l)
    ]
    auds = [l for l in rows if _is_audio(l)]
    rows = [l for l in rows if not _is_audio(l)]
    # Anonymous progressive rows — a bare file URL with no height or label
    # to name it — drop once the list has an identified format to offer.
    # A page whose formats are all anonymous keeps them (it has nothing
    # better); manifests always carry meaning, labeled or not.
    if any(_height(l) or (l.get("quality") or {}).get("label") for l in rows):
        rows = [
            l
            for l in rows
            if l.get("kind") in ("hls", "dash")
            or _height(l)
            or (l.get("quality") or {}).get("label")
        ]
    rows = _supersede(rows)
    rows.sort(
        key=lambda l: (
            _soft(l, duration),
            _tier(l),
            _codec_pref(l),
            -_height(l),
            -_bandwidth(l),
            bool(l.get("is_live")),
            l.get("probed_size") is None,
        )
    )
    out: list[dict] = []
    seen: set[tuple] = set()
    for l in rows:
        slot = _slot(l)
        if slot not in seen:
            seen.add(slot)
            out.append(l)
    out.sort(
        key=lambda l: (
            _soft(l, duration),
            -_order_height(l),
            -_bandwidth(l),
        )
    )
    # The audio row reserves the last slot, so it survives the cap and
    # always renders as the bottom option.
    out = out[: limit - 1 if auds else limit]
    if auds:
        # Bandwidth is the quality signal for audio; size breaks ties.
        out.append(max(auds, key=lambda l: (_bandwidth(l), l.get("probed_size") or 0)))
    return out
