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
use crate::model::{ItemId, MediaItem, MediaKind, MediaLink, SourceLayer};
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
}

/// HTML page path: run all static layers, escalate iframes (two levels) and
/// manifest endpoints, then resolve candidates into grouped items.
///
/// # Errors
///
/// Returns [`ExtractError::Parse`] when the blocking parse task fails and
/// [`ExtractError::NoMediaFound`] when nothing resolves to a link.
#[instrument(skip_all, fields(url = %page_url))]
pub(crate) async fn extract_page(
    net: &Fetcher,
    page_url: &Url,
    response: &FetchResponse,
) -> Result<Vec<MediaItem>, ExtractError> {
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
    let mut wave1: Vec<(Url, Url)> = Vec::new();
    for url in embeds.into_iter().take(MAX_WAVE_IFRAMES) {
        if fetched.insert(url.clone()) {
            wave1.push((url, page_url.clone()));
        }
    }
    for url in endpoints.into_iter().take(MAX_WAVE_ENDPOINTS) {
        if fetched.insert(url.clone()) {
            wave1.push((url, page_url.clone()));
        }
    }

    // Wave 1: sub-pages may reveal nested iframes and nested endpoints.
    let mut nested: Vec<(Url, Url)> = Vec::new();
    for subpage in fetch_subpages(net, wave1).await {
        let Subpage {
            url: sub_url,
            candidates: sub_candidates,
            meta: sub_meta,
            iframes: sub_iframes,
        } = subpage;
        candidates.extend(sub_candidates);
        merge_meta(&mut meta, sub_meta);
        for url in sub_iframes {
            nested.push((url, sub_url.clone()));
        }
    }

    // Wave 2: one more embed level plus the endpoints those sub-pages
    // revealed (their candidates are already absolutized).
    let (rest, fresh_endpoints) = split_endpoints(page_url, candidates);
    candidates = rest;
    nested.extend(
        fresh_endpoints
            .into_iter()
            .map(|url| (url, page_url.clone())),
    );
    let wave2: Vec<(Url, Url)> = nested
        .into_iter()
        .filter(|(url, _)| fetched.insert(url.clone()))
        .take(MAX_NESTED_TARGETS)
        .collect();
    for subpage in fetch_subpages(net, wave2).await {
        candidates.extend(subpage.candidates);
        merge_meta(&mut meta, subpage.meta);
    }

    let links = resolve_candidates(page_url, candidates);
    if links.is_empty() {
        return Err(ExtractError::NoMediaFound);
    }
    Ok(group_items(page_url, links, &meta))
}

/// Fetch sub-pages (iframes, media endpoints) in parallel.
async fn fetch_subpages(net: &Fetcher, targets: Vec<(Url, Url)>) -> Vec<Subpage> {
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
                        Ok(layers) => Subpage {
                            url: sub_url,
                            candidates: layers.candidates,
                            meta: layers.meta,
                            iframes: layers.iframes,
                        },
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
        .buffer_unordered(workers)
        .collect()
        .await
}

/// Split manifest-ish extensionless endpoints out of the candidate list.
fn split_endpoints(base: &Url, candidates: Vec<Candidate>) -> (Vec<Candidate>, Vec<Url>) {
    let mut keep = Vec::with_capacity(candidates.len());
    let mut endpoints = Vec::new();
    for candidate in candidates {
        if let Some(url) = candidates::absolute(base, &candidate.raw)
            && matches!(url.scheme(), "http" | "https")
            && crate::input::looks_manifest_url(&url)
            && crate::input::path_ext(&url).is_none()
        {
            endpoints.push(url);
        } else {
            keep.push(candidate);
        }
    }
    (keep, endpoints)
}

/// Resolve candidates into links.
///
/// Ordering, junk filtering and deduplication are the Python filter's job
/// (`dax/filter.py`) — this crate only discovers addresses.
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
    let Findings {
        candidates: blob_candidates,
        duration_secs: blob_duration,
        title: blob_title,
        thumbnail: blob_thumbnail,
    } = blob_findings;

    merge_meta(&mut page_meta, jsonld_meta);
    if page_meta.title.is_none() {
        page_meta.title = blob_title;
    }
    if page_meta.duration_secs.is_none() {
        page_meta.duration_secs = blob_duration;
    }

    // PRIORITY: video player preview/poster > JSON-LD VideoObject >
    // page-level og:image / twitter:image.
    let player_thumbnail = thumbnail.or(blob_thumbnail).or(page_meta.thumbnail.take());
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
        ContentKind::DirectMedia => PageLayers {
            candidates: vec![direct_candidate(url, MediaKind::Progressive)],
            ..Default::default()
        },
        ContentKind::DirectAudio => PageLayers {
            candidates: vec![direct_candidate(url, MediaKind::Audio)],
            ..Default::default()
        },
        ContentKind::ManifestHls => PageLayers {
            candidates: vec![direct_candidate(url, MediaKind::Hls)],
            ..Default::default()
        },
        ContentKind::ManifestDash => PageLayers {
            candidates: vec![direct_candidate(url, MediaKind::Dash)],
            ..Default::default()
        },
        ContentKind::Json => {
            let findings = serde_json::from_slice::<serde_json::Value>(body)
                .map(|value| json_blobs::walk_value(&value))
                .unwrap_or_default();
            let mut candidates = findings.candidates;
            absolutize_candidates(url, &mut candidates);
            PageLayers {
                candidates,
                meta: PageMeta {
                    title: findings.title,
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
            if let Some(thumbnail) = &layers.meta.thumbnail
                && let Ok(absolute) = url.join(thumbnail)
            {
                layers.meta.thumbnail = Some(absolute.into());
            }
            layers
        }
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
        let raw = candidate.raw.trim();
        if raw.starts_with("//") {
            if let Ok(url) = Url::parse(&format!("{}:{}", base.scheme(), raw)) {
                candidate.raw = url.into();
            }
        } else if !raw.starts_with("http://")
            && !raw.starts_with("https://")
            && !raw.starts_with("http%3A")
            && !raw.starts_with("https%3A")
            && let Ok(url) = base.join(raw)
        {
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
        let mut meta = PageMeta {
            title: Some("Multi Video Page".into()),
            thumbnail: Some("https://site.com/general_header.jpg".into()),
            ..Default::default()
        };
        meta.element_thumbnails
            .insert(1, "https://site.com/video1_thumb.jpg".into());
        meta.element_thumbnails
            .insert(2, "https://site.com/video2_thumb.jpg".into());

        let mut first = MediaLink::new(
            Url::parse("https://site.com/v1.mp4").unwrap(),
            MediaKind::Progressive,
            SourceLayer::Html,
        );
        first.media_index = Some(1);
        let mut second = MediaLink::new(
            Url::parse("https://site.com/v2.mp4").unwrap(),
            MediaKind::Progressive,
            SourceLayer::Html,
        );
        second.media_index = Some(2);

        let items = group_items(&page(), vec![first, second], &meta);
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
        let mut first = MediaLink::new(
            Url::parse("https://site.com/v1.mp4").unwrap(),
            MediaKind::Progressive,
            SourceLayer::Html,
        );
        first.media_index = Some(1);
        let mut second = MediaLink::new(
            Url::parse("https://site.com/v2.mp4").unwrap(),
            MediaKind::Progressive,
            SourceLayer::Html,
        );
        second.media_index = Some(2);
        let items = group_items(&page(), vec![first, second], &meta);
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
}
