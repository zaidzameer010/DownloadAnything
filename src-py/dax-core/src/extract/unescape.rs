use aho_corasick::AhoCorasick;
use std::borrow::Cow;
use std::sync::LazyLock;

/// Decode JS-style escapes (\/, \uXXXX, \xNN) and common HTML entities so
/// regex/JSON scanning sees the real URLs. Borrows the input when nothing
/// needed decoding — the common case for JSON string values.
pub fn unescape_js(s: &str) -> Cow<'_, str> {
    if !s.contains('\\') {
        return decode_entities(s);
    }
    let mut out = String::with_capacity(s.len());
    let mut it = s.chars().peekable();
    while let Some(c) = it.next() {
        if c != '\\' {
            out.push(c);
            continue;
        }
        match it.next() {
            Some('u') => {
                let hex: String = (0..4).filter_map(|_| it.next()).collect();
                match u32::from_str_radix(&hex, 16).ok().and_then(char::from_u32) {
                    Some(ch) => out.push(ch),
                    None => {
                        out.push_str("\\u");
                        out.push_str(&hex);
                    }
                }
            }
            Some('x') => {
                let hex: String = (0..2).filter_map(|_| it.next()).collect();
                match u32::from_str_radix(&hex, 16).ok().and_then(char::from_u32) {
                    Some(ch) => out.push(ch),
                    None => {
                        out.push_str("\\x");
                        out.push_str(&hex);
                    }
                }
            }
            Some('n') => out.push('\n'),
            Some('r') => out.push('\r'),
            Some('t') => out.push('\t'),
            // \/ \\ \" \' etc. — drop the backslash
            Some(other) => out.push(other),
            None => out.push('\\'),
        }
    }
    match decode_entities(&out) {
        Cow::Borrowed(_) => Cow::Owned(out),
        Cow::Owned(o) => Cow::Owned(o),
    }
}

/// Entity → replacement, matched in one aho-corasick pass. Single-decoding
/// is deliberate: "&amp;lt;" yields "&lt;", not "<" — the old sequential
/// .replace() chain decoded twice and could turn text into a tag.
static ENTITIES: LazyLock<(AhoCorasick, [&'static str; 9])> = LazyLock::new(|| {
    (
        AhoCorasick::new([
            "&amp;", "&quot;", "&#39;", "&#x27;", "&apos;", "&#x2F;", "&#47;", "&lt;", "&gt;",
        ])
        .unwrap(),
        ["&", "\"", "'", "'", "'", "/", "/", "<", ">"],
    )
});

/// Decode the entities that actually matter for URL extraction.
/// Borrows when no `&` is present.
pub fn decode_entities(s: &str) -> Cow<'_, str> {
    if !s.contains('&') {
        return Cow::Borrowed(s);
    }
    let (ac, reps) = &*ENTITIES;
    let mut out = String::with_capacity(s.len());
    let mut last = 0;
    for m in ac.find_iter(s) {
        out.push_str(&s[last..m.start()]);
        out.push_str(reps[m.pattern().as_usize()]);
        last = m.end();
    }
    out.push_str(&s[last..]);
    Cow::Owned(out)
}
