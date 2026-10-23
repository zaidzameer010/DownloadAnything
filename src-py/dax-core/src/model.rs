use serde::Serialize;
use url::Url;

/// One logical piece of content (a video, an episode, a track) extracted from a page.
#[derive(Debug, Clone, Serialize)]
pub struct MediaItem {
    pub id: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    pub page_url: Url,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duration_secs: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thumbnail: Option<Url>,
    pub formats: Vec<MediaLink>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum MediaKind {
    Progressive,
    Hls,
    Dash,
    Audio,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceLayer {
    Html,
    JsonBlob,
    JsonLd,
    Meta,
    Regex,
    Direct,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct Quality {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub height: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub width: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bandwidth: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
}

#[derive(Debug, Clone, Default, Serialize)]
pub struct HeaderHints {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub referer: Option<String>,
}

impl HeaderHints {
    pub fn is_empty(&self) -> bool {
        self.referer.is_none()
    }
}

/// A candidate media address extracted from the page (to be probed by yt-dlp).
#[derive(Debug, Clone, Serialize)]
pub struct MediaLink {
    pub url: Url,
    pub kind: MediaKind,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub quality: Option<Quality>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub container: Option<String>,
    pub source: SourceLayer,
    #[serde(skip_serializing_if = "HeaderHints::is_empty")]
    pub headers: HeaderHints,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expires_at: Option<i64>,
    /// Which <video>/<audio> element this link came from (playlist grouping).
    #[serde(skip)]
    pub media_index: Option<u32>,
}

impl MediaLink {
    pub fn new(url: Url, kind: MediaKind, source: SourceLayer) -> Self {
        Self {
            url,
            kind,
            quality: None,
            container: None,
            source,
            headers: HeaderHints::default(),
            expires_at: None,
            media_index: None,
        }
    }
}
