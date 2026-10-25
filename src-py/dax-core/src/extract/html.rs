//! HTML layer: media elements, legacy players, posters and iframe embeds.

use std::collections::BTreeMap;
use std::sync::LazyLock;

use scraper::{ElementRef, Html, Selector};
use url::Url;

use crate::extract::candidates::{
    Candidate, container_from_mime, height_from_label, kind_from_mime, looks_media_str,
    resolution_dims,
};
use crate::extract::meta::is_junk_thumbnail;
use crate::extract::selector;
use crate::input;
use crate::model::{MediaKind, Quality, SourceLayer};

static BASE_SEL: LazyLock<Selector> = LazyLock::new(|| selector("base[href]"));
static MEDIA_SEL: LazyLock<Selector> = LazyLock::new(|| selector("video, audio"));
static SOURCE_SEL: LazyLock<Selector> = LazyLock::new(|| selector("source"));
static A_SEL: LazyLock<Selector> = LazyLock::new(|| selector("a[href]"));
static OBJECT_SEL: LazyLock<Selector> = LazyLock::new(|| selector("object"));
static EMBED_SEL: LazyLock<Selector> = LazyLock::new(|| selector("embed"));
static IFRAME_SEL: LazyLock<Selector> = LazyLock::new(|| selector("iframe[src]"));
static ALL_SEL: LazyLock<Selector> = LazyLock::new(|| selector("*"));
static PARAM_SEL: LazyLock<Selector> = LazyLock::new(|| selector("param"));

static POSTER_IMG_SEL: LazyLock<Selector> = LazyLock::new(|| selector("img[src], img[data-src]"));
static STANDALONE_POSTER_SEL: LazyLock<Selector> = LazyLock::new(|| {
    selector(
        ".vjs-poster, .plyr__poster, .jw-preview, .jw-poster, .fp-poster, [class*='player'] [class*='poster'], [class*='player'] img[class*='preview'], [class*='player'] img[class*='thumb'], [class*='video'] [class*='poster'], [class*='video'] img[class*='preview']",
    )
});

/// Iframes escalated from one page; more are player noise, not media.
const MAX_IFRAMES: usize = 8;

/// Attributes that may carry a player poster.
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

/// What the HTML layer found on one document.
#[derive(Debug)]
pub(crate) struct HtmlPass {
    /// Candidate addresses in document order.
    pub candidates: Vec<Candidate>,
    /// Embedded player pages worth escalating into.
    pub iframes: Vec<Url>,
    /// Effective base URL (honoring `<base href>`).
    pub base: Url,
    /// First poster found, used as the page-level thumbnail.
    pub thumbnail: Option<String>,
    /// Per-element video thumbnails (`<video>` 1-based index → URL).
    pub element_thumbnails: BTreeMap<u32, String>,
}

/// Extract the image URL from a CSS `style` attribute (`background-image: url(...)`).
fn extract_css_url(style: &str) -> Option<String> {
    let lower = style.to_ascii_lowercase();
    let position = lower.find("url(")?;
    let after = &style[position + 4..];
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
    (!inner.is_empty()).then(|| inner.to_owned())
}

/// Trimmed poster URL that is not site chrome.
fn valid_poster(value: &str) -> Option<String> {
    let trimmed = value.trim();
    (!trimmed.is_empty() && !is_junk_thumbnail(trimmed)).then(|| trimmed.to_owned())
}

/// Poster from a CSS background image on the element itself.
fn css_bg_poster(element: &ElementRef<'_>) -> Option<String> {
    element
        .value()
        .attr("style")
        .and_then(extract_css_url)
        .filter(|url| !is_junk_thumbnail(url))
}

/// First non-junk poster attribute on the element.
fn attr_poster(element: &ElementRef<'_>) -> Option<String> {
    VIDEO_POSTER_ATTRS
        .iter()
        .find_map(|attr| element.value().attr(attr).and_then(valid_poster))
}

/// Preview/poster thumbnail for a video element, its container or its children.
fn extract_video_thumbnail(element: &ElementRef<'_>) -> Option<String> {
    // 1. Direct attributes on <video>.
    if let Some(poster) = attr_poster(element) {
        return Some(poster);
    }

    // 2. CSS background-image on <video> itself.
    if let Some(url) = css_bg_poster(element) {
        return Some(url);
    }

    // 3. Children inside <video> (<track kind="thumbnails"> or <source data-poster>).
    for child in element.children() {
        if let Some(child_element) = ElementRef::wrap(child) {
            if let Some(poster) = attr_poster(&child_element) {
                return Some(poster);
            }
            if child_element.value().name() == "track"
                && matches!(
                    child_element.value().attr("kind"),
                    Some("thumbnails" | "metadata")
                )
                && let Some(src) = child_element.value().attr("src").and_then(valid_poster)
            {
                return Some(src);
            }
        }
    }

    // 4. Ancestor player container (up to 4 levels up).
    let mut current = element.parent();
    for _ in 0..4 {
        let Some(parent_node) = current else { break };
        let Some(parent_element) = ElementRef::wrap(parent_node) else {
            current = parent_node.parent();
            continue;
        };

        // Container attributes (e.g. <div class="player" data-poster="...">).
        if let Some(poster) = attr_poster(&parent_element) {
            return Some(poster);
        }

        // Container style background-image.
        if let Some(url) = css_bg_poster(&parent_element) {
            return Some(url);
        }

        // Child preview/poster elements inside this container (.vjs-poster, img.poster).
        for image in parent_element.select(&POSTER_IMG_SEL) {
            let class_or_id = format!(
                "{} {}",
                image.value().attr("class").unwrap_or(""),
                image.value().attr("id").unwrap_or("")
            )
            .to_ascii_lowercase();
            if (class_or_id.contains("poster")
                || class_or_id.contains("preview")
                || class_or_id.contains("thumb")
                || class_or_id.contains("cover")
                || class_or_id.contains("splash"))
                && let Some(src) = image
                    .value()
                    .attr("src")
                    .or_else(|| image.value().attr("data-src"))
                    .and_then(valid_poster)
            {
                return Some(src);
            }
        }

        // Poster div with a background image inside this container.
        for div in parent_element.select(&STANDALONE_POSTER_SEL) {
            if let Some(url) = css_bg_poster(&div) {
                return Some(url);
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

        current = parent_node.parent();
    }

    None
}

/// Resolution attribute value, filtered to a plausible dimension.
fn dim_attr(element: &ElementRef<'_>, names: &[&str]) -> Option<u32> {
    names.iter().find_map(|name| {
        element.value().attr(name).and_then(|value| {
            value
                .trim()
                .parse::<u32>()
                .ok()
                .filter(|dimension| (16..=16_384).contains(dimension))
        })
    })
}

/// Quality hints carried by a `<video>`/`<source>` element — width/height
/// attributes, `res`/`data-res`/`data-quality` labels and the source `label`.
fn media_quality(element: &ElementRef<'_>) -> Option<Quality> {
    let (res_width, res_height) = ["res", "data-res", "data-quality", "data-size", "size"]
        .iter()
        .find_map(|name| element.value().attr(name).map(resolution_dims))
        .unwrap_or_default();
    let height = dim_attr(element, &["height", "data-height"])
        .or(res_height)
        .or_else(|| {
            element
                .value()
                .attr("data-quality")
                .and_then(height_from_label)
        })
        .or_else(|| element.value().attr("res").and_then(height_from_label));
    let width = dim_attr(element, &["width", "data-width"]).or(res_width);
    let label = element
        .value()
        .attr("label")
        .or_else(|| element.value().attr("res"))
        .or_else(|| element.value().attr("data-res"))
        .or_else(|| element.value().attr("data-quality"))
        .map(str::to_owned);
    (height.is_some() || width.is_some() || label.is_some()).then_some(Quality {
        height,
        width,
        bandwidth: None,
        label,
    })
}

/// Apply a media element's resolution/type hints to one of its candidates.
fn apply_media_hints(element: &ElementRef<'_>, candidate: &mut Candidate) {
    if let Some(quality) = media_quality(element) {
        candidate.quality = Some(quality);
    }
    if let Some(mime) = element.value().attr("type") {
        if let Some(kind) = kind_from_mime(mime) {
            candidate.kind_hint = Some(match kind {
                // An <audio> element stays audio even when its source names a
                // video container.
                MediaKind::Progressive if matches!(candidate.kind_hint, Some(MediaKind::Audio)) => {
                    MediaKind::Audio
                }
                _ => kind,
            });
        }
        candidate.container = container_from_mime(mime);
    }
}

/// Static HTML layer: video/source/audio/a/object/embed/iframe plus a data-* sweep.
pub(crate) fn run(document: &Html, base: &Url) -> HtmlPass {
    let mut pass = HtmlPass {
        candidates: Vec::new(),
        iframes: Vec::new(),
        base: base.clone(),
        thumbnail: None,
        element_thumbnails: BTreeMap::new(),
    };

    // <base href> changes relative resolution for everything below.
    if let Some(element) = document.select(&BASE_SEL).next()
        && let Some(href) = element.value().attr("href")
        && let Ok(resolved) = base.join(href)
    {
        pass.base = resolved;
    }

    // <video> / <audio> — each element is a potential playlist item.
    let mut media_elements = 0u32;
    for element in document.select(&MEDIA_SEL) {
        media_elements += 1;
        let index = Some(media_elements);
        let tag = element.value().name();
        if let Some(src) = element.value().attr("src") {
            let mut candidate = Candidate::new(src, SourceLayer::Html);
            candidate.media_index = index;
            out_kind_hint(tag, &mut candidate);
            apply_media_hints(&element, &mut candidate);
            pass.candidates.push(candidate);
        }
        if tag == "video"
            && let Some(thumbnail) = extract_video_thumbnail(&element)
        {
            pass.element_thumbnails
                .insert(media_elements, thumbnail.clone());
            if pass.thumbnail.is_none() {
                pass.thumbnail = Some(thumbnail);
            }
        }
        for source in element.select(&SOURCE_SEL) {
            for attr in ["src", "data-src"] {
                let Some(src) = source.value().attr(attr) else {
                    continue;
                };
                let mut candidate = Candidate::new(src, SourceLayer::Html);
                candidate.media_index = index;
                out_kind_hint(tag, &mut candidate);
                apply_media_hints(&source, &mut candidate);
                pass.candidates.push(candidate);
            }
        }
        let element_quality = media_quality(&element);
        for (name, value) in element.value().attrs() {
            if name.starts_with("data-") && looks_media_str(value) {
                let mut candidate = Candidate::new(value, SourceLayer::Html);
                candidate.media_index = index;
                candidate.quality = element_quality.clone();
                pass.candidates.push(candidate);
            }
        }
    }

    // Standalone player preview, when no <video> element carried a thumbnail.
    if pass.thumbnail.is_none() {
        for element in document.select(&STANDALONE_POSTER_SEL) {
            if let Some(style) = element.value().attr("style")
                && let Some(url) = extract_css_url(style)
                && !is_junk_thumbnail(&url)
            {
                pass.thumbnail = Some(url);
                break;
            }
            if let Some(src) = element
                .value()
                .attr("src")
                .or_else(|| element.value().attr("data-src"))
                && !src.trim().is_empty()
                && !is_junk_thumbnail(src.trim())
            {
                pass.thumbnail = Some(src.trim().to_owned());
                break;
            }
        }
    }

    // <a href> ending in a media extension or naming a manifest.
    for element in document.select(&A_SEL) {
        let Some(href) = element.value().attr("href") else {
            continue;
        };
        let Ok(url) = pass.base.join(href) else {
            continue;
        };
        if input::is_direct_media_url(&url) || input::looks_manifest_url(&url) {
            pass.candidates
                .push(Candidate::new(href, SourceLayer::Html));
        }
    }

    // Legacy players: <object data> / <object><param> / <embed src|flashvars>.
    for element in document.select(&OBJECT_SEL) {
        if let Some(data) = element.value().attr("data")
            && looks_media_str(data)
        {
            pass.candidates
                .push(Candidate::new(data, SourceLayer::Html));
        }
        for param in element.select(&PARAM_SEL) {
            let name = param.value().attr("name").unwrap_or("").to_lowercase();
            let Some(value) = param.value().attr("value") else {
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
    for element in document.select(&EMBED_SEL) {
        if let Some(src) = element.value().attr("src")
            && looks_media_str(src)
        {
            pass.candidates.push(Candidate::new(src, SourceLayer::Html));
        }
        if let Some(flashvars) = element.value().attr("flashvars") {
            push_flashvars(flashvars, &mut pass.candidates);
        }
    }

    // <iframe> players to escalate into.
    for element in document.select(&IFRAME_SEL) {
        if pass.iframes.len() >= MAX_IFRAMES {
            break;
        }
        let Some(src) = element.value().attr("src") else {
            continue;
        };
        if let Ok(url) = pass.base.join(src)
            && matches!(url.scheme(), "http" | "https")
        {
            pass.iframes.push(url);
        }
    }

    // data-* sweep across every element (config injection points).
    // <video>/<audio> data-* were already captured above with their
    // media_index — re-emitting them here as page-level candidates would
    // fold the element row into the page item and lose playlist grouping.
    for element in document.select(&ALL_SEL) {
        let tag = element.value().name();
        if tag == "video" || tag == "audio" {
            continue;
        }
        for (name, value) in element.value().attrs() {
            if name.starts_with("data-") && looks_media_str(value) {
                pass.candidates
                    .push(Candidate::new(value, SourceLayer::Html));
            }
        }
    }

    pass
}

/// An `<audio>` element asserts audio regardless of the source container.
fn out_kind_hint(parent_tag: &str, candidate: &mut Candidate) {
    if parent_tag == "audio" {
        candidate.kind_hint = Some(MediaKind::Audio);
    }
}

/// `flashvars="file=video.mp4&image=thumb.jpg"` → pull media-ish values.
fn push_flashvars(flashvars: &str, out: &mut Vec<Candidate>) {
    for pair in flashvars.split('&') {
        let Some((_, value)) = pair.split_once('=') else {
            continue;
        };
        let Ok(decoded) = urlencoding::decode(value) else {
            continue;
        };
        if looks_media_str(&decoded) {
            out.push(Candidate::new(decoded.into_owned(), SourceLayer::Html));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::extract::candidates::resolve;

    fn base() -> Url {
        Url::parse("https://site.com/watch").unwrap()
    }

    #[test]
    fn extracts_video_player_previews_and_container_posters() {
        // 1. Video inside a custom container with data-poster.
        let document = Html::parse_document(
            r#"
            <div class="video-player" data-poster="https://cdn.x/player_preview.jpg">
                <video src="https://cdn.x/video1.mp4"></video>
            </div>
        "#,
        );
        let pass = run(&document, &base());
        assert_eq!(
            pass.thumbnail.as_deref(),
            Some("https://cdn.x/player_preview.jpg")
        );
        assert_eq!(
            pass.element_thumbnails.get(&1).map(String::as_str),
            Some("https://cdn.x/player_preview.jpg")
        );

        // 2. Video.js container with a .vjs-poster background image.
        let document = Html::parse_document(
            r#"
            <div class="video-js vjs-default-skin">
                <div class="vjs-poster" style="background-image: url('https://cdn.x/vjs_poster.jpg');"></div>
                <video src="https://cdn.x/video2.mp4"></video>
            </div>
        "#,
        );
        let pass = run(&document, &base());
        assert_eq!(
            pass.thumbnail.as_deref(),
            Some("https://cdn.x/vjs_poster.jpg")
        );
        assert_eq!(
            pass.element_thumbnails.get(&1).map(String::as_str),
            Some("https://cdn.x/vjs_poster.jpg")
        );
    }

    #[test]
    fn source_ladder_carries_resolution_and_container() {
        let document = Html::parse_document(
            r#"<video width="1280" height="720">
                 <source src="https://cdn.x/v720.mp4" res="720p" label="720p" type="video/mp4">
                 <source src="https://cdn.x/v1080" data-res="1080p" type="video/webm">
               </video>"#,
        );
        let pass = run(&document, &base());
        let links: Vec<_> = pass
            .candidates
            .iter()
            .filter_map(|candidate| resolve(&base(), candidate))
            .collect();
        let by = |name: &str| {
            links
                .iter()
                .find(|link| link.url.as_str().ends_with(name))
                .unwrap_or_else(|| panic!("{name} missing: {links:?}"))
        };
        let link_720 = by("v720.mp4");
        assert_eq!(link_720.quality.as_ref().unwrap().height, Some(720));
        assert_eq!(link_720.container.as_deref(), Some("mp4"));
        let link_1080 = by("v1080");
        assert_eq!(link_1080.quality.as_ref().unwrap().height, Some(1080));
        assert_eq!(link_1080.container.as_deref(), Some("webm"));
    }

    #[test]
    fn base_href_changes_relative_resolution() {
        let document = Html::parse_document(
            r#"<base href="https://cdn.x/embed/">
               <video src="v.mp4"></video>"#,
        );
        let pass = run(&document, &base());
        assert_eq!(pass.base.as_str(), "https://cdn.x/embed/");
        let link = resolve(&pass.base, &pass.candidates[0]).unwrap();
        assert_eq!(link.url.as_str(), "https://cdn.x/embed/v.mp4");
    }

    #[test]
    fn css_url_handles_quotes_and_rejects_empty() {
        assert_eq!(
            extract_css_url("background-image: url('https://x/p.jpg')").as_deref(),
            Some("https://x/p.jpg")
        );
        assert_eq!(
            extract_css_url("background-image: url(https://x/p.jpg)").as_deref(),
            Some("https://x/p.jpg")
        );
        assert_eq!(extract_css_url("background-image: url('')"), None);
        assert_eq!(extract_css_url("color: red"), None);
    }

    #[test]
    fn junk_posters_are_never_selected() {
        let document = Html::parse_document(
            r#"<video src="https://cdn.x/v.mp4" poster="https://site.com/static/logo.png"></video>"#,
        );
        let pass = run(&document, &base());
        assert!(pass.thumbnail.is_none());
    }
}
