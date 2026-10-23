use crate::config::Config;
use crate::error::ExtractError;
use crate::extract::extract_response;
use crate::input::{self, classify};
use crate::model::{MediaItem, MediaKind, MediaLink, SourceLayer};
use crate::net::NetEngine;
use tracing::{info, instrument};
use url::Url;

/// One URL → (final url, items).
#[instrument(skip_all, fields(url = %raw))]
pub async fn extract_url(
    cfg: &Config,
    net: &NetEngine,
    raw: &str,
) -> Result<(Url, Vec<MediaItem>), ExtractError> {
    let url = input::normalize(raw)?;
    let host = url.host_str().unwrap_or_default();
    if let Some(d) = classify::drm_host(host, cfg.drm_blocklist) {
        return Err(ExtractError::DrmProtected(d.to_string()));
    }
    if input::is_direct_media_url(&url) {
        let mut link = MediaLink::new(url.clone(), infer_direct_kind(&url), SourceLayer::Direct);
        link.container = input::path_ext(&url);
        let item = single_item(&url, link);
        return Ok((url, vec![item]));
    }
    let resp = net.fetch(&url).await?;
    info!(final_url = %resp.url, "extracting");
    let items = extract_response(net, &resp.url, &resp).await?;
    Ok((resp.url, items))
}

fn infer_direct_kind(u: &Url) -> MediaKind {
    match input::path_ext(u).as_deref() {
        Some(e) if input::AUDIO_EXTS.contains(&e) => MediaKind::Audio,
        _ => MediaKind::Progressive,
    }
}

fn single_item(page_url: &Url, link: MediaLink) -> MediaItem {
    MediaItem {
        id: "direct".into(),
        title: None,
        page_url: page_url.clone(),
        duration_secs: None,
        thumbnail: None,
        formats: vec![link],
    }
}
