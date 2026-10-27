//! Raw candidate URLs, deduplication and resolution into media links.

use std::collections::HashMap;
use std::sync::LazyLock;

use url::Url;

use crate::input;
use crate::model::{MediaKind, MediaLink, Quality, SourceLayer};

/// A raw media-ish URL found by a layer, before validation and resolution.
#[derive(Debug, Clone)]
pub(crate) struct Candidate {
    /// Address exactly as the page spelled it.
    pub raw: String,
    /// Layer that produced the candidate.
    pub source: SourceLayer,
    /// Kind asserted by the layer (mime type, element tag).
    pub kind_hint: Option<MediaKind>,
    /// Resolution/bitrate hints carried alongside the address.
    pub quality: Option<Quality>,
    /// Container named by a mime/type attribute when the URL has no extension.
    pub container: Option<String>,
    /// Video/audio codecs declared next to the address.
    pub codecs: Option<String>,
    /// Index of the `<video>`/`<audio>` element it came from (playlist grouping).
    pub media_index: Option<u32>,
    /// Page that actually embedded this address (an iframe URL for subpage
    /// media); falls back to the resolved page URL.
    pub referer: Option<String>,
}

impl Candidate {
    /// Candidate with no hints yet.
    pub(crate) fn new(raw: impl Into<String>, source: SourceLayer) -> Self {
        Self {
            raw: raw.into(),
            source,
            kind_hint: None,
            quality: None,
            container: None,
            codecs: None,
            media_index: None,
            referer: None,
        }
    }
}

/// Tokens that make a string look like a media address.
///
/// Extension tokens are derived from the [`input`] vocabularies, so a
/// container added there is discoverable everywhere at once; the literals are
/// player/manifest conventions that carry no file extension.
const MEDIA_TOKENS: &[&str] = &[
    "format=m3u8",
    "format=mpd",
    "type=m3u8",
    "type=mpd",
    "multi=",
    "/hls/",
    "/manifest",
    "videoplayback",
    "mime=video",
    "mime=audio",
];

/// All media tokens in one automaton — a single SIMD-accelerated pass over the
/// haystack, case-insensitive, instead of lowercasing the whole string and
/// scanning once per token.
static MEDIA_AC: LazyLock<aho_corasick::AhoCorasick> = LazyLock::new(|| {
    let mut tokens: Vec<String> = MEDIA_TOKENS
        .iter()
        .map(|token| (*token).to_owned())
        .collect();
    tokens.extend(
        input::VIDEO_EXTS
            .iter()
            .chain(input::AUDIO_EXTS)
            .chain(input::HLS_EXTS)
            .chain(input::DASH_EXTS)
            .map(|extension| format!(".{extension}")),
    );
    aho_corasick::AhoCorasick::builder()
        .ascii_case_insensitive(true)
        .build(&tokens)
        .expect("media token automaton")
});

/// Does this string contain something that smells like media?
pub(crate) fn contains_media_token(text: &str) -> bool {
    MEDIA_AC.is_match(text)
}

/// Media token must appear in the path or query — a host that merely contains
/// an extension (`https://cdn.ts/…`) isn't media.
pub(crate) fn looks_media_url(url: &Url) -> bool {
    contains_media_token(url.path()) || url.query().is_some_and(contains_media_token)
}

/// URL-ish AND media-ish — used to filter JSON values and `data-*` attributes.
///
/// Relative URLs count (resolved against the base later); whitespace-free
/// strings only so plain sentences can't false-positive.
pub(crate) fn looks_media_str(text: &str) -> bool {
    let text = text.trim();
    if text.is_empty() || text.chars().any(char::is_whitespace) {
        return false;
    }
    contains_media_token(text)
}

/// Resolve a raw candidate string against the page URL into a [`MediaLink`].
pub(crate) fn resolve(base: &Url, candidate: &Candidate) -> Option<MediaLink> {
    let url = absolute(base, &candidate.raw)?;
    if !matches!(url.scheme(), "http" | "https") || input::is_retired_ext(&url) {
        return None;
    }
    let kind = candidate.kind_hint.unwrap_or_else(|| infer_kind(&url));
    let mut link = MediaLink::new(url, kind, candidate.source);
    if matches!(kind, MediaKind::Progressive | MediaKind::Audio) {
        // A mime/type attribute names the container for extensionless URLs.
        link.container = candidate
            .container
            .clone()
            .or_else(|| input::path_ext(&link.url));
    }
    link.quality = candidate.quality.clone();
    link.codecs = candidate.codecs.clone();
    // A label without a height (`label="Full HD"`) still carries its
    // rendition height in the file name.
    if link
        .quality
        .as_ref()
        .is_none_or(|quality| quality.height.is_none())
        && let Some(height) = height_from_filename(&link.url)
    {
        link.quality.get_or_insert_with(Quality::default).height = Some(height);
    }
    link.media_index = candidate.media_index;
    link.headers.referer = Some(
        candidate
            .referer
            .clone()
            .unwrap_or_else(|| base.as_str().to_owned()),
    );
    link.expires_at = expiry_from_query(&link.url);
    Some(link)
}

/// Absolute form of a raw URL-ish string.
///
/// Protocol-relative, percent-encoded absolute and plain relative forms all
/// resolve against `base`.
pub(crate) fn absolute(base: &Url, raw: &str) -> Option<Url> {
    let raw = raw.trim();
    let has_prefix = |length: usize, prefix: &str| {
        raw.get(..length)
            .is_some_and(|head| head.eq_ignore_ascii_case(prefix))
    };
    if raw.is_empty() || has_prefix(5, "data:") || has_prefix(5, "blob:") {
        return None;
    }
    let decoded;
    let raw = if has_prefix(13, "http%3A%2F%2F") || has_prefix(14, "https%3A%2F%2F") {
        decoded = urlencoding::decode(raw).ok()?.into_owned();
        &decoded
    } else {
        raw
    };
    if raw.starts_with("//") {
        Url::parse(&format!("{}:{}", base.scheme(), raw)).ok()
    } else {
        base.join(raw).ok()
    }
}

/// Collapse candidates that name the same URL, keeping the richest metadata.
///
/// One ladder URL surfaces from a sources array, a sibling JSON key and a raw
/// script — only one row should reach the ranker.
pub(crate) fn dedupe(candidates: Vec<Candidate>) -> Vec<Candidate> {
    let mut out: Vec<Candidate> = Vec::with_capacity(candidates.len());
    let mut at: HashMap<String, usize> = HashMap::with_capacity(candidates.len());
    for candidate in candidates {
        let key = candidate.raw.trim().to_owned();
        match at.get(&key) {
            Some(&index) => merge_candidate(&mut out[index], candidate),
            None => {
                at.insert(key, out.len());
                out.push(candidate);
            }
        }
    }
    out
}

/// Weight of a height difference; heights dominate the quality comparison.
const HEIGHT_WEIGHT: u64 = 1_000_000;

/// Weight of a width difference; widths break height ties.
const WIDTH_WEIGHT: u64 = 1_000;

/// Scalar ordering key for quality metadata.
fn quality_score(quality: Option<&Quality>) -> u64 {
    quality.map_or(0, |quality| {
        u64::from(quality.height.unwrap_or(0)) * HEIGHT_WEIGHT
            + u64::from(quality.width.unwrap_or(0)) * WIDTH_WEIGHT
            + quality.bandwidth.unwrap_or(0)
    })
}

/// Fill unset fields of `dst` from `src`.
fn fill_quality(dst: &mut Quality, src: &Quality) {
    if dst.height.is_none() {
        dst.height = src.height;
    }
    if dst.width.is_none() {
        dst.width = src.width;
    }
    if dst.bandwidth.is_none() {
        dst.bandwidth = src.bandwidth;
    }
    if dst.label.is_none() {
        dst.label = src.label.clone();
    }
}

/// Merge a duplicate candidate into the row already kept for its URL.
fn merge_candidate(dst: &mut Candidate, src: Candidate) {
    let richer = quality_score(src.quality.as_ref()) > quality_score(dst.quality.as_ref());
    if richer {
        let mut best = src.quality.clone().unwrap_or_default();
        if let Some(dst_quality) = dst.quality.as_ref() {
            fill_quality(&mut best, dst_quality);
        }
        dst.quality = Some(best);
    } else if let (Some(dst_quality), Some(src_quality)) =
        (dst.quality.as_mut(), src.quality.as_ref())
    {
        fill_quality(dst_quality, src_quality);
    } else if dst.quality.is_none() {
        dst.quality = src.quality.clone();
    }
    if dst.kind_hint.is_none() {
        dst.kind_hint = src.kind_hint;
    }
    if dst.container.is_none() {
        dst.container = src.container;
    }
    if dst.codecs.is_none() {
        dst.codecs = src.codecs;
    }
    if dst.media_index.is_none() {
        dst.media_index = src.media_index;
    }
    if dst.referer.is_none() {
        dst.referer = src.referer;
    }
}

/// Height encoded in a quality label (`720p`, `1080p60`, `hd`, `4k`).
pub(crate) fn height_from_label(label: &str) -> Option<u32> {
    let label = label.trim().to_ascii_lowercase();
    if let Some((head, _)) = label.split_once('p')
        && let Ok(height) = head.parse::<u32>()
        && (64..=4320).contains(&height)
    {
        return Some(height);
    }
    Some(match label.as_str() {
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

/// Pixel dimensions from a `1280x720` token, else a height from `720p`.
pub(crate) fn resolution_dims(text: &str) -> (Option<u32>, Option<u32>) {
    let text = text.trim().to_ascii_lowercase();
    let parse = |value: &str| {
        value
            .trim()
            .parse::<u32>()
            .ok()
            .filter(|dimension| (16..=16_384).contains(dimension))
    };
    if let Some((width, height)) = text.split_once(['x', '×'])
        && let (Some(width), Some(height)) = (parse(width), parse(height))
    {
        return (Some(width), Some(height));
    }
    (None, height_from_label(&text))
}

/// File container named by a mime type (`video/mp4` → `mp4`).
pub(crate) fn container_from_mime(mime: &str) -> Option<String> {
    let mime = mime.split(';').next()?.trim().to_ascii_lowercase();
    let subtype = mime.split('/').nth(1)?;
    let extension = match subtype {
        "mp4" | "x-m4v" => "mp4",
        "webm" => "webm",
        "x-matroska" | "matroska" => "mkv",
        "quicktime" => "mov",
        "mp2t" => "ts",
        "mpeg" if mime.starts_with("audio/") => "mp3",
        "aac" => "aac",
        "opus" => "opus",
        "flac" | "x-flac" => "flac",
        "wav" | "x-wav" => "wav",
        _ => return None,
    };
    Some(extension.to_owned())
}

/// Normalize a comma-separated codec list, dropping yt-dlp's `none`/`unknown`.
pub(crate) fn normalize_codecs(raw: &str) -> Option<String> {
    let codecs: Vec<&str> = raw
        .split(',')
        .map(str::trim)
        .filter(|codec| !codec.is_empty() && codec.len() <= 64)
        .filter(|codec| {
            !codec.eq_ignore_ascii_case("none") && !codec.eq_ignore_ascii_case("unknown")
        })
        .take(8)
        .collect();
    (!codecs.is_empty()).then(|| codecs.join(", "))
}

/// Codecs named by the `codecs=` parameter of a mime type
/// (`video/mp4; codecs="avc1.64001F, mp4a.40.2"`).
pub(crate) fn codecs_from_mime(mime: &str) -> Option<String> {
    let lower = mime.to_ascii_lowercase();
    let start = lower.find("codecs=")? + "codecs=".len();
    let rest = &mime[start..];
    let value = rest.split(';').next()?.trim().trim_matches('"');
    normalize_codecs(value)
}

/// Quality implied by a JSON key that names a rendition (`"720p"`, `"1080"`).
pub(crate) fn quality_from_key(key: &str) -> Option<Quality> {
    let key = key.trim();
    let height = key
        .parse::<u32>()
        .ok()
        .filter(|height| (144..=4320).contains(height))
        .or_else(|| height_from_label(key))?;
    Some(Quality {
        height: Some(height),
        label: Some(key.to_owned()),
        ..Default::default()
    })
}

/// Media kind named by a mime type; `None` for non-media types (`text/html`…).
pub(crate) fn kind_from_mime(mime: &str) -> Option<MediaKind> {
    let mime = mime.split(';').next()?.trim().to_ascii_lowercase();
    if mime.contains("mpegurl") || mime.contains("vnd.apple") {
        Some(MediaKind::Hls)
    } else if mime.contains("dash+xml") {
        Some(MediaKind::Dash)
    } else if mime.starts_with("audio/") {
        Some(MediaKind::Audio)
    } else if mime.starts_with("video/") {
        Some(MediaKind::Progressive)
    } else {
        None
    }
}

/// Height from a `_720p` / `-720p` filename token.
///
/// KVS and many CDNs encode the variant quality in the file name, often
/// followed by a codec marker (`-720p-av1.mp4`). The rightmost `<digits>p`
/// token wins.
fn height_from_filename(url: &Url) -> Option<u32> {
    let segment = url
        .path_segments()?
        .rev()
        .find(|segment| !segment.is_empty())?;
    segment.rsplit(['_', '-', '.']).find_map(|token| {
        let height: u32 = token.strip_suffix('p')?.parse().ok()?;
        (100..=4320).contains(&height).then_some(height)
    })
}

/// Kind implied by the URL itself (extension or manifest tokens).
pub(crate) fn infer_kind(url: &Url) -> MediaKind {
    if let Some(extension) = input::path_ext(url) {
        if input::HLS_EXTS.contains(&extension.as_str()) {
            return MediaKind::Hls;
        }
        if input::DASH_EXTS.contains(&extension.as_str()) {
            return MediaKind::Dash;
        }
        if input::AUDIO_EXTS.contains(&extension.as_str()) {
            return MediaKind::Audio;
        }
    }
    if input::looks_manifest_url(url) {
        let query = url.query().unwrap_or("");
        return if query.contains("mpd") || url.path().contains(".mpd") {
            MediaKind::Dash
        } else {
            MediaKind::Hls
        };
    }
    MediaKind::Progressive
}

/// Unix expiry timestamp from a signed-URL query parameter.
fn expiry_from_query(url: &Url) -> Option<i64> {
    for (key, value) in url.query_pairs() {
        if matches!(
            key.as_ref(),
            "expire" | "expires" | "e" | "exp" | "x-expires"
        ) && let Ok(timestamp) = value.parse::<i64>()
        {
            return Some(timestamp);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base() -> Url {
        Url::parse("https://site.com/watch").unwrap()
    }

    #[test]
    fn resolve_carries_kind_quality_and_no_junk_field() {
        // Ranking/filtering lives in downloadanything/filter.py — the wire link must stay
        // raw: kind + quality metadata, no junk verdict.
        let c1 = Candidate::new("/media/video_720p.mp4", SourceLayer::Html);
        let c2 = Candidate::new(
            "https://cdn.site.com/hls/master.m3u8",
            SourceLayer::JsonBlob,
        );
        let c3 = Candidate::new(
            "https://cdn.site.com/dash/manifest.mpd",
            SourceLayer::JsonBlob,
        );
        let c4 = Candidate::new("https://cdn.site.com/audio/song.m4a", SourceLayer::JsonBlob);

        let l1 = resolve(&base(), &c1).unwrap();
        assert_eq!(l1.kind, MediaKind::Progressive);
        assert_eq!(l1.quality.as_ref().unwrap().height, Some(720));
        assert_eq!(l1.container.as_deref(), Some("mp4"));
        assert_eq!(
            l1.headers.referer.as_deref(),
            Some("https://site.com/watch")
        );

        let l2 = resolve(&base(), &c2).unwrap();
        assert_eq!(l2.kind, MediaKind::Hls);
        assert_eq!(resolve(&base(), &c3).unwrap().kind, MediaKind::Dash);
        assert_eq!(resolve(&base(), &c4).unwrap().kind, MediaKind::Audio);

        let mut embedded = Candidate::new("https://cdn.x/embedded.mp4", SourceLayer::Html);
        embedded.referer = Some("https://site.com/iframe".into());
        assert_eq!(
            resolve(&base(), &embedded)
                .unwrap()
                .headers
                .referer
                .as_deref(),
            Some("https://site.com/iframe"),
            "an iframe candidate keeps its own page as the referer"
        );

        let json = serde_json::to_string(&l1).unwrap();
        assert!(
            !json.contains("\"junk\""),
            "junk verdicts are the Python filter's job: {json}"
        );
    }

    #[test]
    fn filename_height_parses_codec_suffixed_variants() {
        // Multi-codec sites name variants `…-720p-av1.mp4`: the quality token
        // is not the last `-`/`_` segment, so codec-suffixed files must still
        // yield a height — anonymous ones ended up as unknown probe rows.
        for (name, want) in [
            ("18548356-240p-av1.mp4", Some(240)),
            ("18548356-720p.mp4", Some(720)),
            ("clip_1080p_h264.mp4", Some(1080)),
            ("movie.720p.WEB-DL.mp4", Some(720)),
            ("18548356-av1.mp4", None),
            ("movie.mp4", None),
        ] {
            let candidate =
                Candidate::new(format!("https://cdn.site.com/dl/{name}"), SourceLayer::Html);
            let got = resolve(&base(), &candidate)
                .unwrap()
                .quality
                .and_then(|quality| quality.height);
            assert_eq!(got, want, "{name}");
        }
    }

    #[test]
    fn filename_height_fills_a_labeled_but_heightless_quality() {
        let mut candidate =
            Candidate::new("https://cdn.site.com/dl/clip_1080p.mp4", SourceLayer::Html);
        candidate.quality = Some(Quality {
            label: Some("Full HD".into()),
            ..Default::default()
        });
        let quality = resolve(&base(), &candidate).unwrap().quality.unwrap();
        assert_eq!(quality.height, Some(1080));
        assert_eq!(quality.label.as_deref(), Some("Full HD"));
    }

    #[test]
    fn multi_query_urls_classify_as_hls() {
        // `?multi=360p,720p,...` — one URL serving the whole ladder; the
        // Python filter ranks it with masters, and the probe break keys off
        // kind=hls.
        let candidate = Candidate::new(
            "https://cdn.site.com/video?multi=360p,720p,1080p",
            SourceLayer::Regex,
        );
        assert_eq!(resolve(&base(), &candidate).unwrap().kind, MediaKind::Hls);
    }

    #[test]
    fn hls_path_urls_classify_as_hls() {
        // Extensionless signed manifest endpoints — `/media/hls/?s=…` carries
        // the stream in a base64 param, no .m3u8 extension to key on.
        let candidate = Candidate::new(
            "https://site.com/media/hls/?s=eyJ2a2V5",
            SourceLayer::JsonBlob,
        );
        assert_eq!(resolve(&base(), &candidate).unwrap().kind, MediaKind::Hls);
    }

    #[test]
    fn dedupe_merges_duplicates_keeping_the_richest_quality() {
        let mut rich = Candidate::new("https://cdn.x/v.mp4", SourceLayer::JsonBlob);
        rich.quality = Some(Quality {
            height: Some(1080),
            ..Default::default()
        });
        rich.container = Some("mp4".into());
        let generic = Candidate::new("https://cdn.x/v.mp4", SourceLayer::Regex);
        let out = dedupe(vec![generic, rich]);
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].quality.as_ref().unwrap().height, Some(1080));
        assert_eq!(out[0].container.as_deref(), Some("mp4"));
    }

    #[test]
    fn absolute_handles_protocol_relative_and_percent_encoded_urls() {
        let base = base();
        assert_eq!(
            absolute(&base, "//cdn.x/v.mp4").unwrap().as_str(),
            "https://cdn.x/v.mp4"
        );
        assert_eq!(
            absolute(&base, "https%3A%2F%2Fcdn.x%2Fv.mp4")
                .unwrap()
                .as_str(),
            "https://cdn.x/v.mp4"
        );
        assert_eq!(
            absolute(&base, "https%3a%2f%2fcdn.x%2fv.mp4")
                .unwrap()
                .as_str(),
            "https://cdn.x/v.mp4",
            "percent-encoding hex is case-insensitive"
        );
        assert!(absolute(&base, "data:video/mp4;base64,AAAA").is_none());
        assert!(absolute(&base, "DATA:video/mp4;base64,AAAA").is_none());
        assert!(absolute(&base, "blob:https://x/y").is_none());
        assert!(absolute(&base, "   ").is_none());
    }

    #[test]
    fn label_and_mime_vocabulary_maps_to_quality_and_kind() {
        assert_eq!(height_from_label("720p"), Some(720));
        assert_eq!(height_from_label("1080p60"), Some(1080));
        assert_eq!(height_from_label("4k"), Some(2160));
        assert_eq!(height_from_label("unknown"), None);
        assert_eq!(resolution_dims("1920x1080"), (Some(1920), Some(1080)));
        assert_eq!(resolution_dims("720p"), (None, Some(720)));
        assert_eq!(
            container_from_mime("video/mp4; codecs=avc1"),
            Some("mp4".into())
        );
        assert_eq!(container_from_mime("audio/mpeg"), Some("mp3".into()));
        assert_eq!(
            kind_from_mime("application/vnd.apple.mpegurl"),
            Some(MediaKind::Hls)
        );
        assert_eq!(
            kind_from_mime("application/dash+xml"),
            Some(MediaKind::Dash)
        );
        assert_eq!(kind_from_mime("text/html"), None);
    }

    #[test]
    fn codecs_parse_from_mime_and_normalize() {
        assert_eq!(
            codecs_from_mime(r#"video/mp4; codecs="avc1.64001F, mp4a.40.2""#).as_deref(),
            Some("avc1.64001F, mp4a.40.2")
        );
        assert_eq!(
            codecs_from_mime("video/webm; codecs=vp09.00.10.08,opus").as_deref(),
            Some("vp09.00.10.08, opus")
        );
        assert_eq!(codecs_from_mime("video/mp4; codecs=none"), None);
        assert_eq!(codecs_from_mime("video/mp4"), None);
        assert_eq!(
            normalize_codecs("avc1, unknown, , mp4a").as_deref(),
            Some("avc1, mp4a")
        );
    }

    #[test]
    fn quality_keyed_renditions_map_to_height_and_label() {
        assert_eq!(
            quality_from_key("720p"),
            Some(Quality {
                height: Some(720),
                label: Some("720p".into()),
                ..Default::default()
            })
        );
        assert_eq!(
            quality_from_key("1080").and_then(|quality| quality.height),
            Some(1080)
        );
        assert_eq!(quality_from_key("title"), None);
        assert_eq!(quality_from_key("8"), None);
    }

    #[test]
    fn resolve_carries_codecs() {
        let mut candidate = Candidate::new("https://cdn.x/v.mp4", SourceLayer::Html);
        candidate.codecs = Some("avc1.64001F".into());
        assert_eq!(
            resolve(&base(), &candidate).unwrap().codecs.as_deref(),
            Some("avc1.64001F")
        );
    }
}
