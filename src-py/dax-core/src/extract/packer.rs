use crate::extract::unescape::unescape_js;
use regex::{Captures, Regex};
use std::borrow::Cow;
use std::sync::LazyLock;

/// Dean Edwards packer — `eval(function(p,a,c,k,e,d){…}('payload', radix,
/// count, 'k|k|…'.split('|'), 0, {}))`. JS video players commonly pack their
/// setup code this way to hide manifest URLs from static scans.
static PACKED: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r#"(?s)eval\s*\(\s*function\s*\(\s*p\s*,\s*a\s*,\s*c\s*,\s*k\s*,\s*e\s*,\s*d\s*\)\{.*?\}\s*\(\s*'((?:[^'\\]|\\.)*)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'((?:[^'\\]|\\.)*)'\s*\.\s*split\s*\(\s*'\|'\s*\)"#,
    )
    .unwrap()
});

static WORD: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\b[0-9a-z]+\b").unwrap());

/// Replace packed `eval(…)` blocks with their unpacked source. The packer is
/// pure word-substitution — nothing executes, so this is a safe string
/// transform. Unpackable matches are left as-is. Borrows the input when no
/// `eval(` is present — most pages don't pack.
pub fn unpack(text: &str) -> Cow<'_, str> {
    if !text.contains("eval(") {
        return Cow::Borrowed(text);
    }
    Cow::Owned(
        PACKED
            .replace_all(text, |caps: &Captures| {
                unpack_one(caps).unwrap_or_else(|| caps[0].to_string())
            })
            .into_owned(),
    )
}

fn unpack_one(caps: &Captures) -> Option<String> {
    let radix = caps[2]
        .parse::<u32>()
        .ok()
        .filter(|r| (2..=36).contains(r))?;
    let key_list = unescape_js(&caps[4]);
    let keys: Vec<&str> = key_list.split('|').collect();
    let payload = unescape_js(&caps[1]);
    // Single pass: inserted keys are never rescanned, so keys containing
    // token-like words survive intact (the JS original loops per token).
    // Indices beyond the key list stay literal, matching `if (k[c])` in JS.
    Some(
        WORD.replace_all(&payload, |w: &Captures| {
            match usize::from_str_radix(&w[0], radix) {
                Ok(i) if i < keys.len() && !keys[i].is_empty() => keys[i].to_string(),
                _ => w[0].to_string(),
            }
        })
        .into_owned(),
    )
}
