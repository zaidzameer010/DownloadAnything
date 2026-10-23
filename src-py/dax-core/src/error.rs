use thiserror::Error;

#[derive(Error, Debug)]
pub enum ExtractError {
    #[error("invalid URL: {0}")]
    InvalidUrl(String),
    #[error("unsupported scheme (only http/https): {0}")]
    UnsupportedScheme(String),
    #[error("DRM-protected service: {0}")]
    DrmProtected(String),
    #[error("HTTP {0}")]
    Http(u16),
    #[error("fetch failed: {0}")]
    Net(#[from] reqwest::Error),
    #[error("no media found on page")]
    NoMediaFound,
    #[error("parse error in {layer}: {detail}")]
    Parse { layer: &'static str, detail: String },
    #[error("config: {0}")]
    Config(String),
}

impl ExtractError {
    pub fn exit_code(&self) -> i32 {
        match self {
            Self::NoMediaFound => 3,
            Self::DrmProtected(_) => 4,
            _ => 1,
        }
    }
}
