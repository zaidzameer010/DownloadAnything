//! Last-resort regex scan over raw source.

use std::borrow::Cow;
use std::sync::LazyLock;

use regex::Regex;
use url::Url;

use crate::extract::candidates::{self, Candidate};
use crate::extract::unescape::unescape_js;
use crate::model::SourceLayer;

/// URLs pointing at media files or manifests in raw (unescaped) source.
///
/// After the extension, an optional `/` or `?` plus the rest is kept — KVS
/// players append required access tokens as `file.mp4/?v-acctoken=...`.
/// All patterns fold into one alternation: a single automaton pass over the
/// body instead of one pass per pattern.
static RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(concat!(
        r#"(?i)https?://[^\s"'<>\\)]+?\.(?:mp4|webm|mkv|mov|m4v|ts|mp3|m4a|aac|opus|flac|wav|m3u8|mpd)(?:[/?][^\s"'<>\\)]*)?"#,
        r#"|https?://[^\s"'<>\\)]*(?:format=m3u8|format=mpd|type=m3u8|type=mpd|/manifest\.mpd|/master\.m3u8|/playlist\.m3u8|/hls/|videoplayback)[^\s"'<>\\)]*"#,
        // Percent-encoded URLs embedded in query strings / JS.
        r#"|https?%3A%2F%2F[^\s"'<>\\)]*(?:\.m3u8|\.mpd|\.mp4|\.webm|\.mp3|format=m3u8)[^\s"'<>\\)]*"#,
    ))
    .expect("media url regex")
});

/// Extensionless signed stream URLs named by a strong player-config key
/// (`video_url: "https://cdn/api/stream/abc"`) — the raw-JS gap the JSON blob
/// layer cannot reach when no anchor/variable name is known.
static STRONG_KEY_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(concat!(
        r#"(?i)(?:video_?url|stream_?url|playback_?url|hls_?url|dash_?url"#,
        r#"|manifest_?url|file_?url|media_?url|download_?url)"#,
        r#"\s*["']?\s*[:=]\s*["'](https?://[^"'\s]+)["']"#,
    ))
    .expect("strong key regex")
});

/// Merge adjacent string-literal concatenations (`"a" + "b"` → `"ab"`) so URLs
/// split across JS string parts still match.
///
/// Pairwise passes are logarithmic on chains; 8 passes cover 256-part chains.
/// Borrows when no concatenations exist (the common case).
fn merge_concat(text: &str) -> Cow<'_, str> {
    static JOIN_DOUBLE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r#""((?:[^"\\]|\\.)*)"\s*\+\s*"((?:[^"\\]|\\.)*)""#)
            .expect("double-quoted concat regex")
    });
    static JOIN_SINGLE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r#"'((?:[^'\\]|\\.)*)'\s*\+\s*'((?:[^'\\]|\\.)*)'"#)
            .expect("single-quoted concat regex")
    });
    // No '+' → no literal concatenation possible; borrow the input.
    if !text.contains('+') {
        return Cow::Borrowed(text);
    }
    let mut current = text.to_owned();
    for _ in 0..8 {
        let next = JOIN_SINGLE
            .replace_all(&JOIN_DOUBLE.replace_all(&current, "\"$1$2\""), "'$1$2'")
            .into_owned();
        if next == current {
            break;
        }
        current = next;
    }
    Cow::Owned(current)
}

/// Case-insensitive `url` presence check for the strong-key gate.
///
/// The gate must match `URL`, `Url` and `url` alike: the regex below is
/// case-insensitive, so a case-sensitive gate silently skipped uppercase
/// player configs.
fn has_url_key(text: &str) -> bool {
    text.as_bytes()
        .windows(3)
        .any(|window| window.eq_ignore_ascii_case(b"url"))
}

/// Unescape JS/HTML escapes, then regex for media URLs.
pub(crate) fn scan_text(body: &str) -> Vec<Candidate> {
    // Every pattern below requires one of the media tokens, so a token-free
    // body cannot match. The SIMD automaton rejects it before the full-body
    // unescape/concat copies. Exactness: `\uXXXX`/`\xNN` escapes and `+`
    // string concatenation can assemble a token from pieces, so those bodies
    // still take the full scan. Strong config keys (`video_url`, `streamUrl`)
    // name extensionless streams and are scanned too.
    let strong = has_url_key(body);
    if !candidates::contains_media_token(body)
        && !body.contains('\\')
        && !body.contains('+')
        && !strong
    {
        return Vec::new();
    }
    let unescaped_js = unescape_js(body);
    let unescaped = merge_concat(&unescaped_js);
    let mut out = Vec::new();
    for matched in RE.find_iter(&unescaped) {
        // Trim common trailing punctuation glued by the match.
        let candidate = matched.as_str().trim_end_matches(['.', ',', ';', '!', '?']);
        if candidate.starts_with("http%") || candidate.starts_with("https%") {
            if let Ok(decoded) = urlencoding::decode(candidate) {
                push_if_media(&decoded, &mut out);
            }
        } else {
            push_if_media(candidate, &mut out);
        }
    }
    if strong {
        for captures in STRONG_KEY_RE.captures_iter(&unescaped) {
            push_strong(&captures[1], &mut out);
        }
    }
    out
}

/// Extensionless strong-key URL: accepted when it has no extension or a media
/// extension; page/archive/API asset extensions are rejected.
fn push_strong(raw: &str, out: &mut Vec<Candidate>) {
    let raw = raw.trim_end_matches(['.', ',', ';', '!', '?']);
    let Ok(url) = Url::parse(raw) else {
        return;
    };
    if !matches!(url.scheme(), "http" | "https") {
        return;
    }
    if let Some(extension) = crate::input::path_ext(&url) {
        let extension = extension.as_str();
        let media = crate::input::VIDEO_EXTS.contains(&extension)
            || crate::input::AUDIO_EXTS.contains(&extension)
            || crate::input::HLS_EXTS.contains(&extension)
            || crate::input::DASH_EXTS.contains(&extension);
        if !media {
            return;
        }
    }
    if out.iter().any(|candidate| candidate.raw == raw) {
        return;
    }
    out.push(Candidate::new(raw, SourceLayer::Regex));
}

/// Keep a regex match only when it parses as a media-looking URL.
fn push_if_media(raw: &str, out: &mut Vec<Candidate>) {
    // Cheap early reject only — parseable non-media URLs drop here; a match
    // that fails `Url::parse` still passes through and `resolve` drops it.
    if Url::parse(raw).is_ok_and(|url| !candidates::looks_media_url(&url)) {
        return;
    }
    out.push(Candidate::new(raw, SourceLayer::Regex));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn skips_token_free_bodies() {
        // Every regex alternative requires a media token, so the SIMD token
        // gate is exact: a token-free body can never produce a candidate.
        assert!(scan_text("<html><body>no media here</body></html>").is_empty());
        assert!(!scan_text("https://cdn.x/v.mp4").is_empty());
        // m4a is a regex extension — it must be in the token set too.
        assert!(!scan_text("https://cdn.x/v.m4a").is_empty());
        // Tokens assembled at scan time must not be skipped: JS concatenation
        // and \uXXXX escapes both produce a URL with no literal token in the body.
        assert!(!scan_text(r#""https://cdn.x/v." + "mp4""#).is_empty());
        assert!(!scan_text(r#""https://cdn.x/v\u002Emp4""#).is_empty());
    }

    #[test]
    fn matches_case_insensitively() {
        // The last-resort scan must agree with the case-insensitive discovery
        // and ranking layers — uppercase extensions/query tokens are common in
        // signed URLs.
        assert!(!scan_text("url=https://cdn.x/v.MP4").is_empty());
        assert!(!scan_text("url=https://cdn.x/v?FORMAT=M3U8").is_empty());
        assert!(!scan_text("url=https://cdn.x/v?TYPE=MPD").is_empty());
    }

    #[test]
    fn finds_extensionless_strong_key_streams() {
        let body =
            r#"window.cfg = { video_url: "https://cdn.x/api/stream/abc123", poster: "/p.jpg" };"#;
        let candidates = scan_text(body);
        assert!(
            candidates
                .iter()
                .any(|candidate| candidate.raw == "https://cdn.x/api/stream/abc123"),
            "{candidates:?}"
        );
        // A page URL behind a strong key is not media.
        assert!(scan_text(r#"video_url: "https://site.com/watch.html""#).is_empty());
    }

    #[test]
    fn uppercase_strong_keys_are_not_skipped() {
        // The gate used to be case-sensitive while the regex was not, so
        // `VIDEO_URL:` configs never reached the strong-key scan.
        let body = r#"window.cfg = { VIDEO_URL: "https://cdn.x/api/stream/abc123" };"#;
        assert!(
            scan_text(body)
                .iter()
                .any(|candidate| candidate.raw == "https://cdn.x/api/stream/abc123")
        );
    }
}
