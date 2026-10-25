//! Shared HTTP client construction and host-scoped request contexts.

use std::fmt;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use reqwest::cookie::Jar;
use reqwest::header::{ACCEPT, ACCEPT_LANGUAGE, HeaderMap, HeaderName, HeaderValue, USER_AGENT};
use reqwest::{Client, Proxy, redirect};
use tracing::warn;
use url::Url;

use crate::config::{Config, DEFAULT_UA};
use crate::error::ExtractError;
use crate::input::classify;

/// Timeout for a single TCP/TLS handshake.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(15);

/// Redirect hops followed before reqwest reports the redirect as an error.
const MAX_REDIRECTS: usize = 10;

/// Idle connections kept per host for reuse across sub-resource fetches.
const POOL_MAX_IDLE_PER_HOST: usize = 16;

/// Accept header covering pages, JSON, media and manifests in one request.
const ACCEPT_HEADER: &str = "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.9,video/*;q=0.8,audio/*;q=0.8,*/*;q=0.7";

/// Language header matching the default user agent's locale.
const ACCEPT_LANGUAGE_HEADER: &str = "en-US,en;q=0.9";

/// Fields that identify a client configuration; a change rebuilds the client.
#[derive(Debug, Clone, PartialEq, Eq)]
struct ClientKey {
    timeout_secs: u64,
    check_certificate: bool,
    proxy: Option<String>,
    user_agent: String,
}

/// One cached client and the configuration it was built from.
#[derive(Debug)]
struct CachedClient {
    key: ClientKey,
    client: Client,
}

/// One client is reused across extraction calls with the same configuration;
/// rebuilding it would drop the cookie jar and the connection pool.
static SHARED_CLIENT: Mutex<Option<CachedClient>> = Mutex::new(None);

/// HTTP fetcher shared by all URLs of one extraction call.
pub struct Fetcher {
    pub(super) client: Client,
    pub(super) retries: u32,
    pub(super) default_referer: Option<String>,
    /// Request-context headers scoped by host (extension-captured cookies,
    /// referer, user agent). Host match includes subdomains.
    pub(super) ctx_headers: Vec<(String, HeaderMap)>,
}

impl Fetcher {
    /// Build a fetcher for `config`, reusing the cached client when the
    /// connection-relevant settings are unchanged.
    ///
    /// # Errors
    ///
    /// Returns [`ExtractError::Config`] for an invalid proxy URL and
    /// [`ExtractError::Net`] when reqwest rejects the client configuration.
    pub fn new(config: &Config) -> Result<Self, ExtractError> {
        let key = ClientKey {
            timeout_secs: config.timeout_secs,
            check_certificate: config.check_certificate,
            proxy: config.proxy.clone(),
            user_agent: config.user_agent.clone(),
        };
        let client = {
            let mut cache = SHARED_CLIENT
                .lock()
                .expect("http client cache lock poisoned");
            let cached = cache
                .as_ref()
                .filter(|cached| cached.key == key)
                .map(|cached| cached.client.clone());
            match cached {
                Some(client) => client,
                None => {
                    let client = build_client(config)?;
                    *cache = Some(CachedClient {
                        key,
                        client: client.clone(),
                    });
                    client
                }
            }
        };

        let ctx_headers = config
            .contexts
            .iter()
            .filter_map(|context| {
                let mut headers = HeaderMap::new();
                for pair in &context.headers {
                    match (
                        pair.name.parse::<HeaderName>(),
                        HeaderValue::from_str(&pair.value),
                    ) {
                        (Ok(name), Ok(value)) => {
                            headers.append(name, value);
                        }
                        _ => warn!(
                            host = %context.host,
                            header = %pair.name,
                            "invalid context header skipped"
                        ),
                    }
                }
                (!headers.is_empty()).then(|| (context.host.clone(), headers))
            })
            .collect();

        Ok(Self {
            client,
            retries: config.retries,
            default_referer: config.referer.clone(),
            ctx_headers,
        })
    }

    /// Context headers matching `url`'s host (subdomains included), if any.
    pub(super) fn ctx_headers_for(&self, url: &Url) -> Option<&HeaderMap> {
        let host = url.host_str()?;
        self.ctx_headers
            .iter()
            .find(|(candidate, _)| classify::host_matches(host, candidate))
            .map(|(_, headers)| headers)
    }
}

impl fmt::Debug for Fetcher {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // Header values are session cookies and auth tokens — never print them.
        let contexts: Vec<(&str, usize)> = self
            .ctx_headers
            .iter()
            .map(|(host, headers)| (host.as_str(), headers.len()))
            .collect();
        f.debug_struct("Fetcher")
            .field("retries", &self.retries)
            .field("default_referer", &self.default_referer)
            .field("contexts", &contexts)
            .finish_non_exhaustive()
    }
}

/// Build a reqwest client from resolved configuration.
fn build_client(config: &Config) -> Result<Client, ExtractError> {
    let mut headers = HeaderMap::new();
    let user_agent = HeaderValue::from_str(&config.user_agent)
        .unwrap_or_else(|_| HeaderValue::from_static(DEFAULT_UA));
    headers.insert(USER_AGENT, user_agent);
    headers.insert(ACCEPT, HeaderValue::from_static(ACCEPT_HEADER));
    headers.insert(
        ACCEPT_LANGUAGE,
        HeaderValue::from_static(ACCEPT_LANGUAGE_HEADER),
    );

    let mut builder = Client::builder()
        .default_headers(headers)
        .cookie_provider(Arc::new(Jar::default()))
        .redirect(redirect::Policy::limited(MAX_REDIRECTS))
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(Duration::from_secs(config.timeout_secs))
        .danger_accept_invalid_certs(!config.check_certificate)
        .pool_max_idle_per_host(POOL_MAX_IDLE_PER_HOST);
    if let Some(proxy) = &config.proxy {
        let proxy =
            Proxy::all(proxy).map_err(|e| ExtractError::Config(format!("invalid proxy: {e}")))?;
        builder = builder.proxy(proxy);
    }
    builder.build().map_err(ExtractError::from)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{Context, HeaderPair};

    fn config_with_contexts() -> Config {
        Config {
            timeout_secs: 5,
            retries: 0,
            referer: None,
            user_agent: DEFAULT_UA.to_owned(),
            proxy: None,
            check_certificate: true,
            contexts: vec![Context {
                host: "cdn.site.com".into(),
                headers: vec![HeaderPair {
                    name: "cookie".into(),
                    value: "sess=1".into(),
                }],
            }],
        }
    }

    #[test]
    fn context_headers_match_subdomains_only() {
        let fetcher = Fetcher::new(&config_with_contexts()).unwrap();
        let hit = Url::parse("https://a.cdn.site.com/v.m3u8").unwrap();
        let miss = Url::parse("https://other.com/v.m3u8").unwrap();
        assert!(fetcher.ctx_headers_for(&hit).is_some());
        assert!(fetcher.ctx_headers_for(&miss).is_none());
    }

    #[test]
    fn invalid_context_headers_are_dropped() {
        let mut config = config_with_contexts();
        config.contexts[0].headers = vec![HeaderPair {
            name: "bad name".into(),
            value: "x".into(),
        }];
        let fetcher = Fetcher::new(&config).unwrap();
        assert!(fetcher.ctx_headers.is_empty());
    }

    #[test]
    fn debug_does_not_print_header_values() {
        let fetcher = Fetcher::new(&config_with_contexts()).unwrap();
        let debug = format!("{fetcher:?}");
        assert!(!debug.contains("sess=1"), "{debug}");
        assert!(debug.contains("cdn.site.com"), "{debug}");
    }
}
