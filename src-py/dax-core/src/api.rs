//! Batch entry point and the serialized page result.

use futures::stream::{self, StreamExt};
use serde::Serialize;
use tracing::instrument;
use url::Url;

use crate::error::ExtractError;
use crate::model::MediaItem;
use crate::net::Fetcher;
use crate::pipeline::extract_url;

/// Value the Python side keys on to mark the Rust extractor.
const EXTRACTOR: &str = "rust";

/// Maximum URLs extracted concurrently in one call.
const MAX_CONCURRENT_URLS: usize = 4;

/// Extraction result for one input URL.
///
/// Errors are carried, not thrown, so a multi-URL UI call can render per-URL
/// failures. `error` and `error_code` are omitted on success.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct PageResult {
    /// Input string exactly as the caller sent it.
    pub input: String,
    /// Final URL after redirects, when the page was fetched.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub final_url: Option<Url>,
    /// Extractor that produced the result; always `"rust"`.
    pub extractor: &'static str,
    /// Media items found on the page, in grouping order.
    pub items: Vec<MediaItem>,
    /// Failure message, when extraction failed or found nothing.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
    /// [`ExtractError::exit_code`] for `error`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error_code: Option<i32>,
}

impl PageResult {
    /// Successful result; an empty item list becomes a `no media` failure.
    fn success(input: String, final_url: Url, items: Vec<MediaItem>) -> Self {
        let mut page = Self {
            input,
            final_url: Some(final_url),
            extractor: EXTRACTOR,
            items,
            error: None,
            error_code: None,
        };
        if page.items.is_empty() {
            page.error = Some(ExtractError::NoMediaFound.to_string());
            page.error_code = Some(ExtractError::NoMediaFound.exit_code());
        }
        page
    }

    /// Failed result: no final URL and no items.
    fn failure(input: String, error: &ExtractError) -> Self {
        Self {
            input,
            final_url: None,
            extractor: EXTRACTOR,
            items: Vec::new(),
            error: Some(error.to_string()),
            error_code: Some(error.exit_code()),
        }
    }
}

/// Extract URLs in parallel (bounded, order-preserving).
///
/// Per-URL failures never abort the batch: every input yields exactly one
/// [`PageResult`] at the same index.
#[instrument(skip_all, fields(urls = urls.len()))]
pub async fn extract(net: &Fetcher, urls: Vec<String>) -> Vec<PageResult> {
    stream::iter(urls)
        .map(|raw| extract_one(net, raw))
        .buffered(MAX_CONCURRENT_URLS)
        .collect()
        .await
}

#[instrument(skip_all, fields(input = %raw))]
async fn extract_one(net: &Fetcher, raw: String) -> PageResult {
    let page = match extract_url(net, &raw).await {
        Ok((final_url, mut items)) => {
            // Junk filtering, ordering and truncation belong to the Python
            // filter (dax/filter.py) — this crate returns raw candidates.
            items.retain(|item| !item.formats.is_empty());
            PageResult::success(raw, final_url, items)
        }
        Err(error) => PageResult::failure(raw, &error),
    };
    tracing::info!(
        url = %page.input,
        items = page.items.len(),
        candidates = page.items.iter().map(|item| item.formats.len()).sum::<usize>(),
        error = ?page.error,
        "rust extract complete"
    );
    page
}
