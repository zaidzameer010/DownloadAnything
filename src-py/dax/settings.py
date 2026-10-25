"""Persisted app settings.

Stored as one camelCase JSON row in dax.db's `settings` table — the db is
the single store. Unknown/legacy keys are ignored on load and dropped on
the next save. The api token auto-generates on first run. to_dax_options()
feeds the extraction chain (python yt-dlp + the vendored Rust extractor);
download params are built from the same fields at download time.
"""

import os
import secrets
import shutil
import string
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from .db import TaskDb

DEFAULT_API_PORT = 16801
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)

# macOS app data dir shared with the Tauri build — dax.db,
# dax.log and tmp/ all live here so both backends see the same state.
DATA_DIR = Path.home() / "Library" / "Application Support" / "dax"

_TOKEN_ALPHA = string.ascii_letters + string.digits

log = structlog.get_logger(__name__)


def new_token(length: int) -> str:
    """URL-safe token from OS entropy (secrets — same entropy source as getrandom)."""
    return "".join(secrets.choice(_TOKEN_ALPHA) for _ in range(length))


def _snake_to_camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(w.title() for w in rest)


def _camel_to_snake(name: str) -> str:
    out = []
    for ch in name:
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


@dataclass
class AppSettings:
    """The settings schema the py backend consumes."""

    # Named output folders — a download request names one, the path resolves
    # at task start. UI manages the list; entries are {"name", "path"}.
    categories: list[dict[str, str]] = field(default_factory=list)
    api_port: int = DEFAULT_API_PORT
    api_token: str = ""
    max_concurrent_downloads: int = 3
    extractor_engine: str = "auto"  # auto|python_ytdlp|custom
    # auto: False sends pages with no dedicated yt-dlp extractor straight to
    # the Rust extractor (yt-dlp's generic page pass is the slow path).
    # Candidate manifest/media probing still runs through yt-dlp.
    ytdlp_generic: bool = False
    downloader_engine: str = "aria2_next"  # aria2_next|ytdlp
    parallel_segments: int = 16
    player_client: str = "default"  # yt-dlp youtube player_client
    ffmpeg_path: str = ""
    aria2_next_path: str = ""
    aria2_rpc_port: int = 16802
    aria2_split: int = 16
    aria2_max_connection_per_server: int = 16
    aria2_min_split_size: str = "1M"
    aria2_file_allocation: str = "none"  # none|prealloc|falloc|trunc
    aria2_min_tls_version: str = "TLSv1.2"  # TLSv1.1|TLSv1.2|TLSv1.3
    aria2_max_overall_download_limit: str = "0"
    aria2_max_download_limit: str = "0"
    aria2_lowest_speed_limit: str = "0"
    aria2_retry_wait: int = 0
    aria2_state_save_interval: int = 10
    # BitTorrent (aria2-next libtorrent). seed_time is minutes; 0 = no time
    # limit — seed_ratio (0 = unlimited) alone ends seeding, so the default
    # keeps seeding until ratio 1.0 while the task already counts as done.
    bt_seed_ratio: float = 1.0
    bt_seed_time: int = 0
    bt_max_peers: int = 100
    bt_listen_port: int = 6881
    bt_enable_dht: bool = True
    bt_enable_pex: bool = True
    bt_enable_lpd: bool = True
    bt_sequential: bool = False
    bt_trackers: str = ""
    media_format: str = ""  # ""|mp4|mkv — "" renders as mp4
    embed_thumbnail: bool = True
    embed_subtitles: bool = False
    embed_chapters: bool = True
    embed_metadata: bool = False
    check_certificate: bool = True
    cookies_file: str = ""
    cookies_browser: str = "off"
    timeout_secs: int = 30
    retries: int = 3

    @classmethod
    def from_dict(cls, raw: dict[str, Any], fallback_token: str = "") -> AppSettings:
        s = cls()
        known = {f.name for f in fields(cls)}
        for k, v in raw.items():
            name = _camel_to_snake(k)
            if name not in known:
                continue
            default = getattr(s, name)
            # Exact type match: a wrong-typed entry would poison call sites
            # later, and bools must not pass as ints (isinstance trap).
            if type(v) is not type(default):
                # JSON has no float/int split — a whole-number ratio arrives
                # as int and must not be dropped by the exact-type check.
                if isinstance(default, float) and isinstance(v, int) and not isinstance(v, bool):
                    v = float(v)
                else:
                    log.warning("settings key wrong type — skipped", key=k)
                    continue
            setattr(s, name, v)
        if not s.api_token:
            s.api_token = fallback_token or new_token(24)
        return s

    def to_dict(self) -> dict[str, Any]:
        return {_snake_to_camel(f.name): getattr(self, f.name) for f in fields(self)}

    @classmethod
    def load(cls, db: TaskDb) -> AppSettings:
        """The settings row in dax.db (snake_case columns). Fresh install —
        the ws guard needs a real token before the first save persists."""
        raw = db.load_settings()
        if not isinstance(raw, dict):
            raw = {}
        # sqlite stores bools as 0/1 — coerce before from_dict's exact-type
        # check would skip them.
        bools = {f.name for f in fields(cls) if isinstance(f.default, bool)}
        for k, v in raw.items():
            if k in bools and v in (0, 1):
                raw[k] = bool(v)
        return cls.from_dict(raw)

    def save(self, db: TaskDb) -> None:
        vals = {f.name: getattr(self, f.name) for f in fields(self)}
        categories = vals.pop("categories")
        db.save_settings(vals, categories)

    def rotate_tokens(self) -> None:
        self.api_token = new_token(24)

    def to_dax_options(self) -> dict[str, Any]:
        """AppSettings → the extraction chain's camelCase options dict."""
        o: dict[str, Any] = {
            "timeoutSecs": self.timeout_secs,
            "retries": self.retries,
            "extractorEngine": self.extractor_engine,
            "ytdlpGeneric": self.ytdlp_generic,
        }
        if not self.check_certificate:
            o["checkCertificate"] = False
        if self.cookies_file:
            o["cookiesFile"] = self.cookies_file
        if self.cookies_browser and self.cookies_browser != "off":
            o["cookiesBrowser"] = self.cookies_browser
        if self.player_client and self.player_client != "default":
            o["playerClient"] = self.player_client
        return o


def out_dir(s: AppSettings, category: str | None = None) -> str:
    """Final destination dir: the request's category path, then the first
    category, then the app-data downloads dir. `~` expands so subprocess/
    dir consumers get real paths."""
    cats = [c for c in s.categories if isinstance(c, dict) and c.get("path")]
    if category:
        for c in cats:
            if c.get("name") == category:
                return os.path.expanduser(c["path"])
    if cats:
        return os.path.expanduser(cats[0]["path"])
    return str(DATA_DIR / "downloads")


def tmp_root() -> str:
    """Incomplete downloads live here, one dir per task id. Under the app
    data dir so in-progress partials never pollute the output dir."""
    return str(DATA_DIR / "tmp")


def task_tmp_dir(gid: str) -> str:
    return str(Path(tmp_root()) / gid)


def clean_task_tmp(gid: str) -> None:
    shutil.rmtree(task_tmp_dir(gid), ignore_errors=True)


# Found ffmpeg paths by configured value — PATH scans and stat calls repeat
# per download/postprocess otherwise. Misses are NOT cached: a mid-session
# install still resolves on the next call.
_ffmpeg_paths: dict[str, str] = {}


def resolve_ffmpeg(s: AppSettings) -> str | None:
    """ffmpeg binary path: explicit setting, PATH, then macOS Homebrew dirs."""
    if hit := _ffmpeg_paths.get(s.ffmpeg_path):
        return hit
    found: str | None = None
    if s.ffmpeg_path:
        expanded = os.path.expanduser(s.ffmpeg_path)
        if Path(expanded).is_file():
            found = expanded
    if found is None:
        if hit := shutil.which("ffmpeg"):
            found = hit
        else:
            for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
                if Path(candidate).is_file():
                    found = candidate
                    break
    if found:
        _ffmpeg_paths[s.ffmpeg_path] = found
    return found
