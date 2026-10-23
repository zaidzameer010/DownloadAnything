use serde::Deserialize;
use tracing::warn;

/// Keep byte-identical to `dax/settings.py::DEFAULT_UA` — the engine UA and
/// the extractor UA must agree or header replay drifts between the two.
pub const DEFAULT_UA: &str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) \
    AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36";

/// Per-call extraction options, sent by the Python backend (camelCase JSON).
/// The caller may send more keys (download tuning etc.) — serde ignores
/// unknown fields. Any `None` field falls back to defaults.
#[derive(Debug, Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
pub struct Options {
    pub timeout_secs: Option<u64>,
    pub retries: Option<u32>,
    pub referer: Option<String>,
    pub user_agent: Option<String>,
    pub proxy: Option<String>,
    pub check_certificate: Option<bool>,
    /// Per-origin request contexts (extension pushes): headers scoped to a
    /// host, replayed on fetches to that host and its subdomains.
    pub contexts: Vec<ContextHeaders>,
}

/// A request context: `url` identifies the origin, `headers` replay verbatim
/// (cookies, referer, UA captured by the browser).
#[derive(Debug, Clone, Deserialize)]
pub struct ContextHeaders {
    pub url: String,
    #[serde(default)]
    pub headers: Vec<HeaderPair>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct HeaderPair {
    pub name: String,
    pub value: String,
}

/// Resolved configuration: call options > defaults.
pub struct Config {
    pub timeout_secs: u64,
    pub retries: u32,
    pub referer: Option<String>,
    pub user_agent: String,
    pub proxy: Option<String>,
    pub check_certificate: bool,
    pub drm_blocklist: &'static [&'static str],
    /// Host → headers map built from `options.contexts`.
    pub context_headers: Vec<(String, Vec<(String, String)>)>,
}

impl From<&Options> for Config {
    fn from(o: &Options) -> Self {
        Self {
            timeout_secs: o.timeout_secs.unwrap_or(30),
            retries: o.retries.unwrap_or(3),
            referer: o
                .referer
                .as_deref()
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(String::from),
            user_agent: o
                .user_agent
                .as_deref()
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .unwrap_or(DEFAULT_UA)
                .to_string(),
            proxy: o
                .proxy
                .as_deref()
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(String::from),
            check_certificate: o.check_certificate.unwrap_or(true),
            drm_blocklist: crate::input::classify::DRM_DOMAINS,
            context_headers: o
                .contexts
                .iter()
                .filter_map(|c| {
                    let Some(host) = url::Url::parse(&c.url)
                        .ok()
                        .and_then(|u| u.host_str().map(str::to_lowercase))
                    else {
                        warn!(url = %c.url, "context skipped — invalid url");
                        return None;
                    };
                    let hs: Vec<(String, String)> = c
                        .headers
                        .iter()
                        .map(|h| (h.name.to_lowercase(), h.value.clone()))
                        .collect();
                    (!hs.is_empty()).then_some((host, hs))
                })
                .collect(),
        }
    }
}
