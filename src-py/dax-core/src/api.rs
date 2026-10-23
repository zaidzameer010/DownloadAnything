use crate::config::Config;
use crate::error::ExtractError;
use crate::model::MediaItem;
use crate::net::NetEngine;
use crate::pipeline::extract_url;

use futures::stream::{self, StreamExt};
use serde::Serialize;
use tracing::instrument;
use url::Url;

/// Extraction result for one input URL — errors are carried, not thrown,
/// so a multi-URL UI call can render per-URL failures.
#[derive(Debug, Clone, Serialize)]
pub struct PageResult {
    pub input: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub final_url: Option<Url>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub extractor: Option<String>,
    pub items: Vec<MediaItem>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error_code: Option<i32>,
}

/// Extract URLs in parallel (bounded, order-preserving); per-URL failures
/// don't abort the batch.
pub async fn extract(cfg: &Config, net: &NetEngine, urls: Vec<String>) -> Vec<PageResult> {
    stream::iter(urls)
        .map(|raw| extract_one(cfg, net, raw))
        .buffered(4)
        .collect::<Vec<PageResult>>()
        .await
}

#[instrument(skip_all, fields(input = %raw))]
pub async fn extract_one(cfg: &Config, net: &NetEngine, raw: String) -> PageResult {
    let page = match extract_url(cfg, net, &raw).await {
        Ok((final_url, mut items)) => {
            // Junk filtering, ordering and truncation belong to the Python
            // filter (dax/filter.py) — this crate returns raw candidates.
            items.retain(|i| !i.formats.is_empty());
            let no_media = items.is_empty();
            PageResult {
                input: raw,
                final_url: Some(final_url),
                extractor: Some("rust".to_string()),
                items,
                error: no_media.then(|| ExtractError::NoMediaFound.to_string()),
                error_code: no_media.then(|| ExtractError::NoMediaFound.exit_code()),
            }
        }
        Err(e) => {
            tracing::error!(url = %raw, error = %e, code = e.exit_code(), "extract failed");
            PageResult {
                input: raw,
                final_url: None,
                extractor: Some("rust".to_string()),
                items: Vec::new(),
                error: Some(e.to_string()),
                error_code: Some(e.exit_code()),
            }
        }
    };
    tracing::info!(
        url = %page.input,
        items = page.items.len(),
        candidates = page.items.iter().map(|i| i.formats.len()).sum::<usize>(),
        error = ?page.error,
        "rust extract complete"
    );
    page
}
