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
use crate::input::{self, classify};

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
    jar: Arc<Jar>,
}

/// One client is reused across extraction calls with the same configuration;
/// rebuilding it would drop the cookie jar and the connection pool.
static SHARED_CLIENT: Mutex<Option<CachedClient>> = Mutex::new(None);

/// Captured headers scoped to one host.
#[derive(Debug)]
struct ContextHeaders {
    host: String,
    /// Headers replayed on every request to the host.
    base: HeaderMap,
    /// `x-*` headers replayed only on media and manifest requests — signed
    /// CDN tokens bind the request to device/session headers, but replaying
    /// them on page fetches poisons API calls.
    media: HeaderMap,
}

/// Which request class a captured header replays on.
enum Replay {
    Base,
    Media,
}

/// Header names safe to replay beyond the request that captured them.
///
/// Captured `Cookie:` values replay as a request header — seeding the
/// shared client's jar would leak one task's session into every later
/// extraction to that host. `authorization`/`origin` stay request-scoped:
/// replaying a stale auth token globally breaks API calls (the Python
/// chain does the same).
fn replay_target(name: &str) -> Option<Replay> {
    match name {
        "cookie" | "referer" | "user-agent" | "accept-language" => Some(Replay::Base),
        _ if name.starts_with("x-") => Some(Replay::Media),
        _ => None,
    }
}

/// HTTP fetcher shared by all URLs of one extraction call.
pub struct Fetcher {
    pub(super) client: Client,
    pub(super) retries: u32,
    pub(super) default_referer: Option<String>,
    /// Captured headers scoped by host, broadest first so the most specific
    /// context wins when headers merge.
    ctx_headers: Vec<ContextHeaders>,
    /// Test-only handle to the shared cookie jar.
    #[cfg(test)]
    jar: Arc<Jar>,
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
        let (client, jar) = {
            let mut cache = SHARED_CLIENT
                .lock()
                .expect("http client cache lock poisoned");
            let cached = cache
                .as_ref()
                .filter(|cached| cached.key == key)
                .map(|cached| (cached.client.clone(), cached.jar.clone()));
            match cached {
                Some(cached) => cached,
                None => {
                    let (client, jar) = build_client(config)?;
                    *cache = Some(CachedClient {
                        key,
                        client: client.clone(),
                        jar: jar.clone(),
                    });
                    (client, jar)
                }
            }
        };

        let mut ctx_headers: Vec<ContextHeaders> = config
            .contexts
            .iter()
            .filter_map(|context| {
                let mut base = HeaderMap::new();
                let mut media = HeaderMap::new();
                for pair in &context.headers {
                    let name = pair.name.to_ascii_lowercase();
                    let Some(target) = replay_target(&name) else {
                        continue;
                    };
                    match (
                        HeaderName::from_bytes(name.as_bytes()),
                        HeaderValue::from_str(&pair.value),
                    ) {
                        (Ok(name), Ok(value)) => match target {
                            Replay::Base => {
                                base.insert(name, value);
                            }
                            Replay::Media => {
                                media.insert(name, value);
                            }
                        },
                        _ => warn!(
                            host = %context.host,
                            header = %pair.name,
                            "invalid context header skipped"
                        ),
                    }
                }
                (!base.is_empty() || !media.is_empty()).then(|| ContextHeaders {
                    host: context.host.clone(),
                    base,
                    media,
                })
            })
            .collect();
        // Broad contexts first; `insert` in ctx_headers_for then lets the
        // most specific matching host override them.
        ctx_headers.sort_by_key(|context| context.host.len());

        // The jar lives inside the client's cookie provider; the handle is
        // only inspected by tests.
        #[cfg(not(test))]
        drop(jar);

        Ok(Self {
            client,
            retries: config.retries,
            default_referer: config.referer.clone(),
            ctx_headers,
            #[cfg(test)]
            jar,
        })
    }

    /// Context headers matching `url`'s host (subdomains included), if any.
    ///
    /// Matching contexts merge broadest to most specific; `x-*` headers join
    /// only for media and manifest targets.
    pub(super) fn ctx_headers_for(&self, url: &Url) -> Option<HeaderMap> {
        let host = url.host_str()?;
        let media = input::is_direct_media_url(url) || input::looks_manifest_url(url);
        let mut merged: Option<HeaderMap> = None;
        for context in &self.ctx_headers {
            if !classify::host_matches(host, &context.host) {
                continue;
            }
            let headers = merged.get_or_insert_with(HeaderMap::new);
            for (name, value) in &context.base {
                headers.insert(name.clone(), value.clone());
            }
            if media {
                for (name, value) in &context.media {
                    headers.insert(name.clone(), value.clone());
                }
            }
        }
        merged
    }
}

impl fmt::Debug for Fetcher {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // Header values are session cookies and auth tokens — never print them.
        let contexts: Vec<(&str, usize)> = self
            .ctx_headers
            .iter()
            .map(|context| {
                (
                    context.host.as_str(),
                    context.base.len() + context.media.len(),
                )
            })
            .collect();
        f.debug_struct("Fetcher")
            .field("retries", &self.retries)
            .field("default_referer", &self.default_referer)
            .field("contexts", &contexts)
            .finish_non_exhaustive()
    }
}

/// Build a reqwest client and the cookie jar it shares.
fn build_client(config: &Config) -> Result<(Client, Arc<Jar>), ExtractError> {
    let mut headers = HeaderMap::new();
    let user_agent = HeaderValue::from_str(&config.user_agent)
        .unwrap_or_else(|_| HeaderValue::from_static(DEFAULT_UA));
    headers.insert(USER_AGENT, user_agent);
    headers.insert(ACCEPT, HeaderValue::from_static(ACCEPT_HEADER));
    headers.insert(
        ACCEPT_LANGUAGE,
        HeaderValue::from_static(ACCEPT_LANGUAGE_HEADER),
    );

    let jar = Arc::new(Jar::default());
    let mut builder = Client::builder()
        .default_headers(headers)
        .cookie_provider(jar.clone())
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
    builder
        .build()
        .map(|client| (client, jar))
        .map_err(ExtractError::from)
}

#[cfg(test)]
mod tests {
    use super::*;
    use reqwest::cookie::CookieStore;

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
                headers: vec![
                    HeaderPair {
                        name: "cookie".into(),
                        value: "sess=1; theme=dark".into(),
                    },
                    HeaderPair {
                        name: "referer".into(),
                        value: "https://site.com/watch".into(),
                    },
                ],
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
    fn captured_cookies_replay_as_header_and_never_reach_the_shared_jar() {
        let fetcher = Fetcher::new(&config_with_contexts()).unwrap();
        let url = Url::parse("https://cdn.site.com/v.mp4").unwrap();
        let headers = fetcher.ctx_headers_for(&url).expect("context headers");
        assert_eq!(
            headers.get("cookie").unwrap().to_str().unwrap(),
            "sess=1; theme=dark"
        );
        assert!(
            fetcher.ctx_headers_for(&Url::parse("https://other.com/").unwrap())
                .is_none(),
            "cookies must stay scoped to the captured host"
        );
        // The shared jar is reused across extractions — one task's captured
        // session must never seed it for the next task on the same host.
        assert!(
            fetcher.jar.cookies(&url).is_none(),
            "captured cookies must not leak into the shared jar"
        );
    }

    #[test]
    fn authorization_is_not_replayed() {
        let mut config = config_with_contexts();
        config.contexts[0].headers.push(HeaderPair {
            name: "authorization".into(),
            value: "Bearer secret".into(),
        });
        let fetcher = Fetcher::new(&config).unwrap();
        let headers = fetcher
            .ctx_headers_for(&Url::parse("https://cdn.site.com/v.m3u8").unwrap())
            .expect("context headers");
        assert!(!headers.contains_key("authorization"));
        assert!(headers.contains_key("referer"));
    }

    #[test]
    fn x_headers_only_replay_for_media_targets() {
        let mut config = config_with_contexts();
        config.contexts[0].headers.push(HeaderPair {
            name: "x-token".into(),
            value: "abc".into(),
        });
        let fetcher = Fetcher::new(&config).unwrap();
        let page = fetcher
            .ctx_headers_for(&Url::parse("https://cdn.site.com/watch").unwrap())
            .expect("page context");
        assert!(!page.contains_key("x-token"));
        let manifest = fetcher
            .ctx_headers_for(&Url::parse("https://cdn.site.com/hls/master.m3u8").unwrap())
            .expect("manifest context");
        assert_eq!(manifest.get("x-token").unwrap().to_str().unwrap(), "abc");
    }

    #[test]
    fn most_specific_context_wins() {
        let mut config = config_with_contexts();
        config.contexts = vec![
            Context {
                host: "site.com".into(),
                headers: vec![HeaderPair {
                    name: "referer".into(),
                    value: "https://site.com/broad".into(),
                }],
            },
            Context {
                host: "cdn.site.com".into(),
                headers: vec![HeaderPair {
                    name: "referer".into(),
                    value: "https://site.com/specific".into(),
                }],
            },
        ];
        let fetcher = Fetcher::new(&config).unwrap();
        let headers = fetcher
            .ctx_headers_for(&Url::parse("https://cdn.site.com/v.m3u8").unwrap())
            .expect("merged context");
        assert_eq!(
            headers.get("referer").unwrap().to_str().unwrap(),
            "https://site.com/specific"
        );
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
