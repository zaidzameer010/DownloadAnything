//! Cross-layer parity checks between discovery vocabulary and `downloadanything/filter.py`.

use url::Url;

use crate::extract::candidates::{contains_media_token, looks_media_str};
use crate::extract::meta::is_junk_thumbnail;
use crate::extract::regex_scan::scan_text;
use crate::input::{is_direct_media_url, looks_manifest_url};

#[test]
fn discovery_lists_track_python_filter_vocabulary() {
    // downloadanything/filter.py is the ranking authority — Rust's discovery vocabulary
    // must agree with it or the two layers silently fork.

    // Every VIDEO_EXTS/AUDIO_EXTS entry in filter.py must be discoverable:
    // direct-URL classify, JSON/attr token scan, and the last-resort regex.
    for extension in crate::input::VIDEO_EXTS
        .iter()
        .chain(crate::input::AUDIO_EXTS)
    {
        let url = Url::parse(&format!("https://cdn.x/v.{extension}")).unwrap();
        assert!(
            is_direct_media_url(&url),
            "{extension} must be direct media"
        );
        assert!(
            contains_media_token(&format!("https://cdn.x/v.{extension}")),
            "{extension} must be a media token"
        );
        assert!(looks_media_str(&format!("https://cdn.x/v.{extension}")));
        assert!(
            !scan_text(&format!("url=https://cdn.x/v.{extension}")).is_empty(),
            "{extension} must survive the regex scan"
        );
    }
    assert!(looks_media_str("https://cdn.x/v.webm"));
    assert!(!scan_text("url=https://cdn.x/v.m4a").is_empty());

    // Retired containers stay undiscovered in every layer.
    for old in [
        "wmv", "avi", "3gp", "flv", "mpg", "mpeg", "ogg", "wma", "aiff", "m2ts",
    ] {
        let url = Url::parse(&format!("https://cdn.x/v.{old}")).unwrap();
        assert!(!is_direct_media_url(&url), "{old} must be retired");
        assert!(!contains_media_token(&format!("https://cdn.x/v.{old}")));
        assert!(scan_text(&format!("url=https://cdn.x/v.{old}")).is_empty());
    }

    // Junk-thumbnail tokens shared with _ASSET_TOKENS.
    for url in [
        "https://site.com/images/btn_play.png",
        "https://site.com/images/header-logo.png",
        "https://site.com/images/site-logo.png",
        "https://site.com/images/default_avatar.jpg",
    ] {
        assert!(is_junk_thumbnail(url), "{url} must be junk");
    }

    // multi= anchored like _MULTI_QUERY — `xmulti=` is not a manifest.
    assert!(!looks_manifest_url(
        &Url::parse("https://x/v?xmulti=360p,720p").unwrap()
    ));
    assert!(looks_manifest_url(
        &Url::parse("https://x/v?multi=360p,720p").unwrap()
    ));
    assert!(looks_manifest_url(
        &Url::parse("https://x/v?t=1&multi=360p").unwrap()
    ));

    // /manifest anywhere in the path (not just as a suffix), like role().
    assert!(looks_manifest_url(
        &Url::parse("https://x/manifest/v1?token=a").unwrap()
    ));
}
