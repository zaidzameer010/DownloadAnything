use crate::extract::candidates::Candidate;
use crate::model::SourceLayer;
use regex::Regex;
use scraper::{Html, Selector};
use std::sync::LazyLock;
use url::Url;

static META_SEL: LazyLock<Selector> = LazyLock::new(|| Selector::parse("meta").unwrap());
static LINK_SEL: LazyLock<Selector> =
    LazyLock::new(|| Selector::parse("link[itemprop], meta[itemprop]").unwrap());
static H1_SEL: LazyLock<Selector> = LazyLock::new(|| Selector::parse("h1").unwrap());
static TITLE_SEL: LazyLock<Selector> = LazyLock::new(|| Selector::parse("title").unwrap());

/// Clean a page title into what the user actually sees: collapse whitespace,
/// strip tube-site boilerplate and trailing site-name
/// suffixes ("Name | Site" — repeated separators handled).
pub fn clean_title(raw: &str, site_name: Option<&str>) -> String {
    static BOILERPLATE: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
            r"(?i)\s*[-–—|·~]\s*(?:watch\s+)?(?:free\s+)?(?:hd\s+)?\s*(?:video|movie|clip)s*\s*$",
        )
        .unwrap()
    });
    // KVS-style host branding: "{title} - Brand.mp4 - Brand" — the brand is
    // repeated around a media extension and often differs from the site name.
    // Brand segments may not contain separators (no lookbehind in regex; a
    // dashed brand is a rare ceiling).
    static MEDIA_BRAND_PAIR: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
            r#"(?i)\s*[-–—|·~]\s*([a-z0-9 ._+]{1,60}?)\s*\.(?:mp4|mkv|avi|webm|mov|wmv|m4v|ts)\s*[-–—|·~]\s*([a-z0-9 ._+]{1,60})$"#,
        )
        .unwrap()
    });
    // Lone "{title} - Brand.mp4" tail — a media extension in a title is
    // boilerplate, not content.
    static MEDIA_EXT_TAIL: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(
            r#"(?i)\s*[-–—|·~]\s*[a-z0-9 ._+]{1,60}\.(?:mp4|mkv|avi|webm|mov|wmv|m4v|ts)\s*$"#,
        )
        .unwrap()
    });
    let site_re = site_name.and_then(|s| {
        Regex::new(&format!(
            r"(?i)\s*[-–—|·~:]\s*(?:www\.)?{}\s*$",
            regex::escape(s.trim())
        ))
        .ok()
    });
    let mut t = raw.split_whitespace().collect::<Vec<_>>().join(" ");
    for _ in 0..3 {
        let before = t.clone();
        t = BOILERPLATE.replace(&t, "").to_string();
        if let Some(re) = &site_re {
            t = re.replace(&t, "").to_string();
        }
        if let Some(c) = MEDIA_BRAND_PAIR.captures(&t)
            && norm_brand(&c[1]) == norm_brand(&c[2])
        {
            t = t[..c.get(0).unwrap().start()].trim_end().to_string();
        }
        t = MEDIA_EXT_TAIL.replace(&t, "").to_string();
        if t == before {
            break;
        }
    }
    t.trim().to_string()
}

static LINK_THUMB_SEL: LazyLock<Selector> = LazyLock::new(|| {
    Selector::parse("link[rel*='image_src'], link[itemprop='thumbnailUrl'], link[itemprop='image']")
        .unwrap()
});

/// Brand comparison key: lowercase, alphanumeric only
fn norm_brand(s: &str) -> String {
    s.chars()
        .filter(|c| c.is_alphanumeric())
        .collect::<String>()
        .to_lowercase()
}

/// Derive a site name for suffix stripping: og:site_name, else the host
fn site_name_of(og_site_name: Option<&str>, page_url: &Url) -> Option<String> {
    if let Some(s) = og_site_name.map(str::trim).filter(|s| !s.is_empty()) {
        return Some(s.to_string());
    }
    let host = page_url.host_str()?.trim_start_matches("www.").to_string();
    (!host.is_empty()).then_some(host)
}

/// True when a URL looks like a site asset, logo, avatar, UI icon, tracking pixel,
/// or non-content graphic rather than actual media content artwork.
pub fn is_junk_thumbnail(url: &str) -> bool {
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
        Ok(u) => u.path().to_ascii_lowercase(),
        Err(_) => lower.split('?').next().unwrap_or("").to_string(),
    };

    // Vector / icon file extensions are never video thumbnails.
    if path.ends_with(".svg") || path.ends_with(".ico") || path.ends_with(".cur") {
        return true;
    }

    // Keep in sync with dax/filter.py::_ASSET_TOKENS — a thumbnail the
    // ranker rejects must not be picked by discovery either.
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

    for tok in JUNK_TOKENS {
        if lower.contains(tok) {
            return true;
        }
    }

    // Common dimensions for small UI icons or tracking pixels
    const JUNK_DIMS: &[&str] = &["1x1", "16x16", "32x32", "48x48", "64x64"];
    for dim in JUNK_DIMS {
        if lower.contains(dim) {
            return true;
        }
    }

    false
}

/// Page-level metadata collected alongside candidates.
#[derive(Debug, Default, Clone)]
pub struct PageMeta {
    pub title: Option<String>,
    pub duration_secs: Option<f64>,
    pub thumbnail: Option<String>,
    /// Player embed pages (twitter:player et al) — escalated like iframes,
    /// not downloadable media.
    pub embeds: Vec<String>,
    /// Per-element video thumbnails (<video> 1-based index -> thumbnail URL).
    pub element_thumbnails: std::collections::BTreeMap<u32, String>,
}

/// og:video*, twitter:player, itemprop — and title/duration/thumbnail.
/// Title priority: og/twitter (clean, exact) → visible <h1> (gate overlays
/// skipped) → <title> with the site suffix stripped.
pub fn extract(doc: &Html, page_url: &Url) -> (Vec<Candidate>, PageMeta) {
    let mut out = Vec::new();
    let mut meta = PageMeta::default();
    let og_site_name = doc
        .select(&META_SEL)
        .find(|el| el.value().attr("property") == Some("og:site_name"))
        .and_then(|el| el.value().attr("content"));
    let site_name = site_name_of(og_site_name, page_url);
    for el in doc.select(&META_SEL) {
        let key = el
            .value()
            .attr("property")
            .or_else(|| el.value().attr("name"))
            .unwrap_or("")
            .to_lowercase();
        let content = el.value().attr("content").unwrap_or("");
        match key.as_str() {
            "og:video"
            | "og:video:url"
            | "og:video:secure_url"
            | "og:video:video"
            | "og:audio"
            | "twitter:player:stream"
            | "twitter:app:url:iphone" => {
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
                    meta.thumbnail = Some(content.to_string());
                }
            }
            "og:video:duration" | "video:duration" | "duration" => {
                let t = content.trim();
                if let Ok(d) = t.parse::<f64>() {
                    if d > 0.0 {
                        meta.duration_secs = Some(d);
                    }
                } else if let Some(d) = crate::extract::jsonld::iso8601_duration(t) {
                    meta.duration_secs = Some(d);
                }
            }
            "twitter:player" => {
                if !content.is_empty() {
                    meta.embeds.push(content.to_string());
                }
            }
            "contenturl" | "content_url"
                if el.value().attr("itemprop").is_some() && !content.is_empty() =>
            {
                out.push(Candidate::new(content, SourceLayer::Meta));
            }
            _ => {}
        }
    }
    for el in doc.select(&LINK_SEL) {
        if el.value().attr("itemprop") == Some("contentUrl")
            && let Some(href) = el
                .value()
                .attr("href")
                .or_else(|| el.value().attr("content"))
        {
            out.push(Candidate::new(href, SourceLayer::Meta));
        }
        if el.value().attr("itemprop") == Some("name")
            && let Some(c) = el.value().attr("content")
            && meta.title.is_none()
        {
            meta.title = Some(c.to_string());
        }
    }
    // link[rel=image_src] and itemprop thumbnail fallback
    if meta.thumbnail.is_none() {
        for el in doc.select(&LINK_THUMB_SEL) {
            if let Some(href) = el.value().attr("href")
                && !is_junk_thumbnail(href)
            {
                meta.thumbnail = Some(href.to_string());
                break;
            }
        }
    }
    // Visible <h1> fallback.
    if meta.title.is_none() {
        for el in doc.select(&H1_SEL) {
            let t = clean_title(&el.text().collect::<String>(), site_name.as_deref());
            if !t.is_empty() {
                meta.title = Some(t);
                break;
            }
        }
    }
    // <title> fallback (usually carries a site suffix — cleaned above).
    if meta.title.is_none()
        && let Some(el) = doc.select(&TITLE_SEL).next()
    {
        let t = clean_title(&el.text().collect::<String>(), site_name.as_deref());
        if !t.is_empty() {
            meta.title = Some(t);
        }
    }
    (out, meta)
}
