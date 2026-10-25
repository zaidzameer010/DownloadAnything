//! Media discovery for the `dax.dax_rs` Python extension.
//!
//! The crate fetches a page, runs independent discovery layers over it, and
//! returns candidate media addresses plus page metadata. Ranking, junk
//! filtering and format selection stay in `dax/filter.py`; this crate only
//! discovers addresses and never downloads media.
//!
//! # Pipeline
//!
//! [`api::extract`] runs one `pipeline` per input URL: normalize the URL,
//! reject DRM services, short-circuit direct media, otherwise fetch the page
//! and dispatch on the response kind in `extract`. Page bodies run the HTML,
//! meta, JSON-LD, JSON-blob and regex layers, then escalate iframes and
//! manifest endpoints one level deeper.
//!
//! # Wire contract
//!
//! Python sends camelCase options JSON ([`config::Options`]) and reads back
//! [`api::PageResult`] JSON. Per-URL failures are carried in the result, never
//! thrown, so a multi-URL call can render each failure separately.
//!
//! # Modules
//!
//! - [`api`] — batch entry point and the serialized page result.
//! - [`config`] — per-call options and their resolved form.
//! - [`error`] — the single error type for one URL's extraction.
//! - [`model`] — serialized media items, links, quality and header hints.
//! - [`net`] — the shared HTTP client, retries and response cap.
//! - `extract` — discovery layers and the page-extraction pipeline.
//! - `input` — URL normalization, extension vocabularies and response kinds.
//! - `pipeline` — one URL from input string to media items.

use pyo3::pymodule;

mod bridge;
mod pipeline;

pub mod api;
pub mod config;
pub mod error;
pub mod model;
pub mod net;

pub(crate) mod extract;
pub(crate) mod input;

#[pymodule]
mod dax_rs {
    #[pymodule_export]
    use super::bridge::{extract_urls, init_logging};
}
