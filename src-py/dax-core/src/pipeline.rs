//! One URL from input string to media items.

use tracing::{info, instrument};
use url::Url;

use crate::error::ExtractError;
use crate::extract::extract_response;
use crate::input::{self, classify};
use crate::model::{MediaItem, MediaKind, MediaLink, SourceLayer};
use crate::net::Fetcher;

/// Normalize one input, reject DRM services, then either emit the direct
/// media URL or fetch and scrape the page.
///
/// # Errors
///
/// Returns the normalized URL and items on success; every failure class in
/// [`ExtractError`] otherwise.
#[instrument(skip_all, fields(url = %raw))]
pub(crate) async fn extract_url(
    net: &Fetcher,
    raw: &str,
) -> Result<(Url, Vec<MediaItem>), ExtractError> {
    let url = input::normalize(raw)?;
    let host = url.host_str().unwrap_or_default();
    if let Some(domain) = classify::drm_host(host) {
        return Err(ExtractError::DrmProtected(domain.to_owned()));
    }
    if input::is_direct_media_url(&url) {
        let mut link = MediaLink::new(url.clone(), direct_kind(&url), SourceLayer::Direct);
        link.container = input::path_ext(&url);
        let item = MediaItem::direct(url.clone(), link);
        return Ok((url, vec![item]));
    }
    let response = net.fetch(&url).await?;
    info!(final_url = %response.url, "extracting");
    let items = extract_response(net, &response.url, &response).await?;
    Ok((response.url, items))
}

/// Kind of a URL that is itself a media file.
fn direct_kind(url: &Url) -> MediaKind {
    match input::path_ext(url).as_deref() {
        Some(extension) if input::AUDIO_EXTS.contains(&extension) => MediaKind::Audio,
        _ => MediaKind::Progressive,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn direct_kind_uses_the_url_extension() {
        let audio = Url::parse("https://cdn.x/song.mp3").unwrap();
        assert_eq!(direct_kind(&audio), MediaKind::Audio);
        let video = Url::parse("https://cdn.x/clip.mp4").unwrap();
        assert_eq!(direct_kind(&video), MediaKind::Progressive);
        let extensionless = Url::parse("https://cdn.x/stream").unwrap();
        assert_eq!(direct_kind(&extensionless), MediaKind::Progressive);
    }
}
