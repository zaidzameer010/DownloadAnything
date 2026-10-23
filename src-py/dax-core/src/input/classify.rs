use url::Url;

/// Services whose media is always DRM'd — fail fast instead of scraping.
pub const DRM_DOMAINS: &[&str] = &[
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ContentKind {
    Page,
    ManifestHls,
    ManifestDash,
    DirectMedia,
    Json,
}

/// Host equals `domain` or is a subdomain of it — no allocation.
/// (`url` normalizes hosts to lowercase; callers pass lowercase domains.)
pub fn host_matches(host: &str, domain: &str) -> bool {
    host == domain
        || host
            .strip_suffix(domain)
            .is_some_and(|rest| rest.ends_with('.'))
}

/// Returns the matched blocklist domain if `host` is a known-DRM service.
pub fn drm_host<'a>(host: &str, blocklist: &[&'a str]) -> Option<&'a str> {
    let host = host.to_lowercase();
    blocklist.iter().copied().find(|d| host_matches(&host, d))
}

/// Decide what a response body is, from Content-Type, URL extension, and magic bytes.
pub fn classify(content_type: Option<&str>, url: &Url, body: &[u8]) -> ContentKind {
    if let Some(ct) = content_type {
        let ct = ct.to_lowercase();
        if ct.contains("mpegurl") || ct.contains("vnd.apple") || ct.contains("x-mpegurl") {
            return ContentKind::ManifestHls;
        }
        if ct.contains("dash+xml") {
            return ContentKind::ManifestDash;
        }
        if ct.contains("html") {
            return ContentKind::Page;
        }
        if ct.starts_with("video/") || ct.starts_with("audio/") {
            return ContentKind::DirectMedia;
        }
        if ct.contains("json") {
            return ContentKind::Json;
        }
    }
    // Magic sniffing for lying/missing content types.
    let head = &body[..body.len().min(512)];
    if head.starts_with(b"#EXTM3U") {
        return ContentKind::ManifestHls;
    }
    let head_str = String::from_utf8_lossy(head)
        .trim_start_matches('\u{feff}')
        .to_lowercase();
    // Reliable prefix signal first: a JSON body may quote "<html" or "<mpd".
    if head_str.trim_start().starts_with('{') || head_str.trim_start().starts_with('[') {
        return ContentKind::Json;
    }
    if head_str.contains("<!doctype") || head_str.contains("<html") {
        return ContentKind::Page;
    }
    if head_str.contains("<mpd") {
        return ContentKind::ManifestDash;
    }
    if let Some(ext) = super::path_ext(url) {
        if super::HLS_EXTS.contains(&ext.as_str()) {
            return ContentKind::ManifestHls;
        }
        if super::DASH_EXTS.contains(&ext.as_str()) {
            return ContentKind::ManifestDash;
        }
        if super::VIDEO_EXTS.contains(&ext.as_str()) || super::AUDIO_EXTS.contains(&ext.as_str()) {
            return ContentKind::DirectMedia;
        }
    }
    ContentKind::Page
}
