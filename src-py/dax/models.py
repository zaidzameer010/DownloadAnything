"""Request contract structs (camelCase JSON in, snake_case fields).

msgspec rename='camel' decodes camelCase keys, mirroring the UI's JSON.
Unknown keys are ignored on decode. Response payloads are built as dicts —
absent keys mean "not set" and dicts express that directly.
"""

from msgspec import Struct, field


class HeaderPair(Struct, rename="camel"):
    name: str = ""
    value: str = ""


class ContextHeaders(Struct, rename="camel"):
    """Per-origin headers (cookies etc.) replayed on matching-host requests.
    `title` is the page title on the tab the request was sniffed from — it
    names fallback-path items when no extractor metadata exists. `kind`,
    `mime`, `label` and `height` are the extension's capture classification:
    they let the sniffed fallback keep extensionless streams and show the
    resolution encoded in the URL."""

    url: str = ""
    title: str | None = None
    headers: list[HeaderPair] = field(default_factory=list)
    kind: str | None = None
    mime: str | None = None
    label: str | None = None
    height: int | None = None


class ExtractOptions(Struct, rename="camel"):
    """Per-call extraction options — the python yt-dlp chain's knobs plus
    the engine pick. Download/postprocess tuning lives in AppSettings."""

    contexts: list[ContextHeaders] = field(default_factory=list)
    max_items: int | None = None
    cookies_file: str | None = None
    cookies_browser: str | None = None
    referer: str | None = None
    user_agent: str | None = None
    proxy: str | None = None
    timeout_secs: int | None = None
    retries: int | None = None
    rate_limit_rps: float | None = None
    extract_concurrency: int | None = None
    extractor_engine: str | None = None  # auto|python_ytdlp|custom
    # auto only: False routes pages yt-dlp has no dedicated extractor for
    # straight to the Rust extractor; the slow GenericIE page pass never runs.
    # Candidate manifest/media probing still uses yt-dlp either way.
    ytdlp_generic: bool | None = None
    player_client: str | None = None  # yt-dlp youtube player_client pick
    check_certificate: bool | None = None
    # Internal probe mode: truthy = the flat playlist-listing pass (a "video
    # AND playlist" URL resolves the playlist); falsy/None = the video pass,
    # which pins noplaylist so the same URL yields only its video.
    playlist: bool | None = None


class QualitySel(Struct, rename="camel"):
    """Quality identity of the row the user picked after probe — matched
    against yt-dlp's formats at download time."""

    height: int | None = None
    width: int | None = None
    bandwidth: int | None = None
    audio_only: bool | None = None


class PlaylistItem(Struct, rename="camel"):
    """One picked entry of a playlist download — index is yt-dlp's 1-based
    playlist_index, id pairs with ie_key for the resume archive."""

    index: int = 0
    id: str = ""
    url: str = ""
    title: str = ""
    ie_key: str = ""
    thumbnail: str | None = None
    duration_secs: float | None = None


class PlaylistReq(Struct, rename="camel"):
    """Whole-playlist (or picked-subset) add — a single task whose items
    download into a playlist-named folder. `url` is the playlist's page URL
    (also the request's url); `title` names the output folder; `count` is
    the full playlist size so the UI can number items reversed."""

    title: str = ""
    url: str = ""
    count: int = 0
    items: list[PlaylistItem] = field(default_factory=list)


class DownloadReq(Struct, rename="camel"):
    """One download request. Page-URL adds resolve through yt-dlp; direct
    URLs download as-is."""

    url: str = ""
    created_at: int | None = None
    # The extracted item's own metadata — shown in the task row/details
    # (the output filename alone loses the original title and runtime).
    title: str | None = None
    duration_secs: float | None = None
    filename_hint: str | None = None
    # Verbatim output name, ext included — intercepted browser downloads keep
    # the name Chrome resolved (never appended to like filename_hint is).
    filename: str | None = None
    referer: str | None = None
    thumbnail: str | None = None
    media: str | None = None  # file|hls|dash
    repr_id: str | None = None  # yt-dlp format_id of the selected row
    page_url: str | None = None  # extracted item's webpage_url (authoritative page)
    expected_size: int | None = None
    # Category name from settings — resolves to its output path at task start.
    category: str | None = None
    # False → skip every postprocessor (remux/embed) — non-media intercepts
    # would otherwise get an ffmpeg pass over a binary.
    postprocess: bool | None = None
    # Torrent adds: kind='torrent'; magnet links ride `url`, .torrent bytes
    # ride `torrent` (base64), and select_files is the 1-based aria2
    # select-file string ("1-3,5") of the files the user picked.
    kind: str | None = None
    torrent: str | None = None
    select_files: str | None = None
    quality: QualitySel | None = None
    contexts: list[ContextHeaders] = field(default_factory=list)
    # Playlist adds: the picked entries under their playlist's metadata —
    # the task downloads them into <out>/<playlist title>/ as one job.
    playlist: PlaylistReq | None = None
