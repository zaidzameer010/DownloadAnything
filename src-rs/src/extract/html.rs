//! HTML layer: media elements, legacy players, posters and iframe embeds.

use std::collections::BTreeMap;
use std::sync::LazyLock;

use scraper::{ElementRef, Html, Selector};
use url::Url;

use crate::extract::candidates::{
    Candidate, codecs_from_mime, container_from_mime, height_from_label, kind_from_mime,
    looks_media_str, normalize_codecs, resolution_dims,
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
static IFRAME_SEL: LazyLock<Selector> = LazyLock::new(|| selector("iframe"));
static LINK_HINT_SEL: LazyLock<Selector> = LazyLock::new(|| selector("link[rel][href]"));
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

    // 3. Children inside <video> (<source data-poster>).
    for child in element.children() {
        if let Some(child_element) = ElementRef::wrap(child)
            && let Some(poster) = attr_poster(&child_element)
        {
            return Some(poster);
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
        for image in parent_element.select(&POSTER_IMG_SEL).take(32) {
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
        for div in parent_element.select(&STANDALONE_POSTER_SEL).take(32) {
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

/// Dimensions from a `res`/`size` attribute: `1280x720`, `720p` or a bare
/// numeric height (`size="720"`, Video.js style).
fn resolution_or_height(text: &str) -> (Option<u32>, Option<u32>) {
    let (width, height) = resolution_dims(text);
    if height.is_some() {
        return (width, height);
    }
    let bare = text
        .trim()
        .parse::<u32>()
        .ok()
        .filter(|height| (144..=4320).contains(height));
    (width, bare)
}

/// Quality hints carried by a `<video>`/`<source>` element — width/height
/// attributes, `res`/`data-res`/`data-quality` labels and the source `label`.
fn media_quality(element: &ElementRef<'_>) -> Option<Quality> {
    let (res_width, res_height) = ["res", "data-res", "data-quality", "data-size", "size"]
        .iter()
        .find_map(|name| element.value().attr(name).map(resolution_or_height))
        .unwrap_or_default();
    let height = dim_attr(element, &["height", "data-height"])
        .or(res_height)
        .or_else(|| {
            element
                .value()
                .attr("data-quality")
                .and_then(height_from_label)
        })
        .or_else(|| element.value().attr("res").and_then(height_from_label))
        .or_else(|| element.value().attr("label").and_then(height_from_label));
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

/// Codecs declared by a `<video>`/`<source>` element — the mime `type`
/// parameter (`video/mp4; codecs="avc1.64001F, mp4a.40.2"`) or a codec
/// attribute some custom players add next to the address.
fn media_codecs(element: &ElementRef<'_>) -> Option<String> {
    if let Some(codecs) = element.value().attr("type").and_then(codecs_from_mime) {
        return Some(codecs);
    }
    if let Some(codecs) = ["data-codecs", "data-codec", "codecs"]
        .iter()
        .find_map(|name| element.value().attr(name).and_then(normalize_codecs))
    {
        return Some(codecs);
    }
    let parts: Vec<String> = ["vcodec", "data-vcodec", "acodec", "data-acodec"]
        .iter()
        .filter_map(|name| element.value().attr(name).and_then(normalize_codecs))
        .collect();
    normalize_codecs(&parts.join(", "))
}

/// Apply a media element's resolution/type/codec hints to one of its candidates.
fn apply_media_hints(element: &ElementRef<'_>, candidate: &mut Candidate) {
    if let Some(quality) = media_quality(element) {
        candidate.quality = Some(quality);
    }
    candidate.codecs = media_codecs(element);
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
        let element_codecs = media_codecs(&element);
        for (name, value) in element.value().attrs() {
            if media_data_attr(&element, name, value) {
                let mut candidate = Candidate::new(value, SourceLayer::Html);
                candidate.media_index = index;
                candidate.quality = element_quality.clone();
                candidate.codecs = element_codecs.clone();
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
        // Link-heavy pages (nav, footer, pagination) otherwise pay a Url
        // parse per anchor just to reject it.
        if !looks_media_str(href) {
            continue;
        }
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

    // <iframe> players to escalate into. `data-src` is the lazy-load form
    // most embed hosts ship — the browser fills `src` only on intersection.
    for element in document.select(&IFRAME_SEL) {
        if pass.iframes.len() >= MAX_IFRAMES {
            break;
        }
        let Some(src) = element
            .value()
            .attr("src")
            .or_else(|| element.value().attr("data-src"))
        else {
            continue;
        };
        if let Ok(url) = pass.base.join(src)
            && matches!(url.scheme(), "http" | "https")
        {
            pass.iframes.push(url);
        }
    }

    // <link> hints: a preloaded media file or an alternate manifest feed.
    // `as=font|image|script` and non-media `type`s never reach the candidate
    // set, so ordinary resource hints cost one attribute read.
    for element in document.select(&LINK_HINT_SEL) {
        let Some(href) = element.value().attr("href") else {
            continue;
        };
        let kind = element.value().attr("as").unwrap_or("");
        let media_type = element
            .value()
            .attr("type")
            .is_some_and(|mime| kind_from_mime(mime).is_some());
        if matches!(kind, "video" | "audio") || media_type {
            pass.candidates
                .push(Candidate::new(href, SourceLayer::Html));
        }
    }

    // data-* sweep across every element (config injection points).
    // <video>/<audio>/<source> data-* were already captured above with their
    // media_index — re-emitting them here as page-level candidates would
    // fold the element row into the page item and lose playlist grouping.
    for element in document.select(&ALL_SEL) {
        let tag = element.value().name();
        if matches!(tag, "video" | "audio" | "source") {
            continue;
        }
        for (name, value) in element.value().attrs() {
            if media_data_attr(&element, name, value) {
                pass.candidates
                    .push(Candidate::new(value, SourceLayer::Html));
            }
        }
    }

    pass
}

/// A `data-*` attribute naming a media address.
///
/// JSON blobs belong to the JSON layer, and a storage-relative path needs its
/// host base before it can resolve.
fn media_data_attr(element: &ElementRef<'_>, name: &str, value: &str) -> bool {
    name.starts_with("data-")
        && !value.starts_with('{')
        && !value.starts_with('[')
        && looks_media_str(value)
        && !storage_relative(element, value)
}

/// Storage-relative `data-url` (R2/S3) is not page-relative; without its
/// host base it resolves to a 404 on the page host.
fn storage_relative(element: &ElementRef<'_>, value: &str) -> bool {
    element.value().attr("data-host").is_some_and(|host| {
        !host.trim().is_empty()
            && !value.starts_with("http://")
            && !value.starts_with("https://")
            && !value.starts_with("//")
    })
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
                 <source src="https://cdn.x/v720.mp4" res="720p" label="720p" type='video/mp4; codecs="avc1.64001F, mp4a.40.2"'>
                 <source src="https://cdn.x/v1080" data-res="1080p" type='video/webm; codecs="vp09.00.10.08, opus"'>
                 <source src="https://cdn.x/v480.mp4" size="480" type="video/mp4">
                 <source src="https://cdn.x/stream/360" label="360p" type="video/mp4">
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
        assert_eq!(link_720.codecs.as_deref(), Some("avc1.64001F, mp4a.40.2"));
        let link_1080 = by("v1080");
        assert_eq!(link_1080.quality.as_ref().unwrap().height, Some(1080));
        assert_eq!(link_1080.container.as_deref(), Some("webm"));
        assert_eq!(link_1080.codecs.as_deref(), Some("vp09.00.10.08, opus"));
        assert_eq!(
            by("v480.mp4").quality.as_ref().unwrap().height,
            Some(480),
            "a bare numeric size names the rendition height"
        );
        assert_eq!(
            by("stream/360").quality.as_ref().unwrap().height,
            Some(360),
            "a quality label names the height for an extensionless source"
        );
    }

    #[test]
    fn codec_attributes_apply_when_the_mime_has_none() {
        let document = Html::parse_document(
            r#"<video src="https://cdn.x/v.mp4" data-codecs="av01.0.05M.08, opus"></video>
               <video src="https://cdn.x/h.mp4" vcodec="hvc1.1.6.L93" acodec="mp4a.40.2"></video>"#,
        );
        let pass = run(&document, &base());
        let links: Vec<_> = pass
            .candidates
            .iter()
            .filter_map(|candidate| resolve(&base(), candidate))
            .collect();
        assert_eq!(links[0].codecs.as_deref(), Some("av01.0.05M.08, opus"));
        assert_eq!(links[1].codecs.as_deref(), Some("hvc1.1.6.L93, mp4a.40.2"));
    }

    #[test]
    fn storage_relative_data_urls_are_not_page_candidates() {
        let document = Html::parse_document(
            r#"<span data-host="R2" data-url="lessons/2026/10/a.mp4">lesson</span>
               <span data-url="https://cdn.x/b.mp4">absolute</span>
               <span data-url="c.mp4">page relative</span>"#,
        );
        let pass = run(&document, &base());
        let raws: Vec<&str> = pass
            .candidates
            .iter()
            .map(|candidate| candidate.raw.as_str())
            .collect();
        assert!(!raws.iter().any(|raw| raw.contains("a.mp4")), "{raws:?}");
        assert!(raws.iter().any(|raw| raw.contains("b.mp4")), "{raws:?}");
        assert!(raws.contains(&"c.mp4"), "{raws:?}");
    }

    #[test]
    fn data_json_attributes_are_not_url_candidates() {
        // JSON blobs are the JSON layer's job; the URL layer must not push
        // the whole object into candidate resolution.
        let document = Html::parse_document(
            r#"<div data-setup='{"sources":[{"file":"https://cdn.x/v.mp4"}]}'></div>"#,
        );
        let pass = run(&document, &base());
        assert!(pass.candidates.is_empty(), "{:?}", pass.candidates);
    }

    #[test]
    fn anchor_scan_prefilters_non_media_links() {
        let document = Html::parse_document(
            r#"<a href="/about">about</a>
               <a href="/manifest/v1?token=a">manifest</a>
               <a href="/v.mp4">video</a>"#,
        );
        let pass = run(&document, &base());
        let raws: Vec<&str> = pass
            .candidates
            .iter()
            .map(|candidate| candidate.raw.as_str())
            .collect();
        assert_eq!(raws.len(), 2, "{raws:?}");
        assert!(raws.contains(&"/manifest/v1?token=a"));
        assert!(raws.contains(&"/v.mp4"));
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

    #[test]
    fn storyboard_track_is_never_a_poster() {
        // Mux/Video.js players publish a `thumbnails` track — a WebVTT
        // sprite index, not artwork. The player's own poster img must win.
        let document = Html::parse_document(
            r#"<div class="player">
                 <video src="https://cdn.x/v.mp4">
                   <track kind="metadata" label="thumbnails" src="https://cdn.x/storyboard.vtt" default>
                 </video>
                 <div class="media-poster"><img class="media-poster-image" src="https://cdn.x/poster.webp"></div>
               </div>"#,
        );
        let pass = run(&document, &base());
        assert_eq!(pass.thumbnail.as_deref(), Some("https://cdn.x/poster.webp"));
    }

    #[test]
    fn source_data_src_stays_in_its_element_group() {
        // The page-wide data-* sweep must not re-emit a <source data-src>:
        // a page-level duplicate folds the element row into the page item.
        let document =
            Html::parse_document(r#"<video><source data-src="https://cdn.x/only.mp4"></video>"#);
        let pass = run(&document, &base());
        assert_eq!(pass.candidates.len(), 1, "{:?}", pass.candidates);
        assert_eq!(pass.candidates[0].media_index, Some(1));
    }

    #[test]
    fn lazy_iframes_and_preload_hints_are_discovered() {
        let document = Html::parse_document(
            r#"<iframe data-src="https://player.x/embed/1"></iframe>
               <link rel="preload" as="video" href="https://cdn.x/preloaded.mp4">
               <link rel="alternate" type="application/x-mpegURL" href="https://cdn.x/live/index.m3u8">
               <link rel="preload" as="font" href="https://cdn.x/font.woff2">"#,
        );
        let pass = run(&document, &base());
        assert_eq!(
            pass.iframes,
            vec![Url::parse("https://player.x/embed/1").unwrap()]
        );
        let raws: Vec<&str> = pass
            .candidates
            .iter()
            .map(|candidate| candidate.raw.as_str())
            .collect();
        assert!(raws.contains(&"https://cdn.x/preloaded.mp4"), "{raws:?}");
        assert!(raws.contains(&"https://cdn.x/live/index.m3u8"), "{raws:?}");
        assert!(
            !raws.iter().any(|raw| raw.contains("font.woff2")),
            "{raws:?}"
        );
    }
}
