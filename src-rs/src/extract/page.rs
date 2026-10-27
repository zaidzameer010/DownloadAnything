//! HTML page orchestration: parallel layers, escalation waves and item grouping.

use std::borrow::Cow;
use std::collections::{BTreeMap, HashSet};

use futures::stream::StreamExt;
use scraper::Html;
use tracing::{debug, instrument};
use url::Url;

use crate::error::ExtractError;
use crate::extract::candidates::{self, Candidate};
use crate::extract::html::{self, HtmlPass};
use crate::extract::json_blobs::{self, Findings};
use crate::extract::jsonld;
use crate::extract::meta::{self, PageMeta};
use crate::extract::packer;
use crate::extract::regex_scan;
use crate::input::classify::{self, ContentKind};
use crate::model::{ItemId, MediaItem, MediaKind, MediaLink, PageInfo, SourceLayer};
use crate::net::{FetchResponse, Fetcher};

/// Iframes escalated from the main page per wave.
const MAX_WAVE_IFRAMES: usize = 8;

/// Extensionless manifest endpoints escalated per wave.
const MAX_WAVE_ENDPOINTS: usize = 4;

/// Nested iframes and endpoints escalated in the second wave.
const MAX_NESTED_TARGETS: usize = 8;

/// What the static layers produced for one page.
#[derive(Debug, Default)]
pub(crate) struct PageLayers {
    /// Candidate addresses from every layer.
    pub(crate) candidates: Vec<Candidate>,
    /// Page-level metadata merged across layers.
    pub(crate) meta: PageMeta,
    /// Embedded player pages worth escalating into.
    pub(crate) iframes: Vec<Url>,
}

/// One fetched sub-page and what its layers produced.
#[derive(Debug)]
struct Subpage {
    url: Url,
    candidates: Vec<Candidate>,
    meta: PageMeta,
    iframes: Vec<Url>,
}

impl Subpage {
    /// Empty result for a fetch or parse failure.
    fn empty(url: Url) -> Self {
        Self {
            url,
            candidates: Vec::new(),
            meta: PageMeta::default(),
            iframes: Vec::new(),
        }
    }

    /// Shift this subpage's element indexes past the parent page's, so an
    /// iframe `<video>` does not merge with an unrelated parent `<video>`.
    /// Returns the highest index it used.
    fn offset_media(&mut self, base: u32) -> u32 {
        let max = self
            .candidates
            .iter()
            .filter_map(|candidate| candidate.media_index)
            .max()
            .unwrap_or(0)
            .max(
                self.meta
                    .element_thumbnails
                    .keys()
                    .copied()
                    .max()
                    .unwrap_or(0),
            );
        if max == 0 {
            return 0;
        }
        for candidate in &mut self.candidates {
            if let Some(index) = candidate.media_index {
                candidate.media_index = Some(base + index);
            }
        }
        let thumbnails = std::mem::take(&mut self.meta.element_thumbnails);
        self.meta.element_thumbnails = thumbnails
            .into_iter()
            .map(|(index, thumbnail)| (base + index, thumbnail))
            .collect();
        max
    }
}

/// HTML page path: run all static layers, escalate iframes (two levels) and
/// manifest endpoints, then resolve candidates into grouped items.
///
/// Page metadata is returned even when nothing resolves to a link, so the
/// extension fallback can reuse a title or thumbnail Rust already found.
///
/// # Errors
///
/// Returns [`ExtractError::Parse`] when the blocking parse task fails.
#[instrument(skip_all, fields(url = %page_url))]
pub(crate) async fn extract_page(
    net: &Fetcher,
    page_url: &Url,
    response: &FetchResponse,
) -> Result<(Vec<MediaItem>, PageInfo), ExtractError> {
    // CPU-bound scraper parsing runs on the blocking pool — a huge page
    // must not stall the concurrent extraction tasks. (scraper::Html is
    // !Send and never lives across an .await.)
    let body = response.body.clone();
    let url = page_url.clone();
    let layers = tokio::task::spawn_blocking(move || {
        let text = decode_text(&body);
        page_layers(&url, &text)
    })
    .await
    .map_err(|error| ExtractError::Parse {
        layer: "page",
        detail: error.to_string(),
    })?;
    debug!(
        count = layers.candidates.len(),
        "static layers produced candidates"
    );

    let PageLayers {
        candidates,
        mut meta,
        iframes,
    } = layers;

    // Manifest-ish endpoints with no file extension (`/media/hls/?s=…`)
    // answer a JSON media ladder instead of manifest bytes. Relative links
    // resolve against the page so they escalate too; the endpoint itself is
    // undownloadable JSON, so it leaves the candidate set either way.
    let (mut candidates, endpoints) = split_endpoints(page_url, candidates);

    let mut embeds: Vec<Url> = iframes;
    for embed in &meta.embeds {
        if let Ok(url) = page_url.join(embed) {
            embeds.push(url);
        }
    }
    let mut fetched: HashSet<Url> = HashSet::new();
    let mut wave1: Vec<(Url, String)> = Vec::new();
    for url in embeds.into_iter().take(MAX_WAVE_IFRAMES) {
        if fetched.insert(url.clone()) {
            wave1.push((url, page_url.to_string()));
        }
    }
    for candidate in endpoints.into_iter().take(MAX_WAVE_ENDPOINTS) {
        let Some(url) = candidates::absolute(page_url, &candidate.raw) else {
            continue;
        };
        if fetched.insert(url.clone()) {
            wave1.push((
                url,
                candidate.referer.unwrap_or_else(|| page_url.to_string()),
            ));
        }
    }

    // Wave 1: sub-pages may reveal nested iframes and nested endpoints.
    let mut media_base = candidates
        .iter()
        .filter_map(|candidate| candidate.media_index)
        .max()
        .unwrap_or(0);
    let mut nested: Vec<(Url, String)> = Vec::new();
    for mut subpage in fetch_subpages(net, wave1).await {
        media_base += subpage.offset_media(media_base);
        let Subpage {
            url: sub_url,
            candidates: sub_candidates,
            meta: sub_meta,
            iframes: sub_iframes,
        } = subpage;
        let sub_embeds = sub_meta.embeds.clone();
        candidates.extend(sub_candidates);
        merge_meta(&mut meta, sub_meta);
        for url in sub_iframes {
            nested.push((url, sub_url.to_string()));
        }
        for embed in sub_embeds {
            if let Ok(url) = sub_url.join(&embed) {
                nested.push((url, sub_url.to_string()));
            }
        }
    }

    // Wave 2: one more embed level plus the endpoints those sub-pages
    // revealed (their candidates are already absolutized).
    let (rest, fresh_endpoints) = split_endpoints(page_url, candidates);
    candidates = rest;
    nested.extend(fresh_endpoints.into_iter().filter_map(|candidate| {
        let url = candidates::absolute(page_url, &candidate.raw)?;
        Some((
            url,
            candidate.referer.unwrap_or_else(|| page_url.to_string()),
        ))
    }));
    let wave2: Vec<(Url, String)> = nested
        .into_iter()
        .filter(|(url, _)| fetched.insert(url.clone()))
        .take(MAX_NESTED_TARGETS)
        .collect();
    for mut subpage in fetch_subpages(net, wave2).await {
        media_base += subpage.offset_media(media_base);
        candidates.extend(subpage.candidates);
        merge_meta(&mut meta, subpage.meta);
    }

    let links = resolve_candidates(page_url, candidates);
    let info = PageInfo {
        title: meta.title.clone(),
        title_source: meta.title_source,
        thumbnail: meta
            .thumbnail
            .as_ref()
            .and_then(|thumbnail| page_url.join(thumbnail).ok()),
        duration_secs: meta.duration_secs,
    };
    if links.is_empty() {
        return Ok((Vec::new(), info));
    }
    Ok((group_items(page_url, links, &meta), info))
}

/// Fetch sub-pages (iframes, media endpoints) in parallel.
async fn fetch_subpages(net: &Fetcher, targets: Vec<(Url, String)>) -> Vec<Subpage> {
    if targets.is_empty() {
        return Vec::new();
    }
    let workers = targets.len();
    futures::stream::iter(targets)
        .map(|(sub_url, referer)| async move {
            match net
                .fetch_with_referer(&sub_url, Some(referer.as_str()))
                .await
            {
                Ok(response) => {
                    let url = response.url.clone();
                    let content_type = response.content_type().map(str::to_owned);
                    let body = response.body.clone();
                    match tokio::task::spawn_blocking(move || {
                        subpage_candidates(&url, content_type.as_deref(), &body)
                    })
                    .await
                    {
                        Ok(mut layers) => {
                            let parent_referer = referer.clone();
                            for candidate in &mut layers.candidates {
                                if candidate.referer.is_none() {
                                    candidate.referer = Some(parent_referer.clone());
                                }
                            }
                            Subpage {
                                url: sub_url,
                                candidates: layers.candidates,
                                meta: layers.meta,
                                iframes: layers.iframes,
                            }
                        }
                        Err(error) => {
                            debug!(%sub_url, error = %error, "subpage parse task failed");
                            Subpage::empty(sub_url)
                        }
                    }
                }
                Err(error) => {
                    debug!(%sub_url, error = %error, "subpage fetch failed");
                    Subpage::empty(sub_url)
                }
            }
        })
        .buffered(workers)
        .collect()
        .await
}

/// Split manifest-ish extensionless endpoints out of the candidate list.
fn split_endpoints(base: &Url, candidates: Vec<Candidate>) -> (Vec<Candidate>, Vec<Candidate>) {
    let mut keep = Vec::with_capacity(candidates.len());
    let mut endpoints = Vec::new();
    for candidate in candidates {
        if let Some(url) = candidates::absolute(base, &candidate.raw)
            && matches!(url.scheme(), "http" | "https")
            && crate::input::looks_manifest_url(&url)
            && crate::input::path_ext(&url).is_none()
        {
            endpoints.push(candidate);
        } else {
            keep.push(candidate);
        }
    }
    (keep, endpoints)
}

/// Resolve candidates into links.
///
/// Ordering, junk filtering and deduplication are the Python filter's job
/// (`downloadanything/filter.py`) — this crate only discovers addresses.
pub(crate) fn resolve_candidates(page_url: &Url, candidates: Vec<Candidate>) -> Vec<MediaLink> {
    candidates::dedupe(candidates)
        .iter()
        .filter_map(|candidate| candidates::resolve(page_url, candidate))
        .collect()
}

/// Split links into [`MediaItem`]s.
///
/// Distinct `<video>`/`<audio>` elements become playlist items; page-level
/// links form the primary item.
pub(crate) fn group_items(
    page_url: &Url,
    links: Vec<MediaLink>,
    meta: &PageMeta,
) -> Vec<MediaItem> {
    let mut groups: BTreeMap<Option<u32>, Vec<MediaLink>> = BTreeMap::new();
    for link in links {
        groups.entry(link.media_index).or_default().push(link);
    }
    // A <video> element's src also surfaces in the page-level scan — the
    // element group is then the same media listed twice as a second item.
    let page_urls: HashSet<Url> = groups
        .get(&None)
        .map(|links| links.iter().map(|link| link.url.clone()).collect())
        .unwrap_or_default();
    if !page_urls.is_empty() {
        groups.retain(|index, formats| {
            if index.is_none() {
                return true;
            }
            formats.retain(|link| !page_urls.contains(&link.url));
            !formats.is_empty()
        });
    }
    let total_groups = groups.len();
    groups
        .into_iter()
        .map(|(index, formats)| {
            let thumbnail = match index {
                Some(element_index) => meta
                    .element_thumbnails
                    .get(&element_index)
                    .and_then(|thumbnail| page_url.join(thumbnail).ok())
                    .or_else(|| {
                        // Single video on page: safe to fall back to the
                        // page-level thumbnail. Multi-video page: never assign
                        // an unrelated page thumbnail to a secondary video.
                        if total_groups == 1 {
                            meta.thumbnail
                                .as_ref()
                                .and_then(|thumbnail| page_url.join(thumbnail).ok())
                        } else {
                            None
                        }
                    }),
                None => meta
                    .thumbnail
                    .as_ref()
                    .and_then(|thumbnail| page_url.join(thumbnail).ok()),
            };

            MediaItem {
                id: index.map_or(ItemId::Page, ItemId::Media),
                title: meta.title.clone(),
                page_url: page_url.clone(),
                duration_secs: meta.duration_secs,
                thumbnail,
                formats,
            }
        })
        .collect()
}

/// Sync: all discovery layers over a page body.
///
/// The DOM is `!Sync` (scraper's tendril refcounts are single-threaded), so
/// the HTML and meta layers stay on the calling thread. Script text is
/// collected once, then the JSON-LD, JSON-blob and regex layers run on scoped
/// worker threads — the JSON parsing is the expensive part of those layers.
fn page_layers(page_url: &Url, text: &str) -> PageLayers {
    // Packed players (Dean Edwards eval) hide sources from every layer —
    // unpack first so the document and regex scans see the real script text.
    let text = packer::unpack(text);
    let document = Html::parse_document(&text);
    let text: &str = &text;

    let (scripts, data_json) = json_blobs::collect(&document);
    let jsonld_scripts = jsonld::collect(&document);
    let (pass, meta_result, jsonld_result, blob_findings, regex_candidates) =
        std::thread::scope(|scope| {
            let regex = scope.spawn(|| regex_scan::scan_text(text));
            let jsonld = scope.spawn(|| jsonld::extract(&jsonld_scripts));
            let blobs = scope.spawn(|| json_blobs::extract(&scripts, &data_json));
            let pass = html::run(&document, page_url);
            let meta = meta::extract(&document, &pass.base);
            (pass, meta, join(jsonld), join(blobs), join(regex))
        });

    let HtmlPass {
        mut candidates,
        iframes,
        base,
        thumbnail,
        element_thumbnails,
    } = pass;
    let (meta_candidates, mut page_meta) = meta_result;
    let (jsonld_candidates, jsonld_meta) = jsonld_result;
    let jsonld_thumbnail = jsonld_meta.thumbnail.clone();
    let Findings {
        candidates: blob_candidates,
        duration_secs: blob_duration,
        title: blob_title,
        title_source: blob_title_source,
        thumbnail: blob_thumbnail,
        ..
    } = blob_findings;

    merge_meta(&mut page_meta, jsonld_meta);
    if page_meta.title.is_none() {
        page_meta.title = blob_title;
        page_meta.title_source = blob_title_source;
    }
    if page_meta.duration_secs.is_none() {
        page_meta.duration_secs = blob_duration;
    }
    // JSON-LD/blob titles are raw; clean them like the meta layer does,
    // first with the declared site name and then with the page host.
    if let Some(title) = page_meta.title.as_deref() {
        let cleaned = meta::clean_title(title, page_meta.site_name.as_deref());
        let host = page_url
            .host_str()
            .map(|host| host.trim_start_matches("www."));
        let cleaned = meta::clean_title(&cleaned, host);
        page_meta.title = (!cleaned.is_empty()).then_some(cleaned);
    }

    // PRIORITY: video player preview/poster > JSON-LD VideoObject >
    // page-level og:image / twitter:image.
    let player_thumbnail = thumbnail
        .or(blob_thumbnail)
        .or(jsonld_thumbnail)
        .or(page_meta.thumbnail.take());
    if let Some(thumbnail) = player_thumbnail {
        page_meta.thumbnail = Some(thumbnail);
    }
    page_meta.element_thumbnails = element_thumbnails;

    candidates.extend(meta_candidates);
    candidates.extend(jsonld_candidates);
    candidates.extend(blob_candidates);
    candidates.extend(regex_candidates);
    // Resolve relative candidates against `<base href>` if the page set one.
    if base != *page_url {
        for candidate in &mut candidates {
            if let Ok(absolute) = base.join(candidate.raw.trim()) {
                candidate.raw = absolute.into();
            }
        }
        // Metadata URLs follow the same base; group_items would otherwise
        // resolve them against the page URL and pick the wrong host.
        if let Some(thumbnail) = &page_meta.thumbnail
            && let Ok(absolute) = base.join(thumbnail)
        {
            page_meta.thumbnail = Some(absolute.into());
        }
        for thumbnail in page_meta.element_thumbnails.values_mut() {
            if let Ok(absolute) = base.join(thumbnail) {
                *thumbnail = absolute.to_string();
            }
        }
        for embed in &mut page_meta.embeds {
            if let Ok(absolute) = base.join(embed) {
                *embed = absolute.to_string();
            }
        }
    }
    PageLayers {
        candidates,
        meta: page_meta,
        iframes,
    }
}

/// Join a scoped thread, preserving the original panic payload.
fn join<T>(handle: std::thread::ScopedJoinHandle<'_, T>) -> T {
    handle
        .join()
        .unwrap_or_else(|payload| std::panic::resume_unwind(payload))
}

/// Sync: layers over a fetched sub-page body (no deeper escalation here).
///
/// Relative candidate URLs resolve against the sub-page's own URL so
/// cross-origin embeds keep their host when merged into the parent page.
/// Nested iframes are returned for the caller's second escalation wave.
fn subpage_candidates(url: &Url, content_type: Option<&str>, body: &[u8]) -> PageLayers {
    match classify::classify(content_type, url, body) {
        ContentKind::DirectMedia => direct_layer(url, MediaKind::Progressive),
        ContentKind::DirectAudio => direct_layer(url, MediaKind::Audio),
        ContentKind::ManifestHls => direct_layer(url, MediaKind::Hls),
        ContentKind::ManifestDash => direct_layer(url, MediaKind::Dash),
        ContentKind::Json => {
            let findings = serde_json::from_slice::<serde_json::Value>(body)
                .map(|value| json_blobs::walk_value(&value))
                .unwrap_or_default();
            let mut candidates = findings.candidates;
            absolutize_candidates(url, &mut candidates);
            // Same title cleaning and source tracking as the page path — a
            // raw player title ("Clip | site.com") must not reach the caller
            // labelled as high confidence.
            let host = url.host_str().map(|host| host.trim_start_matches("www."));
            let title = findings
                .title
                .map(|title| meta::clean_title(&title, host))
                .filter(|title| !title.is_empty());
            let title_source = title.as_ref().and(findings.title_source);
            PageLayers {
                candidates,
                meta: PageMeta {
                    title,
                    title_source,
                    duration_secs: findings.duration_secs,
                    thumbnail: findings
                        .thumbnail
                        .as_ref()
                        .and_then(|thumbnail| url.join(thumbnail).ok())
                        .map(Into::into),
                    ..Default::default()
                },
                iframes: Vec::new(),
            }
        }
        ContentKind::Page => {
            let text = decode_text(body);
            let mut layers = page_layers(url, &text);
            absolutize_candidates(url, &mut layers.candidates);
            for candidate in &mut layers.candidates {
                candidate.referer = Some(url.to_string());
            }
            if let Some(thumbnail) = &layers.meta.thumbnail
                && let Ok(absolute) = url.join(thumbnail)
            {
                layers.meta.thumbnail = Some(absolute.into());
            }
            // Subpage-relative embeds and element posters are merged into the
            // parent page later, where they resolve against the parent URL.
            for embed in &mut layers.meta.embeds {
                if let Ok(absolute) = url.join(embed) {
                    *embed = absolute.to_string();
                }
            }
            for thumbnail in layers.meta.element_thumbnails.values_mut() {
                if let Ok(absolute) = url.join(thumbnail) {
                    *thumbnail = absolute.to_string();
                }
            }
            layers
        }
    }
}

/// Layers for a response that is itself a media file or manifest.
fn direct_layer(url: &Url, kind: MediaKind) -> PageLayers {
    PageLayers {
        candidates: vec![direct_candidate(url, kind)],
        ..Default::default()
    }
}

/// A direct response is a media link with a kind asserted by the classifier.
fn direct_candidate(url: &Url, kind: MediaKind) -> Candidate {
    let mut candidate = Candidate::new(url.as_str(), SourceLayer::Direct);
    candidate.kind_hint = Some(kind);
    candidate
}

/// Resolve candidates that are relative to `base` into absolute strings.
fn absolutize_candidates(base: &Url, candidates: &mut [Candidate]) {
    for candidate in candidates {
        if let Some(url) = candidates::absolute(base, &candidate.raw) {
            candidate.raw = url.into();
        }
    }
}

/// Borrow the body as UTF-8 when possible, else decode lossily once.
fn decode_text(body: &[u8]) -> Cow<'_, str> {
    std::str::from_utf8(body)
        .map(Cow::Borrowed)
        .unwrap_or_else(|_| Cow::Owned(String::from_utf8_lossy(body).into_owned()))
}

/// Merge `src` metadata into `dst` without overwriting values already set.
fn merge_meta(dst: &mut PageMeta, src: PageMeta) {
    if dst.title.is_none() {
        dst.title = src.title;
        dst.title_source = src.title_source;
    }
    if dst.site_name.is_none() {
        dst.site_name = src.site_name;
    }
    if dst.duration_secs.is_none() {
        dst.duration_secs = src.duration_secs;
    }
    if dst.thumbnail.is_none() {
        dst.thumbnail = src.thumbnail;
    }
    for (index, thumbnail) in src.element_thumbnails {
        dst.element_thumbnails.entry(index).or_insert(thumbnail);
    }
    dst.embeds.extend(src.embeds);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::extract::candidates::resolve;

    fn page() -> Url {
        Url::parse("https://site.com/watch").unwrap()
    }

    #[test]
    fn multi_video_page_isolates_thumbnails_per_element() {
        fn element_link(name: &str, index: u32) -> MediaLink {
            let mut link = MediaLink::new(
                Url::parse(&format!("https://site.com/{name}")).unwrap(),
                MediaKind::Progressive,
                SourceLayer::Html,
            );
            link.media_index = Some(index);
            link
        }

        let mut meta = PageMeta {
            title: Some("Multi Video Page".into()),
            thumbnail: Some("https://site.com/general_header.jpg".into()),
            ..Default::default()
        };
        meta.element_thumbnails
            .insert(1, "https://site.com/video1_thumb.jpg".into());
        meta.element_thumbnails
            .insert(2, "https://site.com/video2_thumb.jpg".into());

        let items = group_items(
            &page(),
            vec![element_link("v1.mp4", 1), element_link("v2.mp4", 2)],
            &meta,
        );
        assert_eq!(items.len(), 2);
        assert_eq!(items[0].id, ItemId::Media(1));
        assert_eq!(
            items[0].thumbnail.as_ref().map(Url::as_str),
            Some("https://site.com/video1_thumb.jpg")
        );
        assert_eq!(items[1].id, ItemId::Media(2));
        assert_eq!(
            items[1].thumbnail.as_ref().map(Url::as_str),
            Some("https://site.com/video2_thumb.jpg")
        );

        // A secondary video without its own poster must not steal video 1's
        // or the page thumbnail.
        meta.element_thumbnails.remove(&2);
        let items = group_items(
            &page(),
            vec![element_link("v1.mp4", 1), element_link("v2.mp4", 2)],
            &meta,
        );
        assert_eq!(
            items[0].thumbnail.as_ref().map(Url::as_str),
            Some("https://site.com/video1_thumb.jpg")
        );
        assert_eq!(
            items[1].thumbnail, None,
            "secondary video without its own poster must not inherit a wrong thumbnail"
        );
    }

    #[test]
    fn element_item_duplicating_page_url_merges_back() {
        // A <video> element whose src the page scan also found is the same
        // media — it must not become a second item.
        let duplicate = Url::parse("https://cdn.site.com/v.webm").unwrap();
        let page_level = MediaLink::new(
            duplicate.clone(),
            MediaKind::Progressive,
            SourceLayer::Regex,
        );
        let mut element = MediaLink::new(duplicate, MediaKind::Progressive, SourceLayer::Html);
        element.media_index = Some(1);
        let mut other = MediaLink::new(
            Url::parse("https://cdn.site.com/other.mp4").unwrap(),
            MediaKind::Progressive,
            SourceLayer::Html,
        );
        other.media_index = Some(2);

        let items = group_items(
            &page(),
            vec![page_level, element, other],
            &PageMeta::default(),
        );
        assert_eq!(items.len(), 2, "dup element folds into the page item");
        assert_eq!(items[0].formats.len(), 1);
        assert_eq!(items[1].id, ItemId::Media(2));
    }

    #[test]
    fn video_data_src_keeps_playlist_grouping() {
        // A <video data-src> is captured by the element loop with its
        // media_index; the page-wide data-* sweep must not re-emit it as a
        // page-level candidate (that folded both element groups into one item).
        let document = Html::parse_document(
            r#"<video data-src="https://cdn.x/v1.mp4"></video>
               <video data-src="https://cdn.x/v2.mp4"></video>"#,
        );
        let pass = html::run(&document, &page());
        assert_eq!(
            pass.candidates.len(),
            2,
            "each <video data-src> must appear exactly once"
        );
        let links: Vec<_> = pass
            .candidates
            .iter()
            .filter_map(|candidate| resolve(&page(), candidate))
            .collect();
        let items = group_items(&page(), links, &PageMeta::default());
        assert_eq!(items.len(), 2, "two video elements stay two playlist items");
        assert_eq!(items[0].id, ItemId::Media(1));
        assert_eq!(items[1].id, ItemId::Media(2));
    }

    #[test]
    fn jsonld_thumbnail_outranks_og_image() {
        let html = r#"<html><head>
            <meta property="og:image" content="https://cdn.x/og.jpg">
            <script type="application/ld+json">{"@type":"VideoObject","contentUrl":"https://cdn.x/v.mp4","thumbnailUrl":"https://cdn.x/ld.jpg"}</script>
        </head><body></body></html>"#;
        let layers = page_layers(&page(), html);
        assert_eq!(
            layers.meta.thumbnail.as_deref(),
            Some("https://cdn.x/ld.jpg"),
            "the VideoObject poster is more specific than og:image"
        );
    }

    #[test]
    fn blob_title_is_cleaned_against_the_page_host() {
        let html = r#"<html><body>
            <script>window.__cfg = { video_title: "Clip | site.com", video_url: "https://cdn.x/v.mp4" };</script>
        </body></html>"#;
        let layers = page_layers(&page(), html);
        assert_eq!(layers.meta.title.as_deref(), Some("Clip"));
    }

    #[test]
    fn blob_title_is_cleaned_with_og_site_name() {
        let html = r#"<html><head><meta property="og:site_name" content="Brand"></head>
            <body><script>window.__cfg = { video_title: "Clip - Brand", video_url: "https://cdn.x/v.mp4" };</script></body>
        </html>"#;
        let layers = page_layers(&page(), html);
        assert_eq!(layers.meta.title.as_deref(), Some("Clip"));
    }

    #[test]
    fn subpage_relative_embeds_and_element_thumbnails_are_absolutized() {
        let url = Url::parse("https://cdn.x/embed/page").unwrap();
        let body = br#"<html><head><meta name="twitter:player" content="/player/2"></head>
            <body><video src="v.mp4" poster="poster.jpg"></video></body></html>"#;
        let layers = subpage_candidates(&url, Some("text/html"), body);
        assert_eq!(layers.meta.embeds, vec!["https://cdn.x/player/2"]);
        assert_eq!(
            layers.meta.element_thumbnails.get(&1).map(String::as_str),
            Some("https://cdn.x/embed/poster.jpg")
        );
    }

    #[test]
    fn base_href_resolves_page_metadata() {
        let html = r#"<html><head>
            <base href="https://cdn.x/embed/">
            <meta property="og:image" content="og.jpg">
        </head><body><video src="v.mp4" poster="poster.jpg"></video></body></html>"#;
        let layers = page_layers(&page(), html);
        assert_eq!(
            layers.meta.thumbnail.as_deref(),
            Some("https://cdn.x/embed/poster.jpg"),
            "the player poster wins and resolves against <base>"
        );
        assert_eq!(
            layers.meta.element_thumbnails.get(&1).map(String::as_str),
            Some("https://cdn.x/embed/poster.jpg")
        );
        assert!(
            layers
                .candidates
                .iter()
                .any(|candidate| candidate.raw == "https://cdn.x/embed/v.mp4"),
            "{:?}",
            layers.candidates
        );
    }

    #[test]
    fn base_href_resolves_og_image_when_no_player_poster() {
        let html = r#"<html><head>
            <base href="https://cdn.x/embed/">
            <meta property="og:image" content="og.jpg">
        </head><body><video src="v.mp4"></video></body></html>"#;
        let layers = page_layers(&page(), html);
        assert_eq!(
            layers.meta.thumbnail.as_deref(),
            Some("https://cdn.x/embed/og.jpg")
        );
    }

    #[test]
    fn parallel_layers_contribute_every_source() {
        let html = r#"<html><head>
            <meta property="og:video" content="https://cdn.x/meta.mp4">
            <script type="application/ld+json">{"@type":"VideoObject","contentUrl":"https://cdn.x/ld.mp4"}</script>
            <script>window.__cfg = { video_url: "https://cdn.x/blob.mp4" };</script>
        </head><body><video src="https://cdn.x/html.mp4"></video></body></html>"#;
        let layers = page_layers(&page(), html);
        for name in ["html.mp4", "meta.mp4", "ld.mp4", "blob.mp4"] {
            assert!(
                layers
                    .candidates
                    .iter()
                    .any(|candidate| candidate.raw.ends_with(name)),
                "{name} missing from {:?}",
                layers.candidates
            );
        }
    }

    #[test]
    fn json_subpage_title_is_cleaned_and_sourced() {
        let url = Url::parse("https://cdn.x/config").unwrap();
        let body = br#"{"video_title":"Clip | cdn.x","video_url":"https://cdn.x/v.mp4"}"#;
        let layers = subpage_candidates(&url, Some("application/json"), body);
        assert_eq!(layers.meta.title.as_deref(), Some("Clip"));
        assert_eq!(
            layers.meta.title_source,
            Some(crate::model::TitleSource::JsonBlob)
        );
    }

    #[test]
    fn subpage_candidates_absolutize_percent_encoded_urls() {
        // Lowercase percent-encoding is as valid as uppercase; joining it
        // against the page used to produce a page-relative 404 URL.
        let url = Url::parse("https://cdn.x/embed/page").unwrap();
        let body = br#"<script>window.cfg={video_url:"https%3a%2f%2fcdn.x%2fv.mp4"}</script>"#;
        let layers = subpage_candidates(&url, Some("text/html"), body);
        assert!(
            layers
                .candidates
                .iter()
                .any(|candidate| candidate.raw == "https://cdn.x/v.mp4"),
            "{:?}",
            layers.candidates
        );
    }
}
