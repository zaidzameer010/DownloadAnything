use crate::extract::candidates::{self, Candidate};
use crate::extract::unescape::unescape_js;
use crate::model::SourceLayer;
use regex::Regex;
use std::borrow::Cow;
use std::sync::LazyLock;
use url::Url;

/// URLs pointing at media files or manifests in raw (unescaped) source.
/// After the extension, an optional `/` or `?` + rest is kept — KVS-style
/// players append required access tokens as `file.mp4/?v-acctoken=...`.
/// All patterns fold into one alternation — a single automaton pass over
/// the body instead of one pass per pattern.
static RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(concat!(
        r#"https?://[^\s"'<>\\)]+?\.(?:mp4|webm|mkv|mov|m4v|3gp|ts|m2ts|mp3|m4a|aac|ogg|oga|opus|flac|wav|wma|aiff|wmv|avi|flv|mpg|mpeg|m3u8|mpd)(?:[/?][^\s"'<>\\)]*)?"#,
        r#"|https?://[^\s"'<>\\)]*(?:format=m3u8|format=mpd|type=m3u8|/manifest\.mpd|/master\.m3u8|/playlist\.m3u8|/hls/|videoplayback)[^\s"'<>\\)]*"#,
        // percent-encoded URLs embedded in query strings / JS
        r#"|https?%3A%2F%2F[^\s"'<>\\)]*(?:\.m3u8|\.mpd|\.mp4|\.webm|\.mp3|format=m3u8)[^\s"'<>\\)]*"#,
    ))
    .unwrap()
});

/// Merge adjacent string-literal concatenations (`"a" + "b"` → `"ab"`) so
/// URLs split across JS string parts still match. Pairwise passes are
/// logarithmic on chains; 8 passes cover 256-part chains. Borrows when no
/// concatenations exist (the common case).
fn merge_concat(s: &str) -> Cow<'_, str> {
    static JOIN_DQ: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r#""((?:[^"\\]|\\.)*)"\s*\+\s*"((?:[^"\\]|\\.)*)""#).unwrap());
    static JOIN_SQ: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r#"'((?:[^'\\]|\\.)*)'\s*\+\s*'((?:[^'\\]|\\.)*)'"#).unwrap());
    // No '+' → no literal concatenation possible; borrow the input.
    if !s.contains('+') {
        return Cow::Borrowed(s);
    }
    let mut cur = s.to_string();
    for _ in 0..8 {
        let next = JOIN_SQ
            .replace_all(&JOIN_DQ.replace_all(&cur, "\"$1$2\""), "'$1$2'")
            .into_owned();
        if next == cur {
            break;
        }
        cur = next;
    }
    Cow::Owned(cur)
}

/// Last-resort scan: unescape JS/HTML escapes, then regex for media URLs.
pub fn scan_text(body: &str) -> Vec<Candidate> {
    // Every pattern below requires one of the media tokens, so a token-free
    // body cannot match. The SIMD automaton rejects it before the full-body
    // unescape/concat copies. Exactness: `\uXXXX`/`\xNN` escapes and `+`
    // string concatenation can assemble a token from pieces, so those bodies
    // still take the full scan.
    if !candidates::contains_media_token(body) && !body.contains('\\') && !body.contains('+') {
        return Vec::new();
    }
    let unescaped_js = unescape_js(body);
    let unescaped = merge_concat(&unescaped_js);
    let mut out = Vec::new();
    for m in RE.find_iter(&unescaped) {
        // trim common trailing punctuation glued by the match
        let s = m.as_str().trim_end_matches(['.', ',', ';', '!', '?']);
        if s.starts_with("http%") || s.starts_with("https%") {
            if let Ok(d) = urlencoding::decode(s) {
                push_if_media(&d, &mut out);
            }
        } else {
            push_if_media(s, &mut out);
        }
    }
    out
}

fn push_if_media(s: &str, out: &mut Vec<Candidate>) {
    // Cheap early reject only — parseable non-media URLs drop here; a match
    // that fails Url::parse still passes through and resolve() drops it.
    if Url::parse(s).is_ok_and(|u| !candidates::looks_media_url(&u)) {
        return;
    }
    out.push(Candidate::new(s, SourceLayer::Regex));
}
