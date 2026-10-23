pub mod classify;

use crate::error::ExtractError;
use url::Url;

/// Extensions treated as directly playable media. Keep in sync with
/// dax/filter.py::VIDEO_EXTS — discovery and ranking must agree.
pub const VIDEO_EXTS: &[&str] = &[
    "mp4", "webm", "mkv", "mov", "m4v", "avi", "ts", "m2ts", "3gp", "flv", "mpg", "mpeg", "wmv",
];
pub const AUDIO_EXTS: &[&str] = &[
    "mp3", "m4a", "aac", "ogg", "oga", "opus", "flac", "wav", "wma", "aiff",
];
pub const HLS_EXTS: &[&str] = &["m3u8", "m3u"];
pub const DASH_EXTS: &[&str] = &["mpd"];

/// Parse and normalize a user-supplied URL. Bare hosts get https:// prepended;
/// anything not http(s) is rejected.
pub fn normalize(raw: &str) -> Result<Url, ExtractError> {
    let t = raw.trim();
    if t.is_empty() {
        return Err(ExtractError::InvalidUrl(raw.into()));
    }
    let mut u = Url::parse(t)
        .or_else(|_| Url::parse(&format!("https://{t}")))
        .map_err(|_| ExtractError::InvalidUrl(raw.into()))?;
    if !matches!(u.scheme(), "http" | "https") {
        // "localhost:8080" parses with scheme "localhost" — a fake scheme
        // followed by a digit is really a bare host:port. Real schemes
        // (data:, mailto:) never start their body with one.
        let bare_host = t[u.scheme().len() + 1..].starts_with(|c: char| c.is_ascii_digit());
        if bare_host {
            u = Url::parse(&format!("https://{t}"))
                .map_err(|_| ExtractError::InvalidUrl(raw.into()))?;
        } else {
            return Err(ExtractError::UnsupportedScheme(u.scheme().to_string()));
        }
    }
    u.set_fragment(None);
    Ok(u)
}

/// Lowercased file extension of the URL path, if any. Empty trailing
/// segments (KVS-style `.../file.mp4/` URLs) are skipped.
pub fn path_ext(u: &Url) -> Option<String> {
    u.path_segments()?
        .rev()
        .find(|seg| !seg.is_empty())
        .and_then(|seg| seg.rsplit_once('.'))
        .map(|(_, e)| e.to_lowercase())
        .filter(|e| !e.is_empty())
}

pub fn is_direct_media_url(u: &Url) -> bool {
    path_ext(u)
        .is_some_and(|e| VIDEO_EXTS.contains(&e.as_str()) || AUDIO_EXTS.contains(&e.as_str()))
}

/// URLs that name a manifest by extension or manifest-ish query/path tokens.
pub fn looks_manifest_url(u: &Url) -> bool {
    if let Some(e) = path_ext(u)
        && (HLS_EXTS.contains(&e.as_str()) || DASH_EXTS.contains(&e.as_str()))
    {
        return true;
    }
    let q = u.query().unwrap_or("");
    q.contains("format=m3u8")
        || q.contains("format=mpd")
        || q.contains("type=m3u8")
        || q.contains("type=mpd")
        // Anchored like the Python ranker's _MULTI_QUERY — `multi=` must
        // open the query or a `&` segment; `xmulti=` is not a manifest.
        || q.split('&')
            .any(|p| p.len() >= 6 && p[..6].eq_ignore_ascii_case("multi="))
        || u.path().contains("/manifest")
        || u.path().contains("/master.")
        || u.path().contains("/playlist.")
        // Signed extensionless endpoints — `/media/hls/?s=…`.
        || u.path().contains("/hls/")
}
