//! Per-call extraction options sent by Python and their resolved form.

use std::fmt;

use serde::Deserialize;
use tracing::warn;
use url::Url;

/// User agent used when the caller does not send one.
///
/// Keep byte-identical to `dax/settings.py::DEFAULT_UA`: the engine and the
/// extractor must agree or header replay drifts between the two.
pub const DEFAULT_UA: &str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) \
    AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36";

/// Default per-request timeout in seconds.
const DEFAULT_TIMEOUT_SECS: u64 = 30;

/// Default retry count for retryable transport and HTTP failures.
const DEFAULT_RETRIES: u32 = 3;

/// Per-call options as sent by the Python backend (camelCase JSON).
///
/// The caller may send more keys (download tuning and so on); serde ignores
/// unknown fields. Every `None` field falls back to a default.
#[derive(Debug, Default, Deserialize)]
#[serde(default, rename_all = "camelCase")]
pub struct Options {
    /// Per-request timeout in seconds.
    pub timeout_secs: Option<u64>,
    /// Retry count for retryable failures.
    pub retries: Option<u32>,
    /// Referer to send when a link carries none.
    pub referer: Option<String>,
    /// User agent for every request.
    pub user_agent: Option<String>,
    /// Proxy URL (`http://`, `socks5://`, …).
    pub proxy: Option<String>,
    /// Verify TLS certificates (default `true`).
    pub check_certificate: Option<bool>,
    /// Per-origin request contexts pushed by the extension: headers scoped to
    /// a host, replayed on fetches to that host and its subdomains.
    pub contexts: Vec<ContextHeaders>,
}

/// A request context: `url` identifies the origin, `headers` replay verbatim
/// (cookies, referer and user agent captured by the browser).
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct ContextHeaders {
    /// Any URL on the origin the headers belong to.
    pub url: String,
    /// Headers captured for that origin.
    #[serde(default)]
    pub headers: Vec<HeaderPair>,
}

/// One captured request header.
///
/// `Debug` redacts the value: contexts carry session cookies and auth tokens.
#[derive(Clone, PartialEq, Eq, Deserialize)]
pub struct HeaderPair {
    /// Header name as captured.
    pub name: String,
    /// Header value as captured.
    pub value: String,
}

impl fmt::Debug for HeaderPair {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("HeaderPair")
            .field("name", &self.name)
            .field("value", &"<redacted>")
            .finish()
    }
}

/// Resolved configuration: call options layered over defaults.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// Per-request timeout in seconds.
    pub timeout_secs: u64,
    /// Retry count for retryable failures.
    pub retries: u32,
    /// Referer to send when a link carries none.
    pub referer: Option<String>,
    /// User agent for every request.
    pub user_agent: String,
    /// Proxy URL, when configured.
    pub proxy: Option<String>,
    /// Verify TLS certificates.
    pub check_certificate: bool,
    /// Host-scoped request contexts, keyed by lowercase host.
    pub contexts: Vec<Context>,
}

/// A request context resolved to a host.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Context {
    /// Lowercase host the headers apply to (subdomains included).
    pub host: String,
    /// Headers to replay, names lowercased.
    pub headers: Vec<HeaderPair>,
}

impl From<&Options> for Config {
    fn from(options: &Options) -> Self {
        Self {
            timeout_secs: options.timeout_secs.unwrap_or(DEFAULT_TIMEOUT_SECS),
            retries: options.retries.unwrap_or(DEFAULT_RETRIES),
            referer: trimmed(&options.referer),
            user_agent: trimmed(&options.user_agent).unwrap_or_else(|| DEFAULT_UA.to_owned()),
            proxy: trimmed(&options.proxy),
            check_certificate: options.check_certificate.unwrap_or(true),
            contexts: options
                .contexts
                .iter()
                .filter_map(|context| {
                    let Some(host) = Url::parse(&context.url)
                        .ok()
                        .and_then(|url| url.host_str().map(str::to_lowercase))
                    else {
                        warn!(url = %context.url, "context skipped: invalid url");
                        return None;
                    };
                    let headers: Vec<HeaderPair> = context
                        .headers
                        .iter()
                        .map(|header| HeaderPair {
                            name: header.name.to_lowercase(),
                            value: header.value.clone(),
                        })
                        .collect();
                    if headers.is_empty() {
                        warn!(url = %context.url, "context skipped: no headers");
                        return None;
                    }
                    Some(Context { host, headers })
                })
                .collect(),
        }
    }
}

/// Trimmed non-empty copy of an optional string.
fn trimmed(value: &Option<String>) -> Option<String> {
    value
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_owned)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn options_deserialize_camel_case_from_python() {
        let options: Options = serde_json::from_str(
            r#"{
                "extractorEngine": "custom",
                "referer": "https://site.com/watch",
                "userAgent": "CustomUA/1.0",
                "proxy": "http://127.0.0.1:8080",
                "checkCertificate": false,
                "contexts": [
                    {"url": "https://cdn.site.com/v.m3u8",
                     "headers": [{"name": "Cookie", "value": "sess=abc"}]}
                ]
            }"#,
        )
        .expect("python camelCase options must deserialize");

        assert_eq!(options.contexts.len(), 1);
        assert_eq!(options.contexts[0].headers[0].name, "Cookie");
        assert_eq!(options.referer.as_deref(), Some("https://site.com/watch"));
        assert_eq!(options.user_agent.as_deref(), Some("CustomUA/1.0"));
        assert_eq!(options.proxy.as_deref(), Some("http://127.0.0.1:8080"));
        assert_eq!(options.check_certificate, Some(false));
        assert_eq!(options.timeout_secs, None);

        let config = Config::from(&options);
        assert_eq!(config.referer.as_deref(), Some("https://site.com/watch"));
        assert_eq!(config.user_agent, "CustomUA/1.0");
        assert_eq!(config.proxy.as_deref(), Some("http://127.0.0.1:8080"));
        assert!(!config.check_certificate);
        assert_eq!(config.contexts[0].host, "cdn.site.com");
        assert_eq!(config.contexts[0].headers[0].name, "cookie");
    }

    #[test]
    fn config_defaults_match_dax_core() {
        let options = Options::default();
        let config = Config::from(&options);
        assert_eq!(config.timeout_secs, DEFAULT_TIMEOUT_SECS);
        assert_eq!(config.retries, DEFAULT_RETRIES);
        assert!(config.check_certificate);
        assert!(config.contexts.is_empty());
        assert_eq!(config.user_agent, DEFAULT_UA);
        assert!(config.referer.is_none());
        assert!(config.proxy.is_none());
    }

    #[test]
    fn invalid_context_url_is_skipped_without_dropping_the_call() {
        let options: Options = serde_json::from_str(
            r#"{"contexts": [
                {"url": "not a url", "headers": [{"name": "Cookie", "value": "a"}]},
                {"url": "https://ok.com/x", "headers": []}
            ]}"#,
        )
        .unwrap();
        let config = Config::from(&options);
        assert!(config.contexts.is_empty());
    }

    #[test]
    fn header_values_are_redacted_in_debug() {
        let pair = HeaderPair {
            name: "cookie".into(),
            value: "session=secret".into(),
        };
        let debug = format!("{pair:?}");
        assert!(!debug.contains("secret"), "{debug}");
        assert!(debug.contains("cookie"), "{debug}");
    }
}
