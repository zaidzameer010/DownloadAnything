use crate::extract::candidates::Candidate;
use crate::extract::meta::PageMeta;
use crate::model::SourceLayer;
use scraper::{Html, Selector};
use serde_json::Value;
use std::sync::LazyLock;

static LD_SEL: LazyLock<Selector> =
    LazyLock::new(|| Selector::parse(r#"script[type="application/ld+json"]"#).unwrap());

/// schema.org JSON-LD: VideoObject/AudioObject → contentUrl/embedUrl/url.
pub fn extract(doc: &Html) -> (Vec<Candidate>, PageMeta) {
    let mut out = Vec::new();
    let mut meta = PageMeta::default();
    for el in doc.select(&LD_SEL) {
        let text = el.text().collect::<String>();
        let Ok(v) = serde_json::from_str::<Value>(&text) else {
            continue;
        };
        walk(&v, &mut out, &mut meta);
    }
    (out, meta)
}

/// Does the object's `@type` include a media type (VideoObject et al.)?
/// `@type` may be a string or an array — checked without collecting.
fn is_media_object(map: &serde_json::Map<String, Value>) -> bool {
    const MEDIA_TYPES: &[&str] = &["videoobject", "audioobject", "mediaobject"];
    let is_media = |t: &str| MEDIA_TYPES.contains(&t.to_lowercase().as_str());
    match map.get("@type") {
        Some(Value::String(s)) => is_media(s),
        Some(Value::Array(a)) => a.iter().filter_map(Value::as_str).any(is_media),
        _ => false,
    }
}

fn push_url(v: &Value, out: &mut Vec<Candidate>) {
    match v {
        Value::String(s) => out.push(Candidate::new(s.as_str(), SourceLayer::JsonLd)),
        Value::Array(a) => a.iter().for_each(|v| push_url(v, out)),
        Value::Object(o) => {
            if let Some(u) = o.get("url").and_then(Value::as_str) {
                out.push(Candidate::new(u, SourceLayer::JsonLd));
            }
        }
        _ => {}
    }
}

/// ISO-8601 duration like PT1H2M3S → seconds.
pub fn iso8601_duration(s: &str) -> Option<f64> {
    let s = s.strip_prefix('P')?;
    let t = s.strip_prefix('T').unwrap_or(s);
    let mut total = 0.0;
    let mut num = String::new();
    for c in t.chars() {
        if c.is_ascii_digit() || c == '.' {
            num.push(c);
        } else {
            let v: f64 = num.parse().ok()?;
            num.clear();
            total += match c {
                'H' => v * 3600.0,
                'M' => v * 60.0,
                'S' => v,
                'D' => v * 86400.0,
                _ => return None,
            };
        }
    }
    if num.is_empty() { Some(total) } else { None }
}

fn walk(v: &Value, out: &mut Vec<Candidate>, meta: &mut PageMeta) {
    match v {
        Value::Object(map) => {
            if is_media_object(map) {
                for k in ["contentUrl", "url"] {
                    if let Some(val) = map.get(k) {
                        push_url(val, out);
                    }
                }
                // Player embeds aren't downloadable — route direct-media
                // embedUrls to candidates, everything else to escalation.
                if let Some(e) = map.get("embedUrl").and_then(Value::as_str)
                    && !e.is_empty()
                {
                    if crate::extract::candidates::looks_media_str(e) {
                        out.push(Candidate::new(e, SourceLayer::JsonLd));
                    } else {
                        meta.embeds.push(e.to_string());
                    }
                }
                if meta.title.is_none() {
                    meta.title = map.get("name").and_then(Value::as_str).map(String::from);
                }
                if meta.duration_secs.is_none() {
                    meta.duration_secs = map
                        .get("duration")
                        .and_then(Value::as_str)
                        .and_then(iso8601_duration);
                }
                if meta.thumbnail.is_none() {
                    meta.thumbnail = map
                        .get("thumbnailUrl")
                        .or_else(|| map.get("thumbnail"))
                        .or_else(|| map.get("image"))
                        .and_then(|val| match val {
                            Value::String(s) => {
                                (!crate::extract::meta::is_junk_thumbnail(s.trim()))
                                    .then(|| s.trim().to_string())
                            }
                            Value::Array(a) => a.iter().find_map(|item| match item {
                                Value::String(s) => {
                                    (!crate::extract::meta::is_junk_thumbnail(s.trim()))
                                        .then(|| s.trim().to_string())
                                }
                                Value::Object(o) => o
                                    .get("url")
                                    .or_else(|| o.get("contentUrl"))
                                    .and_then(Value::as_str)
                                    .map(str::trim)
                                    .filter(|s| !crate::extract::meta::is_junk_thumbnail(s))
                                    .map(str::to_string),
                                _ => None,
                            }),
                            Value::Object(o) => o
                                .get("url")
                                .or_else(|| o.get("contentUrl"))
                                .and_then(Value::as_str)
                                .map(str::trim)
                                .filter(|s| !crate::extract::meta::is_junk_thumbnail(s))
                                .map(str::to_string),
                            _ => None,
                        });
                }
            }
            for val in map.values() {
                walk(val, out, meta);
            }
        }
        Value::Array(a) => a.iter().for_each(|v| walk(v, out, meta)),
        _ => {}
    }
}
