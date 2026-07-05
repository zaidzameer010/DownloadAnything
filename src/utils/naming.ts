import type { Task } from "../types";

const GENERIC_NAMES = new Set([
  "download", "index", "master", "playlist", "stream", "video", "audio",
  "media", "manifest", "chunklist", "output", "main", "live", "hls", "dash",
  "m3u8", "mpd", "ts", "chunk", "segment", "fragment", "part", "track",
]);

const SITE_SUFFIXES = [
  "YouTube", "Twitch", "Vimeo", "Netflix", "Disney+", "TikTok", "Twitter",
  "X", "Facebook", "Instagram", "Reddit", "Dailymotion", "Rumble", "Bilibili",
  "Odysee", "PeerTube", "Niconico", "SoundCloud", "Spotify", "Prime Video", "Apple TV",
];

const cleanCandidate = (value: string | null | undefined): string => {
  if (!value) return "";
  let text = String(value).trim();
  const withoutQuery = text.split("?")[0] || "";
  const withoutFragment = withoutQuery.split("#")[0] || "";
  text = withoutFragment.trim();
  const pathSegment = text.split(/[/\\]/).pop();
  text = (pathSegment || text).trim();
  const extMatch = text.match(/^(.*)\.([a-z0-9]{2,5})$/i);
  if (extMatch) {
    const stem = extMatch[1] || "";
    if (stem && !GENERIC_NAMES.has(stem.toLowerCase())) {
      text = stem.trim();
    }
  }
  const lowered = text.toLowerCase();
  for (const suffix of SITE_SUFFIXES) {
    const dashToken = ` - ${suffix}`.toLowerCase();
    if (lowered.endsWith(dashToken)) {
      return text.slice(0, -dashToken.length).trim();
    }
    const pipeToken = ` | ${suffix}`.toLowerCase();
    if (lowered.endsWith(pipeToken)) {
      return text.slice(0, -pipeToken.length).trim();
    }
  }
  return text.replace(/\s*[-|·•–—]\s*(YouTube|Vimeo|Twitch|Dailymotion|Twitter|X|Facebook|Instagram|TikTok|Reddit|Bilibili|Rumble|Odysee|PeerTube|Niconico|SoundCloud|Spotify|Netflix|Prime Video|Disney\+|Apple TV)\s*$/i, "").trim();
};

const isGeneric = (value: string): boolean => {
  const candidate = cleanCandidate(value).toLowerCase();
  if (!candidate) return true;
  const bare = candidate.replace(/\.[a-z0-9]{2,5}$/i, "").trim();
  return GENERIC_NAMES.has(candidate) || GENERIC_NAMES.has(bare);
};

const basenameFromValue = (value: string | null | undefined): string => {
  if (!value) return "";
  const raw = String(value).trim();
  if (!raw) return "";
  try {
    const url = new URL(raw);
    return url.pathname.split("/").filter(Boolean).pop() || "";
  } catch {
    return raw.split(/[/\\]/).filter(Boolean).pop() || "";
  }
};

export function displayTaskTitle(task: Pick<Task, "title" | "url" | "page_title" | "filename" | "final_path">): string {
  const title = cleanCandidate(task.title);
  if (title && !isGeneric(title)) return title;

  const pageTitle = cleanCandidate(task.page_title);
  if (pageTitle && !isGeneric(pageTitle)) return pageTitle;

  const fromFile = cleanCandidate(basenameFromValue(task.filename) || basenameFromValue(task.final_path));
  if (fromFile && !isGeneric(fromFile)) return fromFile;

  const fromUrl = cleanCandidate(basenameFromValue(task.url));
  if (fromUrl && !isGeneric(fromUrl)) return fromUrl;

  return title || pageTitle || fromFile || fromUrl || "Download Task";
}

export function displayTaskFileName(task: Pick<Task, "filename" | "final_path" | "url">): string {
  return basenameFromValue(task.final_path) || basenameFromValue(task.filename) || basenameFromValue(task.url) || "—";
}

export function taskSearchText(task: Pick<Task, "title" | "page_title" | "filename" | "final_path" | "url">): string {
  return [
    displayTaskTitle(task),
    displayTaskFileName(task),
    task.page_title || "",
    task.url || "",
  ]
    .join(" ")
    .toLowerCase();
}