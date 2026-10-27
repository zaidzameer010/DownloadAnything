//! Serialized media model shared with the Python side.
//!
//! Every type here is part of the JSON contract: field names and enum
//! spellings must match what `downloadanything/extract.py` reads.

use std::fmt;

use serde::{Serialize, Serializer};
use url::Url;

/// Identifier of one item within a page result.
///
/// Serializes as `"page"`, `"direct"` or `"media-<n>"`, the strings the
/// Python probe and the UI already key on.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ItemId {
    /// The page-level item holding links that belong to no single element.
    Page,
    /// The single item synthesized for a direct media URL.
    Direct,
    /// One `<video>`/`<audio>` element, 1-based.
    Media(u32),
}

impl fmt::Display for ItemId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Page => f.write_str("page"),
            Self::Direct => f.write_str("direct"),
            Self::Media(index) => write!(f, "media-{index}"),
        }
    }
}

impl Serialize for ItemId {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_str(self)
    }
}

/// One logical piece of content (a video, an episode, a track) found on a page.
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct MediaItem {
    /// Stable identifier within the page result.
    pub id: ItemId,
    /// Page or player title, when the page exposed one.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    /// Page URL the item was extracted from.
    pub page_url: Url,
    /// Content duration in seconds, when known.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duration_secs: Option<f64>,
    /// Poster/thumbnail URL, when known.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thumbnail: Option<Url>,
    /// Candidate addresses for this item, in discovery order.
    pub formats: Vec<MediaLink>,
}

impl MediaItem {
    /// Page-level item holding links that belong to no single media element.
    #[must_use]
    pub fn page(page_url: Url, formats: Vec<MediaLink>) -> Self {
        Self {
            id: ItemId::Page,
            title: None,
            page_url,
            duration_secs: None,
            thumbnail: None,
            formats,
        }
    }

    /// Item synthesized for a direct media URL.
    #[must_use]
    pub fn direct(page_url: Url, link: MediaLink) -> Self {
        Self {
            id: ItemId::Direct,
            title: None,
            page_url,
            duration_secs: None,
            thumbnail: None,
            formats: vec![link],
        }
    }
}

/// Where a page title came from — a shell `<title>` is weaker than og/JSON.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TitleSource {
    /// `og:title` / `twitter:title` / schema.org itemprop.
    Meta,
    /// JSON-LD `name`.
    JsonLd,
    /// A dedicated `video_title`/`media_title` in a player config.
    JsonBlob,
    /// A generic JSON `title` that may name the site.
    JsonGeneric,
    /// Visible `<h1>`.
    H1,
    /// Document `<title>` — often the app shell, not the content.
    DocumentTitle,
    /// KVS `video_title`.
    Kvs,
}

/// Page-level metadata that survives even when no media item is found.
///
/// The extension fallback path reuses these fields instead of re-extracting a
/// title or thumbnail from the live DOM.
#[derive(Debug, Clone, Default, PartialEq, Serialize)]
pub struct PageInfo {
    /// Clean page title, when the page exposed one.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    /// Source of [`title`](Self::title), so a caller can prefer a live DOM
    /// title over a generic shell `<title>`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title_source: Option<TitleSource>,
    /// Poster/thumbnail URL, when the page exposed one.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub thumbnail: Option<Url>,
    /// Content duration in seconds, when known.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub duration_secs: Option<f64>,
}

/// Delivery mechanism of a media address.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum MediaKind {
    /// A single downloadable file.
    Progressive,
    /// An HLS manifest.
    Hls,
    /// A DASH manifest.
    Dash,
    /// A single audio file.
    Audio,
}

/// Discovery layer that produced a link.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SourceLayer {
    /// `<video>`, `<source>`, `<a>`, `<object>`, `<embed>` or a data attribute.
    Html,
    /// A JSON blob embedded in a script or data attribute.
    JsonBlob,
    /// A schema.org JSON-LD object.
    JsonLd,
    /// An `og:*` or `twitter:*` meta tag.
    Meta,
    /// The last-resort regex scan over raw source.
    Regex,
    /// The input URL itself, or a fetched manifest/media response.
    Direct,
}

/// Resolution and bitrate hints for one address.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct Quality {
    /// Vertical resolution in pixels.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub height: Option<u32>,
    /// Horizontal resolution in pixels.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub width: Option<u32>,
    /// Declared bitrate in bits per second.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub bandwidth: Option<u64>,
    /// Player-provided label such as `720p` or `hd`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
}

/// Request headers the downloader must replay for a link.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct HeaderHints {
    /// Page URL to send as `Referer`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub referer: Option<String>,
}

impl HeaderHints {
    /// Whether no hint is set, so the field can be omitted from JSON.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.referer.is_none()
    }
}

/// A candidate media address extracted from the page, to be probed by yt-dlp.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct MediaLink {
    /// Absolute media address.
    pub url: Url,
    /// Delivery mechanism.
    pub kind: MediaKind,
    /// Resolution/bitrate hints, when the page carried them.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub quality: Option<Quality>,
    /// Container named by the URL or a mime attribute.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub container: Option<String>,
    /// Video/audio codecs declared by the page (`avc1.64001F, mp4a.40.2`).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub codecs: Option<String>,
    /// Discovery layer that produced this link.
    pub source: SourceLayer,
    /// Headers the downloader must replay.
    #[serde(skip_serializing_if = "HeaderHints::is_empty")]
    pub headers: HeaderHints,
    /// Unix timestamp from a signed-URL expiry parameter, when present.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expires_at: Option<i64>,
    /// Which `<video>`/`<audio>` element this link came from; grouping only.
    #[serde(skip)]
    pub(crate) media_index: Option<u32>,
}

impl MediaLink {
    /// Link with no quality, container, header or expiry hints yet.
    #[must_use]
    pub fn new(url: Url, kind: MediaKind, source: SourceLayer) -> Self {
        Self {
            url,
            kind,
            quality: None,
            container: None,
            codecs: None,
            source,
            headers: HeaderHints::default(),
            expires_at: None,
            media_index: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn item_id_serializes_to_python_spelling() {
        assert_eq!(serde_json::to_value(ItemId::Page).unwrap(), "page");
        assert_eq!(serde_json::to_value(ItemId::Direct).unwrap(), "direct");
        assert_eq!(serde_json::to_value(ItemId::Media(2)).unwrap(), "media-2");
        assert_eq!(ItemId::Media(2).to_string(), "media-2");
    }

    #[test]
    fn media_link_omits_empty_hints() {
        let link = MediaLink::new(
            Url::parse("https://cdn.x/v.mp4").unwrap(),
            MediaKind::Progressive,
            SourceLayer::Html,
        );
        let json = serde_json::to_string(&link).unwrap();
        assert!(!json.contains("headers"), "{json}");
        assert!(!json.contains("quality"), "{json}");
        assert!(!json.contains("codecs"), "{json}");
        assert!(!json.contains("media_index"), "{json}");
        assert!(json.contains("\"kind\":\"progressive\""), "{json}");
    }

    #[test]
    fn media_link_serializes_codecs() {
        let mut link = MediaLink::new(
            Url::parse("https://cdn.x/v.mp4").unwrap(),
            MediaKind::Progressive,
            SourceLayer::Html,
        );
        link.codecs = Some("avc1.64001F, mp4a.40.2".into());
        let json = serde_json::to_string(&link).unwrap();
        assert!(
            json.contains("\"codecs\":\"avc1.64001F, mp4a.40.2\""),
            "{json}"
        );
    }
}
