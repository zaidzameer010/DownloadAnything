//! Error type for one URL's extraction.

use std::error::Error;
use std::fmt;

use thiserror::Error;

/// Failure returned while extracting one URL.
///
/// The variant's [`exit_code`](ExtractError::exit_code) mirrors the codes
/// Python publishes (`dax/extract.py::CODE_*`), so the UI can distinguish
/// "nothing found" from a hard failure.
#[derive(Debug, Error)]
#[non_exhaustive]
pub enum ExtractError {
    /// The input string is not a usable URL.
    #[error("invalid url: {0}")]
    InvalidUrl(String),
    /// The URL parsed but uses a scheme other than http or https.
    #[error("unsupported scheme (only http/https): {0}")]
    UnsupportedScheme(String),
    /// The host is a known DRM-only service.
    #[error("drm-protected service: {0}")]
    DrmProtected(String),
    /// The server answered with a non-success status.
    #[error("http {0}")]
    Http(u16),
    /// The request failed below HTTP: DNS, TLS, timeout, decode.
    #[error("fetch failed: {0}")]
    Net(#[source] NetError),
    /// The response body exceeded the configured cap.
    #[error("response body exceeds {limit} bytes")]
    BodyTooLarge { limit: usize },
    /// The page parsed successfully but exposed no media.
    #[error("no media found on page")]
    NoMediaFound,
    /// One discovery layer could not parse its input.
    #[error("parse error in {layer}: {detail}")]
    Parse {
        /// Layer that failed (`json`, `page`, …).
        layer: &'static str,
        /// Human-readable parse failure.
        detail: String,
    },
    /// The HTTP client could not be constructed from the options.
    #[error("config: {0}")]
    Config(String),
}

impl ExtractError {
    /// Stable process exit code for this failure class.
    ///
    /// `1` is a hard failure, `3` means no media, `4` means DRM-protected.
    #[must_use]
    pub fn exit_code(&self) -> i32 {
        match self {
            Self::NoMediaFound => 3,
            Self::DrmProtected(_) => 4,
            _ => 1,
        }
    }
}

impl From<reqwest::Error> for ExtractError {
    fn from(error: reqwest::Error) -> Self {
        Self::Net(NetError(error))
    }
}

/// Transport failure that keeps the `reqwest` error out of the public API.
#[derive(Debug)]
pub struct NetError(reqwest::Error);

impl fmt::Display for NetError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(f)
    }
}

impl Error for NetError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        Some(&self.0)
    }
}

impl From<reqwest::Error> for NetError {
    fn from(error: reqwest::Error) -> Self {
        Self(error)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exit_codes_match_python_contract() {
        assert_eq!(ExtractError::NoMediaFound.exit_code(), 3);
        assert_eq!(ExtractError::DrmProtected("x".into()).exit_code(), 4);
        assert_eq!(ExtractError::Http(403).exit_code(), 1);
        assert_eq!(ExtractError::InvalidUrl("x".into()).exit_code(), 1);
    }

    #[test]
    fn display_messages_are_lowercase() {
        for error in [
            ExtractError::InvalidUrl("x".into()),
            ExtractError::UnsupportedScheme("ftp".into()),
            ExtractError::DrmProtected("netflix.com".into()),
            ExtractError::Http(500),
            ExtractError::BodyTooLarge { limit: 1 },
            ExtractError::NoMediaFound,
            ExtractError::Parse {
                layer: "json",
                detail: "eof".into(),
            },
            ExtractError::Config("bad proxy".into()),
        ] {
            let message = error.to_string();
            let first = message.chars().next().unwrap_or(' ');
            assert!(!first.is_uppercase(), "{message:?} must start lowercase");
            assert!(
                !message.ends_with('.'),
                "{message:?} must not end with punctuation"
            );
        }
    }
}
