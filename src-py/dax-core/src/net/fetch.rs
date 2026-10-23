use crate::error::ExtractError;
use crate::net::client::NetEngine;
use bytes::{Bytes, BytesMut};
use reqwest::header::{HeaderMap, REFERER};
use reqwest::{Method, StatusCode};
use std::time::Duration;
use tracing::{debug, instrument, warn};
use url::Url;

/// Cap response bodies so a hostile "page" can't eat all memory.
const MAX_BODY: u64 = 128 * 1024 * 1024;

pub struct FetchResponse {
    /// Final URL after redirects.
    pub url: Url,
    pub headers: HeaderMap,
    pub body: Bytes,
}

impl FetchResponse {
    pub fn content_type(&self) -> Option<String> {
        headers_content_type(&self.headers)
    }
}

/// Content-Type of a response header map.
pub(crate) fn headers_content_type(h: &HeaderMap) -> Option<String> {
    h.get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .map(|s| s.to_string())
}

fn retryable_status(s: StatusCode) -> bool {
    s == StatusCode::TOO_MANY_REQUESTS || s == StatusCode::REQUEST_TIMEOUT || s.is_server_error()
}

fn retry_after(headers: &HeaderMap) -> Option<Duration> {
    headers
        .get(reqwest::header::RETRY_AFTER)
        .and_then(|v| v.to_str().ok())
        .and_then(|s| s.trim().parse::<u64>().ok())
        .map(|secs| Duration::from_secs(secs.min(30)))
}

fn backoff(attempt: u32) -> Duration {
    let base_ms = (250u64 << attempt.min(6)).min(10_000);
    let jitter = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.subsec_millis() as u64 % 250)
        .unwrap_or(0);
    Duration::from_millis(base_ms + jitter)
}

/// Accumulate the body chunk-by-chunk so the cap applies even when the
/// server withholds Content-Length (chunked/streamed replies bypassed the
/// header-only check). `Ok(None)` = body exceeded MAX_BODY.
async fn body_capped(mut resp: reqwest::Response) -> Result<Option<Bytes>, reqwest::Error> {
    if resp.content_length().is_some_and(|l| l > MAX_BODY) {
        return Ok(None);
    }
    let mut buf = BytesMut::new();
    if let Some(l) = resp.content_length() {
        buf.reserve(l.min(MAX_BODY) as usize);
    }
    while let Some(chunk) = resp.chunk().await? {
        if buf.len() + chunk.len() > MAX_BODY as usize {
            return Ok(None);
        }
        buf.extend_from_slice(&chunk);
    }
    Ok(Some(buf.freeze()))
}

impl NetEngine {
    /// GET a URL with rate limiting, retries, redirect following (<= 10 hops)
    /// and shared cookies.
    pub async fn fetch(&self, url: &Url) -> Result<FetchResponse, ExtractError> {
        self.request(Method::GET, url, None).await
    }

    /// GET with an explicit Referer (e.g. manifest fetches bound to the page).
    pub async fn fetch_ref(
        &self,
        url: &Url,
        referer: Option<&str>,
    ) -> Result<FetchResponse, ExtractError> {
        self.request(Method::GET, url, referer).await
    }

    #[instrument(skip_all, fields(method = %method, url = %url))]
    async fn request(
        &self,
        method: Method,
        url: &Url,
        referer: Option<&str>,
    ) -> Result<FetchResponse, ExtractError> {
        let mut attempt = 0u32;
        let effective_referer = referer.or(self.default_referer.as_deref());
        loop {
            let mut req = self.client.request(method.clone(), url.clone());
            if let Some(m) = self.ctx_headers_for(url) {
                req = req.headers(m.clone());
            }
            if let Some(r) = effective_referer {
                req = req.header(REFERER, r);
            }
            match req.send().await {
                Ok(resp) => {
                    let status = resp.status();
                    if status.is_success() {
                        let final_url = resp.url().clone();
                        let headers = resp.headers().clone();
                        let body = match body_capped(resp).await {
                            Ok(Some(b)) => b,
                            Ok(None) => {
                                return Err(ExtractError::Parse {
                                    layer: "fetch",
                                    detail: "response too large".into(),
                                });
                            }
                            Err(e)
                                if (e.is_decode() || e.is_body() || e.is_timeout())
                                    && attempt < self.retries =>
                            {
                                let delay = backoff(attempt);
                                warn!(%url, ?delay, error = %e, "retrying after body failure");
                                attempt += 1;
                                tokio::time::sleep(delay).await;
                                continue;
                            }
                            Err(e) => return Err(ExtractError::Net(e)),
                        };
                        debug!(url = %final_url, status = status.as_u16(), bytes = body.len(), "fetched");
                        return Ok(FetchResponse {
                            url: final_url,
                            headers,
                            body,
                        });
                    }
                    if retryable_status(status) && attempt < self.retries {
                        let delay = retry_after(resp.headers()).unwrap_or_else(|| backoff(attempt));
                        warn!(%url, status = status.as_u16(), ?delay, "retrying");
                        attempt += 1;
                        tokio::time::sleep(delay).await;
                        continue;
                    }
                    return Err(ExtractError::Http(status.as_u16()));
                }
                Err(e) => {
                    if (e.is_timeout() || e.is_connect()) && attempt < self.retries {
                        let delay = backoff(attempt);
                        warn!(%url, ?delay, error = %e, "retrying after transport error");
                        attempt += 1;
                        tokio::time::sleep(delay).await;
                        continue;
                    }
                    return Err(ExtractError::Net(e));
                }
            }
        }
    }
}
