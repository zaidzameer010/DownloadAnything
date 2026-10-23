//! Contract tests for the custom Rust extractor bridge (src-py/dax-core).
//! The Python side sends camelCase options JSON and consumes PageResult JSON.

use std::borrow::Cow;

use dax_rs::config::Options;
use dax_rs::extract::candidates::{Candidate, resolve};
use dax_rs::model::{MediaKind, SourceLayer};

#[test]
fn options_deserialize_camel_case_from_python() {
    let o: Options = serde_json::from_str(
        r#"{
            "extractorEngine": "custom",
            "referer": "https://site.com/watch",
            "userAgent": "CustomUA/1.0",
            "proxy": "http://127.0.0.1:8080",
            "checkCertificate": false,
            "contexts": [
                {"url": "https://cdn.site.com/v.m3u8",
                 "headers": [{"name": "Cookie", "value": "sess=abc"}]}
            ]
        }"#,
    )
    .expect("python camelCase options must deserialize");

    assert_eq!(o.contexts.len(), 1);
    assert_eq!(o.contexts[0].headers[0].name, "Cookie");
    assert_eq!(o.referer.as_deref(), Some("https://site.com/watch"));
    assert_eq!(o.user_agent.as_deref(), Some("CustomUA/1.0"));
    assert_eq!(o.proxy.as_deref(), Some("http://127.0.0.1:8080"));
    assert_eq!(o.check_certificate, Some(false));
    assert_eq!(o.timeout_secs, None);

    let cfg = dax_rs::config::Config::from(&o);
    assert_eq!(cfg.referer.as_deref(), Some("https://site.com/watch"));
    assert_eq!(cfg.user_agent, "CustomUA/1.0");
    assert_eq!(cfg.proxy.as_deref(), Some("http://127.0.0.1:8080"));
    assert!(!cfg.check_certificate);
}

#[test]
fn config_defaults_match_dax_core() {
    let o: Options = serde_json::from_str("{}").unwrap();
    let cfg = dax_rs::config::Config::from(&o);
    assert_eq!(cfg.timeout_secs, 30);
    assert_eq!(cfg.retries, 3);
    assert!(cfg.check_certificate);
    assert!(cfg.context_headers.is_empty());
}

#[test]
fn candidate_resolve_carries_kind_quality_and_no_junk_field() {
    // Ranking/filtering lives in dax/filter.py — the wire link must stay
    // raw: kind + quality metadata, no junk verdict.
    let base = url::Url::parse("https://site.com/watch").unwrap();
    let c1 = Candidate::new("/media/video_720p.mp4", SourceLayer::Html);
    let c2 = Candidate::new(
        "https://cdn.site.com/hls/master.m3u8",
        SourceLayer::JsonBlob,
    );
    let c3 = Candidate::new(
        "https://cdn.site.com/dash/manifest.mpd",
        SourceLayer::JsonBlob,
    );
    let c4 = Candidate::new("https://cdn.site.com/audio/song.m4a", SourceLayer::JsonBlob);

    let l1 = resolve(&base, &c1).unwrap();
    assert_eq!(l1.kind, MediaKind::Progressive);
    assert_eq!(l1.quality.as_ref().unwrap().height, Some(720));
    assert_eq!(l1.container.as_deref(), Some("mp4"));
    assert_eq!(
        l1.headers.referer.as_deref(),
        Some("https://site.com/watch")
    );

    let l2 = resolve(&base, &c2).unwrap();
    assert_eq!(l2.kind, MediaKind::Hls);
    assert_eq!(resolve(&base, &c3).unwrap().kind, MediaKind::Dash);
    assert_eq!(resolve(&base, &c4).unwrap().kind, MediaKind::Audio);

    let json = serde_json::to_string(&l1).unwrap();
    assert!(
        !json.contains("\"junk\""),
        "junk verdicts are the Python filter's job: {json}"
    );
}

#[test]
fn filename_height_parses_codec_suffixed_variants() {
    // Multi-codec sites name variants `…-720p-av1.mp4`: the quality token is
    // not the last `-`/`_` segment, so codec-suffixed files must still yield
    // a height — anonymous ones ended up as "mp4_ - unknown" probe rows.
    let base = url::Url::parse("https://site.com/watch").unwrap();
    for (name, want) in [
        ("18548356-240p-av1.mp4", Some(240)),
        ("18548356-720p.mp4", Some(720)),
        ("clip_1080p_h264.mp4", Some(1080)),
        ("movie.720p.WEB-DL.mp4", Some(720)),
        ("18548356-av1.mp4", None),
        ("movie.mp4", None),
    ] {
        let c = Candidate::new(format!("https://cdn.site.com/dl/{name}"), SourceLayer::Html);
        let got = resolve(&base, &c).unwrap().quality.and_then(|q| q.height);
        assert_eq!(got, want, "{name}");
    }
}

#[test]
fn walk_json_collects_title_alongside_candidates() {
    // Player-config/JSON blobs carry the content title next to the media
    // URLs — first title-ish key wins; "name" is excluded (sources abuse it
    // as a quality label). Also grabs player preview/poster URLs.
    use dax_rs::extract::json_blobs::walk_json;
    let v: serde_json::Value = serde_json::from_str(
        r#"{"video_title": "Clean Name", "duration": 42, "preview_url": "https://cdn.x/preview.jpg",
            "sources": [{"file": "https://cdn.x/v.mp4", "label": "720p", "name": "720p"}]}"#,
    )
    .unwrap();
    let mut out = Vec::new();
    let mut dur = None;
    let mut title = None;
    let mut thumb = None;
    walk_json(&v, &mut out, &mut dur, &mut title, &mut thumb, 0);
    assert_eq!(title.as_deref(), Some("Clean Name"));
    assert_eq!(dur, Some(42.0));
    assert_eq!(thumb.as_deref(), Some("https://cdn.x/preview.jpg"));
    assert!(!out.is_empty(), "sources must still yield candidates");
}

#[test]
fn junk_thumbnail_filter_rejects_logos_and_icons() {
    use dax_rs::extract::meta::is_junk_thumbnail;
    // Real media thumbnails must pass
    assert!(!is_junk_thumbnail(
        "https://site.com/contents/videos_screenshots/617000/617043/preview.jpg"
    ));
    assert!(!is_junk_thumbnail(
        "https://site.com/images/posters/movie_1080p.png"
    ));
    assert!(!is_junk_thumbnail("/uploads/thumbs/clip_preview.webp"));

    // Site chrome, logos, avatars, icons, SVGs, and trackers must be rejected
    assert!(is_junk_thumbnail("https://site.com/static/images/logo.png"));
    assert!(is_junk_thumbnail("https://site.com/assets/vector-logo.svg"));
    assert!(is_junk_thumbnail("https://site.com/assets/favicon.ico"));
    assert!(is_junk_thumbnail("https://site.com/user/avatar_9921.jpg"));
    assert!(is_junk_thumbnail(
        "https://site.com/uploads/banners/promo_banner.jpg"
    ));
    assert!(is_junk_thumbnail("https://site.com/icons/play_button.png"));
    assert!(is_junk_thumbnail("https://site.com/tracking/pixel_1x1.png"));
    assert!(is_junk_thumbnail("https://site.com/assets/icon_32x32.png"));
    assert!(is_junk_thumbnail("https://site.com/static/no_thumb.jpg"));
    assert!(is_junk_thumbnail(
        "https://site.com/images/default_avatar.png"
    ));
    assert!(is_junk_thumbnail(
        "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    ));
}

#[test]
fn html_extracts_video_player_previews_and_container_posters() {
    use dax_rs::extract::html;
    use scraper::Html;
    use url::Url;

    let base = Url::parse("https://site.com/watch").unwrap();

    // 1. Video inside custom container with data-poster
    let html1 = r#"
        <div class="video-player" data-poster="https://cdn.x/player_preview.jpg">
            <video src="https://cdn.x/video1.mp4"></video>
        </div>
    "#;
    let doc1 = Html::parse_document(html1);
    let pass1 = html::run(&doc1, &base);
    assert_eq!(
        pass1.thumbnail.as_deref(),
        Some("https://cdn.x/player_preview.jpg")
    );
    assert_eq!(
        pass1.element_thumbnails.get(&1).map(String::as_str),
        Some("https://cdn.x/player_preview.jpg")
    );

    // 2. Video.js container with .vjs-poster background-image style
    let html2 = r#"
        <div class="video-js vjs-default-skin">
            <div class="vjs-poster" style="background-image: url('https://cdn.x/vjs_poster.jpg');"></div>
            <video src="https://cdn.x/video2.mp4"></video>
        </div>
    "#;
    let doc2 = Html::parse_document(html2);
    let pass2 = html::run(&doc2, &base);
    assert_eq!(
        pass2.thumbnail.as_deref(),
        Some("https://cdn.x/vjs_poster.jpg")
    );
    assert_eq!(
        pass2.element_thumbnails.get(&1).map(String::as_str),
        Some("https://cdn.x/vjs_poster.jpg")
    );
}

#[test]
fn multi_video_page_isolates_thumbnails_per_element() {
    use dax_rs::extract::group_items;
    use dax_rs::extract::meta::PageMeta;
    use dax_rs::model::{MediaKind, MediaLink, SourceLayer};
    use url::Url;

    let base = Url::parse("https://site.com/watch").unwrap();
    let mut meta = PageMeta {
        title: Some("Multi Video Page".into()),
        thumbnail: Some("https://site.com/general_header.jpg".into()),
        ..Default::default()
    };
    meta.element_thumbnails
        .insert(1, "https://site.com/video1_thumb.jpg".into());
    meta.element_thumbnails
        .insert(2, "https://site.com/video2_thumb.jpg".into());

    let mut l1 = MediaLink::new(
        Url::parse("https://site.com/v1.mp4").unwrap(),
        MediaKind::Progressive,
        SourceLayer::Html,
    );
    l1.media_index = Some(1);
    let mut l2 = MediaLink::new(
        Url::parse("https://site.com/v2.mp4").unwrap(),
        MediaKind::Progressive,
        SourceLayer::Html,
    );
    l2.media_index = Some(2);

    let items = group_items(&base, vec![l1, l2], &meta);
    assert_eq!(items.len(), 2);
    assert_eq!(items[0].id, "media-1");
    assert_eq!(
        items[0].thumbnail.as_ref().map(Url::as_str),
        Some("https://site.com/video1_thumb.jpg")
    );
    assert_eq!(items[1].id, "media-2");
    assert_eq!(
        items[1].thumbnail.as_ref().map(Url::as_str),
        Some("https://site.com/video2_thumb.jpg")
    );

    // If media-2 had no thumbnail on a multi-video page, it must NOT steal video 1 or page thumbnail
    meta.element_thumbnails.remove(&2);
    let mut l1_b = MediaLink::new(
        Url::parse("https://site.com/v1.mp4").unwrap(),
        MediaKind::Progressive,
        SourceLayer::Html,
    );
    l1_b.media_index = Some(1);
    let mut l2_b = MediaLink::new(
        Url::parse("https://site.com/v2.mp4").unwrap(),
        MediaKind::Progressive,
        SourceLayer::Html,
    );
    l2_b.media_index = Some(2);
    let items_b = group_items(&base, vec![l1_b, l2_b], &meta);
    assert_eq!(
        items_b[0].thumbnail.as_ref().map(Url::as_str),
        Some("https://site.com/video1_thumb.jpg")
    );
    assert_eq!(
        items_b[1].thumbnail, None,
        "secondary video without its own poster must not inherit wrong thumbnail"
    );
}

#[test]
fn multi_query_urls_classify_as_hls() {
    // `?multi=360p,720p,...` — one URL serving the whole ladder; the Python
    // filter ranks it with masters, and the probe break keys off kind=hls.
    let base = url::Url::parse("https://site.com/watch").unwrap();
    let c = Candidate::new(
        "https://cdn.site.com/video?multi=360p,720p,1080p",
        SourceLayer::Regex,
    );
    let l = resolve(&base, &c).unwrap();
    assert_eq!(l.kind, MediaKind::Hls);
}

#[test]
fn hls_path_urls_classify_as_hls() {
    // Extensionless signed manifest endpoints — `/media/hls/?s=…` carries
    // the stream in a base64 param, no .m3u8 extension to key on.
    let base = url::Url::parse("https://site.com/watch").unwrap();
    let c = Candidate::new(
        "https://site.com/media/hls/?s=eyJ2a2V5",
        SourceLayer::JsonBlob,
    );
    let l = resolve(&base, &c).unwrap();
    assert_eq!(l.kind, MediaKind::Hls);
}

#[test]
fn element_item_duplicating_page_url_merges_back() {
    // A <video> element whose src the page scan also found is the same
    // media — it must not become a second item.
    use dax_rs::extract::group_items;
    use dax_rs::extract::meta::PageMeta;
    use dax_rs::model::{MediaKind, MediaLink};

    let page = url::Url::parse("https://site.com/watch").unwrap();
    let dup = url::Url::parse("https://cdn.site.com/v.webm").unwrap();
    let page_level = MediaLink::new(dup.clone(), MediaKind::Progressive, SourceLayer::Regex);
    let mut element = MediaLink::new(dup, MediaKind::Progressive, SourceLayer::Html);
    element.media_index = Some(1);
    let mut other = MediaLink::new(
        url::Url::parse("https://cdn.site.com/other.mp4").unwrap(),
        MediaKind::Progressive,
        SourceLayer::Html,
    );
    other.media_index = Some(2);

    let items = group_items(
        &page,
        vec![page_level, element, other],
        &PageMeta::default(),
    );
    assert_eq!(items.len(), 2, "dup element folds into the page item");
    assert_eq!(items[0].formats.len(), 1);
    assert_eq!(items[1].id, "media-2");
}

#[test]
fn normalize_bare_host_port_and_scheme_rules() {
    // "localhost:8080" parses with fake scheme "localhost" — a digit after
    // the colon means it was really a bare host:port.
    let u = dax_rs::input::normalize("localhost:8080/v.m3u8").unwrap();
    assert_eq!(u.scheme(), "https");
    assert_eq!(u.host_str(), Some("localhost"));
    assert_eq!(u.port(), Some(8080));
    let u = dax_rs::input::normalize("example.com/watch#frag").unwrap();
    assert_eq!(u.as_str(), "https://example.com/watch", "fragments drop");
    // Real schemes keep their error class.
    assert!(dax_rs::input::normalize("ftp://x/v").is_err());
    assert!(dax_rs::input::normalize("data:text/html,<b>x</b>").is_err());
    assert!(dax_rs::input::normalize("mailto:a@b.c").is_err());
    assert!(dax_rs::input::normalize("   ").is_err());
    let u = dax_rs::input::normalize("http://a/b").unwrap();
    assert_eq!(u.scheme(), "http");
}

#[test]
fn decode_entities_single_pass() {
    use dax_rs::extract::unescape::decode_entities;
    assert_eq!(decode_entities("a&amp;b"), "a&b");
    assert_eq!(decode_entities("&quot;q&quot;&#39;t&#39;"), "\"q\"'t'");
    // One pass only: "&amp;lt;" decodes to "&lt;" — the old sequential
    // replace() chain decoded twice ("<"), turning text into markup.
    assert_eq!(decode_entities("&amp;lt;"), "&lt;");
    assert_eq!(
        decode_entities("https://x/?a=1&#x2F;b&#47;c"),
        "https://x/?a=1/b/c"
    );
    // No entity → no allocation.
    assert!(matches!(decode_entities("plain"), Cow::Borrowed("plain")));
}

#[test]
fn regex_scan_skips_token_free_bodies() {
    // Every regex alternative requires a media token, so the SIMD token
    // gate is exact: a token-free body can never produce a candidate.
    use dax_rs::extract::regex_scan::scan_text;
    assert!(scan_text("<html><body>no media here</body></html>").is_empty());
    assert!(!scan_text("https://cdn.x/v.mp4").is_empty());
    // 3gp is a regex extension — it must be in the token set too.
    assert!(!scan_text("https://cdn.x/v.3gp").is_empty());
    // Tokens assembled at scan time must not be skipped: JS concatenation
    // and \uXXXX escapes both produce a URL with no literal token in the body.
    assert!(!scan_text(r#""https://cdn.x/v." + "mp4""#).is_empty());
    assert!(!scan_text(r#""https://cdn.x/v\u002Emp4""#).is_empty());
}

#[test]
fn packer_unpack_borrows_without_eval() {
    use dax_rs::extract::packer::unpack;
    let plain = "<html><video src=\"v.mp4\"></video></html>";
    assert!(
        matches!(unpack(plain), Cow::Borrowed(_)),
        "no eval → no copy"
    );
    let packed = "eval(function(p,a,c,k,e,d){}('x',1,1,'y'.split('|')))";
    assert!(matches!(unpack(packed), Cow::Owned(_)), "eval → owned out");
}
#[test]
fn discovery_lists_track_python_filter_vocabulary() {
    // dax/filter.py is the ranking authority — Rust's discovery vocabulary
    // must agree with it or the two layers silently fork (wmv links missed,
    // logo thumbnails picked, xmulti= treated as a manifest).
    use dax_rs::extract::candidates::{contains_media_token, looks_media_str};
    use dax_rs::extract::meta::is_junk_thumbnail;
    use dax_rs::extract::regex_scan::scan_text;
    use dax_rs::input::{is_direct_media_url, looks_manifest_url};
    use url::Url;

    // Every VIDEO_EXTS/AUDIO_EXTS entry in filter.py must be discoverable:
    // direct-URL classify, JSON/attr token scan, and the last-resort regex.
    let wmv = Url::parse("https://cdn.x/v.wmv").unwrap();
    assert!(is_direct_media_url(&wmv), "wmv must be direct media");
    assert!(contains_media_token("https://cdn.x/v.wmv"));
    assert!(looks_media_str("https://cdn.x/v.wmv"));
    assert!(!scan_text("url=https://cdn.x/v.wmv").is_empty());
    assert!(!scan_text("url=https://cdn.x/v.avi").is_empty());

    // Junk-thumbnail tokens shared with _ASSET_TOKENS.
    for u in [
        "https://site.com/images/btn_play.png",
        "https://site.com/images/header-logo.png",
        "https://site.com/images/site-logo.png",
        "https://site.com/images/default_avatar.jpg",
    ] {
        assert!(is_junk_thumbnail(u), "{u} must be junk");
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
