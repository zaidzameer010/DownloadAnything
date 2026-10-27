//! Candidate discovery layers and the page-extraction pipeline.
//!
//! Each layer answers the same question from a different source: the HTML
//! layer reads media elements and attributes, the meta layer reads `og:*`
//! tags, the JSON-LD layer reads schema.org objects, the JSON-blob layer walks
//! embedded player config, and the regex layer is the last resort over raw
//! source. [`response`] classifies a fetched body and dispatches to the right
//! path; [`page`] orchestrates the layers, escalation waves and item grouping.
//!
//! Layers only discover addresses. Validation, ranking and junk filtering
//! happen in `downloadanything/filter.py`, so a candidate may still be rejected there.

mod candidates;
mod html;
mod json_blobs;
mod jsonld;
mod kvs;
mod meta;
mod packer;
mod page;
mod regex_scan;
mod response;
mod unescape;

#[cfg(test)]
mod tests;

pub(crate) use response::extract_response;

use scraper::Selector;

/// Parse a static CSS selector; an invalid selector is a programming error.
pub(crate) fn selector(css: &str) -> Selector {
    Selector::parse(css).unwrap_or_else(|error| panic!("invalid selector {css:?}: {error}"))
}
