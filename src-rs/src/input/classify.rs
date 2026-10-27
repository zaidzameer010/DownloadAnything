//! Response classification and the DRM host blocklist.

use std::borrow::Cow;

use url::Url;

/// Services whose media is always DRM-protected — fail fast instead of scraping.
pub(crate) const DRM_DOMAINS: &[&str] = &[
    "netflix.com",
    "disneyplus.com",
    "max.com",
    "hbomax.com",
    "primevideo.com",
    "amazon.com",
    "hulu.com",
    "spotify.com",
    "tv.apple.com",
    "paramountplus.com",
    "peacocktv.com",
    "crunchyroll.com",
    "showtime.com",
    "dazn.com",
    "discoveryplus.com",
    "tidal.com",
    "deezer.com",
    "music.apple.com",
    "audible.com",
];

/// What a fetched response body is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ContentKind {
    /// An HTML page to scrape.
    Page,
    /// An HLS manifest.
    ManifestHls,
    /// A DASH manifest.
    ManifestDash,
    /// A directly playable video file.
    DirectMedia,
    /// A directly playable audio file.
    DirectAudio,
    /// A JSON document carrying player configuration.
    Json,
}

/// Whether `host` equals `domain` or is a subdomain of it.
///
/// `url` normalizes hosts to lowercase; callers pass lowercase domains.
#[must_use]
pub(crate) fn host_matches(host: &str, domain: &str) -> bool {
    host == domain
        || host
            .strip_suffix(domain)
            .is_some_and(|rest| rest.ends_with('.'))
}

/// Returns the matched blocklist domain if `host` is a known DRM service.
#[must_use]
pub(crate) fn drm_host(host: &str) -> Option<&'static str> {
    let host = if host.bytes().any(|byte| byte.is_ascii_uppercase()) {
        Cow::Owned(host.to_ascii_lowercase())
    } else {
        Cow::Borrowed(host)
    };
    DRM_DOMAINS
        .iter()
        .copied()
        .find(|domain| host_matches(&host, domain))
}

/// Decide what a response body is from Content-Type, URL extension and magic bytes.
#[must_use]
pub(crate) fn classify(content_type: Option<&str>, url: &Url, body: &[u8]) -> ContentKind {
    // A retired container is never a supported media response, even when a
    // server labels it video/* or audio/*.
    if super::is_retired_ext(url) {
        return ContentKind::Page;
    }
    if let Some(content_type) = content_type {
        let content_type = content_type.to_ascii_lowercase();
        if content_type.contains("mpegurl")
            || content_type.contains("vnd.apple")
            || content_type.contains("x-mpegurl")
        {
            return ContentKind::ManifestHls;
        }
        if content_type.contains("dash+xml") {
            return ContentKind::ManifestDash;
        }
        if content_type.contains("html") {
            return ContentKind::Page;
        }
        if content_type.starts_with("video/") {
            return ContentKind::DirectMedia;
        }
        if content_type.starts_with("audio/") {
            return ContentKind::DirectAudio;
        }
        if content_type.contains("json") {
            return ContentKind::Json;
        }
    }

    // Magic sniffing for lying or missing content types.
    let head = &body[..body.len().min(512)];
    if head.starts_with(b"#EXTM3U") {
        return ContentKind::ManifestHls;
    }
    let head_text = String::from_utf8_lossy(head);
    let head_text = head_text.trim_start_matches('\u{feff}').trim_start();
    // Reliable prefix signal first: a JSON body may quote "<html" or "<mpd".
    if head_text.starts_with('{') || head_text.starts_with('[') {
        return ContentKind::Json;
    }
    let head_lower = head_text.to_ascii_lowercase();
    if head_lower.contains("<!doctype") || head_lower.contains("<html") {
        return ContentKind::Page;
    }
    if head_lower.contains("<mpd") {
        return ContentKind::ManifestDash;
    }

    if let Some(extension) = super::path_ext(url) {
        if super::HLS_EXTS.contains(&extension.as_str()) {
            return ContentKind::ManifestHls;
        }
        if super::DASH_EXTS.contains(&extension.as_str()) {
            return ContentKind::ManifestDash;
        }
        if super::VIDEO_EXTS.contains(&extension.as_str()) {
            return ContentKind::DirectMedia;
        }
        if super::AUDIO_EXTS.contains(&extension.as_str()) {
            return ContentKind::DirectAudio;
        }
    }
    ContentKind::Page
}

#[cfg(test)]
mod tests {
    use super::*;

    fn url(raw: &str) -> Url {
        Url::parse(raw).unwrap()
    }

    #[test]
    fn content_type_wins_over_body() {
        assert_eq!(
            classify(Some("text/html; charset=utf-8"), &url("https://x/"), b"{}"),
            ContentKind::Page
        );
        assert_eq!(
            classify(Some("application/json"), &url("https://x/"), b"<html>"),
            ContentKind::Json
        );
        assert_eq!(
            classify(
                Some("application/vnd.apple.mpegurl"),
                &url("https://x/"),
                b""
            ),
            ContentKind::ManifestHls
        );
        assert_eq!(
            classify(Some("application/dash+xml"), &url("https://x/"), b""),
            ContentKind::ManifestDash
        );
        assert_eq!(
            classify(Some("audio/mpeg"), &url("https://x/"), b""),
            ContentKind::DirectAudio
        );
    }

    #[test]
    fn magic_bytes_recover_lying_content_types() {
        assert_eq!(
            classify(Some("text/plain"), &url("https://x/"), b"#EXTM3U\n"),
            ContentKind::ManifestHls
        );
        assert_eq!(
            classify(Some("text/plain"), &url("https://x/"), b"  {\"a\":1}"),
            ContentKind::Json
        );
        assert_eq!(
            classify(Some("text/plain"), &url("https://x/"), b"<!DOCTYPE html>"),
            ContentKind::Page
        );
        assert_eq!(
            classify(Some("text/plain"), &url("https://x/"), b"<MPD xmlns=\"x\">"),
            ContentKind::ManifestDash
        );
    }

    #[test]
    fn extension_is_the_last_resort() {
        assert_eq!(
            classify(None, &url("https://x/v.m3u8"), b""),
            ContentKind::ManifestHls
        );
        assert_eq!(
            classify(None, &url("https://x/v.mpd"), b""),
            ContentKind::ManifestDash
        );
        assert_eq!(
            classify(None, &url("https://x/v.mp4"), b""),
            ContentKind::DirectMedia
        );
        assert_eq!(
            classify(None, &url("https://x/v.mp3"), b""),
            ContentKind::DirectAudio
        );
        assert_eq!(
            classify(None, &url("https://x/watch"), b""),
            ContentKind::Page
        );
    }

    #[test]
    fn drm_host_matches_subdomains_but_not_lookalikes() {
        assert_eq!(drm_host("www.netflix.com"), Some("netflix.com"));
        assert_eq!(drm_host("WWW.NETFLIX.COM"), Some("netflix.com"));
        assert_eq!(drm_host("notnetflix.com"), None);
        assert_eq!(drm_host("netflix.com.evil.test"), None);
        assert_eq!(drm_host("example.com"), None);
    }
}
