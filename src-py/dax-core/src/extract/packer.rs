//! Dean Edwards packer decoder.

use std::borrow::Cow;
use std::sync::LazyLock;

use regex::{Captures, Regex};

use crate::extract::unescape::unescape_js;

/// `eval(function(p,a,c,k,e,d){…}('payload', radix, count, 'k|k|…'.split('|'), 0, {}))`.
///
/// JS video players commonly pack their setup code this way to hide manifest
/// URLs from static scans.
static PACKED: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r#"(?s)eval\s*\(\s*function\s*\(\s*p\s*,\s*a\s*,\s*c\s*,\s*k\s*,\s*e\s*,\s*d\s*\)\{.*?\}\s*\(\s*'((?:[^'\\]|\\.)*)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*'((?:[^'\\]|\\.)*)'\s*\.\s*split\s*\(\s*'\|'\s*\)"#,
    )
    .expect("dean edwards packer regex")
});

/// Words in the packed payload that may be dictionary references.
static WORD: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\b[0-9a-z]+\b").expect("packer word regex"));

/// Replace packed `eval(…)` blocks with their unpacked source.
///
/// The packer is pure word substitution — nothing executes, so this is a safe
/// string transform. Unpackable matches are left as-is. Borrows the input when
/// no `eval(` is present, which is the common case.
pub(crate) fn unpack(text: &str) -> Cow<'_, str> {
    if !text.contains("eval(") {
        return Cow::Borrowed(text);
    }
    Cow::Owned(
        PACKED
            .replace_all(text, |captures: &Captures| {
                unpack_one(captures).unwrap_or_else(|| captures[0].to_owned())
            })
            .into_owned(),
    )
}

/// Decode one packer match, or `None` when its radix is unusable.
fn unpack_one(captures: &Captures) -> Option<String> {
    let radix = captures[2]
        .parse::<u32>()
        .ok()
        .filter(|radix| (2..=36).contains(radix))?;
    let key_list = unescape_js(&captures[4]);
    let keys: Vec<&str> = key_list.split('|').collect();
    let payload = unescape_js(&captures[1]);
    // Single pass: inserted keys are never rescanned, so keys containing
    // token-like words survive intact (the JS original loops per token).
    // Indices beyond the key list stay literal, matching `if (k[c])` in JS.
    Some(
        WORD.replace_all(&payload, |word: &Captures| {
            match usize::from_str_radix(&word[0], radix) {
                Ok(index) if index < keys.len() && !keys[index].is_empty() => {
                    keys[index].to_owned()
                }
                _ => word[0].to_owned(),
            }
        })
        .into_owned(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn borrows_without_eval() {
        let plain = "<html><video src=\"v.mp4\"></video></html>";
        assert!(
            matches!(unpack(plain), Cow::Borrowed(_)),
            "no eval → no copy"
        );
    }

    #[test]
    fn unpacks_a_simple_payload() {
        let packed =
            "eval(function(p,a,c,k,e,d){}('1 0',10,2,'v.mp4|https://cdn.x/'.split('|'),0,{}))";
        let unpacked = unpack(packed);
        assert!(matches!(unpacked, Cow::Owned(_)), "eval → owned out");
        assert!(
            unpacked.contains("https://cdn.x/ v.mp4"),
            "payload must be decoded: {unpacked}"
        );
    }

    #[test]
    fn leaves_unparseable_payload_untouched() {
        let packed = "eval(function(p,a,c,k,e,d){}('x',1,1,'y'.split('|')))";
        assert!(unpack(packed).contains("eval("));
    }
}
