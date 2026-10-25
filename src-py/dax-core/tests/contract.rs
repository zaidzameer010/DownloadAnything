//! Contract tests for the Rust extractor's public API.
//!
//! The Python side sends camelCase options JSON and consumes `PageResult`
//! JSON; these tests pin that boundary and the end-to-end escalation path.

use std::io::{Read, Write};
use std::net::TcpListener;

use url::Url;

use dax_rs::api::{self, PageResult};
use dax_rs::config::{Config, Options};
use dax_rs::model::{ItemId, MediaItem, MediaKind, MediaLink, SourceLayer};
use dax_rs::net::Fetcher;

#[test]
fn options_deserialize_camel_case_from_python() {
    let options: Options = serde_json::from_str(
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

    assert_eq!(options.contexts.len(), 1);
    assert_eq!(options.contexts[0].headers[0].name, "Cookie");

    let config = Config::from(&options);
    assert_eq!(config.referer.as_deref(), Some("https://site.com/watch"));
    assert_eq!(config.user_agent, "CustomUA/1.0");
    assert_eq!(config.proxy.as_deref(), Some("http://127.0.0.1:8080"));
    assert!(!config.check_certificate);
    assert_eq!(config.contexts[0].host, "cdn.site.com");
}

#[test]
fn page_result_wire_shape_matches_python_contract() {
    let page_url = Url::parse("https://site.com/watch").unwrap();
    let item = MediaItem::page(
        page_url.clone(),
        vec![MediaLink::new(
            Url::parse("https://cdn.x/v.mp4").unwrap(),
            MediaKind::Progressive,
            SourceLayer::Html,
        )],
    );
    let result = PageResult {
        input: "https://site.com/watch".into(),
        final_url: Some(page_url),
        extractor: "rust",
        items: vec![item],
        error: None,
        error_code: None,
    };
    let json = serde_json::to_value(&result).unwrap();
    assert_eq!(json["extractor"], "rust");
    assert_eq!(json["items"][0]["id"], "page");
    assert_eq!(json["items"][0]["formats"][0]["kind"], "progressive");
    assert_eq!(json["items"][0]["formats"][0]["source"], "html");
    assert!(
        json.get("error").is_none(),
        "success must omit error: {json}"
    );
    assert!(
        json["items"][0]["formats"][0].get("headers").is_none(),
        "no referer means no headers object: {json}"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn unsupported_scheme_is_carried_not_thrown() {
    let config = Config::from(&Options::default());
    let fetcher = Fetcher::new(&config).unwrap();
    let input = "data:text/html,<b>x</b>";
    let results = api::extract(&fetcher, vec![input.into()]).await;
    assert_eq!(results.len(), 1);
    assert_eq!(results[0].input, input);
    assert_eq!(results[0].error_code, Some(1));
    assert!(results[0].final_url.is_none());
    assert!(results[0].items.is_empty());
    assert!(
        results[0]
            .error
            .as_deref()
            .is_some_and(|error| error.contains("unsupported scheme")),
        "{:?}",
        results[0].error
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn direct_urls_keep_input_order_and_kind() {
    let config = Config::from(&Options::default());
    let fetcher = Fetcher::new(&config).unwrap();
    let urls = vec![
        "https://cdn.x/a.mp4".to_owned(),
        "https://cdn.x/b.mp3".to_owned(),
    ];
    let results = api::extract(&fetcher, urls.clone()).await;
    assert_eq!(
        results
            .iter()
            .map(|result| result.input.clone())
            .collect::<Vec<_>>(),
        urls,
        "buffered extraction must preserve input order"
    );
    assert_eq!(results[0].items[0].id, ItemId::Direct);
    assert_eq!(results[0].items[0].formats[0].kind, MediaKind::Progressive);
    assert_eq!(
        results[0].items[0].formats[0].container.as_deref(),
        Some("mp4")
    );
    assert_eq!(results[1].items[0].formats[0].kind, MediaKind::Audio);
    assert_eq!(
        results[1].items[0].formats[0].container.as_deref(),
        Some("mp3")
    );
    assert!(results.iter().all(|result| result.error.is_none()));
}

#[tokio::test(flavor = "multi_thread")]
async fn extensionless_audio_response_keeps_audio_kind() {
    let port = serve(vec![("/stream", "audio/mpeg", String::new())]);
    let config = Config::from(&Options::default());
    let fetcher = Fetcher::new(&config).unwrap();
    let results = api::extract(&fetcher, vec![format!("http://127.0.0.1:{port}/stream")]).await;
    assert_eq!(results[0].items.len(), 1);
    assert_eq!(
        results[0].items[0].formats[0].kind,
        MediaKind::Audio,
        "an audio/ content type must win over the missing extension"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn page_without_media_reports_code_three() {
    let port = serve(vec![(
        "/empty",
        "text/html",
        "<html><body>nothing here</body></html>".to_owned(),
    )]);
    let config = Config::from(&Options::default());
    let fetcher = Fetcher::new(&config).unwrap();
    let results = api::extract(&fetcher, vec![format!("http://127.0.0.1:{port}/empty")]).await;
    assert!(results[0].items.is_empty());
    assert_eq!(results[0].error_code, Some(3));
    assert_eq!(results[0].error.as_deref(), Some("no media found on page"));
}

#[tokio::test(flavor = "multi_thread")]
async fn relative_endpoint_and_nested_iframe_escalate() {
    // A local page exercises the full escalation: a relative manifest
    // endpoint (JSON ladder) plus an iframe whose page embeds a second
    // iframe. Both levels must contribute media.
    let port = serve(vec![
        (
            "/page",
            "text/html",
            r#"<html><body>
                <a href="/media/hls/?s=1">ladder</a>
                <iframe src="/embed"></iframe>
            </body></html>"#
                .to_owned(),
        ),
        (
            "/embed",
            "text/html",
            r#"<html><body><iframe src="/embed2"></iframe></body></html>"#.to_owned(),
        ),
        (
            "/embed2",
            "text/html",
            r#"<html><body><video src="/v.mp4" width="640" height="360"></video></body></html>"#
                .to_owned(),
        ),
        (
            "/media/hls/",
            "application/json",
            r#"{"sources":[{"file":"/v2.mp4","label":"720p"}]}"#.to_owned(),
        ),
    ]);
    let config = Config::from(&Options::default());
    let fetcher = Fetcher::new(&config).unwrap();
    let input = format!("http://127.0.0.1:{port}/page");
    let results = api::extract(&fetcher, vec![input.clone()]).await;
    assert_eq!(results.len(), 1);
    let page = &results[0];
    assert_eq!(page.input, input);
    assert_eq!(page.extractor, "rust");
    assert_eq!(
        page.final_url.as_ref().map(Url::as_str),
        Some(input.as_str())
    );

    let formats: Vec<&MediaLink> = page
        .items
        .iter()
        .flat_map(|item| item.formats.iter())
        .collect();
    let height = |name: &str| {
        formats
            .iter()
            .find(|link| link.url.as_str().ends_with(name))
            .and_then(|link| link.quality.as_ref().and_then(|quality| quality.height))
    };
    assert_eq!(height("/v.mp4"), Some(360), "nested iframe media");
    assert_eq!(height("/v2.mp4"), Some(720), "relative endpoint ladder");
    assert!(
        !formats
            .iter()
            .any(|link| link.url.as_str().contains("/media/hls/")),
        "the JSON endpoint itself must not be a format: {formats:?}"
    );
}

/// Serve fixed routes on a loopback port for one test.
fn serve(routes: Vec<(&'static str, &'static str, String)>) -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    std::thread::spawn(move || {
        for stream in listener.incoming().take(32) {
            let Ok(mut stream) = stream else { continue };
            let routes = routes.clone();
            std::thread::spawn(move || {
                let mut buffer = [0u8; 4096];
                let read = stream.read(&mut buffer).unwrap_or(0);
                let request = String::from_utf8_lossy(&buffer[..read]);
                let path = request.split_whitespace().nth(1).unwrap_or("/");
                let (content_type, body) = routes
                    .iter()
                    .find(|(route, _, _)| *route == path)
                    .or_else(|| {
                        routes
                            .iter()
                            .find(|(route, _, _)| route.ends_with('/') && path.starts_with(route))
                    })
                    .map(|(_, content_type, body)| (*content_type, body.clone()))
                    .unwrap_or(("text/plain", String::new()));
                let _ = write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
            });
        }
    });
    port
}
