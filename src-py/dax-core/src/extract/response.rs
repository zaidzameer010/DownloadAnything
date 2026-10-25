//! Response classification and dispatch to the matching extraction layer.

use tracing::{debug, instrument};
use url::Url;

use crate::error::ExtractError;
use crate::extract::json_blobs;
use crate::extract::page;
use crate::input::classify::{self, ContentKind};
use crate::model::{MediaItem, MediaKind, MediaLink, SourceLayer};
use crate::net::{FetchResponse, Fetcher};

/// Extract media items from one fetched response.
///
/// The content kind decides the path: direct media and manifests become a
/// single link, JSON is walked as a player config, and an HTML page runs the
/// full discovery pipeline.
///
/// # Errors
///
/// Returns [`ExtractError::NoMediaFound`] when a JSON document yields nothing,
/// [`ExtractError::Parse`] when a JSON body is malformed, and page-layer
/// failures for HTML.
#[instrument(skip_all, fields(url = %page_url))]
pub(crate) async fn extract_response(
    net: &Fetcher,
    page_url: &Url,
    response: &FetchResponse,
) -> Result<Vec<MediaItem>, ExtractError> {
    let kind = classify::classify(response.content_type(), page_url, &response.body);
    debug!(?kind, "classified response");
    match kind {
        ContentKind::DirectMedia => {
            let mut link = MediaLink::new(
                response.url.clone(),
                MediaKind::Progressive,
                SourceLayer::Direct,
            );
            link.container = crate::input::path_ext(&response.url);
            Ok(vec![MediaItem::page(page_url.clone(), vec![link])])
        }
        ContentKind::DirectAudio => {
            let mut link =
                MediaLink::new(response.url.clone(), MediaKind::Audio, SourceLayer::Direct);
            link.container = crate::input::path_ext(&response.url);
            Ok(vec![MediaItem::page(page_url.clone(), vec![link])])
        }
        ContentKind::ManifestHls => {
            let link = MediaLink::new(response.url.clone(), MediaKind::Hls, SourceLayer::Direct);
            Ok(vec![MediaItem::page(page_url.clone(), vec![link])])
        }
        ContentKind::ManifestDash => {
            let link = MediaLink::new(response.url.clone(), MediaKind::Dash, SourceLayer::Direct);
            Ok(vec![MediaItem::page(page_url.clone(), vec![link])])
        }
        ContentKind::Json => {
            let value: serde_json::Value =
                serde_json::from_slice(&response.body).map_err(|error| ExtractError::Parse {
                    layer: "json",
                    detail: error.to_string(),
                })?;
            let findings = json_blobs::walk_value(&value);
            let links = page::resolve_candidates(page_url, findings.candidates);
            if links.is_empty() {
                return Err(ExtractError::NoMediaFound);
            }
            let mut item = MediaItem::page(page_url.clone(), links);
            item.duration_secs = findings.duration_secs;
            item.title = findings.title;
            item.thumbnail = findings
                .thumbnail
                .as_ref()
                .and_then(|thumbnail| page_url.join(thumbnail).ok());
            Ok(vec![item])
        }
        ContentKind::Page => page::extract_page(net, page_url, response).await,
    }
}
