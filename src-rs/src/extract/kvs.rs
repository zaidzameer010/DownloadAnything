//! KVS (Kernel Video Sharing) flashvars parsing and URL deobfuscation.

use std::borrow::Cow;
use std::sync::LazyLock;

use regex::Regex;

use crate::extract::candidates::{Candidate, height_from_label, looks_media_str};
use crate::extract::meta::is_junk_thumbnail;
use crate::extract::unescape::unescape_js;
use crate::model::{Quality, SourceLayer};

/// What one KVS flashvars blob yielded.
#[derive(Debug, Default)]
pub(crate) struct Flashvars {
    /// Media addresses, one per quality variant.
    pub candidates: Vec<Candidate>,
    /// Clean page title (`video_title`).
    pub title: Option<String>,
    /// Player preview/poster URL.
    pub thumbnail: Option<String>,
}

/// Extract media variants, title and preview from a KVS flashvars blob.
///
/// `video_url` / `video_alt_url<N>` hold the quality variants, `<key>_text`
/// the quality label, and license-protected sites obfuscate URLs as
/// `function/0/<url>` decoded via `license_code` (same algorithm as yt-dlp's
/// KVS extractor). Also grabs `video_title` and `preview_url` / `poster`.
pub(crate) fn flashvars_candidates(blob: &str) -> Flashvars {
    static PAIR: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r#"([A-Za-z0-9_]+)\s*:\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')"#)
            .expect("kvs flashvars pair regex")
    });
    let pairs: Vec<(String, Cow<'_, str>)> = PAIR
        .captures_iter(blob)
        .map(|captures| {
            let value = captures
                .get(2)
                .or_else(|| captures.get(3))
                .map(|value| value.as_str())
                .unwrap_or("");
            (captures[1].to_owned(), unescape_js(value))
        })
        .collect();
    let get = |key: &str| -> Option<&str> {
        pairs
            .iter()
            .find(|(candidate, _)| candidate == key)
            .map(|(_, value)| value.as_ref())
    };
    let license = get("license_code");
    let mut out = Vec::new();
    for (key, value) in &pairs {
        // KVS keys are strong keys — extensionless signed URLs are accepted.
        if !is_kvs_url_key(key) || !(looks_media_str(value) || value.starts_with("http")) {
            continue;
        }
        let mut url = value.clone();
        if url.starts_with("function/0/") {
            match license.and_then(|license| kvs_real_url(&url, license)) {
                Some(real) => url = Cow::Owned(real),
                None => continue,
            }
        }
        let mut candidate = Candidate::new(url, SourceLayer::JsonBlob);
        if let Some(label) = get(&format!("{key}_text")) {
            candidate.quality = Some(Quality {
                height: height_from_label(label),
                label: Some(label.to_owned()),
                ..Default::default()
            });
        }
        out.push(candidate);
    }
    let title = get("video_title")
        .filter(|title| !title.trim().is_empty())
        .map(str::to_owned);
    // Highest resolution or primary preview/poster from player flashvars.
    let thumbnail = [
        "preview_url7",
        "preview_url6",
        "preview_url5",
        "preview_url4",
        "preview_url3",
        "preview_url2",
        "preview_url1",
        "preview_url",
        "poster",
        "screen_url",
        "preview",
        "thumbnail",
        "thumb_url",
        "image",
    ]
    .iter()
    .find_map(|key| get(key))
    .map(str::trim)
    .filter(|url| !url.is_empty() && !is_junk_thumbnail(url))
    .map(str::to_owned);
    Flashvars {
        candidates: out,
        title,
        thumbnail,
    }
}

/// `video_url` or `video_alt_url`/`video_alt_url2`/… (digits-only suffix).
fn is_kvs_url_key(key: &str) -> bool {
    key == "video_url"
        || key
            .strip_prefix("video_alt_url")
            .is_some_and(|rest| rest.chars().all(|c| c.is_ascii_digit()))
}

/// Deobfuscate `function/0/<url>`: the 32-char hash path segment is shuffled
/// with the license token (yt-dlp `_kvs_get_real_url`).
fn kvs_real_url(video_url: &str, license_code: &str) -> Option<String> {
    let rest = video_url.strip_prefix("function/0/")?;
    let mut url = url::Url::parse(rest).ok()?;
    let token = kvs_license_token(license_code);
    let mut parts: Vec<String> = url.path().split('/').map(str::to_owned).collect();
    let hash = parts.get(3)?;
    let chars: Vec<char> = hash.chars().collect();
    if chars.len() < 32 {
        return None;
    }
    let mut indices: Vec<usize> = (0..32).collect();
    let mut accum: usize = 0;
    for source in (0..32).rev() {
        accum += token.get(source).copied().unwrap_or(0) as usize;
        let destination = (source + accum) % 32;
        indices.swap(source, destination);
    }
    let shuffled: String = indices.iter().map(|&index| chars[index]).collect();
    let tail: String = chars[32..].iter().collect();
    parts[3] = format!("{shuffled}{tail}");
    url.set_path(&parts.join("/"));
    Some(url.to_string())
}

/// KVS `license_code` → digit token stream (yt-dlp `_kvs_get_license_token`).
fn kvs_license_token(license_code: &str) -> Vec<u32> {
    let digits: String = license_code.chars().filter(char::is_ascii_digit).collect();
    if digits.is_empty() {
        return Vec::new();
    }
    let values: Vec<u32> = digits.chars().filter_map(|c| c.to_digit(10)).collect();
    let modified: String = digits
        .chars()
        .map(|c| if c == '0' { '1' } else { c })
        .collect();
    let center = modified.len() / 2;
    let front: u64 = modified[..=center].parse().unwrap_or(0);
    let back: u64 = modified[center..].parse().unwrap_or(0);
    let product = (4u64 * front.abs_diff(back)).to_string();
    let take = (center + 1).min(product.len());
    let mut token = Vec::new();
    for (index, c) in product[..take].chars().enumerate() {
        let Some(current) = c.to_digit(10) else {
            continue;
        };
        for offset in 0..4 {
            match values.get(index + offset) {
                Some(&value) => token.push((value + current) % 10),
                None => return token,
            }
        }
    }
    token
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kvs_keys_are_recognized_by_shape() {
        assert!(is_kvs_url_key("video_url"));
        assert!(is_kvs_url_key("video_alt_url"));
        assert!(is_kvs_url_key("video_alt_url2"));
        assert!(!is_kvs_url_key("video_alt_urlx"));
        assert!(!is_kvs_url_key("video_title"));
    }

    #[test]
    fn flashvars_yields_variants_with_quality_and_metadata() {
        let blob = r#"{
            video_url: 'https://cdn.x/v.mp4',
            video_url_text: '720p',
            video_alt_url: "https://cdn.x/api/stream/abc",
            video_alt_url_text: '1080p',
            video_title: 'Clean Name',
            preview_url: 'https://cdn.x/preview.jpg'
        }"#;
        let flashvars = flashvars_candidates(blob);
        assert_eq!(flashvars.candidates.len(), 2, "{:?}", flashvars.candidates);
        assert_eq!(
            flashvars.candidates[0].quality.as_ref().unwrap().height,
            Some(720)
        );
        assert_eq!(
            flashvars.candidates[1].quality.as_ref().unwrap().height,
            Some(1080)
        );
        assert_eq!(flashvars.title.as_deref(), Some("Clean Name"));
        assert_eq!(
            flashvars.thumbnail.as_deref(),
            Some("https://cdn.x/preview.jpg")
        );
    }

    #[test]
    fn empty_license_leaves_the_hash_untouched() {
        let hash = "0123456789abcdef0123456789abcdef";
        let input = format!("function/0/https://cdn.x/1/2/{hash}.mp4");
        let output = kvs_real_url(&input, "").expect("parseable url");
        assert_eq!(output, format!("https://cdn.x/1/2/{hash}.mp4"));
    }

    #[test]
    fn junk_preview_is_rejected() {
        let flashvars = flashvars_candidates(
            r#"{ video_url: 'https://cdn.x/v.mp4', preview_url: 'https://cdn.x/logo.png' }"#,
        );
        assert!(flashvars.thumbnail.is_none());
    }
}
