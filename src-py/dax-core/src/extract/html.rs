use crate::extract::candidates::{Candidate, looks_media_str};
use crate::input;
use crate::model::SourceLayer;
use scraper::{Html, Selector};
use std::sync::LazyLock;
use url::Url;

static BASE_SEL: LazyLock<Selector> = LazyLock::new(|| Selector::parse("base[href]").unwrap());
static MEDIA_SEL: LazyLock<Selector> = LazyLock::new(|| Selector::parse("video, audio").unwrap());
static SOURCE_SEL: LazyLock<Selector> = LazyLock::new(|| Selector::parse("source").unwrap());
static A_SEL: LazyLock<Selector> = LazyLock::new(|| Selector::parse("a[href]").unwrap());
static OBJECT_SEL: LazyLock<Selector> = LazyLock::new(|| Selector::parse("object").unwrap());
static EMBED_SEL: LazyLock<Selector> = LazyLock::new(|| Selector::parse("embed").unwrap());
static IFRAME_SEL: LazyLock<Selector> = LazyLock::new(|| Selector::parse("iframe[src]").unwrap());
static ALL_SEL: LazyLock<Selector> = LazyLock::new(|| Selector::parse("*").unwrap());
static PARAM_SEL: LazyLock<Selector> = LazyLock::new(|| Selector::parse("param").unwrap());

static POSTER_IMG_SEL: LazyLock<Selector> =
    LazyLock::new(|| Selector::parse("img[src], img[data-src]").unwrap());
static STANDALONE_POSTER_SEL: LazyLock<Selector> = LazyLock::new(|| {
    Selector::parse(".vjs-poster, .plyr__poster, .jw-preview, .jw-poster, .fp-poster, [class*='player'] [class*='poster'], [class*='player'] img[class*='preview'], [class*='player'] img[class*='thumb'], [class*='video'] [class*='poster'], [class*='video'] img[class*='preview']").unwrap()
});

const MAX_IFRAMES: usize = 8;

const VIDEO_POSTER_ATTRS: &[&str] = &[
    "poster",
    "data-poster",
    "data-thumb",
    "data-thumbnail",
    "data-preview",
    "data-preview-url",
    "data-poster-url",
    "data-image",
    "data-cover",
    "data-splash",
];

pub struct HtmlPass {
    pub candidates: Vec<Candidate>,
    /// Embedded player pages worth escalating into.
    pub iframes: Vec<Url>,
    /// <video>/<audio> element count (for playlist grouping).
    pub media_elements: u32,
    /// Effective base URL (honoring <base href>).
    pub base: Url,
    pub thumbnail: Option<String>,
    /// Per-element video thumbnails (<video> 1-based index -> thumbnail URL).
    pub element_thumbnails: std::collections::BTreeMap<u32, String>,
}

/// Extract image URL from CSS `style` attribute (e.g. `background-image: url('...')`).
fn extract_css_url(style: &str) -> Option<String> {
    let lower = style.to_ascii_lowercase();
    let pos = lower.find("url(")?;
    let after = &style[pos + 4..];
    let end = after.find(')')?;
    let mut inner = after[..end].trim();
    if (inner.starts_with('"') && inner.ends_with('"'))
        || (inner.starts_with('\'') && inner.ends_with('\''))
    {
        if inner.len() < 2 {
            return None;
        }
        inner = inner[1..inner.len() - 1].trim();
    }
    if !inner.is_empty() {
        Some(inner.to_string())
    } else {
        None
    }
}

fn valid_poster(val: &str) -> Option<String> {
    let trimmed = val.trim();
    (!trimmed.is_empty() && !crate::extract::meta::is_junk_thumbnail(trimmed))
        .then(|| trimmed.to_string())
}

fn css_bg_poster(el: &scraper::ElementRef) -> Option<String> {
    el.value()
        .attr("style")
        .and_then(extract_css_url)
        .filter(|u| !crate::extract::meta::is_junk_thumbnail(u))
}

fn attr_poster(el: &scraper::ElementRef) -> Option<String> {
    VIDEO_POSTER_ATTRS
        .iter()
        .find_map(|attr| el.value().attr(attr).and_then(valid_poster))
}

/// Detect and extract preview/poster thumbnail for a video element, its container, or children.
fn extract_video_thumbnail(el: &scraper::ElementRef) -> Option<String> {
    // 1. Direct attributes on <video>
    if let Some(poster) = attr_poster(el) {
        return Some(poster);
    }

    // 2. CSS background-image on <video> itself
    if let Some(u) = css_bg_poster(el) {
        return Some(u);
    }

    // 3. Child elements inside <video> (e.g. <track kind="thumbnails"> or <source data-poster="...">)
    for child in el.children() {
        if let Some(child_el) = scraper::ElementRef::wrap(child) {
            if let Some(poster) = attr_poster(&child_el) {
                return Some(poster);
            }
            if child_el.value().name() == "track"
                && matches!(
                    child_el.value().attr("kind"),
                    Some("thumbnails" | "metadata")
                )
                && let Some(src) = child_el.value().attr("src").and_then(valid_poster)
            {
                return Some(src);
            }
        }
    }

    // 4. Ancestor player container (up to 4 levels up)
    let mut cur = el.parent();
    for _ in 0..4 {
        let Some(parent_node) = cur else { break };
        let Some(parent_el) = scraper::ElementRef::wrap(parent_node) else {
            cur = parent_node.parent();
            continue;
        };

        // Container attributes (e.g. <div class="player" data-poster="...">)
        if let Some(poster) = attr_poster(&parent_el) {
            return Some(poster);
        }

        // Container style background-image
        if let Some(u) = css_bg_poster(&parent_el) {
            return Some(u);
        }

        // Child preview/poster elements inside this container (e.g. .vjs-poster, img.poster)
        for img in parent_el.select(&POSTER_IMG_SEL) {
            let class_or_id = format!(
                "{} {}",
                img.value().attr("class").unwrap_or(""),
                img.value().attr("id").unwrap_or("")
            )
            .to_ascii_lowercase();
            if (class_or_id.contains("poster")
                || class_or_id.contains("preview")
                || class_or_id.contains("thumb")
                || class_or_id.contains("cover")
                || class_or_id.contains("splash"))
                && let Some(src) = img
                    .value()
                    .attr("src")
                    .or_else(|| img.value().attr("data-src"))
                    .and_then(valid_poster)
            {
                return Some(src);
            }
        }

        // Poster div with background image inside this container
        for div in parent_el.select(&STANDALONE_POSTER_SEL) {
            if let Some(u) = css_bg_poster(&div) {
                return Some(u);
            }
            if let Some(src) = div
                .value()
                .attr("src")
                .or_else(|| div.value().attr("data-src"))
                .and_then(valid_poster)
            {
                return Some(src);
            }
        }

        cur = parent_node.parent();
    }

    None
}

/// Static HTML layer: video/source/audio/a/object/embed/iframe + data-* sweep.
pub fn run(doc: &Html, base: &Url) -> HtmlPass {
    let mut pass = HtmlPass {
        candidates: Vec::new(),
        iframes: Vec::new(),
        media_elements: 0,
        base: base.clone(),
        thumbnail: None,
        element_thumbnails: std::collections::BTreeMap::new(),
    };

    // <base href> changes relative resolution for everything below.
    if let Some(b) = doc.select(&BASE_SEL).next()
        && let Some(href) = b.value().attr("href")
        && let Ok(resolved) = base.join(href)
    {
        pass.base = resolved;
    }

    // <video> / <audio> — each element is a potential playlist item.
    for el in doc.select(&MEDIA_SEL) {
        pass.media_elements += 1;
        let idx = Some(pass.media_elements);
        let tag = el.value().name();
        if let Some(src) = el.value().attr("src") {
            let mut c = Candidate::new(src, SourceLayer::Html);
            c.media_index = idx;
            out_kind_hint(tag, &mut c);
            pass.candidates.push(c);
        }
        if tag == "video"
            && let Some(th) = extract_video_thumbnail(&el)
        {
            pass.element_thumbnails
                .insert(pass.media_elements, th.clone());
            if pass.thumbnail.is_none() {
                pass.thumbnail = Some(th);
            }
        }
        for src in el.select(&SOURCE_SEL) {
            if let Some(s) = src.value().attr("src") {
                let mut c = Candidate::new(s, SourceLayer::Html);
                c.media_index = idx;
                out_kind_hint(tag, &mut c);
                if let Some(l) = src.value().attr("label") {
                    c.label = Some(l.to_string());
                }
                pass.candidates.push(c);
            }
        }
        for (name, value) in el.value().attrs() {
            if name.starts_with("data-") && looks_media_str(value) {
                let mut c = Candidate::new(value, SourceLayer::Html);
                c.media_index = idx;
                pass.candidates.push(c);
            }
        }
    }

    // Standalone video player preview check if no <video> element carried a thumbnail
    if pass.thumbnail.is_none() {
        for el in doc.select(&STANDALONE_POSTER_SEL) {
            if let Some(style) = el.value().attr("style")
                && let Some(u) = extract_css_url(style)
                && !crate::extract::meta::is_junk_thumbnail(&u)
            {
                pass.thumbnail = Some(u);
                break;
            }
            if let Some(src) = el
                .value()
                .attr("src")
                .or_else(|| el.value().attr("data-src"))
                && !src.trim().is_empty()
                && !crate::extract::meta::is_junk_thumbnail(src.trim())
            {
                pass.thumbnail = Some(src.trim().to_string());
                break;
            }
        }
    }

    // <a href> ending in a media extension.
    for el in doc.select(&A_SEL) {
        let Some(href) = el.value().attr("href") else {
            continue;
        };
        let Ok(u) = pass.base.join(href) else {
            continue;
        };
        if input::is_direct_media_url(&u) || input::looks_manifest_url(&u) {
            pass.candidates
                .push(Candidate::new(href, SourceLayer::Html));
        }
    }

    // Legacy players: <object data> / <object><param> / <embed src|flashvars>.
    for el in doc.select(&OBJECT_SEL) {
        if let Some(data) = el.value().attr("data")
            && looks_media_str(data)
        {
            pass.candidates
                .push(Candidate::new(data, SourceLayer::Html));
        }
        for p in el.select(&PARAM_SEL) {
            let name = p.value().attr("name").unwrap_or("").to_lowercase();
            let Some(value) = p.value().attr("value") else {
                continue;
            };
            if name == "flashvars" {
                push_flashvars(value, &mut pass.candidates);
            } else if matches!(name.as_str(), "movie" | "src" | "url" | "file" | "filename")
                && looks_media_str(value)
            {
                pass.candidates
                    .push(Candidate::new(value, SourceLayer::Html));
            }
        }
    }
    for el in doc.select(&EMBED_SEL) {
        if let Some(src) = el.value().attr("src")
            && looks_media_str(src)
        {
            pass.candidates.push(Candidate::new(src, SourceLayer::Html));
        }
        if let Some(fv) = el.value().attr("flashvars") {
            push_flashvars(fv, &mut pass.candidates);
        }
    }

    // <iframe> players to escalate into.
    for el in doc.select(&IFRAME_SEL) {
        if pass.iframes.len() >= MAX_IFRAMES {
            break;
        }
        let Some(src) = el.value().attr("src") else {
            continue;
        };
        if let Ok(u) = pass.base.join(src)
            && matches!(u.scheme(), "http" | "https")
        {
            pass.iframes.push(u);
        }
    }

    // data-* sweep across every element (config injection points).
    for el in doc.select(&ALL_SEL) {
        for (name, value) in el.value().attrs() {
            if name.starts_with("data-") && looks_media_str(value) {
                pass.candidates
                    .push(Candidate::new(value, SourceLayer::Html));
            }
        }
    }

    pass
}

fn out_kind_hint(parent_tag: &str, c: &mut Candidate) {
    if parent_tag == "audio" {
        c.kind_hint = Some(crate::model::MediaKind::Audio);
    }
}

/// flashvars="file=video.mp4&image=thumb.jpg" → pull media-ish values.
fn push_flashvars(fv: &str, out: &mut Vec<Candidate>) {
    for pair in fv.split('&') {
        let Some((_, v)) = pair.split_once('=') else {
            continue;
        };
        let Ok(decoded) = urlencoding::decode(v) else {
            continue;
        };
        if looks_media_str(&decoded) {
            out.push(Candidate::new(decoded.into_owned(), SourceLayer::Html));
        }
    }
}
