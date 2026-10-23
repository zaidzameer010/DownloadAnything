use crate::extract::candidates::{Candidate, contains_media_token, looks_media_str};
use crate::extract::regex_scan;
use crate::extract::unescape::unescape_js;
use crate::model::{Quality, SourceLayer};
use base64::Engine;
use regex::Regex;
use scraper::{Html, Selector};
use serde_json::Value;
use std::sync::LazyLock;

static SCRIPT_SEL: LazyLock<Selector> = LazyLock::new(|| Selector::parse("script").unwrap());
static ALL_SEL: LazyLock<Selector> = LazyLock::new(|| Selector::parse("*").unwrap());

/// Variable/anchor names that commonly hold player config blobs.
const ANCHORS: &[&str] = &[
    "__NEXT_DATA__",
    "__NUXT__",
    "__INITIAL_STATE__",
    "__PRELOADED_STATE__",
    "__PLAYER_CONFIG__",
    "ytInitialPlayerResponse",
    "ytInitialData",
    "playerConfig",
    "player_config",
    "videoData",
    "window.__data",
    "mediaJson",
    "flashvars",
];

/// JSON keys whose string values may be media URLs.
const URL_KEYS: &[&str] = &[
    "url",
    "src",
    "hls",
    "dash",
    "hlsurl",
    "dashurl",
    "sources",
    "file",
    "contenturl",
    "streamurl",
    "stream",
    "videourl",
    "video_url",
    "manifest",
    "mpd",
    "m3u8",
    "href",
    "playbackurl",
    "playurl",
    "srcurl",
    "mediaurl",
    "downloadurl",
    "audiourl",
    "audio_url",
    "link",
    "video",
    "videopath",
    "manifesturl",
];

fn key_is_urlish(k: &str) -> bool {
    let k = k.to_lowercase();
    URL_KEYS.contains(&k.as_str())
        || k.contains("url")
        || k.ends_with("src")
        || k.contains("m3u8")
        || k.contains("mpd")
}

/// Keys that always name a media/stream address — signed API URLs often
/// carry no file extension, so the media-token check would reject them.
/// Weak keys ("url", "src", "link", …) still require a media token.
fn is_strong_url_key(k: &str) -> bool {
    matches!(
        k,
        "video_url"
            | "videourl"
            | "videopath"
            | "streamurl"
            | "stream_url"
            | "playbackurl"
            | "playurl"
            | "play_url"
            | "downloadurl"
            | "download_url"
            | "hls"
            | "hlsurl"
            | "hls_url"
            | "dash"
            | "dashurl"
            | "dash_url"
            | "mpd"
            | "m3u8"
            | "manifest"
            | "manifesturl"
            | "srcurl"
            | "mediaurl"
            | "audiourl"
            | "audio_url"
    )
}

/// Find every JSON blob in the document and walk them for media URLs.
/// Returns candidates plus a media duration, page title, and player preview
/// thumbnail found alongside them (player-config `preview_url`, `poster`,
/// `video_title`, blob `title` keys, ...).
/// Raw script text is not regex-scanned here — the whole-body scan in
/// `page_layers` already covers it; only decoded blobs need their own pass.
pub fn extract(doc: &Html) -> (Vec<Candidate>, Option<f64>, Option<String>, Option<String>) {
    let mut out = Vec::new();
    let mut dur = None;
    let mut title = None;
    let mut thumb = None;
    for el in doc.select(&SCRIPT_SEL) {
        let v = el.value();
        if v.attr("src").is_some() {
            continue;
        }
        let id = v.attr("id").unwrap_or("");
        let ty = v.attr("type").unwrap_or("").to_lowercase();
        let text = el.text().collect::<String>();
        let text = text.trim();
        if text.is_empty() {
            continue;
        }
        if (id == "__NEXT_DATA__" || ty.contains("json") || text.starts_with('{'))
            && let Ok(v) = serde_json::from_str::<Value>(text)
        {
            walk_json(&v, &mut out, &mut dur, &mut title, &mut thumb, 0);
            continue;
        }
        // Unanchored scripts (window.__cfg = {...} etc.): try to lift the
        // first object literal, then a regex pass over the whole body.
        if !ANCHORS.iter().any(|a| text.contains(a)) {
            if let Some(eq) = text.find('=')
                && let Some(blob) = extract_balanced(text, eq + 1)
            {
                if let Ok(v) = serde_json::from_str::<Value>(&blob) {
                    walk_json(&v, &mut out, &mut dur, &mut title, &mut thumb, 0);
                } else {
                    let (fv, fv_title, fv_thumb) = flashvars_candidates(&blob);
                    out.extend(fv);
                    if title.is_none() {
                        title = fv_title;
                    }
                    if thumb.is_none() {
                        thumb = fv_thumb;
                    }
                }
            }
            continue;
        }
        for a in ANCHORS {
            let mut search_from = 0;
            while let Some(pos) = text[search_from..].find(a) {
                let pos = search_from + pos + a.len();
                // skip to '=' then to the opening brace
                let Some(eq) = text[pos..].find('=') else {
                    break;
                };
                let eq = pos + eq;
                if eq - pos > 8 {
                    break;
                }
                if let Some(blob) = extract_balanced(text, eq + 1) {
                    if let Ok(v) = serde_json::from_str::<Value>(&blob) {
                        walk_json(&v, &mut out, &mut dur, &mut title, &mut thumb, 0);
                    } else {
                        // JS object literal (KVS flashvars et al.) —
                        // parse key: 'value' pairs directly.
                        let (fv, fv_title, fv_thumb) = flashvars_candidates(&blob);
                        out.extend(fv);
                        if title.is_none() {
                            title = fv_title;
                        }
                        if thumb.is_none() {
                            thumb = fv_thumb;
                        }
                    }
                }
                search_from = eq + 1;
                if search_from >= text.len() {
                    break;
                }
            }
        }
    }
    // data-* attributes containing JSON objects (e.g. video.js data-setup).
    for el in doc.select(&ALL_SEL) {
        for (name, value) in el.value().attrs() {
            if !name.starts_with("data-") {
                continue;
            }
            let value = value.trim();
            if value.len() > 8
                && (value.starts_with('{') || value.starts_with('['))
                && let Ok(v) = serde_json::from_str::<Value>(value)
            {
                walk_json(&v, &mut out, &mut dur, &mut title, &mut thumb, 0);
            }
        }
    }
    (out, dur, title, thumb)
}

/// Extract a balanced {...} / [...] / (...) block starting search at `from`.
fn extract_balanced(text: &str, from: usize) -> Option<String> {
    let bytes = text.as_bytes();
    let mut i = from;
    while i < bytes.len() && !matches!(bytes[i], b'{' | b'[' | b'(') {
        i += 1;
    }
    let start = i;
    let mut stack: Vec<u8> = Vec::new();
    let mut in_str: Option<u8> = None;
    let mut esc = false;
    while i < bytes.len() {
        let b = bytes[i];
        if let Some(q) = in_str {
            if esc {
                esc = false;
            } else if b == b'\\' {
                esc = true;
            } else if b == q {
                in_str = None;
            }
        } else {
            match b {
                b'"' | b'\'' | b'`' => in_str = Some(b),
                b'{' | b'[' | b'(' => stack.push(b),
                b'}' | b']' | b')' => {
                    let want = match b {
                        b'}' => b'{',
                        b']' => b'[',
                        _ => b'(',
                    };
                    if stack.pop() != Some(want) {
                        return None;
                    }
                    if stack.is_empty() {
                        return Some(text[start..=i].to_string());
                    }
                }
                _ => {}
            }
        }
        i += 1;
    }
    None
}

/// KVS (Kernel Video Sharing) flashvars: `video_url` / `video_alt_url<N>`
/// hold the quality variants, `<key>_text` the quality label, and
/// license-protected sites obfuscate URLs as `function/0/<url>` decoded via
/// `license_code` (same algorithm as yt-dlp's KVS extractor). Also grabs
/// `video_title` (clean page title) and `preview_url` / `poster` (preview thumbnail).
fn flashvars_candidates(blob: &str) -> (Vec<Candidate>, Option<String>, Option<String>) {
    static PAIR: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r#"([A-Za-z0-9_]+)\s*:\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')"#).unwrap()
    });
    let pairs: Vec<(String, std::borrow::Cow<'_, str>)> = PAIR
        .captures_iter(blob)
        .map(|c| {
            let v = c
                .get(2)
                .or_else(|| c.get(3))
                .map(|m| m.as_str())
                .unwrap_or("");
            (c[1].to_string(), unescape_js(v))
        })
        .collect();
    let get = |k: &str| -> Option<&str> {
        pairs
            .iter()
            .find(|(key, _)| key == k)
            .map(|(_, v)| v.as_ref())
    };
    let license = get("license_code");
    let mut out = Vec::new();
    for (k, v) in &pairs {
        // KVS keys are strong keys — extensionless signed URLs are accepted.
        if !is_kvs_url_key(k) || !(looks_media_str(v) || v.starts_with("http")) {
            continue;
        }
        let mut url = v.clone();
        if url.starts_with("function/0/") {
            match license.and_then(|l| kvs_real_url(&url, l)) {
                Some(real) => url = std::borrow::Cow::Owned(real),
                None => continue,
            }
        }
        let mut c = Candidate::new(url, SourceLayer::JsonBlob);
        if let Some(label) = get(&format!("{k}_text")) {
            c.label = Some(label.to_string());
            c.quality = Some(Quality {
                height: label_height(label).and_then(|h| u32::try_from(h).ok()),
                label: Some(label.to_string()),
                ..Default::default()
            });
        }
        out.push(c);
    }
    let title = get("video_title")
        .filter(|t| !t.trim().is_empty())
        .map(str::to_string);
    // Highest resolution or primary preview/poster from player flashvars
    let thumb = [
        "preview_url7",
        "preview_url6",
        "preview_url5",
        "preview_url4",
        "preview_url3",
        "preview_url2",
        "preview_url1",
        "preview_url",
        "poster",
        "screen_url",
        "preview",
        "thumbnail",
        "thumb_url",
        "image",
    ]
    .iter()
    .find_map(|k| get(k))
    .map(str::trim)
    .filter(|t| !t.is_empty() && !crate::extract::meta::is_junk_thumbnail(t))
    .map(str::to_string);
    (out, title, thumb)
}

/// `video_url` or `video_alt_url`/`video_alt_url2`/… (digits only suffix).
fn is_kvs_url_key(k: &str) -> bool {
    k == "video_url"
        || k.strip_prefix("video_alt_url")
            .is_some_and(|rest| rest.chars().all(|c| c.is_ascii_digit()))
}

/// Deobfuscate `function/0/<url>`: the 32-char hash path segment is shuffled
/// with the license token (yt-dlp `_kvs_get_real_url`).
fn kvs_real_url(video_url: &str, license_code: &str) -> Option<String> {
    let rest = video_url.strip_prefix("function/0/")?;
    let mut u = url::Url::parse(rest).ok()?;
    let token = kvs_license_token(license_code);
    let parts: Vec<String> = u.path().split('/').map(String::from).collect();
    let hash = parts.get(3)?;
    if hash.len() < 32 {
        return None;
    }
    let chars: Vec<char> = hash.chars().collect();
    let mut indices: Vec<usize> = (0..32).collect();
    let mut accum: usize = 0;
    for src in (0..32).rev() {
        accum += token.get(src).copied().unwrap_or(0) as usize;
        let dest = (src + accum) % 32;
        indices.swap(src, dest);
    }
    let shuffled: String = indices.iter().map(|&i| chars[i]).collect();
    let tail: String = chars[32..].iter().collect();
    let mut parts = parts;
    parts[3] = format!("{shuffled}{tail}");
    u.set_path(&parts.join("/"));
    Some(u.to_string())
}

/// KVS `license_code` → digit token stream (yt-dlp `_kvs_get_license_token`).
fn kvs_license_token(license_code: &str) -> Vec<u32> {
    let lc: String = license_code
        .chars()
        .filter(|c| c.is_ascii_digit())
        .collect();
    if lc.is_empty() {
        return Vec::new();
    }
    let values: Vec<u32> = lc.chars().filter_map(|c| c.to_digit(10)).collect();
    let modl: String = lc.chars().map(|c| if c == '0' { '1' } else { c }).collect();
    let center = modl.len() / 2;
    let front: u64 = modl[..=center].parse().unwrap_or(0);
    let back: u64 = modl[center..].parse().unwrap_or(0);
    let prod = (4u64 * front.abs_diff(back)).to_string();
    let take = (center + 1).min(prod.len());
    let mut token = Vec::new();
    for (i, c) in prod[..take].chars().enumerate() {
        let Some(cur) = c.to_digit(10) else { continue };
        for offset in 0..4 {
            match values.get(i + offset) {
                Some(&v) => token.push((v + cur) % 10),
                None => return token,
            }
        }
    }
    token
}

fn looks_b64(s: &str) -> bool {
    let s = s.trim();
    s.len() >= 24
        && s.len() <= 2_000_000
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '/' | '=' | '-' | '_'))
}

fn looks_hex(s: &str) -> bool {
    let s = s.trim();
    s.len() >= 32 && s.len().is_multiple_of(2) && s.chars().all(|c| c.is_ascii_hexdigit())
}

/// Keys whose string value names the content itself. "name" is excluded —
/// sources arrays abuse it as a quality label.
fn is_title_key(k: &str) -> bool {
    [
        "title",
        "video_title",
        "videotitle",
        "media_title",
        "episode_title",
    ]
    .iter()
    .any(|t| k.eq_ignore_ascii_case(t))
}

fn is_thumb_key(k: &str) -> bool {
    matches!(
        k.to_ascii_lowercase().as_str(),
        "poster"
            | "preview_url"
            | "previewurl"
            | "preview_image"
            | "previewimage"
            | "thumbnail_url"
            | "thumbnailurl"
            | "thumb_url"
            | "thumburl"
            | "thumbnail"
            | "screen_url"
            | "screenurl"
            | "cover_url"
            | "coverurl"
            | "splash"
            | "image"
            | "preview"
    )
}

/// Recursively walk a parsed JSON value collecting media URLs; `dur` grabs
/// the first plausible "duration" (seconds), `title` the first title-ish
/// string, and `thumb` the first player preview/poster URL found alongside them.
pub fn walk_json(
    v: &Value,
    out: &mut Vec<Candidate>,
    dur: &mut Option<f64>,
    title: &mut Option<String>,
    thumb: &mut Option<String>,
    depth: u8,
) {
    match v {
        Value::Object(map) => {
            for (k, val) in map {
                if dur.is_none()
                    && k.eq_ignore_ascii_case("duration")
                    && let Some(d) = val.as_f64().or_else(|| val.as_str()?.parse().ok())
                    && (5.0..=86_400.0).contains(&d)
                {
                    *dur = Some(d);
                }
                if title.is_none()
                    && is_title_key(k)
                    && let Some(s) = val.as_str()
                {
                    let t = s.trim();
                    if !t.is_empty() && t.len() <= 300 {
                        *title = Some(t.to_string());
                    }
                }
                if thumb.is_none()
                    && is_thumb_key(k)
                    && let Some(s) = val.as_str()
                {
                    let t = s.trim();
                    if !t.is_empty()
                        && (t.starts_with("http://")
                            || t.starts_with("https://")
                            || t.starts_with("//")
                            || t.starts_with('/'))
                        && !crate::extract::meta::is_junk_thumbnail(t)
                    {
                        *thumb = Some(t.to_string());
                    }
                }
                if let Value::String(s) = val {
                    if key_is_urlish(k)
                        && (looks_media_str(s)
                            || (is_strong_url_key(&k.to_lowercase()) && s.starts_with("http")))
                    {
                        let mut c = Candidate::new(s.as_str(), SourceLayer::JsonBlob);
                        c.label = Some(k.clone());
                        out.push(c);
                    } else if depth < 2 && looks_b64(s) && s.len() < 200_000 {
                        if let Ok(dec) = base64::engine::general_purpose::STANDARD
                            .decode(s.trim())
                            .or_else(|_| base64::engine::general_purpose::URL_SAFE.decode(s.trim()))
                        {
                            consume_decoded(&dec, out, dur, title, thumb, depth);
                        }
                    } else if depth < 2
                        && looks_hex(s)
                        && s.len() < 200_000
                        && let Ok(dec) = hex::decode(s.trim())
                    {
                        consume_decoded(&dec, out, dur, title, thumb, depth);
                    }
                } else if (k.eq_ignore_ascii_case("sources") || k.eq_ignore_ascii_case("videos"))
                    && val.is_array()
                {
                    extract_sources_array(val, out, thumb);
                    walk_json(val, out, dur, title, thumb, depth + 1);
                } else {
                    walk_json(val, out, dur, title, thumb, depth + 1);
                }
            }
        }
        Value::Array(a) => a
            .iter()
            .for_each(|v| walk_json(v, out, dur, title, thumb, depth + 1)),
        _ => {}
    }
}

/// JW-player-style "sources"/"videos": [{"file": "...", "label": "720p"}, ...]
fn extract_sources_array(val: &Value, out: &mut Vec<Candidate>, thumb: &mut Option<String>) {
    let Some(arr) = val.as_array() else { return };
    for item in arr {
        let Some(obj) = item.as_object() else {
            continue;
        };
        if thumb.is_none()
            && let Some(img) = ["image", "poster", "thumbnail", "preview"]
                .iter()
                .find_map(|k| obj.get(*k).and_then(Value::as_str))
        {
            let img = img.trim();
            if !img.is_empty() && !crate::extract::meta::is_junk_thumbnail(img) {
                *thumb = Some(img.to_string());
            }
        }
        let url = ["file", "src", "url", "stream", "source"]
            .iter()
            .find_map(|k| obj.get(*k).and_then(Value::as_str));
        let Some(url) = url else { continue };
        if !looks_media_str(url) && !url.starts_with("http") {
            continue;
        }
        let mut c = Candidate::new(url, SourceLayer::JsonBlob);
        let label = obj
            .get("label")
            .or_else(|| obj.get("quality"))
            .or_else(|| obj.get("name"))
            .and_then(Value::as_str)
            .map(String::from);
        let height = obj
            .get("height")
            .or_else(|| obj.get("res"))
            .and_then(|v| v.as_u64().or_else(|| v.as_str()?.parse().ok()))
            .or_else(|| label.as_deref().and_then(label_height));
        if label.is_some() || height.is_some() {
            c.quality = Some(Quality {
                height: height.and_then(|h| u32::try_from(h).ok()),
                label,
                ..Default::default()
            });
        }
        out.push(c);
    }
}

/// Quality names that don't carry digits ("hd", "sd", "full") → heights.
fn label_height(l: &str) -> Option<u64> {
    let l = l.trim().to_lowercase();
    if let Some(h) = l.trim_end_matches('p').parse::<u64>().ok()
        && (64..=4320).contains(&h)
    {
        return Some(h);
    }
    Some(match l.as_str() {
        "mobile" | "tiny" => 144,
        "lowest" => 240,
        "low" => 360,
        "sd" | "medium" => 480,
        "hd" => 720,
        "full" | "fullhd" | "fhd" => 1080,
        "qhd" | "quadhd" | "2k" => 1440,
        "uhd" | "4k" => 2160,
        _ => return None,
    })
}

/// A decoded blob: re-parse as JSON or scan for media tokens.
fn consume_decoded(
    dec: &[u8],
    out: &mut Vec<Candidate>,
    dur: &mut Option<f64>,
    title: &mut Option<String>,
    thumb: &mut Option<String>,
    depth: u8,
) {
    let Ok(text) = std::str::from_utf8(dec) else {
        return;
    };
    let text = text.trim();
    if (text.starts_with('{') || text.starts_with('['))
        && let Ok(v) = serde_json::from_str::<Value>(text)
    {
        walk_json(&v, out, dur, title, thumb, depth + 1);
        return;
    }
    if contains_media_token(text) {
        out.extend(regex_scan::scan_text(text));
    }
}
