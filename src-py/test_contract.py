"""Contract checks for the Python backend — the behavior the retired Rust
backend's unit suite pinned (settings, task db, download params, control
verbs) plus the port-specific layers: request camelize, extract-chain
routing, aria2-next ws-rpc, torrent lifecycle.

Run: cd src-py && HOME=/tmp/dax-test-home .venv/bin/python test_contract.py
(HOME is isolated — the suite never touches real app data.)
"""

import asyncio
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

import orjson

# Isolate app data before anything imports dax.settings.
_TMP = Path(tempfile.mkdtemp(prefix="dax-test-"))
os.environ.setdefault("HOME", str(_TMP))

from dax import db as db_mod  # noqa: E402
from dax import downloads as dl  # noqa: E402
from dax import extract as ex  # noqa: E402
from dax import filter as filt  # noqa: E402
from dax import settings as st  # noqa: E402
from dax.aria2_next import torrent as tr  # noqa: E402
from dax.models import DownloadReq  # noqa: E402
from dax.settings import AppSettings, out_dir  # noqa: E402

# Point DATA_DIR (computed from HOME at import) at the temp root.
st.DATA_DIR = _TMP / "appdata"
ex.DATA_DIR = st.DATA_DIR


def _settings(**kw) -> AppSettings:
    return AppSettings(**kw)


# ---- settings ----------------------------------------------------------------

def _sdb() -> db_mod.TaskDb:
    """Fresh scratch db for settings tests."""
    return db_mod.TaskDb.open(_TMP / f"settings-{next(_db_seq)}.db")


def test_settings_roundtrip_camel_case():
    d = _sdb()
    s = AppSettings(media_format="mkv",
                    embed_subtitles=True, max_concurrent_downloads=5,
                    categories=[{"name": "vid", "path": "~/Media"}],
                    api_token="tok-123")
    s.save(d)
    loaded = AppSettings.load(d)
    assert loaded.categories == [{"name": "vid", "path": "~/Media"}]
    assert loaded.media_format == "mkv"
    assert loaded.embed_subtitles is True
    assert loaded.max_concurrent_downloads == 5
    # Secrets survive the roundtrip; empty ones regenerate on load.
    assert loaded.api_token == "tok-123"
    AppSettings().save(d)
    assert AppSettings.load(d).api_token, "empty token regenerates on load"


def test_settings_load_skips_wrong_typed_keys():
    d = _sdb()
    d.save_settings({"api_port": "not-a-number", "media_format": "mp4"}, [])
    s = AppSettings.load(d)
    assert s.api_port == 16801, "wrong-typed column must be skipped"
    assert s.media_format == "mp4"


def test_settings_fresh_load_generates_token():
    """An empty settings row must still mint an api token — the ws guard
    with an empty token accepts ?token= from anything on loopback."""
    s = AppSettings.load(_sdb())
    assert s.api_token and len(s.api_token) >= 16


def test_settings_load_ignores_unknown_keys():
    s = AppSettings.from_dict({"streamMaxConnections": "8", "engineUserAgent": "x"})
    assert not hasattr(s, "stream_max_connections")


def test_settings_bool_columns_and_zero_roundtrip():
    """sqlite stores bools as 0/1 — load coerces them back, and a stored
    zero must not read as 'unset'."""
    d = _sdb()
    _settings(embed_subtitles=True, check_certificate=False, retries=0).save(d)
    s = AppSettings.load(d)
    assert s.embed_subtitles is True
    assert s.check_certificate is False
    assert s.retries == 0, "a valid zero must load"


def test_extract_params_zero_retries_and_timeout():
    params, _ = ex._ydl_params(ex.ExtractOptions(retries=0, timeout_secs=0), "https://x")
    assert params["retries"] == 0 and params["extractor_retries"] == 0
    assert params["socket_timeout"] == 0


def test_extract_params_impersonate_and_accept_language():
    params, _ = ex._ydl_params(ex.ExtractOptions(), "https://x")
    assert params["impersonate"] is not None, "TLS impersonation — anti-bot CDNs 403 python TLS"
    assert params["http_headers"]["Accept-Language"] == ex.DEFAULT_ACCEPT_LANG
    # A captured Accept-Language overrides the default.
    from dax.models import ContextHeaders, HeaderPair
    params2, _ = ex._ydl_params(
        ex.ExtractOptions(contexts=[ContextHeaders(
            url="https://x", headers=[HeaderPair(name="Accept-Language", value="de-DE,de;q=0.9")])]),
        "https://x",
    )
    assert params2["http_headers"]["Accept-Language"] == "de-DE,de;q=0.9"


def test_download_params_impersonate_and_accept_language():
    db, _ = _db()
    p = dl._ydl_dl_params(dl.Task("g", {"url": "https://x"}), _settings(), db)
    assert p["impersonate"] is not None
    assert p["http_headers"]["Accept-Language"] == ex.DEFAULT_ACCEPT_LANG


def test_download_req_contexts_reach_cookie_seeds():
    """Captured contexts ride the download req — their Cookie headers seed
    the download jar (the extractor's session replays at the CDN)."""
    req = DownloadReq(
        url="https://cdn.x/v.m3u8",
        contexts=[{"url": "https://cdn.x", "headers": [
            {"name": "Cookie", "value": "sess=abc"}]}],
    )
    seeds = ex._cookie_seeds(dl._req_to_dict(req)["contexts"])
    assert seeds == [("cdn.x", "sess=abc")]


def test_to_dax_options_extraction_chain():
    o = _settings().to_dax_options()
    assert o["timeoutSecs"] == 30
    assert o["retries"] == 3
    assert o["extractorEngine"] == "auto"
    assert o["ytdlpGeneric"] is False, "Rust replaces the generic page pass"
    assert "cookiesFile" not in o and "cookiesBrowser" not in o
    # Download-only tuning must not leak into extraction options.
    assert "mediaFormat" not in o and "parallelSegments" not in o
    assert "speedProfile" not in o and "extraOptions" not in o
    s = _settings(cookies_browser="chrome", cookies_file="/tmp/c.txt")
    o2 = s.to_dax_options()
    assert o2["cookiesBrowser"] == "chrome"
    assert o2["cookiesFile"] == "/tmp/c.txt"


def test_out_dir_category_and_falls_back():
    """A named category resolves to its path; unknown names and no-category
    settings fall back to the first category, then the app-data dir."""
    cats = [{"name": "vid", "path": "~/Media/Videos"}]
    s = _settings(categories=cats)
    assert out_dir(s, "vid") == os.path.expanduser("~/Media/Videos")
    assert out_dir(s, "nope") == os.path.expanduser("~/Media/Videos")
    assert out_dir(_settings()) == str(st.DATA_DIR / "downloads")


def test_check_name_conflict_disk_and_tasks():
    """Collision = same resolved dir + same stem (hints) or exact file
    (verbatim names); other tasks' pending names count too."""
    from dax import app

    vids = _TMP / "vids"
    docs = _TMP / "docs"
    vids.mkdir()
    docs.mkdir()
    (vids / "Song.mp4").write_bytes(b"x")
    d = _TMP / f"shared-{next(_db_seq)}"
    d.mkdir(parents=True, exist_ok=True)
    shared = app.Shared(d)
    shared.set_settings(_settings(categories=[
        {"name": "vids", "path": str(vids)},
        {"name": "docs", "path": str(docs)},
    ]))
    def req(**kw):
        return DownloadReq(url="u", **kw)
    c = dl.check_name_conflict(shared, req(filename_hint="Song", category="vids"))
    assert c["conflict"] and c["suggested"] == "Song (2)"
    c = dl.check_name_conflict(shared, req(filename="Song.mp4", category="vids"))
    assert c["suggested"] == "Song (2).mp4"
    c = dl.check_name_conflict(shared, req(filename="Song.mkv", category="vids"))
    assert not c["conflict"], "verbatim names collide on the exact file only"
    shared.registry.add(
        {"url": "u2", "filenameHint": "Clip", "category": "vids"}, persist=False
    )
    c = dl.check_name_conflict(shared, req(filename_hint="Clip", category="vids"))
    assert c["suggested"] == "Clip (2)"
    c = dl.check_name_conflict(shared, req(filename_hint="Clip", category="docs"))
    assert not c["conflict"], "a different category dir doesn't collide"


# ---- TaskDb ------------------------------------------------------------------

_db_seq = iter(range(1000))


def _db() -> tuple[db_mod.TaskDb, Path]:
    return db_mod.TaskDb.open(_TMP / f"test-{next(_db_seq)}.db"), _TMP


def test_task_db_lifecycle():
    db, _ = _db()
    req = {"url": "https://example.com/v.mp4", "filenameHint": "v.mp4",
           "thumbnail": "t.jpg", "createdAt": 1000}
    db.insert_task("g1", req, "waiting", 2048, 1000)
    db.update_progress("g1", "active", 1024, 2048, 512)
    db.mark_complete("g1", "/downloads/v.mp4", 2048)

    tasks = db.load_all_tasks("/downloads")
    assert len(tasks) == 1
    info, r = tasks[0]
    assert info["gid"] == "g1"
    assert info["status"] == "complete"
    assert info["files"][0]["path"] == "/downloads/v.mp4"
    assert info["dax"]["thumbnail"] == "t.jpg"
    assert r["url"] == "https://example.com/v.mp4"

    assert db.purge_finished() == 1
    assert db.load_all_tasks("/downloads") == []


def test_task_db_restores_active_as_paused():
    db, _ = _db()
    db.insert_task("g2", {"url": "https://x/v"}, "active", 0, 2000)
    db.insert_task("g3", {"url": "https://x/w"}, "error", 0, 3000)
    db.mark_error("g3", "1", "boom")
    tasks = db.load_all_tasks("/downloads")
    by_gid = {i["gid"]: i for i, _ in tasks}
    assert by_gid["g2"]["status"] == "paused", "live-at-shutdown restores paused"
    assert by_gid["g3"]["status"] == "error"
    assert by_gid["g3"]["errorMessage"] == "boom"


def test_task_db_pause_all_and_delete():
    db, _ = _db()
    db.insert_task("g4", {"url": "https://x"}, "active", 0, 1)
    db.insert_task("g5", {"url": "https://x"}, "waiting", 0, 2)
    assert db.pause_all_active() == 2
    tasks = db.load_all_tasks("/downloads")
    assert all(i["status"] == "paused" for i, _ in tasks)
    db.delete_task("g4")
    assert [i["gid"] for i, _ in db.load_all_tasks("/downloads")] == ["g5"]


def test_task_db_restores_postprocessing_as_paused():
    """A task killed mid-postprocess has no worker on restart — restoring it
    as 'postprocessing' would pin it in the active list forever."""
    db, _ = _db()
    db.insert_task("g6", {"url": "https://x"}, "postprocessing", 0, 1)
    tasks = db.load_all_tasks("/downloads")
    assert tasks[0][0]["status"] == "paused"


def test_task_db_migrates_legacy_schema():
    """A pre-TaskDb database (req/result/postprocessed columns) migrates in
    place: req→req_json rename + new columns, rows preserved."""
    p = _TMP / "legacy.db"
    conn = sqlite3.connect(p)
    conn.execute("""
        CREATE TABLE tasks (
            gid TEXT PRIMARY KEY, url TEXT NOT NULL, thumbnail TEXT,
            req TEXT, status TEXT, result TEXT,
            postprocessed INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL
        )""")
    conn.execute(
        "INSERT INTO tasks (gid, url, req, status, created_at) VALUES (?, ?, ?, ?, ?)",
        ("old1", "https://x/old", orjson.dumps({"url": "https://x/old"}).decode(),
         "complete", 42),
    )
    conn.commit()
    conn.close()

    db = db_mod.TaskDb.open(p)
    tasks = db.load_all_tasks("/downloads")
    assert len(tasks) == 1
    info, r = tasks[0]
    assert info["gid"] == "old1"
    assert info["status"] == "complete"
    assert r["url"] == "https://x/old"
    # The migrated db accepts new writes.
    db.insert_task("new1", {"url": "https://x/new"}, "waiting", 0, 43)
    assert len(db.load_all_tasks("/downloads")) == 2


# ---- request contract --------------------------------------------------------

def test_req_to_dict_camelizes():
    req = DownloadReq(url="https://x", filename_hint="t",
                      expected_size=5, created_at=7)
    d = dl._req_to_dict(req)
    assert d["filenameHint"] == "t"
    assert d["expectedSize"] == 5
    assert d["createdAt"] == 7
    assert "url" in d
    # None fields drop out entirely.
    assert "referer" not in d and "thumbnail" not in d


# ---- extract chain -----------------------------------------------------------

def test_host_matches():
    assert ex.host_matches("cdn.site.com", "site.com")
    assert ex.host_matches("site.com", "site.com")
    assert not ex.host_matches("notsite.com", "site.com")
    assert not ex.host_matches("site.com.evil.com", "site.com")


def test_is_unsupported():
    from yt_dlp.utils import DownloadError, UnsupportedError
    assert ex._is_unsupported(UnsupportedError("https://example.com"))
    assert ex._is_unsupported(DownloadError("ERROR: Unsupported URL: https://example.com"))
    assert ex._is_unsupported(ValueError("No suitable extractor found for URL"))
    assert not ex._is_unsupported(ValueError("HTTP Error 404: Not Found"))


def test_has_media():
    assert not filt.has_media(None)
    assert not filt.has_media({"items": []})
    assert not filt.has_media({"items": [{"formats": []}]})
    assert not filt.has_media({
        "items": [{"formats": [{"url": "https://x/v.mp4", "drm": {"type": "widevine"}}]}]
    })
    assert filt.has_media({
        "items": [{"formats": [{"url": "https://x/v.mp4"}]}]
    })


def test_filter_role_taxonomy():
    """One taxonomy: master / progressive / variant / audio, junk dropped."""
    m = "https://cdn.x/hls2/08/09654/v9_x/master.m3u8?t=abc"
    v = "https://cdn.x/hls2/08/09654/v9_x/index-v1.m3u8?t=abc"
    a = "https://cdn.x/hls2/08/09654/v9_x/index-v1-a1.m3u8?t=abc"
    assert filt.role(m) == "master"
    assert filt.role(v) == "variant" and filt.role(a) == "variant"
    # multi= serves the whole ladder — master tier regardless of extension.
    assert filt.role("https://x/video.mp4?multi=360p,720p,1080p") == "master"
    assert filt.role("https://x/manifest.mpd") == "master"
    assert filt.role("https://x/clip.mp4") == "progressive"
    assert filt.role("https://x/song.m4a") == "audio"
    assert filt.role("https://x/v/playlist.m3u8") == "master"
    assert filt.role("https://x/chunklist_w1.m3u8") == "variant"
    assert filt.role("https://x/download?format=m3u8") == "master"
    # Stream parts and art are never media.
    assert filt.role("https://x/seg-1.ts") is None
    assert filt.role("https://x/init.mp4") is None
    assert filt.role("https://x/poster.jpg") is None
    assert filt.role("https://x/movie.ts") == "progressive"
    assert filt.junk("https://x/preview/clip.mp4") == "preview"
    assert filt.junk("https://x/trailers/t.mp4") == "trailer"
    assert filt.junk("https://x/road.mp4") is None
    # you-porn watch page: related-video *_fb.mp4 fallbacks and imgproxy
    # transform strips (vts:/rs:fit:/ex:/bg: segments) are junk, not media.
    assert filt.junk(
        "https://ev-ph.x/videos/202001/23/278791461/360P_360K_278791461_fb.mp4"
    ) == "preview"
    assert filt.junk(
        "https://pix.x/c6371/videos/202605/11/48573215/original_48573215.mov"
        "/plain/rs:fit:320:180/vts:434"
    ) == "sprite"
    # Signed extensionless manifest endpoints rank as masters.
    assert filt.role("https://site.com/media/hls/?s=abc") == "master"


def test_filter_container_of_script_path():
    """KVS serves media from a script path with the real name in the query —
    remote_control.php?file=…_1440p.mp4 is a progressive mp4, not a page."""
    u = "https://st43.pimpbunny.com/remote_control.php?file=abc_1440p.mp4&acctoken=x"
    assert filt.container_of(u) == "mp4"
    assert filt.role(u) == "progressive"
    assert filt.container_of("https://x/watch/123") is None
    assert filt.container_of("https://x/remote_control.php?file=abc") is None
    # Only script paths qualify — a page URL with a media query is still a page.
    assert filt.container_of("https://x/watch?next=v.mp4") is None


def test_filter_candidate_order():
    """Masters lead the probe list; junk/segments/images drop; tokenized
    URLs beat their bare siblings."""
    m = "https://cdn.x/hls/master.m3u8?t=1"
    v = "https://cdn.x/hls/index-v1.m3u8?t=1"
    cands = [
        {"url": "https://cdn.x/v.mp4"},
        {"url": "https://cdn.x/v.mp4?token=1"},
        {"url": v},
        {"url": m},
        {"url": "https://cdn.x/seg1.ts"},
        {"url": "https://cdn.x/poster.jpg"},
        {"url": "https://cdn.x/preview/clip.mp4"},
        {"url": "https://cdn.x/720p.mp4", "quality": {"height": 720}},
    ]
    urls = [c["url"] for c in filt.order_candidates(cands)]
    assert urls[0] == m, urls
    assert urls.index("https://cdn.x/720p.mp4") < urls.index(v), "progressive before bare variant"
    assert "https://cdn.x/v.mp4" not in urls, "bare sibling dropped"
    assert not any("seg1" in u or "poster" in u or "preview" in u for u in urls)


def test_filter_rank_formats():
    """One row per quality slot — the resolved master ladder wins the slot;
    bare variants of that master are superseded; DRM drops; soft junk
    trails; the list caps at MAX_FORMATS."""
    m = "https://cdn.x/hls/master.m3u8?t=1"
    v = "https://cdn.x/hls/index-v1.m3u8?t=1"
    v2 = "https://cdn.x/hls/index-v2.m3u8?t=1"
    a = "https://cdn.x/hls/index-v1-a1.m3u8?t=1"
    rows = [
        {"url": v, "kind": "hls", "manifest_url": m, "quality": {"height": 1080, "bandwidth": 5_000_000}},
        {"url": v2, "kind": "hls", "manifest_url": m, "quality": {"height": 720, "bandwidth": 2_500_000}},
        {"url": a, "kind": "audio", "manifest_url": m, "quality": {"bandwidth": 128_000}},
        {"url": v, "kind": "hls", "quality": {"height": 1080}},  # raw fallback — superseded
        {"url": "https://cdn.x/file.mp4", "kind": "progressive", "quality": {"height": 1080}},
        {"url": "https://cdn.x/480p.mp4", "kind": "progressive", "quality": {"height": 480}},
        {"url": "https://cdn.x/drm.mp4", "kind": "progressive", "drm": {"type": "widevine"}},
        {"url": "https://cdn.x/tiny.mp4", "kind": "progressive", "quality": {"height": 144}},
        {"url": "https://cdn.x/anon.mp4", "kind": "progressive"},  # no metadata — dropped
    ]
    out = filt.rank_formats(rows)
    # The audio row pins the bottom — below soft junk.
    assert [r["url"] for r in out] == [v, v2, "https://cdn.x/480p.mp4", "https://cdn.x/tiny.mp4", a]
    # Several audio-only rows collapse to the single highest-quality one.
    out = filt.rank_formats(rows + [
        {"url": "https://cdn.x/lo.m4a", "kind": "audio", "quality": {"bandwidth": 64_000}},
    ])
    assert [r["url"] for r in out] == [v, v2, "https://cdn.x/480p.mp4", "https://cdn.x/tiny.mp4", a]
    out = filt.rank_formats(rows + [
        {"url": "https://cdn.x/hi.m4a", "kind": "audio", "quality": {"bandwidth": 320_000}},
    ])
    assert out[-1]["url"] == "https://cdn.x/hi.m4a", "highest-bandwidth audio wins the slot"
    # An audio-only page: the best track is the whole list.
    assert [r["url"] for r in filt.rank_formats([
        {"url": "https://cdn.x/lo.m4a", "kind": "audio", "quality": {"bandwidth": 64_000}},
        {"url": "https://cdn.x/hi.m4a", "kind": "audio", "quality": {"bandwidth": 256_000}},
    ])] == ["https://cdn.x/hi.m4a"]
    # The audio row survives the list cap — it reserves the final slot.
    many = [
        {"url": f"https://x/{h}.mp4", "kind": "progressive", "quality": {"height": h}}
        for h in range(200, 280, 2)
    ]
    out = filt.rank_formats(many + [
        {"url": "https://cdn.x/song.m4a", "kind": "audio", "quality": {"bandwidth": 128_000}},
    ])
    assert len(out) == filt.MAX_FORMATS and out[-1]["url"] == "https://cdn.x/song.m4a"
    # A dead master supersedes nothing — its variant fallback survives.
    assert [r["url"] for r in filt.rank_formats([
        {"url": v, "kind": "hls", "quality": {"height": 720}},
        {"url": m, "kind": "hls"},
    ])] == [m, v]
    many = [
        {"url": f"https://x/{h}.mp4", "kind": "progressive", "quality": {"height": h}}
        for h in range(200, 280, 2)
    ]
    assert len(filt.rank_formats(many)) == filt.MAX_FORMATS
    # All-anonymous lists keep their rows — the page has nothing better.
    anon = [{"url": "https://x/a.mp4", "kind": "progressive"},
            {"url": "https://x/b.mp4", "kind": "progressive"}]
    assert len(filt.rank_formats(anon)) == 2


def test_sniffed_extract_fallback():
    from dax.models import ContextHeaders
    opts = ex.ExtractOptions(
        referer="https://example.com/watch",
        contexts=[
            ContextHeaders(url="https://cdn.example.com/stream/index.m3u8", headers=[]),
            ContextHeaders(url="https://cdn.example.com/video.mp4", headers=[]),
        ],
    )
    items = ex._sniffed_extract(opts, "https://example.com/watch")
    assert items and len(items) == 1
    assert len(items[0]["formats"]) == 2
    # dax.filter orders: progressive file before the bare variant playlist.
    assert items[0]["formats"][0]["kind"] == "progressive"
    assert items[0]["formats"][1]["kind"] == "hls"


def test_sniffed_extract_carries_context_title():
    """The captured tab title rides each context — it names the sniffed
    fallback item (display row + filenameHint downstream)."""
    from dax.models import ContextHeaders
    opts = ex.ExtractOptions(
        referer="https://example.com/watch",
        contexts=[
            ContextHeaders(url="https://cdn.example.com/v.mp4", headers=[],
                           title="Clip Page"),
            ContextHeaders(url="https://cdn.example.com/s.m3u8", headers=[]),
        ],
    )
    items = ex._sniffed_extract(opts, "https://example.com/watch")
    assert items and items[0]["title"] == "Clip Page"


def test_probe_rust_items_context_title_is_last_resort():
    """Scraped and probed titles outrank the captured tab title — it only
    backfills rust/extension-path items that produced none."""
    from dax.models import ContextHeaders
    orig_ytdlp = ex._ytdlp_extract
    ex._ytdlp_extract = lambda o, u, **k: {"input": u, "items": []}
    try:
        opts = ex.ExtractOptions(contexts=[
            ContextHeaders(url="https://cdn.x/v.mp4", headers=[], title="Tab Title")])
        raw = [{"id": "page", "page_url": "https://x/watch",
                "formats": [{"url": "https://cdn.x/v.mp4", "kind": "progressive"}]}]
        items = ex._probe_rust_items(opts, raw, "https://x/watch")
        assert items[0]["title"] == "Tab Title"
        # A scraped page title wins over the tab title.
        raw[0]["title"] = "Scraped"
        items = ex._probe_rust_items(opts, raw, "https://x/watch")
        assert items[0]["title"] == "Scraped"
    finally:
        ex._ytdlp_extract = orig_ytdlp


def test_extract_chain_auto_ytdlp_first():
    """auto: python yt-dlp wins when it has media; rust only on failure."""
    calls = []
    ytdlp_page = {"input": "u", "items": [{"formats": [{"url": "https://x/v.mp4"}]}]}
    rust_page = {"input": "u", "items": [{"formats": [{"url": "https://r/v.mp4"}]}]}
    orig_ytdlp, orig_rust = ex._ytdlp_extract, ex._rust_extract
    ex._ytdlp_extract = lambda o, u, **k: calls.append("ytdlp") or ytdlp_page
    ex._rust_extract = lambda o, u: calls.append("rust") or rust_page
    try:
        page = asyncio.run(ex._extract_one(ex.ExtractOptions(), "u"))
        assert calls == ["ytdlp"], "rust must not run when yt-dlp succeeds"
        assert page["items"][0]["formats"][0]["url"] == "https://x/v.mp4"
    finally:
        ex._ytdlp_extract, ex._rust_extract = orig_ytdlp, orig_rust


def test_extract_chain_auto_rust_fallback():
    """auto: yt-dlp empty → rust extractor runs → its media wins."""
    calls = []
    rust_page = {"input": "u", "items": [{"formats": [{"url": "https://r/v.mp4"}]}]}
    orig_ytdlp, orig_rust = ex._ytdlp_extract, ex._rust_extract
    ex._ytdlp_extract = lambda o, u, **k: calls.append("ytdlp") or {
        "input": u, "items": [], "error": "no media found on page"}
    ex._rust_extract = lambda o, u: calls.append("rust") or rust_page
    try:
        page = asyncio.run(ex._extract_one(ex.ExtractOptions(), "u"))
        assert calls == ["ytdlp", "rust"]
        assert page["items"][0]["formats"][0]["url"] == "https://r/v.mp4"
    finally:
        ex._ytdlp_extract, ex._rust_extract = orig_ytdlp, orig_rust


def test_extract_chain_python_ytdlp_skips_rust():
    """python_ytdlp: never touches the rust extractor."""
    calls = []
    orig_ytdlp, orig_rust = ex._ytdlp_extract, ex._rust_extract
    ex._ytdlp_extract = lambda o, u, **k: calls.append("ytdlp") or {"input": u, "items": []}
    ex._rust_extract = lambda o, u: calls.append("rust") or None
    try:
        page = asyncio.run(ex._extract_one(ex.ExtractOptions(extractor_engine="python_ytdlp"), "u"))
        assert calls == ["ytdlp"]
        assert page["items"] == []
    finally:
        ex._ytdlp_extract, ex._rust_extract = orig_ytdlp, orig_rust


def test_extract_chain_rust_engine_skips_ytdlp():
    """custom: the rust extractor runs first and alone."""
    calls = []
    rust_page = {"input": "u", "items": [{"formats": [{"url": "https://r/v.mp4"}]}]}
    orig_ytdlp, orig_rust = ex._ytdlp_extract, ex._rust_extract
    ex._ytdlp_extract = lambda o, u, **k: calls.append("ytdlp") or {"input": u, "items": []}
    ex._rust_extract = lambda o, u: calls.append("rust") or rust_page
    try:
        page = asyncio.run(ex._extract_one(ex.ExtractOptions(extractor_engine="custom"), "u"))
        assert calls == ["rust"]
        assert page["items"][0]["formats"][0]["url"] == "https://r/v.mp4"
    finally:
        ex._ytdlp_extract, ex._rust_extract = orig_ytdlp, orig_rust


def test_extract_chain_generic_off_skips_generic_page_pass():
    """auto + ytdlp_generic=False: a page with no dedicated yt-dlp extractor
    goes straight to Rust — the slow GenericIE page pass never runs."""
    calls = []
    rust_page = {"input": "u", "items": [{"formats": [{"url": "https://r/v.mp4"}]}]}
    orig_ytdlp, orig_rust = ex._ytdlp_extract, ex._rust_extract
    orig_has = ex._has_dedicated_extractor
    ex._ytdlp_extract = lambda o, u, **k: calls.append("ytdlp") or {"input": u, "items": []}
    ex._rust_extract = lambda o, u: calls.append("rust") or rust_page
    ex._has_dedicated_extractor = lambda u: False
    try:
        page = asyncio.run(ex._extract_one(
            ex.ExtractOptions(ytdlp_generic=False), "https://generic-only.x/watch"))
        assert calls == ["rust"], "generic page pass must be skipped"
        assert page["items"][0]["formats"][0]["url"] == "https://r/v.mp4"
    finally:
        ex._ytdlp_extract, ex._rust_extract = orig_ytdlp, orig_rust
        ex._has_dedicated_extractor = orig_has


def test_extract_chain_generic_off_keeps_dedicated_extractors():
    """auto + ytdlp_generic=False still prefers a dedicated yt-dlp extractor
    (YouTube, Vimeo, …) — only the generic fallback is replaced."""
    calls = []
    ytdlp_page = {"input": "u", "items": [{"formats": [{"url": "https://x/v.mp4"}]}]}
    orig_ytdlp, orig_rust = ex._ytdlp_extract, ex._rust_extract
    orig_has = ex._has_dedicated_extractor
    ex._ytdlp_extract = lambda o, u, **k: calls.append("ytdlp") or ytdlp_page
    ex._rust_extract = lambda o, u: calls.append("rust") or {"input": u, "items": []}
    ex._has_dedicated_extractor = lambda u: True
    try:
        page = asyncio.run(ex._extract_one(
            ex.ExtractOptions(ytdlp_generic=False), "https://youtube.com/watch?v=x"))
        assert calls == ["ytdlp"]
        assert page["items"][0]["formats"][0]["url"] == "https://x/v.mp4"
    finally:
        ex._ytdlp_extract, ex._rust_extract = orig_ytdlp, orig_rust
        ex._has_dedicated_extractor = orig_has


def test_extract_chain_generic_off_probes_media_urls_directly():
    """A pasted manifest/media URL is not a page — with the generic page pass
    disabled it still goes to yt-dlp for ladder expansion, not a Rust fetch."""
    calls = []
    ytdlp_page = {"input": "u", "items": [{"formats": [{"url": "https://x/v.m3u8"}]}]}
    orig_ytdlp, orig_rust = ex._ytdlp_extract, ex._rust_extract
    orig_has = ex._has_dedicated_extractor
    ex._ytdlp_extract = lambda o, u, **k: calls.append("ytdlp") or ytdlp_page
    ex._rust_extract = lambda o, u: calls.append("rust") or {"input": u, "items": []}
    ex._has_dedicated_extractor = lambda u: False
    try:
        page = asyncio.run(ex._extract_one(
            ex.ExtractOptions(ytdlp_generic=False), "https://cdn.x/master.m3u8"))
        assert calls == ["ytdlp"], "media URLs probe directly"
        assert page["items"][0]["formats"][0]["url"] == "https://x/v.m3u8"
    finally:
        ex._ytdlp_extract, ex._rust_extract = orig_ytdlp, orig_rust
        ex._has_dedicated_extractor = orig_has


def test_probe_rust_items_sends_extracted_urls_to_ytdlp():
    """Rust-extracted media URLs are sent to yt-dlp for manifest/format
    probing while preserving Rust's scraped page title/thumbnail/duration."""
    probed_urls = []
    orig_ytdlp = ex._ytdlp_extract

    def fake_ytdlp(opts, url, **kw):
        probed_urls.append((url, opts.referer))
        return {
            "input": url,
            "items": [{
                "id": "master",
                "page_url": url,
                "title": "master",
                "duration_secs": 90.0,
                "formats": [
                    {"url": "https://cdn.x/1080.m3u8", "manifest_url": url,
                     "kind": "hls", "repr_id": "1080", "quality": {"height": 1080}},
                    {"url": "https://cdn.x/720.m3u8", "manifest_url": url,
                     "kind": "hls", "repr_id": "720", "quality": {"height": 720}},
                ],
            }],
        }

    ex._ytdlp_extract = fake_ytdlp
    try:
        raw_items = [{
            "id": "page",
            "page_url": "https://site.x/watch",
            "title": "Scraped Video Title",
            "thumbnail": "https://site.x/thumb.jpg",
            "formats": [
                {"url": "https://cdn.x/master.m3u8", "kind": "hls"},
                {"url": "https://cdn.x/fallback.mp4", "kind": "progressive"},
            ],
        }]
        items = ex._probe_rust_items(ex.ExtractOptions(), raw_items, "https://site.x/watch")
        assert probed_urls == [("https://cdn.x/master.m3u8", "https://site.x/watch")], \
            "manifest probe resolves the full ladder — lower candidates skipped"
        assert len(items) == 1
        assert items[0]["title"] == "Scraped Video Title"
        assert items[0]["thumbnail"] == "https://site.x/thumb.jpg"
        assert items[0]["duration_secs"] == 90.0
        assert items[0]["page_url"] == "https://cdn.x/master.m3u8"
        assert [f["repr_id"] for f in items[0]["formats"]] == ["1080", "720"]
        assert items[0]["formats"][0]["headers"]["referer"] == "https://site.x/watch"
    finally:
        ex._ytdlp_extract = orig_ytdlp


def test_probe_rust_items_probes_all_single_format_masters():
    """A manifest resolving to one format is a per-quality master (you-porn)
    — probing continues so every rendition lands in the format list. A
    manifest resolving to a ladder still short-circuits the rest."""
    probed_urls = []
    orig_ytdlp = ex._ytdlp_extract

    def fake_ytdlp(opts, url, **kw):
        probed_urls.append(url)
        h = 720 if "720" in url else 480
        return {
            "input": url,
            "items": [{
                "id": url,
                "page_url": url,
                "formats": [
                    {"url": f"https://cdn.x/{h}.m3u8", "manifest_url": url,
                     "kind": "hls", "repr_id": str(h), "quality": {"height": h}},
                ],
            }],
        }

    ex._ytdlp_extract = fake_ytdlp
    try:
        raw_items = [{
            "id": "page",
            "page_url": "https://site.x/watch",
            "formats": [
                {"url": "https://cdn.x/720/master.m3u8", "kind": "hls"},
                {"url": "https://cdn.x/480/master.m3u8", "kind": "hls"},
            ],
        }]
        items = ex._probe_rust_items(ex.ExtractOptions(), raw_items, "https://site.x/watch")
        assert probed_urls == ["https://cdn.x/720/master.m3u8",
                               "https://cdn.x/480/master.m3u8"], \
            "single-format masters must not short-circuit probing"
        assert [f["repr_id"] for f in items[0]["formats"]] == ["720", "480"]
    finally:
        ex._ytdlp_extract = orig_ytdlp


def test_probe_rust_items_backfills_candidate_metadata():
    """A resolved redirect row loses the candidate's metadata — the
    `_1440p.mp4` name carried the height and container while the probed
    `remote_control.php` row has neither."""
    orig_ytdlp = ex._ytdlp_extract

    def fake_ytdlp(opts, url, **kw):
        return {
            "input": url,
            "items": [{
                "id": "v",
                "page_url": url,
                "formats": [
                    {"url": "https://st.x/remote_control.php?file=abc",
                     "kind": "progressive", "container": "php"},
                ],
            }],
        }

    ex._ytdlp_extract = fake_ytdlp
    try:
        raw_items = [{
            "id": "page",
            "page_url": "https://site.x/watch",
            "formats": [
                {"url": "https://site.x/get_file/1/v_1440p.mp4",
                 "kind": "progressive", "container": "mp4",
                 "quality": {"height": 1440, "label": "1440p"}},
            ],
        }]
        items = ex._probe_rust_items(ex.ExtractOptions(), raw_items, "https://site.x/watch")
        f = items[0]["formats"][0]
        assert f["quality"]["height"] == 1440
        assert f["quality"]["label"] == "1440p"
        assert f["container"] == "mp4", "junk container must yield to the candidate's"
    finally:
        ex._ytdlp_extract = orig_ytdlp


def test_stat_missing_fills_progressive_rows_only():
    """Every shown row stats: HEAD+range fills size/codecs on progressive
    rows that lack them — hls rows and already-probed files are skipped."""
    statd = []
    orig = ex._stat_row
    ex._stat_row = lambda f, o: statd.append(f) or f.update({"probed_size": 1})
    try:
        rows = [
            {"url": "https://x/a.mp4", "kind": "progressive"},
            {"url": "https://x/b.m3u8", "kind": "hls"},
            {"url": "https://x/c.mp4", "kind": "progressive",
             "probed_size": 9, "codecs": "avc1"},
            {"url": "https://x/d.mp4", "kind": "progressive", "probed_size": 9},
        ]
        ex._stat_missing(rows, ex.ExtractOptions())
        assert [f["url"] for f in statd] == ["https://x/a.mp4", "https://x/d.mp4"]
        assert rows[0]["probed_size"] == 1
    finally:
        ex._stat_row = orig


def test_link_container_from_script_query():
    """A probed remote_control.php row must surface its real container, not
    the script's .php extension."""
    link = ex._link({
        "url": "https://st.x/remote_control.php?file=a_1440p.mp4&acctoken=x",
        "ext": "php", "vcodec": "avc1", "acodec": "none", "format_id": "fmp4",
    }, False, None)
    assert link and link["container"] == "mp4"


def test_sniffed_extract_accepts_remote_control_links():
    """A captured KVS script URL is media — its real name rides the query."""
    from dax.models import ContextHeaders
    opts = ex.ExtractOptions(
        referer="https://pimpbunny.com/videos/x",
        contexts=[ContextHeaders(
            url="https://st43.pimpbunny.com/remote_control.php"
                "?file=a_720p.mp4&acctoken=x",
            headers=[])],
    )
    items = ex._sniffed_extract(opts, "https://pimpbunny.com/videos/x")
    assert items and items[0]["formats"][0]["container"] == "mp4"


def test_probe_rust_items_keeps_per_row_probe_url():
    """One item can merge rows from several manifest probes — each row keeps
    its own probe URL so the download replays the matching cached info."""
    orig_ytdlp = ex._ytdlp_extract

    def fake_ytdlp(opts, url, **kw):
        height = int(url.rsplit("/", 1)[-1].removesuffix(".m3u8"))
        return {
            "input": url,
            "items": [{
                "id": "master",
                "page_url": url,
                "formats": [
                    {"url": f"https://cdn.x/{height}/v.m3u8", "manifest_url": url,
                     "kind": "hls", "repr_id": str(height),
                     "quality": {"height": height}},
                ],
            }],
        }

    ex._ytdlp_extract = fake_ytdlp
    try:
        raw_items = [{
            "id": "page",
            "page_url": "https://site.x/watch",
            "formats": [
                {"url": "https://cdn.x/720.m3u8", "kind": "hls"},
                {"url": "https://cdn.x/1080.m3u8", "kind": "hls"},
            ],
        }]
        items = ex._probe_rust_items(ex.ExtractOptions(), raw_items, "https://site.x/watch")
        rows = items[0]["formats"]
        assert [(f["quality"]["height"], f["page_url"]) for f in rows] == [
            (1080, "https://cdn.x/1080.m3u8"),
            (720, "https://cdn.x/720.m3u8"),
        ], "each row must carry its own probe URL"
    finally:
        ex._ytdlp_extract = orig_ytdlp


def test_extract_chain_sniffed_is_last_hop():
    """auto with both extractors empty → extension-sniffed links win."""
    from dax.models import ContextHeaders
    orig_ytdlp, orig_rust = ex._ytdlp_extract, ex._rust_extract
    ex._ytdlp_extract = lambda o, u, **k: {"input": u, "items": []}
    ex._rust_extract = lambda o, u: {"input": u, "items": []}
    try:
        opts = ex.ExtractOptions(
            contexts=[ContextHeaders(url="https://cdn.x/v.mp4", headers=[])])
        page = asyncio.run(ex._extract_one(opts, "https://x/watch"))
        assert page["extractor"] == "extension"
        assert page["items"][0]["formats"][0]["kind"] == "progressive"
    finally:
        ex._ytdlp_extract, ex._rust_extract = orig_ytdlp, orig_rust


def test_browser_jar_loads_once_per_key():
    """The browser cookie store is decrypted once per (browser, file,
    mtime) — a probe run spawns a YoutubeDL per candidate, so each instance
    seeds from the shared jar instead of re-decrypting."""
    import http.cookiejar
    calls = []
    jar_obj = http.cookiejar.CookieJar()

    class FakeYDL:
        def __init__(self, params):
            calls.append(params)
            self.cookiejar = jar_obj

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    orig = ex.YoutubeDL
    ex.YoutubeDL = FakeYDL
    try:
        opts = ex.ExtractOptions(cookies_browser="brave")
        assert ex._browser_jar(opts) is jar_obj
        assert ex._browser_jar(opts) is jar_obj
        assert len(calls) == 1, "second call must hit the jar cache"
        assert calls[0]["cookiesfrombrowser"] == ("brave",)
        assert ex._browser_jar(ex.ExtractOptions()) is None
    finally:
        ex.YoutubeDL = orig
        ex._cookie_jar_cache.clear()


def test_extract_chain_skips_ytdlp_for_broken_ie_host():
    """A host whose dedicated extractor crashed on site markup (yt-dlp's
    'please report this issue' tag) goes straight to the Rust extractor —
    the doomed yt-dlp page pass is skipped until the mark expires."""
    calls = []
    orig_ytdlp, orig_rust = ex._ytdlp_extract, ex._rust_extract
    ex._ytdlp_extract = lambda o, u, **k: calls.append("ytdlp") or {"input": u, "items": []}
    ex._rust_extract = lambda o, u: calls.append("rust") or {
        "input": u, "items": [{"formats": [{"url": "https://r/v.mp4"}]}]}
    ex._broken_ie.insert("broken.x", True)
    try:
        page = asyncio.run(ex._extract_one(ex.ExtractOptions(), "https://broken.x/watch"))
        assert calls == ["rust"], "marked host must skip the yt-dlp pass"
        assert page["items"][0]["formats"][0]["url"] == "https://r/v.mp4"
    finally:
        ex._ytdlp_extract, ex._rust_extract = orig_ytdlp, orig_rust
        ex._broken_ie.pop("broken.x", None)


def test_is_extractor_bug_and_mark():
    bug = ValueError("x: Failed to parse JSON; please report this issue on "
                     "https://github.com/yt-dlp/yt-dlp/issues?q=")
    assert ex._is_extractor_bug(bug)
    assert not ex._is_extractor_bug(ValueError("HTTP Error 403"))
    ex._mark_ie_broken("https://site.x/watch")
    assert ex._ie_broken("https://site.x/other")
    assert not ex._ie_broken("https://other.x/watch")
    ex._broken_ie.pop("site.x", None)


def test_cookie_seeds_from_contexts():
    ctxs = [{"url": "https://cdn.site.com/v.m3u8", "headers": [
        {"name": "Cookie", "value": "sess=abc; age=1"},
        {"name": "Origin", "value": "https://site.com"},
    ]}, {"url": "https://other.com/x", "headers": [
        {"name": "cookie", "value": "k=v"},
    ]}]
    assert ex._cookie_seeds(ctxs) == [
        ("cdn.site.com", "sess=abc; age=1"), ("other.com", "k=v")]
    assert ex._cookie_seeds([{"url": "https://x", "headers": []}]) == []


# ---- download params ---------------------------------------------------------

def test_ydl_params_format_from_repr_id():
    t = dl.Task("g", {"url": "https://x", "reprId": "137"})
    p = dl._ydl_dl_params(t, _settings(), _db()[0])
    O = ex.ORIG_AUDIO
    assert p["format"] == f"137[acodec!=none]/137+{O}/137+ba/137/bv*+{O}/bv*+ba/b"
    t2 = dl.Task("g", {"url": "https://x"})
    assert dl._ydl_dl_params(t2, _settings(), _db()[0])["format"] == \
        f"bv*+{O}/bv*+ba/b"


def test_format_pick_quality_matrix():
    """The picked row's quality drives the format: video rows merge the
    ORIGINAL audio track (plain best-audio when unmarked); audio rows are
    original-then-best; HLS variants prefer the complete stream, DASH/page
    rows merge."""
    O = ex.ORIG_AUDIO
    # Audio row → original audio, else best available.
    assert dl._format_pick({"quality": {"audioOnly": True}}) == f"{O}/ba/b"
    # Page/DASH video row: picked height + original audio merged.
    assert dl._format_pick({"quality": {"height": 720}}) == \
        f"bv*[height<=720]+{O}/bv*[height<=720]+ba/b[height<=720]/bv*+{O}/bv*+ba/b"
    # HLS variant row: the muxed variant at/below the pick first.
    assert dl._format_pick({"media": "hls", "quality": {"height": 1080}}) == \
        f"b[height<=1080]/bv*[height<=1080]+{O}/bv*[height<=1080]+ba/bv*+{O}/bv*+ba/b"
    # Bandwidth-only rows (HLS variants without resolution).
    assert dl._format_pick({"media": "hls", "quality": {"bandwidth": 3_000_000}}) == \
        f"b[tbr<=3000]/bv*[tbr<=3000]+{O}/bv*[tbr<=3000]+ba/bv*+{O}/bv*+ba/b"
    assert dl._format_pick({"quality": {"bandwidth": 3_000_000}}) == \
        f"bv*[tbr<=3000]+{O}/bv*[tbr<=3000]+ba/b[tbr<=3000]/bv*+{O}/bv*+ba/b"
    # A reprId names an exact yt-dlp row and wins over the media kind —
    # the UI marks every repr row 'dash', including yt-dlp page picks
    # (the 1080p→360p regression: b[height<=h] prefers muxed = fmt 18).
    assert dl._format_pick({"reprId": "137", "quality": {"height": 1080}}) == \
        f"137[acodec!=none]/137+{O}/137+ba/137/" \
        f"bv*[height<=1080]+{O}/bv*[height<=1080]+ba/b[height<=1080]/bv*+{O}/bv*+ba/b"
    assert dl._format_pick(
        {"media": "dash", "reprId": "399", "quality": {"height": 1080}}) == \
        f"399[acodec!=none]/399+{O}/399+ba/399/" \
        f"bv*[height<=1080]+{O}/bv*[height<=1080]+ba/b[height<=1080]/bv*+{O}/bv*+ba/b"
    # MPD rep ids aren't yt-dlp ids — the rid rungs miss at resolve time and
    # the height ladder lands the picked variant.
    assert dl._format_pick({"media": "hls", "reprId": "v9", "quality": {"height": 1080}}) == \
        f"v9[acodec!=none]/v9+{O}/v9+ba/v9/" \
        f"bv*[height<=1080]+{O}/bv*[height<=1080]+ba/b[height<=1080]/bv*+{O}/bv*+ba/b"


def test_format_pick_resolves_original_audio():
    """yt-dlp's own resolver honors the original-audio rung: a '(original)'-
    marked track wins over a higher-tbr dub; unmarked pages still pair the
    plain best audio."""
    from yt_dlp import YoutubeDL
    fmts = [
        {"format_id": "v", "vcodec": "av01", "acodec": "none", "tbr": 2000.0,
         "height": 1080, "url": "uv", "ext": "mp4", "protocol": "https"},
        {"format_id": "a-orig", "vcodec": "none", "acodec": "opus",
         "format_note": "(original)", "tbr": 97.0, "url": "ua",
         "ext": "webm", "protocol": "https"},
        {"format_id": "a-dub", "vcodec": "none", "acodec": "opus",
         "format_note": "Hindi", "tbr": 200.0, "url": "ud",
         "ext": "webm", "protocol": "https"},
    ]
    spec = dl._format_pick({"reprId": "v"})
    with YoutubeDL({"quiet": True}) as ydl:
        picked = list(ydl.build_format_selector(spec)({"formats": fmts}))
    assert picked[0]["format_id"] == "v+a-orig"
    del fmts[1]["format_note"]  # single-language page: no marker → +ba wins
    with YoutubeDL({"quiet": True}) as ydl:
        picked = list(ydl.build_format_selector(spec)({"formats": fmts}))
    assert picked[0]["format_id"] == "v+a-dub"


def test_remux_honors_container_pick():
    """Single-format streams remux to the settings pick (stream copy) —
    the container setting applies to every download, not just merges."""
    p = dl._ydl_dl_params(dl.Task("g", {"url": "u"}), _settings(media_format="mkv"), _db()[0])
    assert p["merge_output_format"] == "mkv"
    remuxer = p["postprocessors"][0]
    assert remuxer == {"key": "FFmpegVideoRemuxer", "preferedformat": "mkv"}
    p2 = dl._ydl_dl_params(dl.Task("g", {"url": "u"}), _settings(), _db()[0])
    assert p2["postprocessors"][0]["preferedformat"] == "mp4"


def test_embed_chapters_standalone():
    """Chapters embed without metadata tags when only chapters are wanted."""
    s = _settings(embed_metadata=False, embed_chapters=True)
    p = dl._ydl_dl_params(dl.Task("g", {"url": "u"}), s, _db()[0])
    pp = next(x for x in p["postprocessors"] if x["key"] == "FFmpegMetadata")
    assert pp.get("add_metadata") is False
    s2 = _settings(embed_metadata=True, embed_chapters=False)
    p2 = dl._ydl_dl_params(dl.Task("g", {"url": "u"}), s2, _db()[0])
    pp2 = next(x for x in p2["postprocessors"] if x["key"] == "FFmpegMetadata")
    assert "add_metadata" not in pp2, "full metadata PP carries chapters along"


def test_ydl_params_settings_mapping():
    cookies = _TMP / "cookies.txt"
    cookies.write_text("")
    s = _settings(media_format="mkv", embed_thumbnail=True, embed_subtitles=True,
                  embed_metadata=True, check_certificate=False,
                  cookies_browser="chrome", parallel_segments=12,
                  player_client="ios", cookies_file=str(cookies))
    t = dl.Task("g", {"url": "https://x", "filenameHint": "vid",
                      "referer": "https://x/page"})
    p = dl._ydl_dl_params(t, s, _db()[0])
    assert p["merge_output_format"] == "mkv"
    assert p["concurrent_fragment_downloads"] == 12
    assert p["nocheckcertificate"] is True
    assert p["cookiesfrombrowser"] == ("chrome",)
    assert p["extractor_args"]["youtube"] == {"player_client": ["ios", "-web_embedded"]}
    assert p["extractor_args"]["generic"] == {"impersonate": ["chrome"]}
    assert p["outtmpl"] == "vid.%(ext)s"
    assert p["http_headers"]["Referer"] == "https://x/page"
    assert p["cookiefile"] == str(cookies)
    # Rust embed_thumbnail policy: aux files stage in the task tmp dir,
    # never next to the output in the download dir.
    assert p["paths"]["home"] == out_dir(s)
    assert p["paths"]["thumbnail"] == p["paths"]["temp"]
    assert p["paths"]["subtitle"] == p["paths"]["temp"]
    assert [x["key"] for x in p["postprocessors"]] == [
        "FFmpegVideoRemuxer", "FFmpegEmbedSubtitle", "FFmpegMetadata",
        "FFmpegThumbnailsConvertor", "EmbedThumbnail"
    ], "yt-dlp upstream order — remuxer first, EmbedThumbnail last (mkv covers)"
    assert p["writethumbnail"] is True and p["writesubtitles"] is True
    assert "writeautomaticsub" not in p
    # Declared default: untouched settings merge to mp4; no embeds configured.
    p2 = dl._ydl_dl_params(
        dl.Task("g", {"url": "u"}),
        _settings(embed_thumbnail=False, embed_subtitles=False,
                  embed_metadata=False, embed_chapters=False),
        _db()[0],
    )
    # Fresh settings still steer YouTube off the broken embedded client.
    assert p2["extractor_args"]["youtube"]["player_client"] == [
        "default", "-web_embedded"
    ]
    assert p2["merge_output_format"] == "mp4"
    assert p2["postprocessors"][0]["preferedformat"] == "mp4"
    assert [x["key"] for x in p2["postprocessors"]] == ["FFmpegVideoRemuxer"]


def test_youtube_client_args_excludes_broken_embedded():
    """web_embedded's config download crashes upstream
    (KeyError INNERTUBE_CONTEXT in _fix_embedded_ytcfg — yt-dlp#14826
    path). Excluded from every pick except an explicit selection."""
    assert ex.youtube_client_args(None) == ["default", "-web_embedded"]
    assert ex.youtube_client_args("default") == ["default", "-web_embedded"]
    assert ex.youtube_client_args("ios") == ["ios", "-web_embedded"]
    assert ex.youtube_client_args("web_embedded") == ["web_embedded"]
    # The probe params carry it too (same crash surface).
    opts = ex.ExtractOptions()
    params, _ = ex._ydl_params(opts, "https://www.youtube.com/watch?v=x")
    assert params["extractor_args"]["youtube"]["player_client"] == [
        "default", "-web_embedded"
    ]


def test_progress_hook_accumulates_and_persists():
    db, _ = _db()
    db.insert_task("g9", {"url": "u", "expectedSize": 5000}, "waiting", 5000, 1)
    t = dl.Task("g9", {"url": "u", "expectedSize": 5000})
    h = dl._progress_hook(t, db)
    h({"status": "downloading", "downloaded_bytes": 400,
       "total_bytes": 1000, "speed": 5})
    assert t.status == "active" and t.downloaded == 400
    assert t.total == 5000, "expectedSize seeds the total; hook totals only raise it"
    h({"status": "finished", "total_bytes": 1000})
    assert t.done_bytes == 1000
    # A+V: the second leg rides on the finished first leg.
    h({"status": "downloading", "downloaded_bytes": 300, "total_bytes": 900, "speed": 7})
    assert t.downloaded == 1300
    # Persisted to the db — the 800ms throttle writes the first tick.
    row = db.load_all_tasks("/x")
    assert row[0][0]["completedLength"] == "400"


def test_progress_hook_fragment_estimate_converges():
    """HLS/DASH: byte estimates swing with fragment-size variance — fragment
    progress is the stable signal; the byte total stays untouched."""
    db, _ = _db()
    t = dl.Task("gf", {"url": "u"})
    h = dl._progress_hook(t, db)
    # Fragment 1 is a big one (VBR) — the estimate spikes to 96MB.
    h({"status": "downloading", "downloaded_bytes": 4_000_000,
       "total_bytes_estimate": 96_000_000, "fragment_count": 24,
       "fragment_index": 0, "speed": 5})
    assert t.downloaded == 4_000_000
    assert t.total == 0, "no byte total for fragment downloads"
    assert (t.frags_done, t.frags_total) == (0, 24)
    # Fragments complete — the fragment count is the stable progress.
    h({"status": "downloading", "downloaded_bytes": 6_000_000,
       "total_bytes_estimate": 14_000_000, "fragment_count": 24,
       "fragment_index": 12, "speed": 5})
    assert (t.frags_done, t.frags_total) == (12, 24)
    assert t.total == 0
    # The snapshot carries fragment progress for the UI bar.
    entry = t.snapshot_entry("/x")
    assert entry["fragments"] == {"done": 12, "total": 24}

    t2 = dl.Task("gp", {"url": "u"})
    h2 = dl._progress_hook(t2, db)
    h2({"status": "downloading", "downloaded_bytes": 7_000_000,
        "total_bytes": 12_000_000, "speed": 5})
    assert t2.total == 12_000_000
    h2({"status": "downloading", "downloaded_bytes": 9_000_000,
        "total_bytes": 12_000_000, "speed": 5})
    assert t2.total == 12_000_000 and t2.downloaded == 9_000_000
    assert "fragments" not in t2.snapshot_entry("/x")


def test_progress_hook_resume_never_dips():
    """Retry/resume relaunches the worker with done_bytes=0 while the db-
    persisted progress stays on the task — a finished A+V leg is not
    re-reported, so the bar must floor at the persisted value, not dip."""
    t = dl.Task("gr", {"url": "u"})
    t.downloaded = 8_000_000  # restored from completedLength
    h = dl._progress_hook(t, _db()[0])
    # Relaunch tick: per-file bytes only (the finished video leg is gone
    # from the count) — below the persisted progress.
    h({"status": "downloading", "downloaded_bytes": 2_000_000,
       "total_bytes": 3_000_000, "speed": 5})
    assert t.downloaded == 8_000_000, "resume must not dip below persisted"
    # New bytes pass the floor — progress continues from there.
    h({"status": "downloading", "downloaded_bytes": 9_000_000,
       "total_bytes": 12_000_000, "speed": 5})
    assert t.downloaded == 9_000_000


def test_pp_hook_waits_for_download_phase():
    """before_dl postprocessors (thumbnail convertor, pair-size seed) fire
    their hooks before any byte moves — they must not flip the task to
    'postprocessing'; post-download processors do."""
    t = dl.Task("g", {"url": "u"})
    h = dl._pp_hook(t)
    h({"status": "started", "postprocessor": "FFmpegThumbnailsConvertor"})
    assert t.status == "waiting", "before_dl PP must not fake postprocessing"
    ph = dl._progress_hook(t, _db()[0])
    ph({"status": "downloading", "downloaded_bytes": 100, "total_bytes": 1000, "speed": 5})
    assert t.status == "active"
    h({"status": "started", "postprocessor": "FFmpegMetadata"})
    assert t.status == "postprocessing"


def _fake_dl(resp, boom=False):
    """A real quiet YoutubeDL with urlopen patched — the PP's hook chain
    (params, to_screen, warnings) runs for real, the network does not."""
    from yt_dlp import YoutubeDL as RealYdl

    ydl = RealYdl({"quiet": True})

    def urlopen(url):
        if boom:
            raise OSError("down")
        return resp

    ydl.urlopen = urlopen
    return ydl


def test_req_thumbnail_pp_downloads_and_injects():
    """Non-yt-dlp sites: the req's captured thumbnail is fetched into the
    task tmp dir and injected so EmbedThumbnail embeds it."""
    import io

    tmp = _TMP / "thumbpp"
    t = dl.Task("g", {"url": "u", "thumbnail": "https://cdn.x/cover.jpg"})
    pp = dl._ReqThumbnailPP(t, str(tmp))
    pp._downloader = _fake_dl(io.BytesIO(b"imgdata"))
    files, info = pp.run({})
    path = info["thumbnails"][0]["filepath"]
    assert path.endswith("cover.jpg") and Path(path).read_bytes() == b"imgdata"
    assert files == []


def test_req_thumbnail_pp_skips_extractors_and_failures():
    import io

    tmp = _TMP / "thumbpp2"
    existing = tmp / "cover.jpg"
    tmp.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"real")

    # The extractor already put a thumbnail on disk — untouched.
    t = dl.Task("g", {"url": "u", "thumbnail": "https://cdn.x/cover.jpg"})
    pp = dl._ReqThumbnailPP(t, str(tmp))
    pp._downloader = _fake_dl(io.BytesIO(b"x"))
    _, info = pp.run({"thumbnails": [{"filepath": str(existing)}]})
    assert info["thumbnails"][0]["filepath"] == str(existing)

    # No thumbnail on disk + fetch failure — best effort, no raise.
    t2 = dl.Task("g", {"url": "u", "thumbnail": "https://cdn.x/c.jpg"})
    pp2 = dl._ReqThumbnailPP(t2, str(tmp))
    def _boom(url):
        raise OSError("down")
    pp2._downloader = _fake_dl(None, boom=True)
    _, info2 = pp2.run({})
    assert "thumbnails" not in info2


def test_fix_thumbnail_exts_corrects_mislabeled_bytes():
    """CDNs serve AVIF at .jpg URLs — AVIF gets transcoded to real JPEG
    (image2 can't probe it) or dropped; other mismatches are renamed."""
    tmp = _TMP / "thumbfix"
    tmp.mkdir(parents=True, exist_ok=True)

    # AVIF without an ffmpeg path → thumb dropped, ftm entry cleared.
    avif = tmp / "cover.jpg"
    avif.write_bytes(b"\x00\x00\x00\x1cftypavif" + b"\x00" * 64)
    info = {"thumbnails": [{"filepath": str(avif)}]}
    ftm = {str(avif): str(tmp / "final.jpg")}
    dl._fix_thumbnail_exts(info, ftm)
    assert info["thumbnails"][0]["filepath"] is None
    assert ftm == {}

    # AVIF + ffmpeg → real JPEG bytes at the same .jpg path.
    import shutil, subprocess as sp
    ff = shutil.which("ffmpeg")
    if ff:
        avif2 = tmp / "cover2.jpg"
        sp.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i",
                "testsrc=duration=1:size=64x64:rate=1", "-frames:v", "1",
                "-c:v", "libsvtav1", "-f", "avif", str(avif2)],
               check=True, capture_output=True)
        info3 = {"thumbnails": [{"filepath": str(avif2)}]}
        dl._fix_thumbnail_exts(info3, {str(avif2): str(avif2)}, ff)
        assert info3["thumbnails"][0]["filepath"] == str(avif2)
        assert dl._sniff_image_ext(str(avif2)) == "jpg"

    # Real jpg stays put; identity ftm entries re-key.
    jpg = tmp / "ok.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 8)
    info2 = {"thumbnails": [{"filepath": str(jpg)}]}
    ftm2 = {str(jpg): str(jpg)}
    dl._fix_thumbnail_exts(info2, ftm2)
    assert info2["thumbnails"][0]["filepath"] == str(jpg)
    assert ftm2 == {str(jpg): str(jpg)}


def test_ranged_fragment_native_fetch_sends_no_range_when_whole():
    """Cloudflare-fronted CDN nodes 410 aria2's curl fingerprint — the
    fallback refetches the whole fragment through yt-dlp's stack."""
    import io

    class Resp(io.BytesIO):
        def __init__(self):
            super().__init__(b"segdata")

    sent = []
    tmp = _TMP / "fragfb"
    tmp.mkdir(parents=True, exist_ok=True)
    out = str(tmp / "frag-0.part")
    ydl = _fake_dl(Resp())

    def spy(req):
        sent.append(dict(req.headers))
        return Resp()

    ydl.urlopen = spy
    fd = dl.aria2_next.downloader.Aria2NextFD(ydl, {})
    fd._fetch_ranged_fragment("https://cdn/seg.ts", 0, 0, out, {"http_headers": {"Referer": "https://x/"}}, None)
    assert Path(out).read_bytes() == b"segdata"
    assert "Range" not in sent[0] and sent[0]["Referer"] == "https://x/"


def test_expect_pp_repairs_unsafe_ext():
    """Script URLs (remote_control.php?file=…mp4) yield ext='php' which
    yt-dlp's existing_file check rejects — before_dl must substitute the
    format's real container ext."""
    t = dl.Task("g", {"url": "u"})
    pp = dl._ExpectPP(t)
    _, info = pp.run({"ext": "php", "requested_formats": [{"ext": "fmp4"}]})
    assert info["ext"] == "mp4"  # fmp4 isn't on the allowlist either
    _, info = pp.run({"ext": "php", "vcodec": "h264"})
    assert info["ext"] == "mp4"
    _, info = pp.run({"ext": "mp4"})  # safe ext untouched
    assert info["ext"] == "mp4"


def test_sanitize_ext_resolves_script_path_query():
    """remote_control.php?file=…mp4 yields ext='php'; the real ext rides the
    query and must replace it before the output name is prepared."""
    info = {
        "ext": "php",
        "url": "https://st.x/remote_control.php?file=abc_1440p.mp4&acctoken=x",
    }
    dl._sanitize_ext(info)
    assert info["ext"] == "mp4"
    # A split pair follows the video leg's container.
    info = {"ext": "php", "requested_formats": [
        {"ext": "php", "vcodec": "avc1",
         "url": "https://st.x/remote_control.php?file=v.mp4"},
        {"ext": "php", "vcodec": "none",
         "url": "https://st.x/remote_control.php?file=a.m4a"},
    ]}
    dl._sanitize_ext(info)
    assert info["ext"] == "mp4"
    safe = {"ext": "mkv"}
    dl._sanitize_ext(safe)
    assert safe["ext"] == "mkv"


def test_prepare_filename_sanitizes_script_ext():
    """The output name is decided before before_dl runs — the YoutubeDL
    subclass must fix the ext at prepare_filename time."""
    with dl.YoutubeDL({"outtmpl": "%(title)s.%(ext)s",
                       "paths": {"home": "/tmp"}}) as ydl:
        name = ydl.prepare_filename({
            "title": "T", "ext": "php",
            "url": "https://st.x/remote_control.php?file=a_1440p.mp4",
        })
    assert name.endswith("/T.mp4"), name


def test_expect_pp_seeds_pair_total():
    t = dl.Task("g", {"url": "u", "expectedSize": 5000})
    pp = dl._ExpectPP(t)
    pp.run({"requested_formats": [{"filesize": 3000}, {"filesize_approx": 3500}]})
    assert t.total == 6500


def test_best_audio_mirrors_ytdlp_ba_pick():
    """`+ba` merges yt-dlp's own pick — codec preference (opus) beats a
    higher-tbr mp4a — so probe must pair the same audio or the shown size
    drifts from the download total (the 653/646 regression)."""
    fmts = [
        {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2",
         "tbr": 129.475, "filesize": 28139214, "url": "u1"},
        {"format_id": "251-19", "vcodec": "none", "acodec": "opus",
         "tbr": 97.778, "filesize": 21248891, "url": "u2"},
    ]
    assert ex._best_audio(fmts, 100)[0] == 28139214  # tbr fallback: mp4a
    pick = lambda ctx: [f for f in ctx["formats"] if f["format_id"] == "251-19"]
    size, tbr = ex._best_audio(fmts, 100, pick)
    assert (size, tbr) == (21248891, 97.778)  # yt-dlp's pick wins
    boom = lambda ctx: (_ for _ in ()).throw(RuntimeError("x"))
    assert ex._best_audio(fmts, 100, boom)[0] == 28139214  # selector fails -> tbr


# ---- control verbs -----------------------------------------------------------

class _FakeState:
    def __init__(self, s, db, registry):
        self._s, self.db, self.registry = s, db, registry

    def settings(self):
        return self._s


def _state_with_task(status: str = "active"):
    db, _ = _db()
    s = _settings()
    reg = dl.Registry(db, s)
    t = reg.add({"url": "https://x/v.mp4", "createdAt": 1})
    t.status = status
    return _FakeState(s, db, reg), t


def test_control_pause_and_resume():
    state, t = _state_with_task("active")
    r, e = dl._control_local(state, t, "pause")
    assert (r, e) == ("OK", None) and t.status == "paused"
    assert state.db.load_all_tasks("/x")[0][0]["status"] == "paused"
    orig = dl._run_task
    dl._run_task = lambda *a: None
    try:
        dl._control_local(state, t, "unpause")
        assert t.status == "waiting"
    finally:
        dl._run_task = orig
        if t.thread:
            t.thread.join(5)


def test_control_pause_terminal_is_noop():
    state, t = _state_with_task("complete")
    dl._control_local(state, t, "pause")
    assert t.status == "complete", "pausing a finished task must not resurrect it"


def test_control_remove_and_clear_result():
    state, t = _state_with_task("complete")
    r, e = dl._control_local(state, t, "remove")
    assert (r, e) == ("OK", None)
    assert state.registry.get(t.gid) is None
    assert state.db.load_all_tasks("/x") == []
    # clearResult = remove (Rust parity): works on live tasks too.
    state2, t2 = _state_with_task("active")
    r, e = dl._control_local(state2, t2, "clearResult")
    assert (r, e) == ("OK", None)
    assert state2.registry.get(t2.gid) is None


def test_control_purge_and_pause_all():
    state, t = _state_with_task("complete")
    _, t2 = _state_with_task("active")
    _, t3 = _state_with_task("error")
    # One shared registry view for the global verbs.
    state.registry._tasks[t2.gid] = t2
    state.registry._tasks[t3.gid] = t3
    r, e = dl._control_local(state, None, "pauseAll")
    assert (r, e) == ("OK", None)
    assert t2.status == "paused"
    r, e = dl._control_local(state, None, "purge")
    assert (r, e) == ("OK", None)
    assert state.registry.get(t.gid) is None, "complete task purged"
    assert state.registry.get(t2.gid) is not None, "paused task survives purge"
    assert state.registry.get(t3.gid) is not None, "errored task survives purge"


def test_control_delete_removes_file_on_disk():
    state, t = _state_with_task("complete")
    f = _TMP / f"dl-{t.gid}.mp4"
    f.write_text("dummy video")
    t.filename = str(f)
    assert f.exists()
    r, e = dl._control_local(state, t, "delete")
    assert (r, e) == ("OK", None)
    assert not f.exists(), "delete must remove downloaded file from disk"
    assert state.registry.get(t.gid) is None
    assert state.db.load_all_tasks("/x") == []


def test_control_restart_and_unpause_all():
    state, t = _state_with_task("error")
    t.error = "network failed"
    t.downloaded = 100
    launched = []
    orig_launch = dl.Registry.launch
    dl.Registry.launch = lambda self, task, s: launched.append(task.gid)
    try:
        r, e = dl._control_local(state, t, "download")
        assert (r, e) == ("OK", None)
        assert t.status == "waiting" and t.error == "" and t.downloaded == 0
        assert launched == [t.gid]

        launched.clear()
        t.status = "paused"
        r, e = dl._control_local(state, None, "unpauseAll")
        assert (r, e) == ("OK", None)
        assert launched == [t.gid]
    finally:
        dl.Registry.launch = orig_launch


def test_control_unknown_action():
    state, t = _state_with_task("active")
    r, e = dl._control_local(state, t, "explode")
    assert r is None and "unknown action" in (e or "")


def test_add_download_page_routing():
    """When repr_id and page_url are set, download URL routes to page_url;
    without repr_id, the media URL downloads directly."""
    state, _ = _state_with_task("complete")
    launched = []
    orig_launch = dl.Registry.launch
    dl.Registry.launch = lambda self, t, s: launched.append(t.gid)
    try:
        req = DownloadReq(
            url="https://cdn.x/f.mp4", page_url="https://x/watch", repr_id="22")
        gid, e = asyncio.run(dl.add_download(state, req))
        assert e is None and gid
        assert state.registry.get(gid).req["url"] == "https://x/watch"
        assert launched == [gid]
        # referer present, no repr_id → media URL downloads directly.
        req2 = DownloadReq(url="https://cdn.x/g.mp4", referer="https://x/")
        gid2, e2 = asyncio.run(dl.add_download(state, req2))
        assert e2 is None and state.registry.get(gid2).req["url"] == "https://cdn.x/g.mp4"
    finally:
        dl.Registry.launch = orig_launch


# ---- robustness fixes --------------------------------------------------------

def test_extract_chain_auto_surfaces_rust_drm_rows():
    """auto: yt-dlp returns empty/error, rust finds only-DRM items — the
    rust rows must surface so the user sees the DRM indicator."""
    orig_ytdlp, orig_rust = ex._ytdlp_extract, ex._rust_extract
    rust_page = {"input": "u", "items": [
        {"formats": [{"url": "https://x/v.mpd", "drm": {"type": "widevine"}}]}]}
    ex._ytdlp_extract = lambda o, u, **k: {"input": u, "items": [], "error": "no media found"}
    ex._rust_extract = lambda o, u: rust_page
    try:
        page = asyncio.run(ex._extract_one(ex.ExtractOptions(), "u"))
        assert page is rust_page, "drm-flagged rows must reach the user"
    finally:
        ex._ytdlp_extract, ex._rust_extract = orig_ytdlp, orig_rust


def test_registry_restore_preserves_error_and_created_at():
    """Restored error tasks keep their errorMessage and createdAt."""
    db, _ = _db()
    db.insert_task("ge", {"url": "https://x/v"}, "error", 1000, 12345)
    db.mark_error("ge", "1", "network down")
    reg = dl.Registry(db, _settings())
    reg.restore(db.load_all_tasks("/downloads"))
    t = reg.get("ge")
    assert t is not None
    assert t.error == "network down"
    assert t.created_at == 12345


def test_launch_paused_while_queued_on_semaphore_never_runs():
    """A task paused while waiting for a concurrency slot on _sem must not
    enter _run_task when the slot finally frees."""
    db, _ = _db()
    reg = dl.Registry(db, _settings(max_concurrent_downloads=1))
    assert reg._sem.acquire(blocking=False), "occupy the single concurrency slot"
    t = reg.add({"url": "https://x/v.mp4", "createdAt": 1})
    ran = []
    orig = dl._run_task
    dl._run_task = lambda *a: ran.append(a)
    try:
        reg.launch(t, _settings(max_concurrent_downloads=1))
        # Pause while t's worker thread is blocked waiting on reg._sem
        with t.lock:
            t.status = "paused"
        t.terminate_proc()
        reg._sem.release()
        if t.thread:
            t.thread.join(5)
        assert ran == [], "paused queued task must not run when permit frees"
    finally:
        dl._run_task = orig


def test_extract_per_url_failure_isolation():
    """One malformed URL must not sink the batch — per-URL failures are
    carried in the result, never thrown."""
    ex._result_cache.clear()
    orig = ex._extract_one

    async def boom(opts, u):
        if "bad" in u:
            raise ValueError("malformed")
        return {"input": u, "items": [{"formats": [{"url": "https://x/v.mp4"}]}]}

    ex._extract_one = boom
    try:
        pages = asyncio.run(ex.extract(
            ex.ExtractOptions(), ["https://ok/1", "bad-url", "https://ok/2"]))
        assert len(pages) == 3
        assert pages[0]["items"] and pages[2]["items"], "good urls still extract"
        assert pages[1]["input"] == "bad-url"
        assert pages[1]["items"] == [] and pages[1]["error"] == "malformed"
    finally:
        ex._extract_one = orig


def test_extract_result_caches_success_only():
    """A second identical probe reuses the result; empty/error pages are
    never cached so a retry re-probes."""
    ex._result_cache.clear()
    calls = []
    orig = ex._extract_one

    async def fake(opts, u):
        calls.append(u)
        if "good" in u:
            return {"input": u, "items": [{"formats": [{"url": "https://x/v.mp4"}]}]}
        return {"input": u, "items": [], "error": "none"}

    ex._extract_one = fake
    try:
        opts = ex.ExtractOptions()
        asyncio.run(ex.extract(opts, ["https://good"]))
        asyncio.run(ex.extract(opts, ["https://good"]))
        assert calls == ["https://good"], "identical probe must hit the cache"
        asyncio.run(ex.extract(opts, ["https://empty"]))
        asyncio.run(ex.extract(opts, ["https://empty"]))
        assert calls == ["https://good", "https://empty", "https://empty"], \
            "error/empty pages re-probe every time"
    finally:
        ex._extract_one = orig
        ex._result_cache.clear()


def test_extract_single_flight_shares_one_probe():
    """Two concurrent identical probes share one extraction — the second
    awaits the first instead of paying another network round-trip."""
    ex._result_cache.clear()
    calls = []
    orig = ex._extract_one

    async def slow(opts, u):
        calls.append(u)
        await asyncio.sleep(0.05)
        return {"input": u, "items": [{"formats": [{"url": "https://x/v.mp4"}]}]}

    ex._extract_one = slow
    try:
        async def both():
            return await asyncio.gather(
                ex.extract(ex.ExtractOptions(), ["https://same"]),
                ex.extract(ex.ExtractOptions(), ["https://same"]),
            )
        a, b = asyncio.run(both())
        assert calls == ["https://same"], "one extraction, two callers"
        assert a[0]["items"] == b[0]["items"]
    finally:
        ex._extract_one = orig
        ex._result_cache.clear()
        ex._inflight.clear()


# ---- probe → download info cache ----------------------------------------------

def _fake_page_info(vid="vid1", page="https://x/watch?v=1"):
    """A sanitized-shaped video info dict as extract_info would return."""
    return {
        "id": vid,
        "_type": "video",
        "webpage_url": page,
        "original_url": page,
        "title": "T",
        "formats": [
            {"format_id": "h264", "url": "https://cdn.x/v.mp4",
             "vcodec": "avc1", "acodec": "mp4a", "height": 720,
             "ext": "mp4", "protocol": "https", "tbr": 1000.0},
        ],
    }


def test_probe_info_cache_covers_download_urls():
    """The download looks the cached probe info up by the probed input URL
    and by the canonical webpage_url (a repr row's task URL is page_url)."""
    ex._info_cache.clear()
    opts = ex.ExtractOptions()
    info = _fake_page_info()
    item = ex._item(info, None)
    ex._register_info(opts, "https://x/watch?v=1&pp=x", info, [(info, item)])
    for u in ("https://x/watch?v=1&pp=x", "https://x/watch?v=1"):
        hit = ex.cached_info(opts, u)
        assert hit and hit["id"] == "vid1", f"cache must answer for {u}"
    # The handed-out copy is private — process_ie_result mutates it.
    hit["id"] = "mutated"
    assert ex.cached_info(opts, "https://x/watch?v=1")["id"] == "vid1"
    # A different session must not reuse the extraction.
    from dax.models import ContextHeaders, HeaderPair
    ctx_opts = ex.ExtractOptions(contexts=[ContextHeaders(
        url="https://x", headers=[HeaderPair(name="Cookie", value="s=1")])])
    assert ex.cached_info(ctx_opts, "https://x/watch?v=1") is None
    assert ex.cached_info(
        ex.ExtractOptions(cookies_browser="chrome"),
        "https://x/watch?v=1") is None
    ex._info_cache.clear()


def test_drop_cached_info_clears_every_alias():
    """A stale replay drops the entry and the other URLs it was registered
    under — otherwise the poisoned dict is served again."""
    ex._info_cache.clear()
    opts = ex.ExtractOptions()
    info = _fake_page_info()
    ex._register_info(opts, "https://x/watch?v=1&pp=x", info,
                      [(info, ex._item(info, None))])
    assert ex.cached_info(opts, "https://x/watch?v=1") is not None
    ex.drop_cached_info(opts, "https://x/watch?v=1")
    assert ex.cached_info(opts, "https://x/watch?v=1") is None
    assert ex.cached_info(opts, "https://x/watch?v=1&pp=x") is None
    ex._info_cache.clear()


def test_stat_row_ignores_error_body_size():
    """An error stub's Content-Length is the error body's size, not the
    file's — xhpingcdn's 403 "wrong key" page is 9 bytes and must not
    surface as `probed_size`. The range GET doubles as the size source:
    a 206 Content-Range total is trusted when HEAD failed."""
    ex._stat_cache.clear()

    class Resp:
        def __init__(self, status, headers):
            self.status_code = status
            self.headers = headers

        def iter_content(self):
            yield b""

        def close(self):
            pass

    class Denied:
        def head(self, url, headers, timeout):
            return Resp(403, {"content-length": "9", "content-type": "text/plain"})

        def get(self, url, headers, timeout, stream):
            return Resp(403, {"content-type": "text/plain"})

    class Ranged:
        def head(self, url, headers, timeout):
            return Resp(403, {"content-type": "text/plain"})

        def get(self, url, headers, timeout, stream):
            return Resp(206, {
                "content-range": "bytes 0-511/987654",
                "content-type": "video/mp4",
            })

    orig = ex._stat_session
    try:
        ex._stat_session = Denied
        row = {"url": "https://cdn.x/denied.mp4", "kind": "progressive"}
        ex._stat_row(row, ex.ExtractOptions())
        assert "probed_size" not in row and "codecs" not in row

        ex._stat_session = Ranged
        row = {"url": "https://cdn.x/ranged.mp4", "kind": "progressive"}
        ex._stat_row(row, ex.ExtractOptions())
        assert row["probed_size"] == 987654
    finally:
        ex._stat_session = orig
        ex._stat_cache.clear()


def test_stat_row_caches_and_reuses_session():
    """The HEAD/range result is cached by (url, referer) and the same
    per-thread session serves both requests."""
    ex._stat_cache.clear()
    calls = []

    class Resp:
        status_code = 200
        headers = {"content-length": "1234", "content-type": "video/mp4"}

        def iter_content(self):
            yield b"....ftypavc1...."

        def close(self):
            pass

    class Session:
        def head(self, url, headers, timeout):
            calls.append(("head", url))
            return Resp()

        def get(self, url, headers, timeout, stream):
            calls.append(("get", url))
            return Resp()

    orig = ex._stat_session
    ex._stat_session = lambda: Session()
    try:
        row = {"url": "https://cdn.x/v.mp4", "headers": {"referer": "https://x"}}
        ex._stat_row(row, ex.ExtractOptions())
        assert row["probed_size"] == 1234 and "avc1" in row["codecs"]
        assert calls == [("head", row["url"]), ("get", row["url"])]
        row2 = {"url": "https://cdn.x/v.mp4", "headers": {"referer": "https://x"}}
        ex._stat_row(row2, ex.ExtractOptions())
        assert row2["probed_size"] == 1234 and "avc1" in row2["codecs"]
        assert len(calls) == 2, "second row must hit the stat cache"
    finally:
        ex._stat_session = orig
        ex._stat_cache.clear()


def test_probe_info_cache_indexes_playlist_entries():
    """A playlist probe indexes each entry under its own page URL — an entry
    add downloads that video's dict, not the whole list."""
    ex._info_cache.clear()
    opts = ex.ExtractOptions()
    entries = [
        _fake_page_info("e1", "https://x/watch?v=e1"),
        _fake_page_info("e2", "https://x/watch?v=e2"),
    ]
    pl = {"id": "pl", "_type": "playlist", "entries": entries,
          "webpage_url": "https://x/playlist"}
    pairs = [(e, ex._item(e, None)) for e in entries]
    ex._register_info(opts, "https://x/playlist", pl, pairs)
    assert ex.cached_info(opts, "https://x/playlist")["_type"] == "playlist"
    assert ex.cached_info(opts, "https://x/watch?v=e2")["id"] == "e2"
    ex._info_cache.clear()


class _FakeYdl:
    """YoutubeDL stub for _run_task: records which entry point served the
    task so tests can prove the probe cache short-circuits extraction."""
    fail_process = None
    last = None

    def __init__(self, params):
        import http.cookiejar
        self.params = params
        self.calls: list[str] = []
        self.cookiejar = http.cookiejar.CookieJar()
        _FakeYdl.last = self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def add_post_processor(self, pp, when=None):
        pass

    def _write_subtitles(self, info, filename):
        return []

    def _write_thumbnails(self, label, info, filename, thumb_filename_base=None):
        return []

    def post_process(self, filename, info, files_to_move=None):
        return info

    def extract_info(self, url, download=True):
        self.calls.append("extract")
        return {"id": "fresh", "_type": "video"}

    def process_ie_result(self, info, download=True):
        self.calls.append("process")
        if _FakeYdl.fail_process:
            raise _FakeYdl.fail_process
        return info


def _run_with_fake_ydl(task, s, db):
    orig = dl.YoutubeDL
    dl.YoutubeDL = _FakeYdl
    try:
        dl._run_task(task, s, db)
    finally:
        dl.YoutubeDL = orig
    return _FakeYdl.last


def test_download_reuses_cached_probe_info():
    """A download added right after probe feeds the cached info dict to
    process_ie_result — the page is never extracted a second time."""
    db, _ = _db()
    reg = dl.Registry(db, _settings())
    ex._info_cache.clear()
    opts = ex.ExtractOptions()
    info = _fake_page_info()
    # Probe result: the task URL is the canonical page (repr_id picks rows
    # off it — the probe input may carry tracking params like &pp=).
    ex._register_info(
        opts, "https://x/watch?v=1&pp=x", info, [(info, ex._item(info, None))])
    try:
        t = reg.add({"url": "https://x/watch?v=1", "reprId": "h264",
                     "pageUrl": "https://x/watch?v=1", "createdAt": 1})
        ydl = _run_with_fake_ydl(t, _settings(), db)
        assert ydl.calls == ["process"], \
            "cached probe must skip a second extract_info"
        assert t.status == "complete"
        # A different-format add off the same probe reuses the entry too.
        t2 = reg.add({"url": "https://x/watch?v=1", "reprId": "h264",
                      "createdAt": 1})
        ydl2 = _run_with_fake_ydl(t2, _settings(), db)
        assert ydl2.calls == ["process"]
    finally:
        ex._info_cache.clear()


def test_download_extracts_fresh_on_miss_and_stale_replay():
    """No cached probe → extract_info. A cached replay that fails (expired
    signed URLs mid-queue) drops the entry and falls back to extracting."""
    db, _ = _db()
    reg = dl.Registry(db, _settings())
    ex._info_cache.clear()
    try:
        t = reg.add({"url": "https://x/none", "createdAt": 1})
        ydl = _run_with_fake_ydl(t, _settings(), db)
        assert ydl.calls == ["extract"] and t.status == "complete"

        opts = ex.ExtractOptions()
        info = _fake_page_info("stale", "https://x/stale")
        ex._register_info(
            opts, "https://x/stale", info, [(info, ex._item(info, None))])
        _FakeYdl.fail_process = ValueError("expired signature")
        t2 = reg.add({"url": "https://x/stale", "createdAt": 1})
        ydl2 = _run_with_fake_ydl(t2, _settings(), db)
        assert ydl2.calls == ["process", "extract"], \
            "a failed cached replay must fall back to extract_info"
        assert t2.status == "complete"
        assert ex.cached_info(opts, "https://x/stale") is None, \
            "the poisoned entry must be dropped"
    finally:
        _FakeYdl.fail_process = None
        ex._info_cache.clear()


def test_download_req_opts_rebuild_matches_probe_sig():
    """The download-side rebuild must reproduce the probe's info sig —
    contexts ride the request; referer deliberately stays out."""
    from dax.models import ContextHeaders, HeaderPair
    ctxs = [{"url": "https://x", "headers": [
        {"name": "Cookie", "value": "s=1"}]}]
    t = dl.Task("g", {"url": "https://x/watch", "contexts": ctxs,
                      "referer": "https://x/picked-row-referer"})
    s = _settings(cookies_browser="brave", player_client="ios")
    opts = dl._dl_extract_opts(t, s)
    probe_opts = ex.ExtractOptions(
        cookies_browser="brave", player_client="ios",
        referer="https://x/watch",
        contexts=[ContextHeaders(url="https://x", headers=[
            HeaderPair(name="Cookie", value="s=1")])])
    assert ex._info_sig(opts) == ex._info_sig(probe_opts)


def test_write_subs_parallel_one_thread_per_track():
    """Every requested_subtitles track fetches on its own thread — the
    barrier deadlocks if the fan-out ever runs serially. The merged
    (staged, final) pairs are what __files_to_move consumes."""
    import threading

    langs = [f"l{i}" for i in range(6)]
    bar = threading.Barrier(len(langs))
    seen: list[str] = []

    def fake_write(i, f):
        lang = next(iter(i["requested_subtitles"]))
        seen.append(lang)
        bar.wait(2)
        return [(f"tmp.{lang}.vtt", f"home.{lang}.vtt")]

    info = {"requested_subtitles": {l: {"ext": "vtt"} for l in langs}}
    ret = dl._write_subs_parallel(fake_write, info, "v.mp4")
    assert sorted(seen) == langs
    assert dict(ret) == {f"tmp.{l}.vtt": f"home.{l}.vtt" for l in langs}

    # One or zero tracks skip the pool entirely — straight passthrough.
    one = {"requested_subtitles": {"en": {"ext": "srt"}}}
    assert dl._write_subs_parallel(lambda i, f: [("a", "b")], one, "v") == [
        ("a", "b")]
    assert dl._write_subs_parallel(lambda i, f: None, {}, "v") == []


def test_launch_superseded_worker_never_runs():
    """A worker whose gen was bumped mid-unwind must exit before _run_task —
    the double-resume race guard."""
    import threading

    db, _ = _db()
    reg = dl.Registry(db, _settings())
    t = reg.add({"url": "https://x/v.mp4", "createdAt": 1})
    t.status = "paused"

    ev = threading.Event()
    prev = threading.Thread(target=ev.wait, daemon=True)
    prev.start()
    t.thread = prev  # still-unwinding previous worker

    ran = []
    orig = dl._run_task
    dl._run_task = lambda *a: ran.append(a)
    try:
        reg.launch(t, _settings())
        assert t.gen == 1 and t.status == "waiting"
        t.gen += 1   # a concurrent launch supersedes this worker
        ev.set()     # prev finishes unwinding → the chained thread resumes
        t.thread.join(5)
        assert not t.thread.is_alive()
        assert ran == [], "stale worker must not reach _run_task"
    finally:
        dl._run_task = orig
        ev.set()


def test_launch_gen_increments_under_lock():
    """Sequential resumes each bump the generation — a stale worker can
    never match the current one."""
    db, _ = _db()
    reg = dl.Registry(db, _settings())
    t = reg.add({"url": "https://x/v.mp4", "createdAt": 1})
    t.status = "paused"
    orig = dl._run_task
    dl._run_task = lambda *a: None
    try:
        reg.launch(t, _settings())
        reg.launch(t, _settings())
        assert t.gen == 2 and t.status == "waiting"
    finally:
        dl._run_task = orig
        if t.thread:
            t.thread.join(5)


def test_apply_settings_types_and_live_concurrency():
    """save_app_settings path: bool-for-int is skipped (isinstance trap),
    valid entries apply, and the registry semaphore resizes live."""
    from dax import app  # import runs create_app against the temp HOME

    d = _TMP / "appstate"
    d.mkdir(parents=True, exist_ok=True)
    shared = app.Shared(d)
    asyncio.run(app._apply_settings(shared, {
        "apiPort": True,                  # bool must not pass as int
        "maxConcurrentDownloads": 5,
        "embedThumbnail": False,
        "bogusKey": "ignored",
    }))
    s = shared.settings()
    assert s.api_port == 16801
    assert s.max_concurrent_downloads == 5
    assert s.embed_thumbnail is False
    sem = shared.registry._sem
    assert all(sem.acquire(blocking=False) for _ in range(5))
    assert not sem.acquire(blocking=False), "6th worker must wait"
    for _ in range(5):
        sem.release()


def test_ui_target_guard():
    """Traversal outside ui/dist falls back to index.html — is_relative_to
    keeps a hypothetical dist2 sibling out (a str-prefix check passed it)."""
    from dax import app

    idx = app.UI_DIST / "index.html"
    assert app._resolve_ui_target("../dist2/secret.js") == idx
    assert app._resolve_ui_target("../../etc/passwd") == idx
    assert app._resolve_ui_target("assets/../app.js") == \
        (app.UI_DIST / "app.js").resolve()


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send_text(self, body):
        self.sent.append(body)


def test_push_state_dedups_unchanged():
    """The broadcast loop must not resend identical state — snapshot and
    engine status go out per change (or per new client), not per tick."""
    from dax import app

    d = _TMP / "appstate2"
    d.mkdir(parents=True, exist_ok=True)
    shared = app.Shared(d)
    ws = _FakeWs()
    shared.ws_clients.add(ws)

    async def run():
        await app._push_state(shared, [ws])
        n = len(ws.sent)
        await app._push_state(shared, [ws])
        assert len(ws.sent) == n, "unchanged state must not resend"
        ws2 = _FakeWs()
        await app._push_state(shared, [ws2], force=True)
        assert len(ws2.sent) == n, "a new client always gets full state"
        return n

    assert asyncio.run(run()) == 2, "snapshot + engine-status per push"


def test_task_db_updated_at_is_ms():
    """created_at is seconds (legacy parity); updated_at is ms on every
    write path — insert used to store seconds, mixing units."""
    db, _ = _db()
    db.insert_task("gu", {"url": "u"}, "waiting", 0, 1000)
    created, updated = db._conn.execute(
        "SELECT created_at, updated_at FROM tasks WHERE gid = 'gu'"
    ).fetchone()
    assert created == 1000
    assert updated > 1_000_000_000_000, "updated_at must be ms"


# ---- PyO3 bridge -------------------------------------------------------------

def test_dax_rs_bridge_extract():
    """The vendored Rust extractor carries per-URL errors, never throws."""
    from dax import dax_rs
    out = orjson.loads(dax_rs.extract("{}", ["data:text/html,<b>x</b>"]))
    assert len(out) == 1
    assert out[0]["input"] == "data:text/html,<b>x</b>"
    assert out[0]["error_code"] == 1, "unsupported scheme → CODE_FAIL"
    assert out[0]["items"] == []


def test_dax_rs_bridge_bad_options():
    from dax import dax_rs
    try:
        dax_rs.extract("not-json", ["https://x"])
        raise AssertionError("bad options must raise")
    except ValueError:
        pass


# ---- settings migration ------------------------------------------------------


def test_settings_rebuild_drops_legacy_columns_and_preserves_token():
    """A settings table whose shape diverges from the declared schema (e.g.
    carrying old legacy columns) is rebuilt on open — the api token survives,
    stale columns do not."""
    p = _TMP / "legacy-settings.db"
    conn = sqlite3.connect(p)
    conn.execute("""
        CREATE TABLE settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            api_port INTEGER NOT NULL DEFAULT 16801,
            api_token TEXT NOT NULL DEFAULT 'tok-keep',
            legacy_unused_col TEXT NOT NULL DEFAULT 'old',
            aria2_next_split INTEGER NOT NULL DEFAULT 16
        )""")
    conn.execute("INSERT INTO settings (id, api_port, api_token) VALUES (1, 16801, 'tok-keep')")
    conn.commit()
    conn.close()

    d = db_mod.TaskDb.open(p)
    cols = {
        r[1] for r in d._conn.execute("PRAGMA table_info(settings)").fetchall()
    }
    assert "parallel_segments" in cols
    assert "downloader_engine" in cols
    assert "aria2_split" in cols
    assert "legacy_unused_col" not in cols
    assert "aria2_next_split" not in cols
    s = AppSettings.load(d)
    assert s.api_token == "tok-keep"
    s.parallel_segments = 32
    s.save(d)
    s2 = AppSettings.load(d)
    assert s2.parallel_segments == 32


def test_settings_additive_migration_preserves_values():
    """A missing new settings column is added in place — the user's other
    settings survive (a table rebuild would reset them all)."""
    p = _TMP / "settings-add-column.db"
    d = db_mod.TaskDb.open(p)
    AppSettings(
        extractor_engine="custom", cookies_browser="brave",
        ytdlp_generic=True, api_token="tok-keep",
    ).save(d)
    d._conn.execute("ALTER TABLE settings DROP COLUMN ytdlp_generic")
    d._conn.commit()

    d2 = db_mod.TaskDb.open(p)
    cols = {r[1] for r in d2._conn.execute("PRAGMA table_info(settings)")}
    assert "ytdlp_generic" in cols
    s = AppSettings.load(d2)
    assert s.api_token == "tok-keep"
    assert s.extractor_engine == "custom"
    assert s.cookies_browser == "brave"
    assert s.ytdlp_generic is False, "new column takes its declared default"


# ---- aria2-next external downloader & WebSocket RPC --------------------------

def test_aria2_next_settings_roundtrip_and_global_options():
    from dax import aria2_next

    d = _sdb()
    s = AppSettings(
        downloader_engine="aria2_next",
        aria2_next_path="/custom/aria2-next",
        aria2_rpc_port=16899,
        aria2_split=32,
        aria2_max_connection_per_server=24,
        aria2_min_split_size="4M",
        aria2_file_allocation="falloc",
        aria2_min_tls_version="TLSv1.3",
        aria2_max_overall_download_limit="50M",
        aria2_max_download_limit="10M",
        aria2_lowest_speed_limit="128K",
        aria2_retry_wait=5,
        aria2_state_save_interval=15,
    )
    s.save(d)
    loaded = AppSettings.load(d)
    assert loaded.downloader_engine == "aria2_next"
    assert loaded.aria2_next_path == "/custom/aria2-next"
    assert loaded.aria2_rpc_port == 16899
    assert loaded.aria2_split == 32
    assert loaded.aria2_max_connection_per_server == 24
    assert loaded.aria2_min_split_size == "4M"
    assert loaded.aria2_file_allocation == "falloc"
    assert loaded.aria2_min_tls_version == "TLSv1.3"
    assert loaded.aria2_max_overall_download_limit == "50M"
    assert loaded.aria2_max_download_limit == "10M"
    assert loaded.aria2_lowest_speed_limit == "128K"
    assert loaded.aria2_retry_wait == 5
    assert loaded.aria2_state_save_interval == 15

    gopts = aria2_next.global_options_from_settings(loaded)
    assert gopts["split"] == "32"
    assert gopts["max-connection-per-server"] == "24"
    assert gopts["min-split-size"] == "4M"
    assert gopts["file-allocation"] == "falloc"
    assert gopts["min-tls-version"] == "TLSv1.3"
    assert gopts["max-overall-download-limit"] == "50M"
    assert gopts["max-download-limit"] == "10M"
    assert gopts["lowest-speed-limit"] == "128K"
    assert gopts["retry-wait"] == "5"
    assert gopts["state-save-interval"] == "15"


def test_aria2_next_ytdlp_registration_and_protocol_routing():
    from yt_dlp.downloader import get_suitable_downloader
    from yt_dlp.downloader.external import Aria2cFD, get_external_downloader
    from dax import aria2_next

    aria2_next.register_ytdlp_downloader()
    ed = get_external_downloader("aria2_next")
    assert ed is aria2_next.Aria2NextFD
    assert ed is not Aria2cFD
    assert get_external_downloader("aria2-next") is aria2_next.Aria2NextFD

    exe = aria2_next.resolve_aria2_next()
    assert exe is not None, "aria2-next binary should be resolved"

    db, _ = _db()
    task = dl.Task("a1b2c3d4e5f60718", {"url": "https://example.com/video.mp4"})
    s = _settings(downloader_engine="aria2_next")
    params = dl._ydl_dl_params(task, s, db)
    assert params["external_downloader"] == aria2_next.EXTERNAL_DOWNLOADER_MAP

    # Verify prepare_info_for_external strips impersonate markers so yt-dlp
    # routes transfers (http, https) and delegated fragment lists
    # (m3u8_frag_urls, dash_frag_urls) to Aria2NextFD.
    for proto in ("http", "https", "m3u8_frag_urls", "dash_frag_urls"):
        info = {
            "url": "https://example.com/media",
            "protocol": proto,
            "impersonate": "chrome",
            "hls_aes": {"key": "00112233445566778899aabbccddeeff"},
        }
        aria2_next.prepare_info_for_external(info, s)
        assert "impersonate" not in info
        fd_cls = get_suitable_downloader(info, params)
        assert fd_cls is aria2_next.Aria2NextFD, f"protocol {proto} must route to Aria2NextFD"

    # Manifest protocols stay with yt-dlp's native FDs: they parse the
    # manifest (AES keys, EXT-X-MAP, byte ranges, live detection) and delegate
    # the segment downloads to Aria2NextFD via the *_frag_urls protocols.
    from yt_dlp.downloader.dash import DashSegmentsFD
    from yt_dlp.downloader.external import FFmpegFD
    from yt_dlp.downloader.hls import HlsFD

    for proto, expected in (
        ("m3u8_native", HlsFD),
        ("m3u8", FFmpegFD),
        ("http_dash_segments", DashSegmentsFD),
        ("http_dash_segments_generator", DashSegmentsFD),
    ):
        info = {"url": "https://example.com/media", "protocol": proto}
        fd_cls = get_suitable_downloader(info, params)
        assert fd_cls is expected, f"protocol {proto} must route to {expected.FD_NAME}"


def test_aria2_next_delegated_fd_inherits_ydl_progress_hooks():
    """HlsFD/DashSegmentsFD never copy progress hooks to the delegated FD
    (yt-dlp TODO) — Aria2NextFD must adopt the ydl's hooks itself, once,
    or dax's _progress_hook never fires and the UI stays 'waiting'."""
    from dax import aria2_next

    db, _ = _db()
    task = dl.Task("aabbccddeeff0011", {"url": "https://example.com/v.m3u8"})
    hook = dl._progress_hook(task, db)
    params = {"progress_hooks": [hook]}
    with dl.YoutubeDL(params) as ydl:
        fd = aria2_next.Aria2NextFD(ydl, params)
        fd._adopt_progress_hooks()
        assert fd._progress_hooks.count(hook) == 1
        fd._adopt_progress_hooks()
        assert fd._progress_hooks.count(hook) == 1


def test_aria2_next_leg_gid():
    from dax import aria2_next

    gid1 = aria2_next.derive_leg_gid("1122334455667788", "video.f137.mp4.part")
    gid2 = aria2_next.derive_leg_gid("1122334455667788", "video.f137.mp4.part")
    gid3 = aria2_next.derive_leg_gid("1122334455667788", "video.f140.m4a.part")
    assert len(gid1) == 16 and int(gid1, 16) > 0
    assert gid1 == gid2 and gid1 != gid3


def test_aria2_next_direct_leg_falls_back_to_native():
    """aria2-next aborts a leg after its fixed 8s stall window; a direct
    download must finish through yt-dlp's own HTTP stack instead of failing."""
    from dax import aria2_next

    class _Daemon:
        def ensure_running(self, s):
            return None

    calls: list[tuple] = []
    with dl.YoutubeDL({"quiet": True}) as ydl:
        fd = aria2_next.Aria2NextFD(ydl, {"_dax_aria2_daemon": _Daemon()})
        fd._download_http_single = lambda *a, **k: (False, "Connection stalled")
        fd._fetch_ranged_fragment = lambda *a, **k: calls.append(a)
        rc = fd._call_downloader(
            str(_TMP / "fallback-leg.bin.part"),
            {"url": "https://example.com/f.bin"},
        )
    assert rc == 0
    assert calls and calls[0][:3] == ("https://example.com/f.bin", 0, 0)


def test_aria2_next_max_tries_is_a_stall_budget():
    """aria2's max-tries counts consecutive per-slot failures, not whole
    download retries; the yt-dlp retry count must not cap it at 3."""
    from dax import aria2_next

    with dl.YoutubeDL({"quiet": True}) as ydl:
        fd = aria2_next.Aria2NextFD(ydl, {})
        opts = fd._base_http_options("https://x/f.bin", {}, _settings(retries=3))
        assert opts["max-tries"] == "5"
        opts = fd._base_http_options("https://x/f.bin", {}, _settings(retries=9))
        assert opts["max-tries"] == "9"
        opts = fd._base_http_options("https://x/f.bin", {}, _settings(retries=0))
        assert opts["max-tries"] == "1", "0 means unlimited in aria2"


def test_aria2_next_daemon_ws_rpc_and_e2e_download():
    import http.server
    import threading
    from dax import aria2_next

    if not aria2_next.resolve_aria2_next():
        print("  SKIP  aria2-next binary not found — e2e unexercised")
        return

    payload = b"aria2-next-websocket-rpc-test-payload-" * 1024
    seg0 = b"SEG0-" * 512
    seg1 = b"SEG1-" * 512
    seg2 = b"SEG2-" * 512
    big = bytes(range(256)) * 4096  # 1 MiB resource for byte-range fragments
    hits: dict[str, int] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            hits[self.path] = hits.get(self.path, 0) + 1
            if self.path == "/big.bin":
                rng = self.headers.get("Range") or ""
                m = re.match(r"bytes=(\d+)-(\d*)", rng)
                if m:
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else len(big) - 1
                    end = min(end, len(big) - 1)
                    body = big[start : end + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(big)}")
                else:
                    body = big
                    self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/test.bin":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/seg0.bin":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(seg0)))
                self.end_headers()
                self.wfile.write(seg0)
            elif self.path == "/seg1.bin":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(seg1)))
                self.end_headers()
                self.wfile.write(seg1)
            elif self.path == "/seg2.bin":
                # Flaky: the first fetch fails, later ones succeed.
                if hits["/seg2.bin"] == 1:
                    self.send_response(500)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(seg2)))
                self.end_headers()
                self.wfile.write(seg2)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv_port = srv.server_address[1]
    srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    srv_thread.start()

    daemon = aria2_next.Aria2NextDaemon(state_root=_TMP / "aria2-state-e2e")
    try:
        s = _settings(downloader_engine="aria2_next", aria2_rpc_port=16895, aria2_split=4)
        rpc = daemon.ensure_running(s)
        ver = rpc.get_version()
        assert ver.get("product") == "aria2-next"
        assert "mediaFeatures" in ver

        # Live global option update over WebSocket RPC
        assert rpc.change_global_option({"split": "8"}) == "OK"
        assert rpc.get_global_option().get("split") == "8"

        # 1. End-to-end HTTP download via yt-dlp + Aria2NextFD over WebSocket RPC
        db, _ = _db()
        out_folder = _TMP / "aria2-e2e-out"
        out_folder.mkdir(parents=True, exist_ok=True)
        s_dl = _settings(
            downloader_engine="aria2_next",
            categories=[{"name": "default", "path": str(out_folder)}],
        )
        req_dict = {
            "url": f"http://127.0.0.1:{srv_port}/test.bin",
            "filename": "downloaded.bin",
            "postprocess": False,
        }
        task = dl.Task("abcdef0123456789", req_dict)
        db.insert_task(task.gid, req_dict, "waiting", len(payload), task.created_at)
        params = dl._ydl_dl_params(task, s_dl, db)
        params["_dax_aria2_daemon"] = daemon

        with dl.YoutubeDL(params) as ydl:
            ydl.add_post_processor(dl._ExpectPP(task, s_dl), when="before_dl")
            ydl.download([req_dict["url"]])

        dest_file = out_folder / "downloaded.bin"
        assert dest_file.is_file()
        assert dest_file.read_bytes() == payload
        assert task.aria2_gid, "task must record its aria2-next GID"

        # 2. End-to-end fragmented stream download via Aria2NextFD over WebSocket RPC
        frag_task = dl.Task("fedcba9876543210", {"url": f"http://127.0.0.1:{srv_port}/stream.mpd"})
        frag_params = dl._ydl_dl_params(frag_task, s_dl, db)
        frag_params["_dax_aria2_daemon"] = daemon
        frag_out = out_folder / "fragments_assembled.bin"
        with dl.YoutubeDL(frag_params) as ydl:
            fd = aria2_next.Aria2NextFD(ydl, frag_params)
            ok = fd.real_download(
                str(frag_out),
                {
                    "id": "fragtest",
                    "title": "fragtest",
                    "ext": "bin",
                    "protocol": "http_dash_segments",
                    "url": f"http://127.0.0.1:{srv_port}/stream.mpd",
                    "fragments": [
                        {"url": f"http://127.0.0.1:{srv_port}/seg0.bin"},
                        {"url": f"http://127.0.0.1:{srv_port}/seg1.bin"},
                    ],
                },
            )
        assert ok is True
        assert frag_out.read_bytes() == seg0 + seg1
        # Completed fragment gids are pruned — task controls would otherwise
        # sweep dead ids and stall on evict retries per stale gid.
        assert len(frag_task.aria2_gids) == 0

        # 2b. Fragment robustness: a finished `.part` is reused, a stale
        # `.inc` leftover is restarted, and a transient failure is retried.
        seg0_hits = hits.get("/seg0.bin", 0)
        retry_out = out_folder / "frag_retry.bin"
        retry_frags_dir = Path(f"{retry_out}.part.frags")
        retry_frags_dir.mkdir(parents=True, exist_ok=True)
        (retry_frags_dir / "frag-000000.part").write_bytes(seg0)
        (retry_frags_dir / "frag-000001.part.inc").write_bytes(b"stale-garbage")
        with dl.YoutubeDL(frag_params) as ydl:
            fd = aria2_next.Aria2NextFD(ydl, frag_params)
            ok = fd.real_download(
                str(retry_out),
                {
                    "id": "fragretry",
                    "title": "fragretry",
                    "ext": "bin",
                    "protocol": "http_dash_segments",
                    "url": f"http://127.0.0.1:{srv_port}/stream.mpd",
                    "fragments": [
                        {"url": f"http://127.0.0.1:{srv_port}/seg0.bin"},
                        {"url": f"http://127.0.0.1:{srv_port}/seg2.bin"},
                    ],
                },
            )
        assert ok is True
        assert retry_out.read_bytes() == seg0 + seg2
        assert hits.get("/seg0.bin", 0) == seg0_hits, "finished fragment must not be re-downloaded"
        assert hits["/seg2.bin"] >= 2, "transiently failing fragment must be retried"

        # 2c. Byte-range fragments (HLS EXT-X-BYTERANGE / DASH SegmentBase):
        # aria2-next's planner can only fill [0,total) of one resource, so
        # these must fetch the exact slice through yt-dlp's HTTP stack —
        # regression for the infinite "server ignores ranges" restart loop.
        ranged_out = out_folder / "ranged.bin"
        with dl.YoutubeDL(frag_params) as ydl:
            fd = aria2_next.Aria2NextFD(ydl, frag_params)
            ok = fd.real_download(
                str(ranged_out),
                {
                    "id": "ranged",
                    "title": "ranged",
                    "ext": "bin",
                    "protocol": "m3u8_frag_urls",
                    "url": f"http://127.0.0.1:{srv_port}/big.bin",
                    "fragments": [
                        {"url": f"http://127.0.0.1:{srv_port}/big.bin",
                         "byte_range": {"start": 1000, "end": 2000}},
                        {"url": f"http://127.0.0.1:{srv_port}/big.bin",
                         "byte_range": {"start": 500_000, "end": 501_000}},
                    ],
                },
            )
        assert ok is True
        assert ranged_out.read_bytes() == big[1000:2000] + big[500_000:501_000]

        # 3. Task controls (pause / unpause / remove) over WebSocket RPC
        ctrl_gid = "1234567890abcdef"
        added_gid = rpc.add_uri(
            [f"http://127.0.0.1:{srv_port}/test.bin"],
            {"dir": str(out_folder), "out": "paused.bin", "gid": ctrl_gid, "pause": "true"},
        )
        assert added_gid == ctrl_gid
        assert rpc.tell_status(ctrl_gid).get("status") == "paused"
        ctrl_task = dl.Task(ctrl_gid, {"url": f"http://127.0.0.1:{srv_port}/test.bin"})
        ctrl_task.aria2_gid = ctrl_gid
        ctrl_task.aria2_gids.add(ctrl_gid)
        aria2_next.control_task_rpc(daemon, ctrl_task, "remove")
        try:
            st_after = rpc.tell_status(ctrl_gid)
            assert st_after.get("status") in ("removed", None, "")
        except aria2_next.Aria2RpcError:
            pass

        # 4. Switching the engine away stops the daemon and frees its port.
        assert daemon.is_running()
        daemon.sync_settings(_settings(downloader_engine="ytdlp"))
        assert not daemon.is_running(), "deselecting aria2_next must stop the daemon"
    finally:
        daemon.shutdown()
        srv.shutdown()


def test_control_unpause_during_cancelled_thread_unwind():
    """Unpausing while the previous worker thread is still unwinding from a
    pause (`cancel.is_set()` is True, `thread.is_alive()` is True) must launch
    a fresh chained worker rather than leaving the task stuck."""
    import threading

    state, t = _state_with_task("active")
    ev = threading.Event()
    prev = threading.Thread(target=ev.wait, daemon=True)
    prev.start()
    t.thread = prev

    r, e = dl._control_local(state, t, "pause")
    assert (r, e) == ("OK", None) and t.status == "paused" and t.cancel.is_set()

    ran = []
    orig = dl._run_task
    dl._run_task = lambda *a: ran.append(a)
    try:
        r, e = dl._control_local(state, t, "unpause")
        assert (r, e) == ("OK", None)
        assert t.status == "waiting"
        assert t.thread is not prev, "unpause must spawn a new chained worker"
        ev.set()
        t.thread.join(5)
        assert len(ran) == 1, "chained worker must run after prev unwinds"
    finally:
        dl._run_task = orig
        ev.set()


# ---- torrents -----------------------------------------------------------------

_MAGNET = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Some%20Torrent&tr=udp%3A%2F%2Ft%3A80"


class _FakeRpc:
    """Duck-typed Aria2RpcClient — records calls, no socket."""

    def __init__(self, status: dict | None = None):
        self.status = status
        self.calls: list[tuple] = []

    def tell_status(self, gid, keys=None):
        if self.status is None:
            from dax.aria2_next import Aria2RpcError
            raise Aria2RpcError(1, "GID not found", "aria2.tellStatus")
        return self.status

    def add_uri(self, uris, options=None, position=None):
        self.calls.append(("add_uri", uris, options))
        return "g"

    def add_torrent(self, b64, uris=None, options=None, position=None):
        self.calls.append(("add_torrent", b64, options))
        return "g"

    def unpause(self, gid):
        self.calls.append(("unpause", gid))
        return gid

    def change_option(self, gid, options):
        self.calls.append(("change_option", gid, options))
        return "OK"

    def remove(self, gid, force=False):
        self.calls.append(("remove", gid))
        return gid

    def remove_download_result(self, gid):
        self.calls.append(("rm_result", gid))
        return gid

    def evict(self, gid):
        # Mirrors Aria2RpcClient.evict: forceRemove + removeDownloadResult.
        self.remove(gid, force=True)
        self.remove_download_result(gid)


def _torrent_task(req: dict | None = None, gid: str = "t1") -> "dl.Task":
    db, _ = _db()
    reg = dl.Registry(db, _settings())
    return reg.add(req or {"url": _MAGNET, "kind": "torrent", "createdAt": 1}, gid=gid)


def test_torrent_detection():
    assert tr.is_magnet(_MAGNET) and tr.is_magnet(" MAGNET:?xt=urn:btih:x")
    assert not tr.is_magnet("https://x/f.torrent")
    assert tr.is_torrent_url("https://x/a/b.TORRENT?token=1")
    assert not tr.is_torrent_url("magnet:?xt=1") and not tr.is_torrent_url("https://x/a.mp4")
    assert tr.is_torrent_req({"kind": "torrent", "url": ""})
    assert tr.is_torrent_req({"url": "", "torrent": "ZDQ6"})
    assert tr.is_torrent_req({"url": _MAGNET})
    assert not tr.is_torrent_req({"url": "https://x/v.mp4"})


def test_format_selection_ranges():
    assert tr.format_selection([1, 2, 3, 5, 9]) == "1-3,5,9"
    assert tr.format_selection([2, 1, 2]) == "1-2"
    assert tr.format_selection([]) == ""


def test_valid_selection_intersects_known_indices():
    files = [{"index": "1"}, {"index": "3"}]  # pad at 2 is absent
    assert tr.valid_selection([1, 2, 3, 9], files) == [1, 3]
    assert tr.valid_selection([2], files) == []


def test_torrent_task_options_magnet_vs_file():
    s = _settings(bt_seed_ratio=2.0, bt_seed_time=30, bt_max_peers=42,
                  bt_enable_dht=False, bt_sequential=True,
                  bt_trackers="udp://tr:80")
    opts = tr.task_options(s, {"selectFiles": "1-3"}, dir_path="/dl", magnet=True)
    assert opts["seed-ratio"] == "2.0" and opts["seed-time"] == "30"
    assert opts["bt-max-peers"] == "42" and opts["enable-dht"] == "false"
    assert opts["force-sequential"] == "true" and opts["bt-tracker"] == "udp://tr:80"
    assert opts["select-file"] == "1-3" and "pause-metadata" not in opts
    assert opts["dir"] == "/dl"
    # No selection + magnet → park for the picker; a .torrent file has its
    # metadata already, so it must not pause.
    assert tr.task_options(s, {}, dir_path="/dl", magnet=True)["pause-metadata"] == "true"
    assert "pause-metadata" not in tr.task_options(s, {}, dir_path="/dl", magnet=False)
    assert "seed-time" not in tr.task_options(_settings(bt_seed_time=0), {}, dir_path="/dl", magnet=False)


def test_torrent_headers_replay_cookies_for_host():
    req = {
        "url": "https://tracker.example.com/a.torrent",
        "referer": "https://tracker.example.com/page",
        "contexts": [
            {"url": "https://tracker.example.com/page",
             "headers": [{"name": "Cookie", "value": "sid=1"},
                         {"name": "User-Agent", "value": "ignored"}]},
            {"url": "https://other.example.org/x",
             "headers": [{"name": "Cookie", "value": "leak=1"}]},
        ],
    }
    h = tr.torrent_headers(req)
    assert h["Cookie"] == "sid=1" and h["Referer"] == "https://tracker.example.com/page"
    assert "User-Agent" in h


def test_torrent_status_mapping():
    t = _torrent_task()
    dl._apply_torrent_status(t, {
        "status": "active", "totalLength": "100", "completedLength": "40",
        "downloadSpeed": "2048", "uploadSpeed": "0",
        "files": [{"index": "1", "path": "/dl/data.bin", "length": "100",
                   "completedLength": "40", "selected": "true"}],
        "bittorrent": {"state": "downloading", "fileSelectionState": "none",
                       "info": {"name": "data.bin"}, "numPeers": "3",
                       "numSeeds": "1", "infoHashV1": "ab" * 20},
    })
    assert t.status == "active" and t.downloaded == 40 and t.total == 100
    assert t.filename == "/dl/data.bin" and t.speed == 2048
    assert t.torrent["name"] == "data.bin" and t.torrent["numPeers"] == "3"
    assert t.torrent_files[0]["index"] == "1"

    dl._apply_torrent_status(t, {"status": "active", "bittorrent": {"state": "downloadingMetadata"}})
    assert t.status == "active", "metadata phase keeps the task active"

    dl._apply_torrent_status(t, {"status": "active", "uploadSpeed": "512",
                                 "bittorrent": {"state": "seeding", "info": {"name": "data.bin"}}})
    assert t.status == "seeding" and t.upload_speed == 512

    dl._apply_torrent_status(t, {"status": "paused", "bittorrent": {
        "state": "paused", "fileSelectionState": "awaiting"}})
    assert t.status == "selecting"

    dl._apply_torrent_status(t, {"status": "paused", "bittorrent": {
        "state": "paused", "fileSelectionState": "none"}})
    assert t.status == "paused"

    dl._apply_torrent_status(t, {"status": "waiting", "bittorrent": {"state": "queued"}})
    assert t.status == "waiting"

    dl._apply_torrent_status(t, {
        "status": "active",
        "files": [{"index": "1", "path": "/dl/box/a.bin", "length": "1"},
                  {"index": "2", "path": "/dl/box/b.bin", "length": "1"}],
        "bittorrent": {"state": "downloading", "mode": "multi"}})
    assert t.filename == "/dl/box", "multi-file root is the torrent folder"


def test_apply_torrent_status_gen_guard():
    """A worker superseded by a newer launch (pause→resume overlap) must not
    clobber the status the new launch is setting up."""
    t = _torrent_task()
    t.gen = 2
    t.status = "waiting"
    dl._apply_torrent_status(t, {"status": "active", "bittorrent": {"state": "downloading"}}, 1)
    assert t.status == "waiting", "stale worker must not write status"
    dl._apply_torrent_status(t, {"status": "active", "bittorrent": {"state": "downloading"}}, 2)
    assert t.status == "active", "current worker writes status"
    dl._apply_torrent_status(t, {"status": "paused", "bittorrent": {"state": "paused"}})
    assert t.status == "paused", "unversioned callers (tests) still write"


def test_snapshot_entry_carries_torrent_view():
    t = _torrent_task()
    t.torrent = {"name": "data.bin", "state": "seeding"}
    t.torrent_files = [{"index": "1", "path": "/dl/data.bin", "length": "1"}]
    e = t.snapshot_entry("/dl")
    assert e["bittorrent"]["state"] == "seeding"
    assert e["files"][0]["index"] == "1"


def test_registry_restore_sets_torrent_aria2_gid():
    db, _ = _db()
    reg = dl.Registry(db, _settings())
    info = {"gid": "tg1", "status": "paused", "files": [{"path": "/dl/x"}], "createdAt": 1}
    reg.restore([(info, {"url": _MAGNET, "kind": "torrent"})])
    assert reg.get("tg1").aria2_gid == "tg1", "torrent controls reach aria2 after restore"
    reg.restore([({"gid": "h1", "status": "paused", "files": [], "createdAt": 1},
                  {"url": "https://x/v.mp4"})])
    assert reg.get("h1").aria2_gid == ""


def test_submit_torrent_magnet_pauses_for_picker():
    t = _torrent_task({"url": _MAGNET, "kind": "torrent", "createdAt": 1})
    rpc = _FakeRpc()
    dl._submit_torrent(rpc, t, _settings())
    kind, uris, opts = rpc.calls[0]
    assert kind == "add_uri" and uris == [_MAGNET]
    assert opts["pause-metadata"] == "true" and opts["gid"] == t.gid
    assert opts["dir"] == out_dir(_settings())
    assert t.aria2_gid == t.gid


def test_submit_torrent_attach_and_resume_paths():
    # Base64 .torrent data (local file picker) goes through addTorrent.
    t = _torrent_task({"url": "", "kind": "torrent", "torrent": "ZDQ6", "createdAt": 1})
    rpc = _FakeRpc()
    dl._submit_torrent(rpc, t, _settings())
    assert rpc.calls[0][0] == "add_torrent" and rpc.calls[0][1] == "ZDQ6"

    # Already in the session → attach, never re-add.
    rpc2 = _FakeRpc(status={"status": "active"})
    dl._submit_torrent(rpc2, t, _settings())
    assert rpc2.calls == []

    # Awaiting selection + explicit resume → select every file, then unpause.
    t2 = _torrent_task({"url": _MAGNET, "kind": "torrent", "createdAt": 1})
    t2.status = "waiting"
    rpc3 = _FakeRpc(status={"status": "paused",
                            "files": [{"index": "1"}, {"index": "3"}],
                            "bittorrent": {"fileSelectionState": "awaiting"}})
    dl._submit_torrent(rpc3, t2, _settings())
    assert rpc3.calls[0] == ("change_option", t2.gid, {"select-file": "1,3"})
    assert rpc3.calls[1] == ("unpause", t2.gid)

    # Awaiting selection while only monitoring → leave it for the picker.
    t3 = _torrent_task({"url": _MAGNET, "kind": "torrent", "createdAt": 1})
    t3.status = "selecting"
    rpc4 = _FakeRpc(status={"status": "paused", "files": [{"index": "1"}],
                            "bittorrent": {"fileSelectionState": "awaiting"}})
    dl._submit_torrent(rpc4, t3, _settings())
    assert rpc4.calls == []

    # Terminal leftover with the same gid → remove, purge, re-add.
    t4 = _torrent_task({"url": _MAGNET, "kind": "torrent", "createdAt": 1})
    rpc5 = _FakeRpc(status={"status": "complete"})
    dl._submit_torrent(rpc5, t4, _settings())
    assert [c[0] for c in rpc5.calls] == ["remove", "rm_result", "add_uri"]


def test_add_download_torrent_requires_aria2_next():
    state, _ = _state_with_task("complete")
    launched = []
    orig_launch = dl.Registry.launch
    dl.Registry.launch = lambda self, t, s: launched.append(t.gid)
    try:
        state._s = _settings(downloader_engine="ytdlp")
        gid, e = asyncio.run(dl.add_download(state, DownloadReq(url=_MAGNET, kind="torrent")))
        assert gid is None and "aria2-next" in (e or "")
        # A magnet without kind still routes to the torrent path.
        state._s = _settings(downloader_engine="ytdlp")
        gid2, e2 = asyncio.run(dl.add_download(state, DownloadReq(url=_MAGNET)))
        assert gid2 is None and "aria2-next" in (e2 or "")
        # A local .torrent needs no URL.
        state._s = _settings(downloader_engine="ytdlp")
        gid3, e3 = asyncio.run(dl.add_download(
            state, DownloadReq(url="", torrent="ZDQ6", kind="torrent")))
        assert gid3 is None and "aria2-next" in (e3 or "")
    finally:
        dl.Registry.launch = orig_launch


def test_settings_bt_roundtrip_and_int_ratio():
    d = _sdb()
    _settings(bt_seed_ratio=2.5, bt_seed_time=30, bt_max_peers=50,
              bt_listen_port=51413, bt_enable_dht=False, bt_sequential=True,
              bt_trackers="udp://t:80").save(d)
    s = AppSettings.load(d)
    assert s.bt_seed_ratio == 2.5 and s.bt_seed_time == 30
    assert s.bt_enable_dht is False and s.bt_enable_lpd is True
    assert s.bt_sequential is True and s.bt_trackers == "udp://t:80"
    # JSON has no int/float split — a whole-number ratio must not be dropped.
    s2 = AppSettings.from_dict({"btSeedRatio": 1, "btMaxPeers": 20})
    assert s2.bt_seed_ratio == 1.0 and isinstance(s2.bt_seed_ratio, float)
    assert s2.bt_max_peers == 20


def test_site_asset_filters_junk_and_logos():
    # Valid video screenshots/thumbnails must pass
    assert not filt.site_asset("https://cdn.example.com/thumbs/poster_1080p.png")
    assert not filt.site_asset("/media/preview.webp")

    # Logos, vectors, avatars, banners, tracking pixels must be rejected
    assert filt.site_asset("https://site.com/logo.png")
    assert filt.site_asset("https://site.com/vector-logo.svg")
    assert filt.site_asset("https://site.com/favicon.ico")
    assert filt.site_asset("https://site.com/avatar_123.jpg")
    assert filt.site_asset("https://site.com/header_banner.jpg")
    assert filt.site_asset("https://site.com/btn_play.png")
    assert filt.site_asset("https://site.com/pixel_1x1.png")
    assert filt.site_asset("https://site.com/default_thumb.jpg")
    assert filt.site_asset("data:image/svg+xml;base64,PHN2Zw==")


# ---- playlists ---------------------------------------------------------------

def _fake_ydl_for_extract(info):
    """YoutubeDL stand-in for _ytdlp_extract: extract_info returns `info`,
    sanitize passes through, the format selector yields all formats."""
    import http.cookiejar

    class FakeYDL:
        def __init__(self, params):
            self.params = params
            self.cookiejar = http.cookiejar.CookieJar()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return info

        def sanitize_info(self, i):
            return i

        def build_format_selector(self, spec):
            return lambda d: list(d.get("formats") or [])

    return FakeYDL


_PL_INFO = {
    "_type": "playlist",
    "id": "PL1",
    "title": "My Mix",
    "extractor_key": "youtube:tab",
    "webpage_url": "https://youtube.com/playlist?list=PL1",
    "playlist_count": 2,
    "entries": [
        {"_type": "url", "url": "https://youtube.com/watch?v=a", "id": "a",
         "ie_key": "Youtube", "title": "Alpha", "playlist_index": 1,
         "duration": 61,
         "thumbnails": [{"url": "https://i/a-small.jpg"},
                        {"url": "https://i/a-big.jpg"}]},
        {"_type": "url", "url": "https://youtube.com/watch?v=b", "id": "b",
         "ie_key": "Youtube", "title": "Beta", "playlist_index": 2},
        None,  # a deleted/private slot
    ],
}


def test_extract_video_pass_pins_noplaylist_and_flat():
    """The video pass pins noplaylist (a watch?v+list URL yields only its
    video); both passes extract playlists flat — listings never resolve
    every entry during a probe."""
    params, _ = ex._ydl_params(ex.ExtractOptions(), "https://x")
    assert params["noplaylist"] is True
    assert params["extract_flat"] == "in_playlist"
    params2, _ = ex._ydl_params(
        ex.ExtractOptions(playlist=True), "https://x")
    assert "noplaylist" not in params2
    assert params2["extract_flat"] == "in_playlist"


def test_ytdlp_extract_playlist_page_meta():
    """A flat playlist result carries the Playlist tab's listing: title,
    count, per-entry index/url/thumbnail — and no media error."""
    orig = ex.YoutubeDL
    ex.YoutubeDL = _fake_ydl_for_extract(_PL_INFO)
    try:
        page = ex._ytdlp_extract(
            ex.ExtractOptions(), "https://youtube.com/playlist?list=PL1")
    finally:
        ex.YoutubeDL = orig
    pl = page["playlist"]
    assert pl["title"] == "My Mix"
    assert pl["count"] == 2
    assert pl["url"] == "https://youtube.com/playlist?list=PL1"
    assert [e["index"] for e in pl["entries"]] == [1, 2]
    assert pl["entries"][0]["url"] == "https://youtube.com/watch?v=a"
    assert pl["entries"][0]["title"] == "Alpha"
    assert pl["entries"][0]["ie_key"] == "Youtube"
    assert pl["entries"][0]["thumbnail"] == "https://i/a-big.jpg"
    assert pl["entries"][0]["duration_secs"] == 61.0
    # Flat stubs have no formats — items is empty, but the playlist is the
    # result, so no 'no media' error is reported.
    assert page["items"] == []
    assert "error" not in page


def test_extract_chain_stops_at_playlist_page():
    """A playlist page must not fall through to the Rust pass — its items
    are intentionally flat, so has_media is false by design."""
    calls = []
    pl_page = {"input": "u", "items": [], "playlist": {"entries": [{"index": 1}]}}
    orig_ytdlp, orig_rust = ex._ytdlp_extract, ex._rust_extract
    ex._ytdlp_extract = lambda o, u, **k: calls.append("ytdlp") or pl_page
    ex._rust_extract = lambda o, u: calls.append("rust") or None
    try:
        page = asyncio.run(ex._extract_one(ex.ExtractOptions(), "u"))
        assert calls == ["ytdlp"], "playlist pages stop the chain"
        assert page["playlist"]["entries"][0]["index"] == 1
    finally:
        ex._ytdlp_extract, ex._rust_extract = orig_ytdlp, orig_rust


def test_extract_merges_hidden_playlist_for_watch_plus_list():
    """watch?v=…&list=… probes twice: the video pass yields the single
    video, and a flat listing pass recovers the playlist for its tab.
    The second pass only runs because the tab/playlist extractor claims
    the raw URL while the resolved video URL is claimed by the video IE."""
    video_page = {
        "input": "u", "items": [{"formats": [{"url": "https://x/v.mp4"}]}],
        "final_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    }
    pl_page = {"input": "u", "items": [], "playlist": {"title": "Mix", "entries": [{"index": 1}]}}
    seen = []
    orig_ytdlp = ex._ytdlp_extract

    def fake(opts, url, **k):
        seen.append(bool(opts.playlist))
        return pl_page if opts.playlist else video_page

    ex._ytdlp_extract = fake
    try:
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLtest000000001"
        [page] = asyncio.run(ex.extract(ex.ExtractOptions(), [url]))
        assert seen == [False, True], "video pass then flat listing pass"
        assert page["items"][0]["formats"][0]["url"] == "https://x/v.mp4"
        assert page["playlist"]["title"] == "Mix"
    finally:
        ex._ytdlp_extract = orig_ytdlp


def test_extract_skips_listing_probe_for_plain_video():
    """A plain dedicated-extractor video pays one probe — the claiming IE
    set is identical to the resolved URL's, so no listing can hide."""
    seen = []
    video_page = {
        "input": "u", "items": [{"formats": [{"url": "https://x/v.mp4"}]}],
        "final_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    }
    orig_ytdlp = ex._ytdlp_extract
    ex._ytdlp_extract = lambda o, u, **k: (seen.append(o.playlist), video_page)[1]
    try:
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        [page] = asyncio.run(ex.extract(ex.ExtractOptions(), [url]))
        assert seen == [None], "no second probe for plain videos"
        assert "playlist" not in page
    finally:
        ex._ytdlp_extract = orig_ytdlp


def test_items_spec_ranges():
    """Picked indices → yt-dlp playlist_items syntax — contiguous runs
    collapse to ranges, singletons stay bare."""
    items = [{"index": i} for i in (1, 3, 4, 5, 9)]
    assert dl._items_spec(items) == "1,3-5,9"
    assert dl._items_spec([{"index": 2}]) == "2"
    assert dl._items_spec([]) == ""


def test_playlist_dir_sanitizer():
    """The playlist folder can never escape the out dir or carry reserved
    characters — traversal, separators and trailing dots all go."""
    assert dl._playlist_dir("My Playlist") == "My Playlist"
    assert dl._playlist_dir("a/b\\c:d*e?f\"g<h>i|") == "a b c d e f g h i"
    assert dl._playlist_dir("..\\..\\evil") == "evil"
    assert dl._playlist_dir("   ") == "Playlist"
    assert dl._playlist_dir(None) == "Playlist"
    assert len(dl._playlist_dir("x" * 500)) == 120


def test_ydl_dl_params_playlist():
    """Playlist params: entries land under <out>/<playlist title>/ with
    index-prefixed names; picked indices become a playlist_items spec; the
    tmp archive + ignoreerrors drive per-item resume and skip-on-failure."""
    req = {
        "url": "https://youtube.com/playlist?list=PL1",
        "playlist": {
            "title": "My Mix",
            "url": "https://youtube.com/playlist?list=PL1",
            "items": [{"index": 2}, {"index": 3}, {"index": 7}],
        },
    }
    t = dl.Task("g", req)
    p = dl._ydl_dl_params(t, _settings(), _db()[0])
    assert p["outtmpl"] == "My Mix/%(playlist_index)02d - %(title)s.%(ext)s"
    assert p["playlist_items"] == "2-3,7"
    assert p["ignoreerrors"] is True
    assert p["download_archive"].endswith("/g/archive.txt")
    # The out dir stays the resolved category root — the playlist folder is
    # inside it, not a replacement for it.
    assert p["paths"]["home"] == out_dir(_settings(), None)


def test_playlist_task_snapshot_and_progress():
    """A playlist task exposes per-item rows in its snapshot and tracks
    them via playlist_index in the progress/postprocessor hooks."""
    req = {
        "url": "https://youtube.com/playlist?list=PL1",
        "playlist": {
            "title": "My Mix",
            "url": "https://youtube.com/playlist?list=PL1",
            "items": [
                {"index": 1, "id": "a", "ieKey": "Youtube", "url": "u1",
                 "title": "Alpha"},
                {"index": 2, "id": "b", "ieKey": "Youtube", "url": "u2",
                 "title": "Beta"},
            ],
        },
    }
    t = dl.Task("g", req)
    snap = t.snapshot_entry("/out")
    assert snap["playlist"]["title"] == "My Mix"
    assert [i["index"] for i in snap["playlist"]["items"]] == [1, 2]
    assert all(i["status"] == "waiting" for i in snap["playlist"]["items"])

    db = _db()[0]
    hook = dl._progress_hook(t, db)
    hook({"status": "downloading", "downloaded_bytes": 40,
          "total_bytes": 100, "info_dict": {"playlist_index": 1}})
    it = t._pl_by_idx[1]
    assert it["status"] == "active" and it["downloaded"] == 40
    assert it["total"] == 100
    hook({"status": "finished", "total_bytes": 100,
          "info_dict": {"playlist_index": 1}})
    assert it["done"] == 100 and it["downloaded"] == 100
    pp = dl._pp_hook(t)
    pp({"status": "finished",
        "info_dict": {"playlist_index": 1, "filepath": "/out/My Mix/01 - a.mp4",
                      "filesize": 100}})
    assert it["status"] == "complete"
    assert it["filename"] == "/out/My Mix/01 - a.mp4"
    assert t._pl_by_idx[2]["status"] == "waiting"


def test_finish_playlist_marks_items():
    """Post-run resolution: entries present in the result are complete;
    absent-but-archived are earlier downloads (skipped, still complete);
    absent-and-unarchived are failures."""
    tmp = Path(st.task_tmp_dir("plg"))
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "archive.txt").write_text("youtube b\n")
    req = {"url": "u", "playlist": {"title": "Mix", "items": [
        {"index": 1, "id": "a", "ieKey": "Youtube"},
        {"index": 2, "id": "b", "ieKey": "Youtube"},
        {"index": 3, "id": "c", "ieKey": "Youtube"},
    ]}}
    t = dl.Task("plg", req)
    info = {"_type": "playlist", "entries": [
        {"playlist_index": 1, "id": "a",
         "requested_downloads": [{"filepath": "/out/Mix/01 - a.mp4"}]},
    ]}
    final = dl._finish_playlist(t, info, _settings())
    st_ = {i["index"]: i["status"] for i in t.playlist_items}
    assert st_ == {1: "complete", 2: "complete", 3: "error"}
    assert t.playlist_items[0]["filename"] == "/out/Mix/01 - a.mp4"
    assert final.endswith("Mix")


def test_download_info_replays_cached_listing_under_both_sigs():
    """A playlist task replays the probe's cached listing regardless of
    which probe produced it: the video pass registers playlist?list pages
    under sig(playlist=None), the flat listing pass registers watch?v+list
    listings under sig(playlist=True). Both must hit — otherwise every
    playlist download re-fetches the listing page."""
    import msgspec
    s = _settings()
    url = "https://youtube.com/playlist?list=PLx"
    task = dl.Task("g", {"url": url,
                         "playlist": {"title": "P", "items": [{"index": 1}]}})
    opts = dl._dl_extract_opts(task, s)
    info = {"_type": "playlist", "id": "PLx", "entries": [{"id": "a"}]}
    calls = []

    class FakeYDL:
        def process_ie_result(self, i, download):
            calls.append("replay")
            return i

        def extract_info(self, u, download):
            calls.append("refetch")
            return info

    try:
        for flag in (None, True):
            sig = ex._info_sig(
                msgspec.structs.replace(opts, playlist=flag))
            ex._info_cache.insert((url, sig), info)
            got = dl._download_info(FakeYDL(), task, s, url)
            # cached_info returns an orjson copy — same content, new object.
            assert calls[-1] == "replay" and got["id"] == "PLx", \
                f"sig(playlist={flag}) must replay, not refetch"
            ex._info_cache.clear()
    finally:
        ex._info_cache.clear()


def test_playlist_workers_split_and_parallel_download():
    """Picked indices split into contiguous per-worker specs bounded by
    maxConcurrentDownloads; every worker runs its own yt-dlp pass and the
    returned infos merge for the finish pass."""
    idxs = [1, 2, 3, 4, 5, 6, 7]
    req = {"url": "u", "playlist": {
        "title": "P", "count": 7,
        "items": [{"index": i} for i in idxs]}}
    t = dl.Task("g", req)
    seen_specs = []
    orig = dl._ydl_download

    def expand(spec):
        out = []
        for seg in spec.split(","):
            a, _, b = seg.partition("-")
            out.extend(range(int(a), int(b or a) + 1))
        return out

    def fake(task, s, db, params, url, **kw):
        spec = params["playlist_items"]
        seen_specs.append(spec)
        return {"_type": "playlist", "entries": [
            {"playlist_index": i} for i in expand(spec)]}
    dl._ydl_download = fake
    try:
        infos = dl._run_playlist_workers(
            t, _settings(max_concurrent_downloads=3), _db()[0], "u")
    finally:
        dl._ydl_download = orig
    # 7 items / 3 workers → chunks of 3,3,1.
    assert sorted(seen_specs) == ["1-3", "4-6", "7"]
    assert len(infos) == 3
    merged = {e["playlist_index"] for i in infos for e in i["entries"]}
    assert merged == set(idxs)


def test_playlist_workers_run_concurrently():
    """Regression: the pool must run all chunk workers at once — a lazy
    submit loop (`for f in (submit(...)...)`) serializes them one by one."""
    req = {"url": "u", "playlist": {
        "title": "P", "items": [{"index": i} for i in range(1, 7)]}}
    t = dl.Task("g", req)
    orig = dl._ydl_download
    import threading as th
    lk = th.Lock()
    live = [0]
    peak = [0]

    def fake(task, s, db, params, url, **kw):
        with lk:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        th.Event().wait(0.2)  # stand-in for download time
        with lk:
            live[0] -= 1
        return {"_type": "playlist", "entries": []}

    dl._ydl_download = fake
    try:
        dl._run_playlist_workers(
            t, _settings(max_concurrent_downloads=3), _db()[0], "u")
    finally:
        dl._ydl_download = orig
    assert peak[0] == 3, \
        f"workers must overlap — peak concurrency was {peak[0]}"


def test_playlist_worker_failure_marks_its_items():
    """A worker that dies at extraction leaves its chunk's items absent
    from every returned info — the finish pass marks just them failed."""
    req = {"url": "u", "playlist": {
        "title": "P", "items": [{"index": i} for i in (1, 2, 3, 4)]}}
    t = dl.Task("g", req)
    orig = dl._ydl_download

    def fake(task, s, db, params, url, **kw):
        if params["playlist_items"] == "3-4":
            raise RuntimeError("playlist page 500")
        return {"_type": "playlist", "entries": [
            {"playlist_index": 1}, {"playlist_index": 2}]}

    dl._ydl_download = fake
    try:
        infos = dl._run_playlist_workers(
            t, _settings(max_concurrent_downloads=2), _db()[0], "u")
        final = dl._finish_playlist(t, infos, _settings())
    finally:
        dl._ydl_download = orig
    st_ = {i["index"]: i["status"] for i in t.playlist_items}
    assert st_ == {1: "complete", 2: "complete", 3: "error", 4: "error"}
    assert final.endswith("P")


def test_snapshot_playlist_reports_item_size_sums():
    """The task row's bytes are the sum of every item's merged (video+audio)
    size — including archive-skipped items recovered from disk — not just
    the legs reported this run."""
    req = {"url": "u", "playlist": {
        "title": "P", "items": [{"index": 1}, {"index": 2}]}}
    t = dl.Task("g", req)
    t.downloaded, t.total = 50, 50  # leg accumulators mid-run
    t.playlist_items[0].update(
        status="complete", downloaded=100, total=100, done=100)
    t.playlist_items[1].update(
        status="active", downloaded=40, total=80)
    snap = t.snapshot_entry("/out")
    assert snap["completedLength"] == "140"
    assert snap["totalLength"] == "180"
    assert snap["files"][0]["length"] == "180"


def test_ydl_dl_params_playlist_worker_spec_override():
    """A worker's pl_spec overrides the full picked set — that is how the
    parallel pool restricts each pass to its own index chunk."""
    req = {"url": "u", "playlist": {
        "title": "P", "items": [{"index": i} for i in (1, 2, 3, 4)]}}
    p = dl._ydl_dl_params(
        dl.Task("g", req), _settings(), _db()[0], pl_spec="3-4")
    assert p["playlist_items"] == "3-4"


def test_check_name_conflict_playlist_skips():
    """Playlist adds name a folder — never a file conflict, even when the
    request also carries a filename hint."""
    from dax.models import PlaylistReq, PlaylistItem
    req = DownloadReq(
        url="https://youtube.com/playlist?list=PL1",
        filename_hint="anything",
        playlist=PlaylistReq(title="Mix", items=[PlaylistItem(index=1)]),
    )
    assert dl.check_name_conflict(None, req) == {"conflict": False}


def test_req_to_dict_playlist_camelizes():
    """The nested playlist payload rides req_json camelized — ie_key and
    duration_secs become ieKey/durationSecs on the wire and in the db."""
    from dax.models import PlaylistReq, PlaylistItem
    req = DownloadReq(
        url="https://youtube.com/playlist?list=PL1",
        playlist=PlaylistReq(
            title="Mix", url="u",
            items=[PlaylistItem(index=1, id="a", ie_key="Youtube",
                                duration_secs=61.0)],
        ),
    )
    d = dl._req_to_dict(req)
    item = d["playlist"]["items"][0]
    assert item["ieKey"] == "Youtube" and item["durationSecs"] == 61.0
    assert d["playlist"]["title"] == "Mix"


# ---- runner ------------------------------------------------------------------

def main() -> None:
    from dax import aria2_next

    tests = [
        (n, f) for n, f in sorted(globals().items())
        if n.startswith("test_") and callable(f)
    ]
    failed = 0
    try:
        for name, fn in tests:
            try:
                fn()
                print(f"  ok  {name}")
            except Exception as e:
                failed += 1
                import traceback
                print(f"FAIL  {name}: {e}")
                traceback.print_exc()
    finally:
        aria2_next.get_daemon().shutdown()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
