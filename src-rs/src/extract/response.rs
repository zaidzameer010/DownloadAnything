//! Response classification and dispatch to the matching extraction layer.

use tracing::{debug, instrument};
use url::Url;

use crate::error::ExtractError;
use crate::extract::json_blobs;
use crate::extract::page;
use crate::input::classify::{self, ContentKind};
use crate::model::{MediaItem, MediaKind, MediaLink, PageInfo, SourceLayer};
use crate::net::{FetchResponse, Fetcher};

/// Extract media items and page metadata from one fetched response.
///
/// The content kind decides the path: direct media and manifests become a
/// single link, JSON is walked as a player config, and an HTML page runs the
/// full discovery pipeline.
///
/// # Errors
///
/// Returns [`ExtractError::Parse`] when a JSON or HTML body is malformed.
#[instrument(skip_all, fields(url = %page_url))]
pub(crate) async fn extract_response(
    net: &Fetcher,
    page_url: &Url,
    response: &FetchResponse,
) -> Result<(Vec<MediaItem>, PageInfo), ExtractError> {
    let kind = classify::classify(response.content_type(), page_url, &response.body);
    debug!(?kind, "classified response");
    match kind {
        ContentKind::DirectMedia => Ok((
            vec![direct_item(page_url, &response.url, MediaKind::Progressive)],
            PageInfo::default(),
        )),
        ContentKind::DirectAudio => Ok((
            vec![direct_item(page_url, &response.url, MediaKind::Audio)],
            PageInfo::default(),
        )),
        ContentKind::ManifestHls => Ok((
            vec![direct_item(page_url, &response.url, MediaKind::Hls)],
            PageInfo::default(),
        )),
        ContentKind::ManifestDash => Ok((
            vec![direct_item(page_url, &response.url, MediaKind::Dash)],
            PageInfo::default(),
        )),
        ContentKind::Json => {
            let value: serde_json::Value =
                serde_json::from_slice(&response.body).map_err(|error| ExtractError::Parse {
                    layer: "json",
                    detail: error.to_string(),
                })?;
            let findings = json_blobs::walk_value(&value);
            let thumbnail = findings
                .thumbnail
                .as_ref()
                .and_then(|thumbnail| page_url.join(thumbnail).ok());
            let site = page_url
                .host_str()
                .map(|host| host.trim_start_matches("www."));
            let title = findings
                .title
                .map(|title| crate::extract::meta::clean_title(&title, site))
                .filter(|title| !title.is_empty());
            let info = PageInfo {
                title: title.clone(),
                title_source: findings.title_source,
                thumbnail: thumbnail.clone(),
                duration_secs: findings.duration_secs,
            };
            let links = page::resolve_candidates(page_url, findings.candidates);
            if links.is_empty() {
                return Ok((Vec::new(), info));
            }
            let mut item = MediaItem::page(page_url.clone(), links);
            item.duration_secs = findings.duration_secs;
            item.title = title;
            item.thumbnail = thumbnail;
            Ok((vec![item], info))
        }
        ContentKind::Page => page::extract_page(net, page_url, response).await,
    }
}

/// The page item for a response that is itself the media file.
///
/// Only a file (not a manifest) names a container.
fn direct_item(page_url: &Url, url: &Url, kind: MediaKind) -> MediaItem {
    let mut link = MediaLink::new(url.clone(), kind, SourceLayer::Direct);
    if matches!(kind, MediaKind::Progressive | MediaKind::Audio) {
        link.container = crate::input::path_ext(url);
    }
    MediaItem::page(page_url.clone(), vec![link])
}
