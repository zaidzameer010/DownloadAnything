//! JSON-blob layer: script blobs and `data-*` JSON objects.

use std::sync::LazyLock;

use aho_corasick::AhoCorasick;
use base64::Engine;
use scraper::{Html, Selector};
use serde_json::Value;

use crate::extract::candidates::{
    Candidate, codecs_from_mime, contains_media_token, height_from_label, looks_media_str,
    normalize_codecs, quality_from_key, resolution_dims,
};
use crate::extract::kvs::{self, Flashvars};
use crate::extract::meta::{has_suffix, is_junk_thumbnail};
use crate::extract::regex_scan;
use crate::extract::selector;
use crate::model::{Quality, SourceLayer, TitleSource};

static SCRIPT_SEL: LazyLock<Selector> = LazyLock::new(|| selector("script"));
static ALL_SEL: LazyLock<Selector> = LazyLock::new(|| selector("*"));

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

/// One automaton pass instead of one substring scan per anchor.
static ANCHOR_AC: LazyLock<AhoCorasick> = LazyLock::new(|| {
    AhoCorasick::builder()
        .build(ANCHORS)
        .expect("player anchor automaton")
});

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
    "manifesturi",
    "progressive",
];

/// Keys that always name a media/stream address.
///
/// Signed API URLs often carry no file extension, so the media-token check
/// would reject them. Weak keys (`url`, `src`, `link`, …) still require a
/// media token.
const STRONG_URL_KEYS: &[&str] = &[
    "video_url",
    "videourl",
    "videopath",
    "streamurl",
    "stream_url",
    "playbackurl",
    "playurl",
    "play_url",
    "downloadurl",
    "download_url",
    "hls",
    "hlsurl",
    "hls_url",
    "dash",
    "dashurl",
    "dash_url",
    "mpd",
    "m3u8",
    "manifest",
    "manifesturl",
    "manifesturi",
    "srcurl",
    "mediaurl",
    "audiourl",
    "audio_url",
];

/// Keys whose string value names a poster/thumbnail.
const THUMB_KEYS: &[&str] = &[
    "poster",
    "preview_url",
    "previewurl",
    "preview_image",
    "previewimage",
    "thumbnail_url",
    "thumbnailurl",
    "thumb_url",
    "thumburl",
    "thumbnail",
    "screen_url",
    "screenurl",
    "cover_url",
    "coverurl",
    "splash",
    "image",
    "preview",
];

/// Maximum recursion depth for one JSON blob.
///
/// Hostile pages can nest objects arbitrarily deep; player configs need a
/// handful of levels, and a bound keeps the recursive walk off the stack limit.
const MAX_JSON_DEPTH: u8 = 32;

/// A generic `title` may name the site; a dedicated media title wins.
const TITLE_RANK_GENERIC: u8 = 1;
const TITLE_RANK_STRONG: u8 = 2;

/// Longest string still attempted as base64 or hex.
const MAX_ENCODED_LEN: usize = 200_000;

/// What the JSON-blob layer found on one document.
#[derive(Debug, Default)]
pub(crate) struct Findings {
    /// Candidate addresses in walk order.
    pub candidates: Vec<Candidate>,
    /// First plausible duration in seconds.
    pub duration_secs: Option<f64>,
    /// Best title-ish string seen.
    pub title: Option<String>,
    /// Source of [`title`](Self::title).
    pub title_source: Option<TitleSource>,
    /// Strength of [`title`](Self::title) — a dedicated `video_title` beats a
    /// generic `title` that may name the site.
    title_rank: u8,
    /// First player preview/poster URL.
    pub thumbnail: Option<String>,
}

impl Findings {
    /// Fold a KVS flashvars blob into the findings.
    fn merge_flashvars(&mut self, flashvars: Flashvars) {
        self.candidates.extend(flashvars.candidates);
        if self.title_rank < TITLE_RANK_STRONG
            && let Some(title) = flashvars.title
        {
            self.title = Some(title);
            self.title_source = Some(TitleSource::Kvs);
            self.title_rank = TITLE_RANK_STRONG;
        }
        if self.thumbnail.is_none() {
            self.thumbnail = flashvars.thumbnail;
        }
    }
}

/// Walk a parsed JSON value and return what it yielded.
pub(crate) fn walk_value(value: &Value) -> Findings {
    let mut findings = Findings::default();
    walk_json(value, &mut findings, 0);
    findings
}

/// One inline `<script>` worth parsing.
#[derive(Debug, Clone)]
pub(crate) struct ScriptBlob {
    id: String,
    script_type: String,
    text: String,
}

/// Collect inline script text and JSON-valued `data-*` attributes.
///
/// The DOM is `!Sync`, so the page pipeline extracts owned text serially and
/// hands it to worker threads that do the expensive JSON parsing.
pub(crate) fn collect(document: &Html) -> (Vec<ScriptBlob>, Vec<String>) {
    let mut scripts = Vec::new();
    for element in document.select(&SCRIPT_SEL) {
        let value = element.value();
        if value.attr("src").is_some() {
            continue;
        }
        let text = element.text().collect::<String>();
        let text = text.trim();
        if text.is_empty() {
            continue;
        }
        scripts.push(ScriptBlob {
            id: value.attr("id").unwrap_or("").to_owned(),
            script_type: value.attr("type").unwrap_or("").to_lowercase(),
            text: text.to_owned(),
        });
    }
    let mut data_json = Vec::new();
    for element in document.select(&ALL_SEL) {
        for (name, value) in element.value().attrs() {
            if !name.starts_with("data-") {
                continue;
            }
            let value = value.trim();
            if value.len() > 8 && (value.starts_with('{') || value.starts_with('[')) {
                data_json.push(value.to_owned());
            }
        }
    }
    (scripts, data_json)
}

/// Find every JSON blob in the collected script text and walk it for media URLs.
///
/// Raw script text is not regex-scanned here — the whole-body scan in the page
/// pipeline already covers it; only decoded blobs need their own pass.
pub(crate) fn extract(scripts: &[ScriptBlob], data_json: &[String]) -> Findings {
    let mut findings = Findings::default();
    for script in scripts {
        let text = script.text.as_str();
        if (script.id == "__NEXT_DATA__"
            || script.script_type.contains("json")
            || text.starts_with('{'))
            && let Ok(parsed) = serde_json::from_str::<Value>(text)
        {
            walk_json(&parsed, &mut findings, 0);
            continue;
        }
        // Unanchored scripts (window.__cfg = {...} etc.): lift the first object
        // literal, else parse it as KVS flashvars.
        if !ANCHOR_AC.is_match(text) {
            if let Some(equal) = text.find('=')
                && let Some(blob) = extract_balanced(text, equal + 1)
            {
                consume_blob(&blob, &mut findings);
            }
            continue;
        }
        for anchor in ANCHORS {
            let mut search_from = 0;
            while let Some(position) = text[search_from..].find(anchor) {
                let position = search_from + position + anchor.len();
                // Skip to '=' then to the opening brace.
                let Some(equal) = text[position..].find('=') else {
                    break;
                };
                let equal = position + equal;
                if equal - position > 8 {
                    break;
                }
                if let Some(blob) = extract_balanced(text, equal + 1) {
                    consume_blob(&blob, &mut findings);
                }
                search_from = equal + 1;
                if search_from >= text.len() {
                    break;
                }
            }
        }
    }
    // data-* attributes containing JSON objects (e.g. video.js data-setup).
    for value in data_json {
        if let Ok(parsed) = serde_json::from_str::<Value>(value) {
            walk_json(&parsed, &mut findings, 0);
        }
    }
    findings
}

/// Parse a blob as JSON, falling back to KVS flashvars key/value pairs.
fn consume_blob(blob: &str, findings: &mut Findings) {
    if let Ok(parsed) = serde_json::from_str::<Value>(blob) {
        walk_json(&parsed, findings, 0);
    } else {
        findings.merge_flashvars(kvs::flashvars_candidates(blob));
    }
}

/// Extract a balanced `{...}` / `[...]` / `(...)` block starting at `from`.
fn extract_balanced(text: &str, from: usize) -> Option<String> {
    let bytes = text.as_bytes();
    let mut index = from;
    while index < bytes.len() && !matches!(bytes[index], b'{' | b'[' | b'(') {
        index += 1;
    }
    let start = index;
    let mut stack: Vec<u8> = Vec::new();
    let mut in_string: Option<u8> = None;
    let mut escaped = false;
    while index < bytes.len() {
        let byte = bytes[index];
        if let Some(quote) = in_string {
            if escaped {
                escaped = false;
            } else if byte == b'\\' {
                escaped = true;
            } else if byte == quote {
                in_string = None;
            }
        } else {
            match byte {
                b'"' | b'\'' | b'`' => in_string = Some(byte),
                b'{' | b'[' | b'(' => stack.push(byte),
                b'}' | b']' | b')' => {
                    let wanted = match byte {
                        b'}' => b'{',
                        b']' => b'[',
                        _ => b'(',
                    };
                    if stack.pop() != Some(wanted) {
                        return None;
                    }
                    if stack.is_empty() {
                        return Some(text[start..=index].to_owned());
                    }
                }
                _ => {}
            }
        }
        index += 1;
    }
    None
}

/// Whether a JSON key may hold a media URL.
fn key_is_urlish(key: &str) -> bool {
    URL_KEYS
        .iter()
        .any(|candidate| key.eq_ignore_ascii_case(candidate))
        || contains_ignore_ascii_case(key, "url")
        || has_suffix(key, "src")
        || contains_ignore_ascii_case(key, "m3u8")
        || contains_ignore_ascii_case(key, "mpd")
}

/// ASCII case-insensitive substring check without allocating a lowercase key.
fn contains_ignore_ascii_case(haystack: &str, needle: &str) -> bool {
    let needle = needle.as_bytes();
    haystack
        .as_bytes()
        .windows(needle.len())
        .any(|window| window.eq_ignore_ascii_case(needle))
}

/// Keys that always name a media/stream address.
fn is_strong_url_key(key: &str) -> bool {
    STRONG_URL_KEYS
        .iter()
        .any(|candidate| key.eq_ignore_ascii_case(candidate))
}

/// Whether a string could be base64 (standard or URL-safe alphabet).
fn looks_b64(text: &str) -> bool {
    let text = text.trim();
    text.len() >= 24
        && text.len() <= MAX_ENCODED_LEN
        && text
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '/' | '=' | '-' | '_'))
}

/// Whether a string could be hex-encoded bytes.
fn looks_hex(text: &str) -> bool {
    let text = text.trim();
    text.len() >= 32 && text.len().is_multiple_of(2) && text.chars().all(|c| c.is_ascii_hexdigit())
}

/// Rank of a title key: dedicated media titles beat a generic `title`.
fn title_rank(key: &str) -> Option<u8> {
    if ["video_title", "videotitle", "media_title", "episode_title"]
        .iter()
        .any(|title| key.eq_ignore_ascii_case(title))
    {
        Some(TITLE_RANK_STRONG)
    } else if key.eq_ignore_ascii_case("title") {
        Some(TITLE_RANK_GENERIC)
    } else {
        None
    }
}

/// Keys whose string value names a poster/thumbnail.
fn is_thumb_key(key: &str) -> bool {
    THUMB_KEYS
        .iter()
        .any(|candidate| key.eq_ignore_ascii_case(candidate))
}

/// Recursively walk a parsed JSON value collecting media URLs, duration,
/// title and player preview/poster URL.
fn walk_json(value: &Value, findings: &mut Findings, depth: u8) {
    if depth > MAX_JSON_DEPTH {
        return;
    }
    match value {
        Value::Object(map) => {
            for (key, value) in map {
                if findings.duration_secs.is_none()
                    && key.eq_ignore_ascii_case("duration")
                    && let Some(duration) = value.as_f64().or_else(|| value.as_str()?.parse().ok())
                    && (5.0..=86_400.0).contains(&duration)
                {
                    findings.duration_secs = Some(duration);
                }
                if let Some(rank) = title_rank(key)
                    && rank > findings.title_rank
                    && let Some(title) = value.as_str()
                {
                    let title = title.trim();
                    if !title.is_empty() && title.len() <= 300 {
                        findings.title = Some(title.to_owned());
                        findings.title_source = Some(if rank == TITLE_RANK_STRONG {
                            TitleSource::JsonBlob
                        } else {
                            TitleSource::JsonGeneric
                        });
                        findings.title_rank = rank;
                    }
                }
                if findings.thumbnail.is_none()
                    && is_thumb_key(key)
                    && let Some(thumbnail) = value.as_str()
                {
                    let thumbnail = thumbnail.trim();
                    if !thumbnail.is_empty()
                        && (thumbnail.starts_with("http://")
                            || thumbnail.starts_with("https://")
                            || thumbnail.starts_with("//")
                            || thumbnail.starts_with('/'))
                        && !is_junk_thumbnail(thumbnail)
                    {
                        findings.thumbnail = Some(thumbnail.to_owned());
                    }
                }
                if let Value::String(text) = value {
                    if key_is_urlish(key)
                        && (looks_media_str(text)
                            || (is_strong_url_key(key) && text.starts_with("http")))
                    {
                        let mut candidate = Candidate::new(text.as_str(), SourceLayer::JsonBlob);
                        candidate.quality = quality_of_object(map);
                        candidate.codecs = codecs_of_object(map);
                        findings.candidates.push(candidate);
                    } else if let Some(quality) = quality_from_key(key)
                        && (looks_media_str(text) || text.starts_with("http"))
                    {
                        // Quality-keyed source maps: {"720p": "…", "1080p": "…"}.
                        let mut candidate = Candidate::new(text.as_str(), SourceLayer::JsonBlob);
                        candidate.quality = Some(quality);
                        candidate.codecs = codecs_of_object(map);
                        findings.candidates.push(candidate);
                    } else if depth < 2
                        && looks_hex(text)
                        && text.len() < MAX_ENCODED_LEN
                        && let Ok(decoded) = hex::decode(text.trim())
                    {
                        consume_decoded(&decoded, findings, depth);
                    } else if depth < 2 && looks_b64(text) && text.len() < MAX_ENCODED_LEN {
                        // Hex is a strict subset of the base64 alphabet — try
                        // it first, or a hex-encoded blob decodes to garbage.
                        if let Ok(decoded) = base64::engine::general_purpose::STANDARD
                            .decode(text.trim())
                            .or_else(|_| {
                                base64::engine::general_purpose::URL_SAFE.decode(text.trim())
                            })
                        {
                            consume_decoded(&decoded, findings, depth);
                        }
                    }
                } else if (key.eq_ignore_ascii_case("sources")
                    || key.eq_ignore_ascii_case("videos"))
                    && value.is_array()
                {
                    extract_sources_array(value, findings);
                    walk_json(value, findings, depth + 1);
                } else if value.is_array() && (key_is_urlish(key) || is_strong_url_key(key)) {
                    // Player ladders that list bare URL strings
                    // (`progressive: ["…"]`); object entries are walked below.
                    for item in value
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter_map(Value::as_str)
                    {
                        if looks_media_str(item)
                            || (is_strong_url_key(key) && item.starts_with("http"))
                        {
                            findings
                                .candidates
                                .push(Candidate::new(item, SourceLayer::JsonBlob));
                        }
                    }
                    walk_json(value, findings, depth + 1);
                } else {
                    walk_json(value, findings, depth + 1);
                }
            }
        }
        Value::Array(items) => items
            .iter()
            .for_each(|item| walk_json(item, findings, depth + 1)),
        _ => {}
    }
}

/// JW-player-style `sources`/`videos`: `[{"file": "...", "label": "720p"}, ...]`
/// or a bare list of URL strings.
fn extract_sources_array(value: &Value, findings: &mut Findings) {
    let Some(items) = value.as_array() else {
        return;
    };
    for item in items {
        if let Some(url) = item.as_str() {
            if looks_media_str(url) {
                findings
                    .candidates
                    .push(Candidate::new(url, SourceLayer::JsonBlob));
            }
            continue;
        }
        let Some(object) = item.as_object() else {
            continue;
        };
        if findings.thumbnail.is_none()
            && let Some(image) = ["image", "poster", "thumbnail", "preview"]
                .iter()
                .find_map(|key| object.get(*key).and_then(Value::as_str))
        {
            let image = image.trim();
            if !image.is_empty() && !is_junk_thumbnail(image) {
                findings.thumbnail = Some(image.to_owned());
            }
        }
        let url = ["file", "src", "url", "stream", "source"]
            .iter()
            .find_map(|key| object.get(*key).and_then(Value::as_str));
        let Some(url) = url else { continue };
        if !looks_media_str(url) && !url.starts_with("http") {
            continue;
        }
        let mut candidate = Candidate::new(url, SourceLayer::JsonBlob);
        let mut quality = quality_of_object(object);
        if let Some(name) = object.get("name").and_then(Value::as_str)
            && quality
                .as_ref()
                .is_none_or(|quality| quality.label.is_none())
        {
            quality.get_or_insert_with(Quality::default).label = Some(name.to_owned());
        }
        candidate.quality = quality;
        candidate.codecs = codecs_of_object(object);
        findings.candidates.push(candidate);
    }
}

/// Codecs declared next to a URL key (`vcodec`/`acodec`, `codec(s)` or a
/// mime `type` carrying `codecs=`), joined video-first like yt-dlp rows.
fn codecs_of_object(map: &serde_json::Map<String, Value>) -> Option<String> {
    let string_of = |keys: &[&str]| -> Option<&str> {
        map.iter().find_map(|(key, value)| {
            keys.iter()
                .any(|candidate| key.eq_ignore_ascii_case(candidate))
                .then(|| value.as_str())
                .flatten()
        })
    };
    let parts = [
        string_of(&["vcodec", "video_codec", "videocodec"]),
        string_of(&["acodec", "audio_codec", "audiocodec"]),
    ]
    .into_iter()
    .flatten()
    .collect::<Vec<_>>()
    .join(", ");
    if let Some(codecs) = normalize_codecs(&parts) {
        return Some(codecs);
    }
    if let Some(codecs) = map.iter().find_map(|(key, value)| {
        ["codecs", "codec"]
            .iter()
            .any(|candidate| key.eq_ignore_ascii_case(candidate))
            .then(|| match value {
                Value::String(text) => normalize_codecs(text),
                Value::Array(items) => normalize_codecs(
                    &items
                        .iter()
                        .filter_map(Value::as_str)
                        .collect::<Vec<_>>()
                        .join(", "),
                ),
                _ => None,
            })
            .flatten()
    }) {
        return Some(codecs);
    }
    string_of(&["type", "mime", "mimetype", "mime_type", "content_type"]).and_then(codecs_from_mime)
}

/// Resolution/bandwidth hints carried by the JSON object next to a URL key
/// (`{"url": …, "height": 1080, "bandwidth": 5_000_000}`) or a sources entry
/// (`quality: 720`, `resolution: "1920x1080"`, `res: "720p"`).
fn quality_of_object(map: &serde_json::Map<String, Value>) -> Option<Quality> {
    let number = |key: &str| -> Option<u64> {
        map.get(key).and_then(|value| {
            value
                .as_u64()
                .or_else(|| value.as_str()?.trim().parse().ok())
        })
    };
    let (res_width, res_height) = map
        .get("resolution")
        .or_else(|| map.get("res"))
        .and_then(Value::as_str)
        .map(resolution_dims)
        .unwrap_or_default();
    let quality_value = map.get("quality");
    let label = map
        .get("label")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .or_else(|| quality_value.and_then(Value::as_str).map(str::to_owned));
    let height = number("height")
        .or_else(|| quality_value.and_then(Value::as_u64))
        .or_else(|| number("res"))
        .or_else(|| number("resolution"))
        .or(res_height.map(u64::from))
        .or_else(|| label.as_deref().and_then(height_from_label).map(u64::from));
    let width = number("width").or(res_width.map(u64::from));
    let bandwidth = number("bandwidth");
    if height.is_none() && width.is_none() && bandwidth.is_none() && label.is_none() {
        return None;
    }
    Some(Quality {
        height: height.and_then(|height| u32::try_from(height).ok()),
        width: width.and_then(|width| u32::try_from(width).ok()),
        bandwidth,
        label,
    })
}

/// A decoded blob: re-parse as JSON or scan for media tokens.
fn consume_decoded(decoded: &[u8], findings: &mut Findings, depth: u8) {
    let Ok(text) = std::str::from_utf8(decoded) else {
        return;
    };
    let text = text.trim();
    if (text.starts_with('{') || text.starts_with('['))
        && let Ok(value) = serde_json::from_str::<Value>(text)
    {
        walk_json(&value, findings, depth + 1);
        return;
    }
    if contains_media_token(text) {
        findings.candidates.extend(regex_scan::scan_text(text));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn walk_json_collects_title_alongside_candidates() {
        // Player-config/JSON blobs carry the content title next to the media
        // URLs — first title-ish key wins; "name" is excluded (sources abuse
        // it as a quality label). Also grabs player preview/poster URLs.
        let value: Value = serde_json::from_str(
            r#"{"video_title": "Clean Name", "duration": 42, "preview_url": "https://cdn.x/preview.jpg",
                "sources": [{"file": "https://cdn.x/v.mp4", "label": "720p", "name": "720p"}]}"#,
        )
        .unwrap();
        let findings = walk_value(&value);
        assert_eq!(findings.title.as_deref(), Some("Clean Name"));
        assert_eq!(findings.duration_secs, Some(42.0));
        assert_eq!(
            findings.thumbnail.as_deref(),
            Some("https://cdn.x/preview.jpg")
        );
        assert!(
            !findings.candidates.is_empty(),
            "sources must still yield candidates"
        );
    }

    #[test]
    fn json_sources_carry_numeric_quality_and_sibling_resolution() {
        let value: Value = serde_json::from_str(
            r#"{"sources":[
                 {"file":"https://cdn.x/a.mp4","quality":720},
                 {"file":"https://cdn.x/b.mp4","resolution":"1920x1080"},
                 {"file":"https://cdn.x/c.mp4","label":"4k"},
                 {"file":"https://cdn.x/e.mp4","resolution":720}
               ],
               "url":"https://cdn.x/d.mp4","height":"480","bandwidth":900000}"#,
        )
        .unwrap();
        let findings = walk_value(&value);
        let quality = |name: &str| {
            findings
                .candidates
                .iter()
                .find(|candidate| candidate.raw.ends_with(name))
                .and_then(|candidate| candidate.quality.clone())
                .unwrap_or_else(|| panic!("{name} missing: {:?}", findings.candidates))
        };
        assert_eq!(quality("a.mp4").height, Some(720));
        assert_eq!(quality("b.mp4").height, Some(1080));
        assert_eq!(quality("c.mp4").height, Some(2160));
        assert_eq!(quality("e.mp4").height, Some(720));
        let direct = quality("d.mp4");
        assert_eq!(direct.height, Some(480));
        assert_eq!(direct.bandwidth, Some(900_000));
    }

    #[test]
    fn depth_cap_stops_hostile_nesting() {
        let mut value = serde_json::json!({"url": "https://cdn.x/v.mp4"});
        for _ in 0..100 {
            value = serde_json::json!({"nested": value});
        }
        assert!(
            walk_value(&value).candidates.is_empty(),
            "media beyond the depth cap must be skipped, not overflow the stack"
        );

        let mut shallow = serde_json::json!({"url": "https://cdn.x/v.mp4"});
        for _ in 0..10 {
            shallow = serde_json::json!({"nested": shallow});
        }
        assert_eq!(walk_value(&shallow).candidates.len(), 1);
    }

    #[test]
    fn base64_decoded_json_is_walked() {
        let encoded =
            base64::engine::general_purpose::STANDARD.encode(r#"{"url":"https://cdn.x/v.mp4"}"#);
        let value = serde_json::json!({ "config": encoded });
        let findings = walk_value(&value);
        assert!(
            findings
                .candidates
                .iter()
                .any(|candidate| candidate.raw.ends_with("v.mp4")),
            "{:?}",
            findings.candidates
        );
    }

    #[test]
    fn json_sources_carry_video_and_audio_codecs() {
        let value: Value = serde_json::from_str(
            r#"{"sources":[
                 {"file":"https://cdn.x/a.mp4","vcodec":"av01.0.05M.08","acodec":"opus","height":720},
                 {"src":"https://cdn.x/b.mp4","type":"video/webm; codecs=\"vp09.00.10.08, opus\"","label":"1080p"},
                 {"url":"https://cdn.x/c.mp4","codecs":["avc1.64001F","mp4a.40.2"],"height":480}
               ]}"#,
        )
        .unwrap();
        let findings = walk_value(&value);
        let codecs = |name: &str| {
            findings
                .candidates
                .iter()
                .find(|candidate| candidate.raw.ends_with(name))
                .and_then(|candidate| candidate.codecs.clone())
                .unwrap_or_else(|| panic!("{name} missing: {:?}", findings.candidates))
        };
        assert_eq!(codecs("a.mp4"), "av01.0.05M.08, opus");
        assert_eq!(codecs("b.mp4"), "vp09.00.10.08, opus");
        assert_eq!(codecs("c.mp4"), "avc1.64001F, mp4a.40.2");
    }

    #[test]
    fn quality_keyed_source_maps_yield_every_rendition() {
        let value: Value = serde_json::from_str(
            r#"{"sources":{"360p":"https://cdn.x/360.mp4","720p":"https://cdn.x/720.mp4","1080":"https://cdn.x/1080.mp4"}}"#,
        )
        .unwrap();
        let findings = walk_value(&value);
        let mut heights: Vec<Option<u32>> = findings
            .candidates
            .iter()
            .map(|candidate| {
                candidate
                    .quality
                    .as_ref()
                    .and_then(|quality| quality.height)
            })
            .collect();
        heights.sort_unstable();
        assert_eq!(heights, vec![Some(360), Some(720), Some(1080)]);
    }

    #[test]
    fn codecs_prefer_declared_vcodec_over_mime_parameter() {
        let value: Value = serde_json::from_str(
            r#"{"url":"https://cdn.x/v.mp4","vcodec":"hvc1.1.6.L93","acodec":"mp4a.40.2","type":"video/mp4; codecs=avc1.64001F"}"#,
        )
        .unwrap();
        let findings = walk_value(&value);
        assert_eq!(
            findings.candidates[0].codecs.as_deref(),
            Some("hvc1.1.6.L93, mp4a.40.2")
        );
    }

    #[test]
    fn dedicated_title_key_beats_generic_title() {
        let value: Value =
            serde_json::from_str(r#"{"title":"Site","video_title":"Clip"}"#).unwrap();
        assert_eq!(walk_value(&value).title.as_deref(), Some("Clip"));
        let value: Value = serde_json::from_str(r#"{"title":"Site"}"#).unwrap();
        assert_eq!(walk_value(&value).title.as_deref(), Some("Site"));
    }

    #[test]
    fn url_and_thumbnail_keys_match_case_insensitively() {
        let value: Value = serde_json::from_str(
            r#"{"VIDEO_URL":"https://cdn.x/stream/abc","THUMBNAIL_URL":"https://cdn.x/p.jpg"}"#,
        )
        .unwrap();
        let findings = walk_value(&value);
        assert!(
            findings
                .candidates
                .iter()
                .any(|candidate| candidate.raw.ends_with("/stream/abc")),
            "{:?}",
            findings.candidates
        );
        assert_eq!(findings.thumbnail.as_deref(), Some("https://cdn.x/p.jpg"));
    }

    #[test]
    fn hex_encoded_json_is_decoded_before_base64() {
        // Hex is valid base64 alphabet, so a hex-encoded blob used to decode
        // as base64 garbage and never reach the hex branch.
        let encoded = hex::encode(r#"{"url":"https://cdn.x/v.mp4"}"#);
        let value = serde_json::json!({ "config": encoded });
        let findings = walk_value(&value);
        assert!(
            findings
                .candidates
                .iter()
                .any(|candidate| candidate.raw.ends_with("v.mp4")),
            "{:?}",
            findings.candidates
        );
    }

    #[test]
    fn data_attributes_with_json_are_walked() {
        let document = Html::parse_document(
            r#"<div data-setup='{"sources":[{"file":"https://cdn.x/v.mp4"}]}'></div>"#,
        );
        let (scripts, data_json) = collect(&document);
        let findings = extract(&scripts, &data_json);
        assert!(
            findings
                .candidates
                .iter()
                .any(|candidate| candidate.raw.ends_with("v.mp4")),
            "{:?}",
            findings.candidates
        );
    }

    #[test]
    fn balanced_extraction_handles_nested_strings() {
        assert_eq!(
            extract_balanced(r#"x = {"a":"}"};"#, 0).as_deref(),
            Some(r#"{"a":"}"}"#)
        );
        assert_eq!(extract_balanced("no braces", 0), None);
    }

    #[test]
    fn shaka_manifest_uri_and_bare_url_arrays_are_discovered() {
        // Shaka Player names its manifest `manifestUri`; Bitmovin lists
        // progressive files as a bare string array.
        let value: Value = serde_json::from_str(
            r#"{"manifestUri":"https://cdn.x/dash/manifest.mpd",
                "source":{"progressive":["https://cdn.x/a.mp4","https://cdn.x/b.mp4"]},
                "videos":["https://cdn.x/c.mp4"]}"#,
        )
        .unwrap();
        let findings = walk_value(&value);
        let raws: Vec<&str> = findings
            .candidates
            .iter()
            .map(|candidate| candidate.raw.as_str())
            .collect();
        assert!(
            raws.contains(&"https://cdn.x/dash/manifest.mpd"),
            "{raws:?}"
        );
        assert!(raws.contains(&"https://cdn.x/a.mp4"), "{raws:?}");
        assert!(raws.contains(&"https://cdn.x/b.mp4"), "{raws:?}");
        assert!(raws.contains(&"https://cdn.x/c.mp4"), "{raws:?}");
    }
}
