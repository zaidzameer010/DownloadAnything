//! URL normalization and the extension vocabularies shared by all layers.

pub(crate) mod classify;

use crate::error::ExtractError;
use url::Url;

/// Extensions treated as directly playable video.
///
/// Keep in sync with `dax/filter.py::VIDEO_EXTS` — discovery and ranking must
/// agree or links found here never rank there.
pub(crate) const VIDEO_EXTS: &[&str] = &["mp4", "webm", "mkv", "mov", "m4v", "ts"];

/// Extensions treated as directly playable audio.
pub(crate) const AUDIO_EXTS: &[&str] = &["mp3", "m4a", "aac", "opus", "flac", "wav"];

/// Containers deliberately retired from discovery and ranking.
pub(crate) const RETIRED_EXTS: &[&str] = &[
    "ogg", "oga", "avi", "3gp", "wmv", "flv", "mpg", "mpeg", "wma", "aiff", "m2ts", "alac",
];

/// Extensions that name an HLS manifest.
pub(crate) const HLS_EXTS: &[&str] = &["m3u8", "m3u"];

/// Extensions that name a DASH manifest.
pub(crate) const DASH_EXTS: &[&str] = &["mpd"];

/// Parse and normalize a user-supplied URL.
///
/// Bare hosts get `https://` prepended; anything not http(s) is rejected. The
/// fragment is dropped because it never reaches the server.
///
/// # Errors
///
/// Returns [`ExtractError::InvalidUrl`] for an unparseable or empty input and
/// [`ExtractError::UnsupportedScheme`] for a real non-http scheme.
pub(crate) fn normalize(raw: &str) -> Result<Url, ExtractError> {
    let text = raw.trim();
    if text.is_empty() {
        return Err(ExtractError::InvalidUrl(raw.into()));
    }
    let mut url = Url::parse(text)
        .or_else(|_| Url::parse(&format!("https://{text}")))
        .map_err(|_| ExtractError::InvalidUrl(raw.into()))?;
    if !matches!(url.scheme(), "http" | "https") {
        // "localhost:8080" parses with scheme "localhost" — a fake scheme
        // followed by a digit is really a bare host:port. Real schemes
        // (data:, mailto:) never start their body with one.
        let bare_host = text
            .get(url.scheme().len() + 1..)
            .is_some_and(|rest| rest.starts_with(|c: char| c.is_ascii_digit()));
        if bare_host {
            url = Url::parse(&format!("https://{text}"))
                .map_err(|_| ExtractError::InvalidUrl(raw.into()))?;
        } else {
            return Err(ExtractError::UnsupportedScheme(url.scheme().to_owned()));
        }
    }
    url.set_fragment(None);
    Ok(url)
}

/// Lowercased file extension of the URL path, if any.
///
/// Empty trailing segments (KVS-style `.../file.mp4/` URLs) are skipped.
#[must_use]
pub(crate) fn path_ext(url: &Url) -> Option<String> {
    url.path_segments()?
        .rev()
        .find(|segment| !segment.is_empty())
        .and_then(|segment| segment.rsplit_once('.'))
        .map(|(_, extension)| extension.to_ascii_lowercase())
        .filter(|extension| !extension.is_empty())
}

/// Whether the URL names a retired container we no longer support.
#[must_use]
pub(crate) fn is_retired_ext(url: &Url) -> bool {
    path_ext(url).is_some_and(|extension| RETIRED_EXTS.contains(&extension.as_str()))
}

/// Whether the URL names a directly playable media file.
#[must_use]
pub(crate) fn is_direct_media_url(url: &Url) -> bool {
    path_ext(url).is_some_and(|extension| {
        VIDEO_EXTS.contains(&extension.as_str()) || AUDIO_EXTS.contains(&extension.as_str())
    })
}

/// Whether the URL names a manifest by extension or manifest-ish tokens.
#[must_use]
pub(crate) fn looks_manifest_url(url: &Url) -> bool {
    if let Some(extension) = path_ext(url) {
        if HLS_EXTS.contains(&extension.as_str()) || DASH_EXTS.contains(&extension.as_str()) {
            return true;
        }
        // A real media extension is a download, never a manifest —
        // `/master.mp4` and `/playlist.webm` are progressive files.
        if VIDEO_EXTS.contains(&extension.as_str()) || AUDIO_EXTS.contains(&extension.as_str()) {
            return false;
        }
    }
    // Case-insensitive like dax/filter.py::role — a signed `?FORMAT=M3U8`
    // or `/MANIFEST` path is the same manifest the Python ranker sees.
    let query = url.query().unwrap_or("").to_ascii_lowercase();
    let path = url.path().to_ascii_lowercase();
    query.contains("format=m3u8")
        || query.contains("format=mpd")
        || query.contains("type=m3u8")
        || query.contains("type=mpd")
        // Anchored like the Python ranker's _MULTI_QUERY — `multi=` must
        // open the query or a `&` segment; `xmulti=` is not a manifest.
        || query.split('&').any(|part| part.starts_with("multi="))
        || path.contains("/manifest")
        || path.contains("/master.")
        || path.contains("/playlist.")
        // Signed extensionless endpoints — `/media/hls/?s=…`.
        || path.contains("/hls/")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_bare_host_port_and_scheme_rules() {
        // "localhost:8080" parses with fake scheme "localhost" — a digit after
        // the colon means it was really a bare host:port.
        let url = normalize("localhost:8080/v.m3u8").unwrap();
        assert_eq!(url.scheme(), "https");
        assert_eq!(url.host_str(), Some("localhost"));
        assert_eq!(url.port(), Some(8080));

        let url = normalize("example.com/watch#frag").unwrap();
        assert_eq!(url.as_str(), "https://example.com/watch", "fragments drop");

        // Real schemes keep their error class.
        assert!(matches!(
            normalize("ftp://x/v"),
            Err(ExtractError::UnsupportedScheme(_))
        ));
        assert!(matches!(
            normalize("data:text/html,<b>x</b>"),
            Err(ExtractError::UnsupportedScheme(_))
        ));
        assert!(matches!(
            normalize("mailto:a@b.c"),
            Err(ExtractError::UnsupportedScheme(_))
        ));
        assert!(matches!(normalize("   "), Err(ExtractError::InvalidUrl(_))));

        assert_eq!(normalize("http://a/b").unwrap().scheme(), "http");
    }

    #[test]
    fn path_ext_skips_trailing_slash_and_lowercases() {
        let url = Url::parse("https://x/dl/FILE.MP4/").unwrap();
        assert_eq!(path_ext(&url).as_deref(), Some("mp4"));
        assert_eq!(path_ext(&Url::parse("https://x/watch").unwrap()), None);
        assert_eq!(path_ext(&Url::parse("https://x/a.").unwrap()), None);
    }

    #[test]
    fn manifest_predicate_ignores_media_file_extensions() {
        // `/master.mp4` and `/playlist.webm` are downloads, not manifests.
        assert!(!looks_manifest_url(
            &Url::parse("https://x/videos/master.mp4").unwrap()
        ));
        assert!(!looks_manifest_url(
            &Url::parse("https://x/playlist.webm").unwrap()
        ));
        assert!(looks_manifest_url(
            &Url::parse("https://x/videos/master.m3u8").unwrap()
        ));
    }

    #[test]
    fn manifest_tokens_match_python_case_insensitively() {
        // dax/filter.py::role lowercases path+query before matching, so a
        // signed `?FORMAT=M3U8` or `/MANIFEST` path must classify as a master
        // here too — otherwise discovery and ranking fork on case alone.
        assert!(looks_manifest_url(
            &Url::parse("https://x/v?FORMAT=M3U8&token=a").unwrap()
        ));
        assert!(looks_manifest_url(
            &Url::parse("https://x/v?TYPE=MPD").unwrap()
        ));
        assert!(looks_manifest_url(
            &Url::parse("https://x/MANIFEST/v1?token=a").unwrap()
        ));
        assert!(looks_manifest_url(
            &Url::parse("https://x/MEDIA/HLS/?s=abc").unwrap()
        ));
        // Anchoring still holds case-insensitively: `XMULTI=` is not `multi=`.
        assert!(!looks_manifest_url(
            &Url::parse("https://x/v?XMULTI=360p,720p").unwrap()
        ));
        assert!(looks_manifest_url(
            &Url::parse("https://x/v?t=1&MULTI=360p,720p").unwrap()
        ));
    }

    #[test]
    fn non_ascii_query_does_not_panic() {
        // `part[..6]` used to panic when a query segment was shorter than the
        // multi= token in bytes; the anchored check must stay total.
        assert!(!looks_manifest_url(
            &Url::parse("https://x/v?q=%C3%A9").unwrap()
        ));
    }

    #[test]
    fn every_video_and_audio_extension_is_direct_media() {
        for extension in VIDEO_EXTS.iter().chain(AUDIO_EXTS) {
            let url = Url::parse(&format!("https://cdn.x/v.{extension}")).unwrap();
            assert!(is_direct_media_url(&url), "{extension}");
        }
    }
}
