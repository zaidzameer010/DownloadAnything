pub mod candidates;
pub mod html;
pub mod json_blobs;
pub mod jsonld;
pub mod meta;
pub mod packer;
pub mod regex_scan;
pub mod unescape;

use crate::error::ExtractError;
use crate::input::classify::{self, ContentKind};
use crate::model::{MediaItem, MediaKind, MediaLink, SourceLayer};
use crate::net::{FetchResponse, NetEngine};
use candidates::Candidate;
use futures::stream::StreamExt;
use meta::PageMeta;
use scraper::Html;
use std::borrow::Cow;
use std::collections::{BTreeMap, HashSet};
use tracing::{debug, instrument};
use url::Url;

#[instrument(skip_all, fields(url = %page_url))]
pub async fn extract_response(
    net: &NetEngine,
    page_url: &Url,
    resp: &FetchResponse,
) -> Result<Vec<MediaItem>, ExtractError> {
    let kind = classify::classify(resp.content_type().as_deref(), page_url, &resp.body);
    debug!(?kind, "classified response");
    match kind {
        ContentKind::DirectMedia => {
            let mut link = MediaLink::new(
                resp.url.clone(),
                MediaKind::Progressive,
                SourceLayer::Direct,
            );
            link.container = crate::input::path_ext(&resp.url);
            Ok(vec![item(page_url, vec![link])])
        }
        ContentKind::ManifestHls => {
            let link = MediaLink::new(resp.url.clone(), MediaKind::Hls, SourceLayer::Direct);
            Ok(vec![item(page_url, vec![link])])
        }
        ContentKind::ManifestDash => {
            let link = MediaLink::new(resp.url.clone(), MediaKind::Dash, SourceLayer::Direct);
            Ok(vec![item(page_url, vec![link])])
        }
        ContentKind::Json => {
            let v: serde_json::Value =
                serde_json::from_slice(&resp.body).map_err(|e| ExtractError::Parse {
                    layer: "json",
                    detail: e.to_string(),
                })?;
            let mut cands = Vec::new();
            let mut dur = None;
            let mut title = None;
            let mut thumb = None;
            json_blobs::walk_json(&v, &mut cands, &mut dur, &mut title, &mut thumb, 0);
            let links = resolve_candidates(page_url, cands);
            if links.is_empty() {
                return Err(ExtractError::NoMediaFound);
            }
            let mut it = item(page_url, links);
            it.duration_secs = dur;
            it.title = title;
            it.thumbnail = thumb.as_ref().and_then(|t| page_url.join(t).ok());
            Ok(vec![it])
        }
        ContentKind::Page => extract_page(net, page_url, resp).await,
    }
}

/// HTML page path: run all static layers, escalate iframes, resolve candidates.
async fn extract_page(
    net: &NetEngine,
    page_url: &Url,
    resp: &FetchResponse,
) -> Result<Vec<MediaItem>, ExtractError> {
    // CPU-bound scraper parsing runs on the blocking pool — a huge page
    // must not stall the concurrent extraction tasks. (scraper::Html is
    // !Send and never lives across an .await.)
    let body = resp.body.clone();
    let url = page_url.clone();
    let (mut candidates, mut page_meta, iframes) = tokio::task::spawn_blocking(move || {
        // Valid UTF-8 (the common case) borrows the fetched bytes —
        // no second full-body copy before parsing.
        let text = std::str::from_utf8(&body)
            .map(Cow::Borrowed)
            .unwrap_or_else(|_| Cow::Owned(String::from_utf8_lossy(&body).into_owned()));
        page_layers(&url, &text)
    })
    .await
    .map_err(|e| ExtractError::Parse {
        layer: "page",
        detail: e.to_string(),
    })?;
    debug!(
        count = candidates.len(),
        "static layers produced candidates"
    );

    // Iframe escalation: embedded players get one recursive pass, fetched
    // in parallel. HTML iframes plus player-embed metadata (jsonld
    // embedUrl, twitter:player).
    let mut targets: Vec<Url> = iframes;
    for e in &page_meta.embeds {
        if let Ok(u) = page_url.join(e) {
            targets.push(u);
        }
    }
    // The historical per-wave cap (4 iframes + 4 endpoints) still holds.
    targets.truncate(4);

    // Manifest-ish endpoints with no file extension (`/media/hls/?s=…`)
    // answer a JSON media ladder instead of manifest bytes — resolve them
    // like embeds and keep the URLs inside. The endpoint itself is
    // undownloadable JSON, so it leaves the candidate set either way.
    // Collected before the first wave so iframes and page-level endpoints
    // fetch together; only endpoints surfaced inside subpages need a
    // second pass.
    let mut endpoints: Vec<Url> = Vec::new();
    candidates.retain(|c| {
        if let Ok(u) = Url::parse(c.raw.trim())
            && crate::input::looks_manifest_url(&u)
            && crate::input::path_ext(&u).is_none()
        {
            endpoints.push(u);
            return false;
        }
        true
    });
    targets.extend(endpoints.into_iter().take(4));
    let page_len = candidates.len();
    for (cands, sub_meta) in fetch_subpages(net, page_url, targets, 8).await {
        candidates.extend(cands);
        merge_meta(&mut page_meta, sub_meta);
    }

    // Endpoints that only the subpages revealed — one more parallel pass.
    let mut fresh = candidates.split_off(page_len);
    let mut nested: Vec<Url> = Vec::new();
    fresh.retain(|c| {
        if let Ok(u) = Url::parse(c.raw.trim())
            && crate::input::looks_manifest_url(&u)
            && crate::input::path_ext(&u).is_none()
        {
            nested.push(u);
            return false;
        }
        true
    });
    candidates.extend(fresh);
    for (cands, sub_meta) in fetch_subpages(net, page_url, nested, 4).await {
        candidates.extend(cands);
        merge_meta(&mut page_meta, sub_meta);
    }

    let links = resolve_candidates(page_url, candidates);
    if links.is_empty() {
        return Err(ExtractError::NoMediaFound);
    }
    Ok(group_items(page_url, links, &page_meta))
}

/// Fetch sub-pages (iframes, media endpoints) in parallel — deduped and
/// capped at `limit` — returning each page's candidates and metadata.
async fn fetch_subpages(
    net: &NetEngine,
    page_url: &Url,
    targets: Vec<Url>,
    limit: usize,
) -> Vec<(Vec<Candidate>, PageMeta)> {
    let mut seen = HashSet::new();
    let targets: Vec<Url> = targets
        .into_iter()
        .filter(|u| seen.insert(u.clone()))
        .take(limit)
        .collect();
    if targets.is_empty() {
        return Vec::new();
    }
    let workers = targets.len().max(1);
    futures::stream::iter(targets)
        .map(|sub| async move {
            match net.fetch_ref(&sub, Some(page_url.as_str())).await {
                Ok(resp) => {
                    let url = resp.url.clone();
                    let ctype = resp.content_type();
                    let body = resp.body.clone();
                    match tokio::task::spawn_blocking(move || {
                        subpage_candidates(&url, ctype.as_deref(), &body)
                    })
                    .await
                    {
                        Ok(res) => res,
                        Err(e) => {
                            debug!(%sub, error = %e, "subpage parse task failed");
                            (Vec::new(), PageMeta::default())
                        }
                    }
                }
                Err(e) => {
                    debug!(%sub, error = %e, "subpage fetch failed");
                    (Vec::new(), PageMeta::default())
                }
            }
        })
        .buffer_unordered(workers)
        .collect()
        .await
}

/// Resolve candidates into links. Ordering, junk and dedup are the Python
/// filter's job (dax/filter.py) — this crate only discovers addresses.
fn resolve_candidates(page_url: &Url, candidates: Vec<Candidate>) -> Vec<MediaLink> {
    candidates
        .iter()
        .filter_map(|c| candidates::resolve(page_url, c))
        .collect()
}

/// Split links into MediaItems: distinct <video>/<audio> elements become
/// playlist items; page-level links form the primary item.
pub fn group_items(page_url: &Url, links: Vec<MediaLink>, meta: &PageMeta) -> Vec<MediaItem> {
    let mut groups: BTreeMap<Option<u32>, Vec<MediaLink>> = BTreeMap::new();
    for l in links {
        groups.entry(l.media_index).or_default().push(l);
    }
    // A <video> element's src also surfaces in the page-level scan — the
    // element group is then the same media listed twice as a second item.
    let page_urls: HashSet<Url> = groups
        .get(&None)
        .map(|fs| fs.iter().map(|l| l.url.clone()).collect())
        .unwrap_or_default();
    if !page_urls.is_empty() {
        groups.retain(|idx, fmts| {
            if idx.is_none() {
                return true;
            }
            fmts.retain(|l| !page_urls.contains(&l.url));
            !fmts.is_empty()
        });
    }
    let total_groups = groups.len();
    groups
        .into_iter()
        .map(|(idx, formats)| {
            let item_thumb = match idx {
                Some(i) => meta
                    .element_thumbnails
                    .get(&i)
                    .and_then(|t| page_url.join(t).ok())
                    .or_else(|| {
                        // Single video on page: safe to fall back to page-level thumbnail.
                        // Multi-video page: do NOT assign unrelated page thumbnail to secondary video.
                        if total_groups == 1 {
                            meta.thumbnail.as_ref().and_then(|t| page_url.join(t).ok())
                        } else {
                            None
                        }
                    }),
                None => meta.thumbnail.as_ref().and_then(|t| page_url.join(t).ok()),
            };

            MediaItem {
                id: idx
                    .map(|i| format!("media-{i}"))
                    .unwrap_or_else(|| "page".into()),
                title: meta.title.clone(),
                page_url: page_url.clone(),
                duration_secs: meta.duration_secs,
                thumbnail: item_thumb,
                formats,
            }
        })
        .collect()
}

/// Sync: all extraction layers over a page body. Returns candidates,
/// page metadata and iframe embeds to escalate.
fn page_layers(page_url: &Url, text: &str) -> (Vec<Candidate>, PageMeta, Vec<Url>) {
    // Packed players (Dean Edwards eval) hide sources from every layer —
    // unpack first so the doc and regex scans see the real script text.
    let text = packer::unpack(text);
    let doc = Html::parse_document(&text);
    let text: &str = &text;

    // Layer order matters for source tagging: structured before regex.
    let (pass, meta_res, ld_res, blob_res, rx_cands) = std::thread::scope(|s| {
        let rx = s.spawn(|| regex_scan::scan_text(text));
        let pass = html::run(&doc, page_url);
        let meta = meta::extract(&doc, &pass.base);
        let ld = jsonld::extract(&doc);
        let blob = json_blobs::extract(&doc);
        (
            pass,
            meta,
            ld,
            blob,
            rx.join().unwrap_or_else(|e| std::panic::resume_unwind(e)),
        )
    });

    let (meta_cands, mut page_meta) = meta_res;
    let (ld_cands, ld_meta) = ld_res;
    let (blob_cands, blob_dur, blob_title, blob_thumb) = blob_res;
    merge_meta(&mut page_meta, ld_meta);
    if page_meta.title.is_none() {
        page_meta.title = blob_title;
    }
    if page_meta.duration_secs.is_none() {
        page_meta.duration_secs = blob_dur;
    }

    // PRIORITY: Video player preview/poster > JSON-LD VideoObject > page-level og:image / twitter:image
    // pass.thumbnail comes directly from <video poster="..."> or player preview elements.
    // blob_thumb comes directly from the video player config / KVS flashvars.
    // ld_meta.thumbnail comes from schema.org VideoObject.
    let player_thumb = pass.thumbnail.or(blob_thumb).or(page_meta.thumbnail.take());

    if let Some(pt) = player_thumb {
        page_meta.thumbnail = Some(pt);
    }
    page_meta.element_thumbnails = pass.element_thumbnails;

    let mut candidates = Vec::new();
    candidates.extend(pass.candidates);
    candidates.extend(meta_cands);
    candidates.extend(ld_cands);
    candidates.extend(blob_cands);
    candidates.extend(rx_cands);
    // Resolve relative candidates against `<base href>` if the page set one.
    if pass.base != *page_url {
        for c in &mut candidates {
            if let Ok(abs) = pass.base.join(c.raw.trim()) {
                c.raw = abs.into();
            }
        }
    }
    (candidates, page_meta, pass.iframes)
}

/// Sync: layers over a fetched iframe body (no deeper escalation).
/// Relative candidate URLs are resolved against the iframe's own URL so
/// cross-origin embeds keep their host when merged into the parent page.
fn subpage_candidates(
    url: &Url,
    content_type: Option<&str>,
    body: &[u8],
) -> (Vec<Candidate>, PageMeta) {
    match classify::classify(content_type, url, body) {
        ContentKind::DirectMedia | ContentKind::ManifestHls | ContentKind::ManifestDash => (
            vec![Candidate::new(url.as_str(), SourceLayer::Direct)],
            PageMeta::default(),
        ),
        ContentKind::Json => {
            let mut cands = Vec::new();
            let mut dur = None;
            let mut title = None;
            let mut thumb = None;
            if let Ok(v) = serde_json::from_slice::<serde_json::Value>(body) {
                json_blobs::walk_json(&v, &mut cands, &mut dur, &mut title, &mut thumb, 0);
            }
            absolutize_candidates(url, &mut cands);
            (
                cands,
                PageMeta {
                    title,
                    duration_secs: dur,
                    thumbnail: thumb
                        .as_ref()
                        .and_then(|t| url.join(t).ok())
                        .map(Into::into),
                    ..Default::default()
                },
            )
        }
        ContentKind::Page => {
            let text = std::str::from_utf8(body)
                .map(Cow::Borrowed)
                .unwrap_or_else(|_| Cow::Owned(String::from_utf8_lossy(body).into_owned()));
            let (mut cands, mut meta, _) = page_layers(url, &text);
            absolutize_candidates(url, &mut cands);
            if let Some(t) = &meta.thumbnail
                && let Ok(abs) = url.join(t)
            {
                meta.thumbnail = Some(abs.into());
            }
            (cands, meta)
        }
    }
}

fn absolutize_candidates(base: &Url, cands: &mut [Candidate]) {
    for c in cands {
        let raw = c.raw.trim();
        if raw.starts_with("//") {
            if let Ok(u) = Url::parse(&format!("{}:{}", base.scheme(), raw)) {
                c.raw = u.into();
            }
        } else if !raw.starts_with("http://")
            && !raw.starts_with("https://")
            && !raw.starts_with("http%3A")
            && !raw.starts_with("https%3A")
            && let Ok(u) = base.join(raw)
        {
            c.raw = u.into();
        }
    }
}

fn item(page_url: &Url, formats: Vec<MediaLink>) -> MediaItem {
    MediaItem {
        id: "page".into(),
        title: None,
        page_url: page_url.clone(),
        duration_secs: None,
        thumbnail: None,
        formats,
    }
}

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
    for (k, v) in src.element_thumbnails {
        dst.element_thumbnails.entry(k).or_insert(v);
    }
    dst.embeds.extend(src.embeds);
}
