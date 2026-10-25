//! schema.org JSON-LD layer.

use std::sync::LazyLock;

use scraper::{Html, Selector};
use serde_json::Value;

use crate::extract::candidates::{Candidate, looks_media_str};
use crate::extract::meta::{PageMeta, is_junk_thumbnail};
use crate::extract::selector;
use crate::model::{Quality, SourceLayer};

static LD_SEL: LazyLock<Selector> =
    LazyLock::new(|| selector(r#"script[type="application/ld+json"]"#));

/// Maximum recursion depth for one JSON-LD document.
///
/// schema.org graphs are shallow; a bound keeps a hostile document off the
/// stack limit.
const MAX_LD_DEPTH: u8 = 32;

/// schema.org `@type` values that describe playable media.
const MEDIA_TYPES: &[&str] = &["videoobject", "audioobject", "mediaobject"];

/// Collect inline JSON-LD script bodies.
///
/// The DOM is `!Sync`, so the page pipeline extracts owned text serially and
/// parses it on a worker thread.
pub(crate) fn collect(document: &Html) -> Vec<String> {
    document
        .select(&LD_SEL)
        .map(|element| element.text().collect::<String>())
        .collect()
}

/// Extract `VideoObject`/`AudioObject` content URLs and page metadata.
pub(crate) fn extract(scripts: &[String]) -> (Vec<Candidate>, PageMeta) {
    let mut candidates = Vec::new();
    let mut meta = PageMeta::default();
    for text in scripts {
        let Ok(value) = serde_json::from_str::<Value>(text) else {
            continue;
        };
        walk(&value, &mut candidates, &mut meta, 0);
    }
    (candidates, meta)
}

/// Does the object's `@type` include a media type?
///
/// `@type` may be a string or an array — checked without collecting.
fn is_media_object(map: &serde_json::Map<String, Value>) -> bool {
    let is_media = |media_type: &str| MEDIA_TYPES.contains(&media_type.to_lowercase().as_str());
    match map.get("@type") {
        Some(Value::String(media_type)) => is_media(media_type),
        Some(Value::Array(types)) => types.iter().filter_map(Value::as_str).any(is_media),
        _ => false,
    }
}

/// Push a URL-shaped JSON-LD value (`string`, array of strings, `{"url": …}`).
fn push_url(value: &Value, out: &mut Vec<Candidate>, quality: Option<&Quality>) {
    match value {
        Value::String(url) => {
            let mut candidate = Candidate::new(url.as_str(), SourceLayer::JsonLd);
            candidate.quality = quality.cloned();
            out.push(candidate);
        }
        Value::Array(items) => items.iter().for_each(|item| push_url(item, out, quality)),
        Value::Object(object) => {
            if let Some(url) = object.get("url").and_then(Value::as_str) {
                let mut candidate = Candidate::new(url, SourceLayer::JsonLd);
                candidate.quality = quality.cloned();
                out.push(candidate);
            }
        }
        _ => {}
    }
}

/// `width`/`height` on a schema.org MediaObject — the resolution of the
/// `contentUrl`, carried onto every row the object yields.
fn media_quality(map: &serde_json::Map<String, Value>) -> Option<Quality> {
    let number = |key: &str| {
        map.get(key).and_then(|value| {
            value
                .as_u64()
                .or_else(|| value.as_str()?.trim().parse().ok())
        })
    };
    let height = number("height").and_then(|height| u32::try_from(height).ok());
    let width = number("width").and_then(|width| u32::try_from(width).ok());
    (height.is_some() || width.is_some()).then_some(Quality {
        height,
        width,
        bandwidth: None,
        label: None,
    })
}

/// ISO-8601 duration like `PT1H2M3S` → seconds.
///
/// A bare `P`/`PT` carries no components and is not a duration (it would
/// otherwise read as 0 seconds).
pub(crate) fn iso8601_duration(text: &str) -> Option<f64> {
    let text = text.strip_prefix('P')?;
    let text = text.strip_prefix('T').unwrap_or(text);
    let mut total = 0.0;
    let mut number = String::new();
    let mut components = 0u32;
    for c in text.chars() {
        if c.is_ascii_digit() || c == '.' {
            number.push(c);
        } else if c == 'T' {
            // Date/time separator in mixed forms (P1DT2H); a no-op.
            continue;
        } else {
            let value: f64 = number.parse().ok()?;
            number.clear();
            total += match c {
                'H' => value * 3600.0,
                'M' => value * 60.0,
                'S' => value,
                'D' => value * 86_400.0,
                _ => return None,
            };
            components += 1;
        }
    }
    (number.is_empty() && components > 0).then_some(total)
}

/// Walk a JSON-LD value, collecting media objects and page metadata.
fn walk(value: &Value, out: &mut Vec<Candidate>, meta: &mut PageMeta, depth: u8) {
    if depth > MAX_LD_DEPTH {
        return;
    }
    match value {
        Value::Object(map) => {
            if is_media_object(map) {
                let quality = media_quality(map);
                for key in ["contentUrl", "url"] {
                    if let Some(value) = map.get(key) {
                        push_url(value, out, quality.as_ref());
                    }
                }
                // Player embeds aren't downloadable — route direct-media
                // embedUrls to candidates, everything else to escalation.
                if let Some(embed) = map.get("embedUrl").and_then(Value::as_str)
                    && !embed.is_empty()
                {
                    if looks_media_str(embed) {
                        let mut candidate = Candidate::new(embed, SourceLayer::JsonLd);
                        candidate.quality = quality.clone();
                        out.push(candidate);
                    } else {
                        meta.embeds.push(embed.to_owned());
                    }
                }
                if meta.title.is_none() {
                    meta.title = map.get("name").and_then(Value::as_str).map(str::to_owned);
                }
                if meta.duration_secs.is_none() {
                    meta.duration_secs = map
                        .get("duration")
                        .and_then(Value::as_str)
                        .and_then(iso8601_duration);
                }
                if meta.thumbnail.is_none() {
                    meta.thumbnail = thumbnail_of(map);
                }
            }
            for value in map.values() {
                walk(value, out, meta, depth + 1);
            }
        }
        Value::Array(items) => items
            .iter()
            .for_each(|item| walk(item, out, meta, depth + 1)),
        _ => {}
    }
}

/// First non-junk thumbnail URL from a media object.
fn thumbnail_of(map: &serde_json::Map<String, Value>) -> Option<String> {
    let value = map
        .get("thumbnailUrl")
        .or_else(|| map.get("thumbnail"))
        .or_else(|| map.get("image"))?;
    match value {
        Value::String(url) => {
            let url = url.trim();
            (!is_junk_thumbnail(url)).then(|| url.to_owned())
        }
        Value::Array(items) => items.iter().find_map(thumbnail_item),
        Value::Object(object) => thumbnail_object(object),
        _ => None,
    }
}

/// Thumbnail from one array entry.
fn thumbnail_item(value: &Value) -> Option<String> {
    match value {
        Value::String(url) => {
            let url = url.trim();
            (!is_junk_thumbnail(url)).then(|| url.to_owned())
        }
        Value::Object(object) => thumbnail_object(object),
        _ => None,
    }
}

/// Thumbnail from an `ImageObject`-shaped value.
fn thumbnail_object(object: &serde_json::Map<String, Value>) -> Option<String> {
    object
        .get("url")
        .or_else(|| object.get("contentUrl"))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|url| !is_junk_thumbnail(url))
        .map(str::to_owned)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso8601_duration_rejects_componentless_values() {
        assert_eq!(iso8601_duration("PT1H2M3S"), Some(3723.0));
        assert_eq!(iso8601_duration("P1D"), Some(86_400.0));
        assert_eq!(
            iso8601_duration("P1DT2H"),
            Some(93_600.0),
            "mixed date/time"
        );
        assert_eq!(iso8601_duration("PT0S"), Some(0.0));
        // A bare designator is not a duration — it must not read as 0 seconds.
        assert_eq!(iso8601_duration("P"), None);
        assert_eq!(iso8601_duration("PT"), None);
        assert_eq!(
            iso8601_duration("PT1H2"),
            None,
            "trailing digits have no unit"
        );
    }

    #[test]
    fn media_object_carries_width_height_and_metadata() {
        let document = Html::parse_document(
            r#"<script type="application/ld+json">
            {"@type":"VideoObject","contentUrl":"https://cdn.x/v.mp4",
             "width":1920,"height":1080,"name":"Title","duration":"PT1M",
             "thumbnailUrl":"https://cdn.x/p.jpg"}
            </script>"#,
        );
        let scripts = collect(&document);
        let (candidates, meta) = extract(&scripts);
        let candidate = candidates
            .iter()
            .find(|candidate| candidate.raw.contains("v.mp4"))
            .expect("jsonld contentUrl");
        let quality = candidate.quality.as_ref().expect("jsonld resolution");
        assert_eq!((quality.width, quality.height), (Some(1920), Some(1080)));
        assert_eq!(meta.title.as_deref(), Some("Title"));
        assert_eq!(meta.duration_secs, Some(60.0));
        assert_eq!(meta.thumbnail.as_deref(), Some("https://cdn.x/p.jpg"));
    }

    #[test]
    fn embed_urls_route_by_media_likeness() {
        let document = Html::parse_document(
            r#"<script type="application/ld+json">
            {"@type":"VideoObject","embedUrl":"https://player.x/embed/1"}
            </script>"#,
        );
        let scripts = collect(&document);
        let (candidates, meta) = extract(&scripts);
        assert!(candidates.is_empty());
        assert_eq!(meta.embeds, vec!["https://player.x/embed/1"]);
    }

    #[test]
    fn depth_cap_stops_hostile_nesting() {
        let mut value =
            serde_json::json!({"@type": "VideoObject", "contentUrl": "https://cdn.x/v.mp4"});
        for _ in 0..100 {
            value = serde_json::json!({"nested": value});
        }
        let document = Html::parse_document(&format!(
            r#"<script type="application/ld+json">{value}</script>"#
        ));
        let scripts = collect(&document);
        let (candidates, _) = extract(&scripts);
        assert!(candidates.is_empty());
    }
}
