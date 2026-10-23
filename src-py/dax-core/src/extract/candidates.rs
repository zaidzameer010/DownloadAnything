use crate::input;
use crate::model::{MediaKind, MediaLink, Quality, SourceLayer};
use std::sync::LazyLock;
use url::Url;

/// A raw media-ish URL found by a layer, before validation/resolution.
#[derive(Debug, Clone)]
pub struct Candidate {
    pub raw: String,
    pub source: SourceLayer,
    pub kind_hint: Option<MediaKind>,
    pub quality: Option<Quality>,
    /// Index of the <video>/<audio> element it came from (playlist grouping).
    pub media_index: Option<u32>,
    pub label: Option<String>,
}

impl Candidate {
    pub fn new(raw: impl Into<String>, source: SourceLayer) -> Self {
        Self {
            raw: raw.into(),
            source,
            kind_hint: None,
            quality: None,
            media_index: None,
            label: None,
        }
    }
}

// Covers every extension in input::{VIDEO_EXTS,AUDIO_EXTS} — a token the
// ranker recognizes must also be discoverable here (dax/filter.py parity).
const MEDIA_TOKENS: &[&str] = &[
    ".m3u8",
    ".mpd",
    ".mp4",
    ".webm",
    ".mkv",
    ".mov",
    ".m4v",
    ".3gp",
    ".mp3",
    ".m4a",
    ".ogg",
    ".oga",
    ".opus",
    ".flac",
    ".wav",
    ".aac",
    ".ts",
    ".wmv",
    ".avi",
    ".flv",
    ".mpg",
    ".mpeg",
    ".m2ts",
    ".wma",
    ".aiff",
    "format=m3u8",
    "format=mpd",
    "multi=",
    "/hls/",
    "videoplayback",
    "mime=video",
    "mime=audio",
];

/// All media tokens in one automaton — a single SIMD-accelerated pass over
/// the haystack, case-insensitive, instead of lowercasing the whole string
/// and scanning once per token.
static MEDIA_AC: LazyLock<aho_corasick::AhoCorasick> = LazyLock::new(|| {
    aho_corasick::AhoCorasick::builder()
        .ascii_case_insensitive(true)
        .build(MEDIA_TOKENS)
        .unwrap()
});

/// Does this string contain something that smells like media?
pub fn contains_media_token(s: &str) -> bool {
    MEDIA_AC.is_match(s)
}

/// Media token must appear in the path or query — a host that merely
/// contains an extension (`https://cdn.ts/…`) isn't media.
pub fn looks_media_url(u: &Url) -> bool {
    contains_media_token(u.path()) || u.query().is_some_and(contains_media_token)
}

/// URL-ish AND media-ish — used to filter JSON values / data-* attributes.
/// Relative URLs count (resolved against the base later); whitespace-free
/// strings only so plain sentences can't false-positive.
pub fn looks_media_str(s: &str) -> bool {
    let t = s.trim();
    if t.is_empty() || t.chars().any(|c| c.is_whitespace()) {
        return false;
    }
    contains_media_token(t)
}

/// Resolve a raw candidate string against the page URL into a MediaLink.
pub fn resolve(base: &Url, c: &Candidate) -> Option<MediaLink> {
    let raw = c.raw.trim();
    if raw.is_empty() || raw.starts_with("data:") || raw.starts_with("blob:") {
        return None;
    }
    // Percent-encoded absolute URLs show up inside query strings / JS.
    let raw_owned;
    let raw = if raw.starts_with("http%3A%2F%2F") || raw.starts_with("https%3A%2F%2F") {
        raw_owned = urlencoding::decode(raw).ok()?.into_owned();
        &raw_owned
    } else {
        raw
    };
    let u = if raw.starts_with("//") {
        Url::parse(&format!("{}:{}", base.scheme(), raw)).ok()?
    } else {
        base.join(raw).ok()?
    };
    if !matches!(u.scheme(), "http" | "https") {
        return None;
    }
    let kind = c.kind_hint.unwrap_or_else(|| infer_kind(&u));
    let mut link = MediaLink::new(u, kind, c.source);
    if matches!(kind, MediaKind::Progressive | MediaKind::Audio) {
        link.container = input::path_ext(&link.url);
    }
    link.quality = c.quality.clone();
    if link.quality.is_none() {
        link.quality = height_from_filename(&link.url).map(|h| Quality {
            height: Some(h),
            ..Default::default()
        });
    }
    if let Some(label) = c.label.clone() {
        link.quality.get_or_insert_with(Quality::default).label = Some(label);
    }
    link.media_index = c.media_index;
    link.headers.referer = Some(base.as_str().to_string());
    link.expires_at = expiry_from_query(&link.url);
    Some(link)
}

/// Height from a `_720p` / `-720p` filename token — KVS and many CDNs
/// encode the variant quality in the file name, often followed by a codec
/// marker (`-720p-av1.mp4`). The rightmost `<digits>p` token wins.
fn height_from_filename(u: &Url) -> Option<u32> {
    let seg = u.path_segments()?.rev().find(|s| !s.is_empty())?;
    seg.rsplit(['_', '-', '.']).find_map(|tok| {
        let h: u32 = tok.strip_suffix('p')?.parse().ok()?;
        (100..=4320).contains(&h).then_some(h)
    })
}

fn infer_kind(u: &Url) -> MediaKind {
    if let Some(e) = input::path_ext(u) {
        if input::HLS_EXTS.contains(&e.as_str()) {
            return MediaKind::Hls;
        }
        if input::DASH_EXTS.contains(&e.as_str()) {
            return MediaKind::Dash;
        }
        if input::AUDIO_EXTS.contains(&e.as_str()) {
            return MediaKind::Audio;
        }
    }
    if input::looks_manifest_url(u) {
        let q = u.query().unwrap_or("");
        return if q.contains("mpd") || u.path().contains(".mpd") {
            MediaKind::Dash
        } else {
            MediaKind::Hls
        };
    }
    MediaKind::Progressive
}

fn expiry_from_query(u: &Url) -> Option<i64> {
    for (k, v) in u.query_pairs() {
        if matches!(k.as_ref(), "expire" | "expires" | "e" | "exp" | "x-expires")
            && let Ok(ts) = v.parse::<i64>()
        {
            return Some(ts);
        }
    }
    None
}
