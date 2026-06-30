/**
 * filename_extractor.js — Filename and title extraction utility pipeline
 */
"use strict";

const safeDecode = (value) => {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
};

/** Best-effort human filename from a DownloadItem or URL. Never throws. */
function extractFilename(source, suggested = "") {
  let url = "";
  let filename = suggested;
  if (source && typeof source === "object") {
    url = source.url || "";
    filename = filename || source.filename || "";
  } else if (typeof source === "string") {
    url = source;
  }

  // GENERIC_NAMES is imported from constants.js

  const clean = (value) => {
    if (!value) return "";
    const base = value.split(/[/\\]/).pop().split("?")[0];
    if (!base || base.endsWith(".crdownload") || !base.includes(".")) return "";
    
    const stem = base.split(".").slice(0, -1).join(".").toLowerCase().trim();
    if (!stem || GENERIC_NAMES.has(stem)) return "";
    
    return safeDecode(base);
  };

  let resolved = "";
  if (clean(filename)) resolved = clean(filename);
  else if (url) {
    try {
      const parsed = new URL(url);
      const cleanPath = parsed.pathname.replace(/\/+$/, "");
      let foundQuery = "";
      for (const queryValue of parsed.searchParams.values()) {
        if (queryValue && /\.[a-z0-9]{2,5}$/i.test(queryValue)) {
          const parsedName = clean(queryValue);
          if (parsedName) {
            foundQuery = parsedName;
            break;
          }
        }
      }
      if (foundQuery) {
        resolved = foundQuery;
      } else {
        const segment = cleanPath.split("/").pop();
        if (segment && segment.includes(".")) resolved = safeDecode(segment);
      }
    } catch (err) {
      console.debug("[DownloadAnything] Failed parsing potential filename URL:", err);
    }
  }

  if (!resolved) resolved = "download";

  // If the resolved name has no extension, try to append one based on MIME type
  if (!resolved.includes(".") && source && typeof source === "object" && source.mime) {
    const matchedExt = MIME_TO_EXT[source.mime.toLowerCase()];
    if (matchedExt) {
      resolved = `${resolved}.${matchedExt}`;
    }
  }

  return resolved;
}
