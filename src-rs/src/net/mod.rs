//! HTTP fetching: one shared client, retries, redirects and a response cap.

mod client;
mod fetch;

pub use client::Fetcher;
pub use fetch::FetchResponse;
