//! JS escape and HTML entity decoding for scanned source text.

use std::borrow::Cow;
use std::sync::LazyLock;

use aho_corasick::AhoCorasick;

/// Decode JS-style escapes (`\/`, `\uXXXX`, `\xNN`) and common HTML entities
/// so regex and JSON scanning see the real URLs.
///
/// Borrows the input when nothing needed decoding — the common case for JSON
/// string values.
pub(crate) fn unescape_js(text: &str) -> Cow<'_, str> {
    if !text.contains('\\') {
        return decode_entities(text);
    }
    let mut out = String::with_capacity(text.len());
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        if c != '\\' {
            out.push(c);
            continue;
        }
        match chars.next() {
            Some('u') => {
                let hex: String = (0..4).filter_map(|_| chars.next()).collect();
                if let Some(decoded) = decode_hex(&hex, 4) {
                    out.push(decoded);
                } else {
                    out.push_str("\\u");
                    out.push_str(&hex);
                }
            }
            Some('x') => {
                let hex: String = (0..2).filter_map(|_| chars.next()).collect();
                if let Some(decoded) = decode_hex(&hex, 2) {
                    out.push(decoded);
                } else {
                    out.push_str("\\x");
                    out.push_str(&hex);
                }
            }
            Some('n') => out.push('\n'),
            Some('r') => out.push('\r'),
            Some('t') => out.push('\t'),
            // \/ \\ \" \' etc. — drop the backslash.
            Some(other) => out.push(other),
            None => out.push('\\'),
        }
    }
    match decode_entities(&out) {
        Cow::Borrowed(_) => Cow::Owned(out),
        Cow::Owned(owned) => Cow::Owned(owned),
    }
}

/// Decode exactly `width` hex digits into one char.
fn decode_hex(hex: &str, width: usize) -> Option<char> {
    (hex.len() == width)
        .then(|| u32::from_str_radix(hex, 16).ok())
        .flatten()
        .and_then(char::from_u32)
}

/// Entity → replacement, matched in one Aho-Corasick pass.
///
/// Single decoding is deliberate: `&amp;lt;` yields `&lt;`, not `<` — a
/// sequential `.replace()` chain decodes twice and could turn text into a tag.
static ENTITIES: LazyLock<(AhoCorasick, [&'static str; 9])> = LazyLock::new(|| {
    (
        AhoCorasick::new([
            "&amp;", "&quot;", "&#39;", "&#x27;", "&apos;", "&#x2F;", "&#47;", "&lt;", "&gt;",
        ])
        .expect("html entity automaton"),
        ["&", "\"", "'", "'", "'", "/", "/", "<", ">"],
    )
});

/// Decode the entities that matter for URL extraction.
///
/// Borrows when no `&` is present.
pub(crate) fn decode_entities(text: &str) -> Cow<'_, str> {
    let (automaton, replacements) = &*ENTITIES;
    // A bare `&` (query separators are everywhere) is not an entity — the
    // automaton pass keeps the common case allocation-free.
    if !text.contains('&') || !automaton.is_match(text) {
        return Cow::Borrowed(text);
    }
    let mut out = String::with_capacity(text.len());
    let mut last = 0;
    for matched in automaton.find_iter(text) {
        out.push_str(&text[last..matched.start()]);
        out.push_str(replacements[matched.pattern().as_usize()]);
        last = matched.end();
    }
    out.push_str(&text[last..]);
    Cow::Owned(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decode_entities_single_pass() {
        assert_eq!(decode_entities("a&amp;b"), "a&b");
        assert_eq!(decode_entities("&quot;q&quot;&#39;t&#39;"), "\"q\"'t'");
        // One pass only: "&amp;lt;" decodes to "&lt;" — a sequential
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
    fn bare_ampersands_do_not_allocate() {
        for text in ["a & b", "https://x/?a=1&b=2", "&notanentity;"] {
            assert!(
                matches!(decode_entities(text), Cow::Borrowed(_)),
                "{text:?} must borrow"
            );
        }
    }

    #[test]
    fn js_escapes_decode_to_real_urls() {
        assert_eq!(
            unescape_js(r"https:\/\/cdn.x\/v.mp4"),
            "https://cdn.x/v.mp4"
        );
        assert_eq!(
            unescape_js(r"https://cdn.x/v\u002Emp4"),
            "https://cdn.x/v.mp4"
        );
        assert_eq!(
            unescape_js(r"https://cdn.x/v\x2Emp4"),
            "https://cdn.x/v.mp4"
        );
        // Truncated or invalid escapes stay literal.
        assert_eq!(unescape_js(r"\u12"), r"\u12");
        assert_eq!(unescape_js(r"\xZZ"), r"\xZZ");
        // No backslash and no entity → borrow.
        assert!(matches!(unescape_js("plain"), Cow::Borrowed("plain")));
    }
}
