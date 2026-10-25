//! Page metadata layer: `og:*`/`twitter:*` tags, titles and posters.

use std::collections::BTreeMap;
use std::sync::LazyLock;

use aho_corasick::AhoCorasick;
use regex::Regex;
use scraper::{Html, Selector};
use url::Url;

use crate::extract::candidates::{Candidate, container_from_mime};
use crate::extract::selector;
use crate::model::{MediaKind, Quality, SourceLayer};

static META_SEL: LazyLock<Selector> = LazyLock::new(|| selector("meta"));
static LINK_SEL: LazyLock<Selector> = LazyLock::new(|| selector("link[itemprop], meta[itemprop]"));
static H1_SEL: LazyLock<Selector> = LazyLock::new(|| selector("h1"));
static TITLE_SEL: LazyLock<Selector> = LazyLock::new(|| selector("title"));
static LINK_THUMB_SEL: LazyLock<Selector> = LazyLock::new(|| {
    selector("link[rel*='image_src'], link[itemprop='thumbnailUrl'], link[itemprop='image']")
});

/// Tube-site boilerplate suffix (`Name - Watch Free HD Videos`).
static BOILERPLATE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?i)\s*[-–—|·~]\s*(?:watch\s+)?(?:free\s+)?(?:hd\s+)?\s*(?:video|movie|clip)s*\s*$",
    )
    .expect("boilerplate title regex")
});

/// KVS-style host branding: `{title} - Brand.mp4 - Brand` — the brand is
/// repeated around a media extension and often differs from the site name.
/// Brand segments may not contain separators (no lookbehind in regex; a
/// dashed brand is a rare ceiling).
static MEDIA_BRAND_PAIR: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r#"(?i)\s*[-–—|·~]\s*([a-z0-9 ._+]{1,60}?)\s*\.(?:mp4|mkv|webm|mov|m4v|ts)\s*[-–—|·~]\s*([a-z0-9 ._+]{1,60})$"#,
    )
    .expect("media brand pair regex")
});

/// Lone `{title} - Brand.mp4` tail — a media extension in a title is
/// boilerplate, not content.
static MEDIA_EXT_TAIL: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?i)\s*[-–—|·~]\s*[a-z0-9 ._+]{1,60}\.(?:mp4|mkv|webm|mov|m4v|ts)\s*$"#)
        .expect("media extension tail regex")
});

/// Tokens that mark a URL as site chrome rather than content artwork.
///
/// Keep in sync with `dax/filter.py::_ASSET_TOKENS` — a thumbnail the ranker
/// rejects must not be picked by discovery either.
const JUNK_TOKENS: &[&str] = &[
    "logo",
    "watermark",
    "brandmark",
    "favicon",
    "avatar",
    "user_pic",
    "user-pic",
    "user_avatar",
    "profile_pic",
    "author",
    "spinner",
    "loader",
    "loading",
    "spacer",
    "blank",
    "placeholder",
    "default_avatar",
    "default_thumb",
    "nothumb",
    "no_thumb",
    "no-thumb",
    "no_image",
    "no-image",
    "pixel",
    "tracking",
    "analytics",
    "badge",
    "header-logo",
    "site-logo",
    "apple-touch-icon",
    "play_button",
    "play_btn",
    "play-btn",
    "btn_play",
    "button",
    "btn",
    "ad-",
    "advert",
    "sponsor",
    "banner",
];

/// Dimensions common to UI icons and tracking pixels.
const JUNK_DIMS: &[&str] = &["1x1", "16x16", "32x32", "48x48", "64x64"];

/// One automaton pass over the lowercased URL instead of one scan per token.
static JUNK_TOKEN_AC: LazyLock<AhoCorasick> = LazyLock::new(|| {
    AhoCorasick::builder()
        .ascii_case_insensitive(true)
        .build(JUNK_TOKENS)
        .expect("junk thumbnail token automaton")
});

/// One automaton pass over the lowercased URL for junk dimensions.
static JUNK_DIM_AC: LazyLock<AhoCorasick> = LazyLock::new(|| {
    AhoCorasick::builder()
        .ascii_case_insensitive(true)
        .build(JUNK_DIMS)
        .expect("junk thumbnail dimension automaton")
});

/// Clean a page title into what the user actually sees: collapse whitespace,
/// strip tube-site boilerplate and trailing site-name suffixes (`Name | Site`,
/// repeated separators handled).
pub(crate) fn clean_title(raw: &str, site_name: Option<&str>) -> String {
    let site_re = site_name.and_then(|site| {
        Regex::new(&format!(
            r"(?i)\s*[-–—|·~:]\s*(?:www\.)?{}\s*$",
            regex::escape(site.trim())
        ))
        .ok()
    });
    let mut title = raw.split_whitespace().collect::<Vec<_>>().join(" ");
    for _ in 0..3 {
        let before = title.clone();
        title = BOILERPLATE.replace(&title, "").to_string();
        if let Some(regex) = &site_re {
            title = regex.replace(&title, "").to_string();
        }
        if let Some(captures) = MEDIA_BRAND_PAIR.captures(&title)
            && norm_brand(&captures[1]) == norm_brand(&captures[2])
            && let Some(matched) = captures.get(0)
        {
            title = title[..matched.start()].trim_end().to_owned();
        }
        title = MEDIA_EXT_TAIL.replace(&title, "").to_string();
        if title == before {
            break;
        }
    }
    title.trim().to_owned()
}

/// Brand comparison key: lowercase, alphanumeric only.
fn norm_brand(text: &str) -> String {
    text.chars()
        .filter(|c| c.is_alphanumeric())
        .collect::<String>()
        .to_lowercase()
}

/// Derive a site name for suffix stripping: `og:site_name`, else the host.
fn site_name_of(og_site_name: Option<&str>, page_url: &Url) -> Option<String> {
    if let Some(site) = og_site_name.map(str::trim).filter(|site| !site.is_empty()) {
        return Some(site.to_owned());
    }
    let host = page_url.host_str()?.trim_start_matches("www.").to_owned();
    (!host.is_empty()).then_some(host)
}

/// True when a URL looks like a site asset, logo, avatar, UI icon, tracking
/// pixel or non-content graphic rather than actual media artwork.
pub(crate) fn is_junk_thumbnail(url: &str) -> bool {
    let raw = url.trim();
    if raw.is_empty() {
        return true;
    }
    // Reject data URIs that are SVGs, 1x1 GIFs, or too short to be real images.
    if raw.starts_with("data:") {
        let lower = raw.to_ascii_lowercase();
        if lower.contains("svg") || lower.contains("gif") || raw.len() < 128 {
            return true;
        }
    }
    let lower = raw.to_ascii_lowercase();
    let path = match Url::parse(raw) {
        Ok(url) => url.path().to_ascii_lowercase(),
        Err(_) => lower.split('?').next().unwrap_or("").to_owned(),
    };

    // Vector and icon file extensions are never video thumbnails.
    if path.ends_with(".svg") || path.ends_with(".ico") || path.ends_with(".cur") {
        return true;
    }
    JUNK_TOKEN_AC.is_match(&lower) || JUNK_DIM_AC.is_match(&lower)
}

/// Page-level metadata collected alongside candidates.
#[derive(Debug, Default, Clone, PartialEq)]
pub(crate) struct PageMeta {
    /// Clean page title.
    pub title: Option<String>,
    /// Content duration in seconds.
    pub duration_secs: Option<f64>,
    /// Page-level thumbnail URL, still unresolved.
    pub thumbnail: Option<String>,
    /// Player embed pages (`twitter:player` et al) — escalated like iframes,
    /// not downloadable media.
    pub embeds: Vec<String>,
    /// Per-element video thumbnails (`<video>` 1-based index → URL).
    pub element_thumbnails: BTreeMap<u32, String>,
}

/// `og:video*`, `twitter:player`, `itemprop` — plus title/duration/thumbnail.
///
/// Title priority: og/twitter (clean, exact) → visible `<h1>` (gate overlays
/// skipped) → `<title>` with the site suffix stripped.
pub(crate) fn extract(document: &Html, page_url: &Url) -> (Vec<Candidate>, PageMeta) {
    let mut out = Vec::new();
    let mut meta = PageMeta::default();
    let og_site_name = document
        .select(&META_SEL)
        .find(|element| element.value().attr("property") == Some("og:site_name"))
        .and_then(|element| element.value().attr("content"));
    let site_name = site_name_of(og_site_name, page_url);

    // og:video metadata arrives as sibling tags — collect the URL(s) and the
    // width/height/type, then emit each candidate with its quality.
    let mut video_urls: Vec<&str> = Vec::new();
    let mut audio_urls: Vec<&str> = Vec::new();
    let mut og_width = None;
    let mut og_height = None;
    let mut og_type: Option<&str> = None;
    for element in document.select(&META_SEL) {
        let key = element
            .value()
            .attr("property")
            .or_else(|| element.value().attr("name"))
            .unwrap_or("")
            .to_lowercase();
        let content = element.value().attr("content").unwrap_or("");
        match key.as_str() {
            "og:video"
            | "og:video:url"
            | "og:video:secure_url"
            | "og:video:video"
            | "twitter:player:stream" => {
                if !content.is_empty() {
                    video_urls.push(content);
                }
            }
            "og:audio" | "og:audio:url" | "og:audio:secure_url" => {
                if !content.is_empty() {
                    audio_urls.push(content);
                }
            }
            "og:video:width" => og_width = content.trim().parse::<u32>().ok(),
            "og:video:height" => og_height = content.trim().parse::<u32>().ok(),
            "og:video:type" | "twitter:player:stream:content_type" => {
                if og_type.is_none() && !content.is_empty() {
                    og_type = Some(content);
                }
            }
            "twitter:app:url:iphone" => {
                if !content.is_empty() {
                    out.push(Candidate::new(content, SourceLayer::Meta));
                }
            }
            "og:title" | "twitter:title" => {
                if meta.title.is_none() && !content.is_empty() {
                    meta.title = Some(clean_title(content, site_name.as_deref()));
                }
            }
            "og:image" | "twitter:image" | "thumbnail" => {
                if meta.thumbnail.is_none() && !content.is_empty() && !is_junk_thumbnail(content) {
                    meta.thumbnail = Some(content.to_owned());
                }
            }
            "og:video:duration" | "video:duration" | "duration" => {
                let value = content.trim();
                if let Ok(duration) = value.parse::<f64>() {
                    if duration > 0.0 {
                        meta.duration_secs = Some(duration);
                    }
                } else if let Some(duration) = crate::extract::jsonld::iso8601_duration(value) {
                    meta.duration_secs = Some(duration);
                }
            }
            "twitter:player" => {
                if !content.is_empty() {
                    meta.embeds.push(content.to_owned());
                }
            }
            "contenturl" | "content_url"
                if element.value().attr("itemprop").is_some() && !content.is_empty() =>
            {
                out.push(Candidate::new(content, SourceLayer::Meta));
            }
            _ => {}
        }
    }
    let quality = (og_width.is_some() || og_height.is_some()).then_some(Quality {
        height: og_height,
        width: og_width,
        bandwidth: None,
        label: None,
    });
    let container = og_type.and_then(container_from_mime);
    for url in video_urls {
        let mut candidate = Candidate::new(url, SourceLayer::Meta);
        candidate.quality = quality.clone();
        candidate.container = container.clone();
        out.push(candidate);
    }
    for url in audio_urls {
        let mut candidate = Candidate::new(url, SourceLayer::Meta);
        candidate.kind_hint = Some(MediaKind::Audio);
        out.push(candidate);
    }
    for element in document.select(&LINK_SEL) {
        if element.value().attr("itemprop") == Some("contentUrl")
            && let Some(href) = element
                .value()
                .attr("href")
                .or_else(|| element.value().attr("content"))
        {
            out.push(Candidate::new(href, SourceLayer::Meta));
        }
        if element.value().attr("itemprop") == Some("name")
            && let Some(content) = element.value().attr("content")
            && meta.title.is_none()
        {
            meta.title = Some(content.to_owned());
        }
    }
    // link[rel=image_src] and itemprop thumbnail fallback.
    if meta.thumbnail.is_none() {
        for element in document.select(&LINK_THUMB_SEL) {
            if let Some(href) = element.value().attr("href")
                && !is_junk_thumbnail(href)
            {
                meta.thumbnail = Some(href.to_owned());
                break;
            }
        }
    }
    // Visible <h1> fallback.
    if meta.title.is_none() {
        for element in document.select(&H1_SEL) {
            let title = clean_title(&element.text().collect::<String>(), site_name.as_deref());
            if !title.is_empty() {
                meta.title = Some(title);
                break;
            }
        }
    }
    // <title> fallback (usually carries a site suffix — cleaned above).
    if meta.title.is_none()
        && let Some(element) = document.select(&TITLE_SEL).next()
    {
        let title = clean_title(&element.text().collect::<String>(), site_name.as_deref());
        if !title.is_empty() {
            meta.title = Some(title);
        }
    }
    (out, meta)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base() -> Url {
        Url::parse("https://site.com/watch").unwrap()
    }

    #[test]
    fn junk_thumbnail_filter_rejects_logos_and_icons() {
        // Real media thumbnails must pass.
        assert!(!is_junk_thumbnail(
            "https://site.com/contents/videos_screenshots/617000/617043/preview.jpg"
        ));
        assert!(!is_junk_thumbnail(
            "https://site.com/images/posters/movie_1080p.png"
        ));
        assert!(!is_junk_thumbnail("/uploads/thumbs/clip_preview.webp"));

        // Site chrome, logos, avatars, icons, SVGs and trackers must be rejected.
        for url in [
            "https://site.com/static/images/logo.png",
            "https://site.com/assets/vector-logo.svg",
            "https://site.com/assets/favicon.ico",
            "https://site.com/user/avatar_9921.jpg",
            "https://site.com/uploads/banners/promo_banner.jpg",
            "https://site.com/icons/play_button.png",
            "https://site.com/tracking/pixel_1x1.png",
            "https://site.com/assets/icon_32x32.png",
            "https://site.com/static/no_thumb.jpg",
            "https://site.com/images/default_avatar.png",
            "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7",
        ] {
            assert!(is_junk_thumbnail(url), "{url} must be junk");
        }
    }

    #[test]
    fn og_video_carries_resolution_and_container() {
        let document = Html::parse_document(
            r#"<meta property="og:video" content="https://cdn.x/v.mp4">
               <meta property="og:video:width" content="1920">
               <meta property="og:video:height" content="1080">
               <meta property="og:video:type" content="video/mp4">"#,
        );
        let (candidates, _) = extract(&document, &base());
        let candidate = candidates
            .iter()
            .find(|candidate| candidate.raw.contains("v.mp4"))
            .expect("og:video candidate");
        let quality = candidate.quality.as_ref().expect("og:video resolution");
        assert_eq!((quality.width, quality.height), (Some(1920), Some(1080)));
        assert_eq!(candidate.container.as_deref(), Some("mp4"));
    }

    #[test]
    fn title_priority_and_suffix_stripping() {
        let document = Html::parse_document(
            r#"<html><head>
                 <meta property="og:site_name" content="Site">
                 <meta property="og:title" content="Clean Name | Site">
                 <title>Ignored | Site</title>
               </head></html>"#,
        );
        let (_, meta) = extract(&document, &base());
        assert_eq!(meta.title.as_deref(), Some("Clean Name"));
    }

    #[test]
    fn clean_title_strips_kvs_branding() {
        assert_eq!(
            clean_title("Clip Name - Brand.mp4 - Brand", None),
            "Clip Name"
        );
        assert_eq!(
            clean_title("Clip Name - Watch Free HD Videos", None),
            "Clip Name"
        );
    }
}
