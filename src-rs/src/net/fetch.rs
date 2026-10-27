//! Requests, retries and the response body cap.

use std::fmt;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use bytes::{Bytes, BytesMut};
use reqwest::StatusCode;
use reqwest::header::{CONTENT_TYPE, HeaderMap, HeaderValue, REFERER, RETRY_AFTER};
use tracing::{debug, instrument, warn};
use url::Url;

use crate::error::ExtractError;
use crate::net::client::Fetcher;

/// Hard cap on a response body; a hostile page must not exhaust memory.
const MAX_BODY: usize = 128 * 1024 * 1024;

/// Upper bound on a server-supplied `Retry-After` delay.
const MAX_RETRY_AFTER_SECS: u64 = 30;

/// First retry delay; doubles per attempt.
const BACKOFF_BASE_MS: u64 = 250;

/// Ceiling for the exponential part of the retry delay.
const BACKOFF_MAX_MS: u64 = 10_000;

/// Extra random spread added to a retry delay so parallel URLs don't sync.
const BACKOFF_JITTER_MS: u64 = 250;

/// One fetched page or sub-resource.
pub struct FetchResponse {
    /// Final URL after redirects.
    pub url: Url,
    /// Response headers.
    pub headers: HeaderMap,
    /// Full response body, capped at `MAX_BODY`.
    pub body: Bytes,
}

impl FetchResponse {
    /// Content-Type header, when present and valid UTF-8.
    #[must_use]
    pub fn content_type(&self) -> Option<&str> {
        self.headers
            .get(CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
    }
}

impl fmt::Debug for FetchResponse {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // The body is page markup; print its size, never its contents.
        f.debug_struct("FetchResponse")
            .field("url", &self.url)
            .field("content_type", &self.content_type())
            .field("bytes", &self.body.len())
            .finish_non_exhaustive()
    }
}

impl Fetcher {
    /// GET a URL with retries, redirects (at most 10 hops) and shared cookies.
    ///
    /// # Errors
    ///
    /// Returns [`ExtractError::Http`] for a non-success status,
    /// [`ExtractError::BodyTooLarge`] past the cap, and [`ExtractError::Net`]
    /// for transport failures after retries are exhausted.
    pub async fn fetch(&self, url: &Url) -> Result<FetchResponse, ExtractError> {
        self.fetch_with_referer(url, None).await
    }

    /// GET with an explicit `Referer` (for manifests bound to their page).
    ///
    /// # Errors
    ///
    /// Same failure classes as [`Fetcher::fetch`].
    pub async fn fetch_with_referer(
        &self,
        url: &Url,
        referer: Option<&str>,
    ) -> Result<FetchResponse, ExtractError> {
        self.request(url, referer).await
    }

    #[instrument(skip_all, fields(url = %url))]
    async fn request(
        &self,
        url: &Url,
        referer: Option<&str>,
    ) -> Result<FetchResponse, ExtractError> {
        let effective_referer = referer.or(self.default_referer.as_deref());
        let mut attempt = 0u32;
        loop {
            let mut request = self.client.get(url.clone());
            let mut context = self.ctx_headers_for(url);
            if let Some(referer) = effective_referer {
                match HeaderValue::from_str(referer) {
                    Ok(value) => {
                        context
                            .get_or_insert_with(HeaderMap::new)
                            .insert(REFERER, value);
                    }
                    Err(error) => warn!(%url, %error, "invalid referer skipped"),
                }
            }
            if let Some(headers) = context {
                request = request.headers(headers);
            }
            match request.send().await {
                Ok(response) => {
                    let status = response.status();
                    if status.is_success() {
                        let final_url = response.url().clone();
                        let headers = response.headers().clone();
                        let body = match body_capped(response).await {
                            Ok(Some(body)) => body,
                            Ok(None) => {
                                return Err(ExtractError::BodyTooLarge { limit: MAX_BODY });
                            }
                            Err(error)
                                if retryable_body_error(&error) && attempt < self.retries =>
                            {
                                let delay = backoff(attempt);
                                warn!(%url, ?delay, error = %error, "retrying after body failure");
                                attempt += 1;
                                tokio::time::sleep(delay).await;
                                continue;
                            }
                            Err(error) => return Err(ExtractError::from(error)),
                        };
                        debug!(
                            url = %final_url,
                            status = status.as_u16(),
                            bytes = body.len(),
                            "fetched"
                        );
                        return Ok(FetchResponse {
                            url: final_url,
                            headers,
                            body,
                        });
                    }
                    if retryable_status(status) && attempt < self.retries {
                        let delay =
                            retry_after(response.headers()).unwrap_or_else(|| backoff(attempt));
                        warn!(%url, status = status.as_u16(), ?delay, "retrying");
                        attempt += 1;
                        tokio::time::sleep(delay).await;
                        continue;
                    }
                    return Err(ExtractError::Http(status.as_u16()));
                }
                Err(error) => {
                    if (error.is_timeout() || error.is_connect()) && attempt < self.retries {
                        let delay = backoff(attempt);
                        warn!(%url, ?delay, error = %error, "retrying after transport error");
                        attempt += 1;
                        tokio::time::sleep(delay).await;
                        continue;
                    }
                    return Err(ExtractError::from(error));
                }
            }
        }
    }
}

/// Statuses worth retrying: rate limits, request timeouts and server errors.
fn retryable_status(status: StatusCode) -> bool {
    status == StatusCode::TOO_MANY_REQUESTS
        || status == StatusCode::REQUEST_TIMEOUT
        || status.is_server_error()
}

/// Body failures worth retrying: truncated or decoded bodies and read timeouts.
fn retryable_body_error(error: &reqwest::Error) -> bool {
    error.is_decode() || error.is_body() || error.is_timeout()
}

/// `Retry-After` in seconds, capped so a hostile header can't stall a batch.
fn retry_after(headers: &HeaderMap) -> Option<Duration> {
    headers
        .get(RETRY_AFTER)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.trim().parse::<u64>().ok())
        .map(|seconds| Duration::from_secs(seconds.min(MAX_RETRY_AFTER_SECS)))
}

/// Exponential backoff with a small time-derived jitter.
fn backoff(attempt: u32) -> Duration {
    let base_ms = (BACKOFF_BASE_MS << attempt.min(6)).min(BACKOFF_MAX_MS);
    let jitter = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| {
            u64::from(elapsed.subsec_millis()) % BACKOFF_JITTER_MS
        });
    Duration::from_millis(base_ms + jitter)
}

/// Read the body chunk by chunk so the cap holds even when the server omits
/// `Content-Length`. `Ok(None)` means the body exceeded [`MAX_BODY`].
async fn body_capped(mut response: reqwest::Response) -> Result<Option<Bytes>, reqwest::Error> {
    if response
        .content_length()
        .is_some_and(|length| length > MAX_BODY as u64)
    {
        return Ok(None);
    }
    let mut buffer = BytesMut::new();
    if let Some(length) = response.content_length() {
        buffer.reserve(usize::try_from(length).unwrap_or(MAX_BODY).min(MAX_BODY));
    }
    while let Some(chunk) = response.chunk().await? {
        if buffer.len().saturating_add(chunk.len()) > MAX_BODY {
            return Ok(None);
        }
        buffer.extend_from_slice(&chunk);
    }
    Ok(Some(buffer.freeze()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use reqwest::header::HeaderValue;

    #[test]
    fn retryable_statuses_are_rate_limits_timeouts_and_server_errors() {
        for status in [
            StatusCode::TOO_MANY_REQUESTS,
            StatusCode::REQUEST_TIMEOUT,
            StatusCode::INTERNAL_SERVER_ERROR,
            StatusCode::BAD_GATEWAY,
            StatusCode::SERVICE_UNAVAILABLE,
        ] {
            assert!(retryable_status(status), "{status}");
        }
        assert!(!retryable_status(StatusCode::NOT_FOUND));
        assert!(!retryable_status(StatusCode::FORBIDDEN));
    }

    #[test]
    fn retry_after_is_parsed_and_capped() {
        let mut headers = HeaderMap::new();
        headers.insert(RETRY_AFTER, HeaderValue::from_static("7"));
        assert_eq!(retry_after(&headers), Some(Duration::from_secs(7)));

        headers.insert(RETRY_AFTER, HeaderValue::from_static("9999"));
        assert_eq!(
            retry_after(&headers),
            Some(Duration::from_secs(MAX_RETRY_AFTER_SECS))
        );

        headers.insert(RETRY_AFTER, HeaderValue::from_static("not-a-date"));
        assert_eq!(retry_after(&headers), None);
    }

    #[test]
    fn backoff_stays_within_its_bounds() {
        let first = backoff(0).as_millis() as u64;
        assert!((BACKOFF_BASE_MS..BACKOFF_BASE_MS + BACKOFF_JITTER_MS).contains(&first));
        let last = backoff(10).as_millis() as u64;
        assert!((BACKOFF_MAX_MS..BACKOFF_MAX_MS + BACKOFF_JITTER_MS).contains(&last));
    }

    #[test]
    fn response_debug_reports_size_not_body() {
        let mut headers = HeaderMap::new();
        headers.insert(CONTENT_TYPE, HeaderValue::from_static("text/html"));
        let response = FetchResponse {
            url: Url::parse("https://x/watch").unwrap(),
            headers,
            body: Bytes::from_static(b"<html>secret</html>"),
        };
        let debug = format!("{response:?}");
        assert!(!debug.contains("secret"), "{debug}");
        assert!(debug.contains("text/html"), "{debug}");
        assert_eq!(response.content_type(), Some("text/html"));
    }
}
