use crate::config::Config;
use crate::error::ExtractError;
use reqwest::cookie::Jar;
use reqwest::header::{ACCEPT, ACCEPT_LANGUAGE, HeaderMap, HeaderValue, USER_AGENT};
use reqwest::{Client, Proxy, redirect};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tracing::warn;

type ClientKey = (u64, bool, Option<String>, String);
static SHARED_CLIENT: Mutex<Option<(ClientKey, Client)>> = Mutex::new(None);

/// Shared HTTP engine.
pub struct NetEngine {
    pub client: Client,
    pub retries: u32,
    pub default_referer: Option<String>,
    /// Request-context headers scoped by host (extension-captured cookies,
    /// referer, UA). Host match includes subdomains.
    pub ctx_headers: Vec<(String, HeaderMap)>,
}

impl NetEngine {
    pub fn new(cfg: &Config) -> Result<Self, ExtractError> {
        let key: ClientKey = (
            cfg.timeout_secs,
            cfg.check_certificate,
            cfg.proxy.clone(),
            cfg.user_agent.clone(),
        );
        let client = {
            let mut guard = SHARED_CLIENT.lock().unwrap_or_else(|e| e.into_inner());
            if let Some((k, c)) = guard.as_ref()
                && *k == key
            {
                c.clone()
            } else {
                let jar = Arc::new(Jar::default());
                let mut headers = HeaderMap::new();
                let ua = HeaderValue::from_str(&cfg.user_agent)
                    .unwrap_or_else(|_| HeaderValue::from_static(crate::config::DEFAULT_UA));
                headers.insert(USER_AGENT, ua);
                headers.insert(
                    ACCEPT,
                    HeaderValue::from_static(
                        "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.9,video/*;q=0.8,audio/*;q=0.8,*/*;q=0.7",
                    ),
                );
                headers.insert(ACCEPT_LANGUAGE, HeaderValue::from_static("en-US,en;q=0.9"));

                let mut b = Client::builder()
                    .default_headers(headers)
                    .cookie_provider(jar)
                    .redirect(redirect::Policy::limited(10))
                    .connect_timeout(Duration::from_secs(15))
                    .timeout(Duration::from_secs(cfg.timeout_secs))
                    .danger_accept_invalid_certs(!cfg.check_certificate)
                    .pool_max_idle_per_host(16);
                if let Some(p) = &cfg.proxy {
                    let proxy = Proxy::all(p).map_err(|e| ExtractError::Config(e.to_string()))?;
                    b = b.proxy(proxy);
                }
                let built = b.build()?;
                *guard = Some((key, built.clone()));
                built
            }
        };
        Ok(Self {
            client,
            retries: cfg.retries,
            default_referer: cfg.referer.clone(),
            ctx_headers: cfg
                .context_headers
                .iter()
                .filter_map(|(host, pairs)| {
                    let mut m = HeaderMap::new();
                    for (n, v) in pairs {
                        match (
                            n.parse::<reqwest::header::HeaderName>(),
                            HeaderValue::from_str(v),
                        ) {
                            (Ok(n), Ok(v)) => {
                                m.append(n, v);
                            }
                            _ => warn!(%host, header = %n, "invalid context header skipped"),
                        }
                    }
                    (!m.is_empty()).then(|| (host.clone(), m))
                })
                .collect(),
        })
    }

    /// Context headers matching `url`'s host (incl. subdomains), if any.
    pub fn ctx_headers_for(&self, url: &url::Url) -> Option<&HeaderMap> {
        let host = url.host_str()?;
        self.ctx_headers
            .iter()
            .find(|(h, _)| crate::input::classify::host_matches(host, h))
            .map(|(_, m)| m)
    }
}
