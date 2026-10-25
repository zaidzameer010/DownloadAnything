// Mirrors the backend's JSON output (src-py/dax-core/src/model.rs, src-py/dax/).
// Only fields the UI actually reads are declared — extra wire keys are ignored.

export type MediaKind = 'progressive' | 'hls' | 'dash' | 'audio'

export type SourceLayer =
  | 'html'
  | 'json_blob'
  | 'jsonld'
  | 'meta'
  | 'regex'
  | 'extractor'
  | 'direct'

export interface Quality {
  height?: number
  bandwidth?: number
  label?: string
}

export interface MediaLink {
  url: string
  kind: MediaKind
  quality?: Quality
  container?: string
  codecs?: string
  source: SourceLayer
  headers?: { referer?: string }
  expires_at?: number
  repr_id?: string
  manifest_url?: string
  /** The probe URL this row resolved from — authoritative page identity for
   *  the download's cached-info replay (rows can come from several probes). */
  page_url?: string
  /** Bytes the download's totalLength seeds from (filesize/filesize_approx,
   *  paired audio included for split rows). The task's own size — not the
   *  bandwidth×duration estimate. */
  probed_size?: number
}

export interface MediaItem {
  id: string
  title?: string
  page_url: string
  duration_secs?: number
  thumbnail?: string
  formats: MediaLink[]
}

/** One playlist listing entry — a flat yt-dlp stub (title/index/url, no
 *  formats). `index` is yt-dlp's 1-based playlist_index; picks and the
 *  download's playlist_items spec select by it. */
export interface PlaylistEntry {
  index: number
  id?: string
  url: string
  ie_key?: string
  title?: string
  duration_secs?: number
  thumbnail?: string
}

/** The Playlist tab's listing — produced when the probe URL resolves a
 *  playlist (or a "video AND playlist" page whose listing a second probe
 *  pass recovered). */
export interface PlaylistInfo {
  id?: string
  title?: string
  url?: string
  count?: number
  entries: PlaylistEntry[]
}

export interface PageResult {
  input: string
  final_url?: string
  /** Which extractor produced the result: yt-dlp | yt-dlp:generic | rust | extension. */
  extractor?: string
  items: MediaItem[]
  playlist?: PlaylistInfo
  error?: string
  error_code?: number
}

/** Chrome's DownloadItem forwarded by the extension on a download intercept —
 *  the fields the UI reads (resolved name, size, finalUrl, danger verdict). */
export interface ChromeDownloadMeta {
  url: string
  finalUrl?: string
  referrer?: string
  /** `filename`'s basename — authoritative filename on submit. */
  name?: string
  ext?: string
  mime?: string
  /** Post-decompression bytes; -1 when unknown. */
  fileSize?: number
  /** On-the-wire bytes; -1 when unknown. */
  totalBytes?: number
  startTime?: string
  danger?: string
  incognito?: boolean
}

// camelCase — per-call fields only; saved settings merge in server-side
// (app.py _m_extract). The backend's ExtractOptions accepts more keys
// (cookiesFile, timeoutSecs, ...) — the UI never sends them.
export interface ExtractOptions {
  /** Per-origin headers (cookies etc.) replayed on matching-host requests. */
  contexts?: RequestContext[]
  referer?: string
}

// ---- downloads / engine settings ----
// Mirrors the py backend's AppSettings (camelCase JSON on disk).

/** Named output folder — picked per download in the download dialog. */
export interface Category {
  name: string
  path: string
}

export interface AppSettings {
  // General & Server
  categories: Category[]
  apiPort: number
  apiToken: string

  // Extractor & Paths
  extractorEngine: 'auto' | 'python_ytdlp' | 'custom'
  /** auto only: off = pages without a dedicated yt-dlp extractor go straight
   *  to the Rust extractor (yt-dlp's slow generic page pass never runs). */
  ytdlpGeneric: boolean
  downloaderEngine: 'aria2_next' | 'ytdlp'
  playerClient:
    | 'default'
    | 'web'
    | 'web_safari'
    | 'web_embedded'
    | 'android'
    | 'android_vr'
    | 'ios'
    | 'visionos'
    | 'tv'
    | 'tv_samsung'
    | 'tv_simply'
    | 'mweb'
  ffmpegPath: string
  aria2NextPath: string
  aria2RpcPort: number
  aria2Split: number
  aria2MaxConnectionPerServer: number
  aria2MinSplitSize: string
  aria2FileAllocation: 'none' | 'prealloc' | 'falloc' | 'trunc'
  aria2MinTlsVersion: 'TLSv1.1' | 'TLSv1.2' | 'TLSv1.3'
  aria2MaxOverallDownloadLimit: string
  aria2MaxDownloadLimit: string
  aria2LowestSpeedLimit: string
  aria2RetryWait: number
  aria2StateSaveInterval: number

  // BitTorrent (aria2-next libtorrent)
  btSeedRatio: number
  /** Minutes to seed after the download; 0 = no time limit. */
  btSeedTime: number
  btMaxPeers: number
  btListenPort: number
  btEnableDht: boolean
  btEnablePex: boolean
  btEnableLpd: boolean
  btSequential: boolean
  /** Extra comma-separated tracker URIs (--bt-tracker). */
  btTrackers: string

  // Concurrency
  maxConcurrentDownloads: number
  parallelSegments: number

  // Network & Authentication
  cookiesFile: string
  cookiesBrowser: string
  timeoutSecs: number
  retries: number
  checkCertificate: boolean

  // Media & Post-processing
  mediaFormat: string
  embedThumbnail: boolean
  embedSubtitles: boolean
  embedChapters: boolean
  embedMetadata: boolean
}

export interface YtDlpInfo {
  available: boolean
  ytdlp_path?: string
  ffmpeg_path?: string
  aria2_next_available?: boolean
  aria2_next_running?: boolean
  aria2_next_path?: string
  aria2_next_version?: string
  aria2_next_rpc_port?: number
}

export interface EngineStatus {
  running: boolean
  version?: string
  error?: string
  downloaderEngine?: 'aria2_next' | 'ytdlp'
  aria2NextRunning?: boolean
}

export interface HeaderPair {
  name: string
  value: string
}

export interface RequestContext {
  url: string
  /** Page title on the tab the request was sniffed from — names the
   *  extension-sniffed fallback item when no extractor metadata exists. */
  title?: string
  headers: HeaderPair[]
  /** Extension capture classification — keeps extensionless streams and
   *  carries the resolution encoded in the URL. */
  kind?: string
  mime?: string
  label?: string
  height?: number
}

export interface DownloadReq {
  url: string
  createdAt?: number
  /** The extracted item's own title — the task row/details show it instead
   *  of only the resolved output filename. */
  title?: string
  /** Runtime of the picked item, seconds. */
  durationSecs?: number
  filenameHint?: string
  /** Verbatim output filename, extension included — Chrome-intercepted
   *  downloads keep the browser-resolved name exactly; never appended to. */
  filename?: string
  /** False skips every postprocessor (remux/embed) — non-media intercepts. */
  postprocess?: boolean
  referer?: string
  /** Cover art embedded into the finished file by backend postprocess. */
  thumbnail?: string
  media?: 'file' | 'hls' | 'dash'
  /** yt-dlp format_id of the selected row — page-URL tasks download the
   *  exact rendition the user picked. */
  reprId?: string
  /** The extracted item's webpage_url — the authoritative page for yt-dlp
   *  page-URL downloads (never the browser referer). */
  pageUrl?: string
  /** Captured request contexts — cookies replay into the download jar,
   *  scoped to the hosts the browser sent them on. */
  contexts?: RequestContext[]
  /** Quality identity of the picked row — the video format is matched at
   *  download time; audio is always the best available. */
  quality?: {
    height?: number
    bandwidth?: number
    audioOnly?: boolean
  }
  /** The probe's size for this row — seeds the task's initial totalLength
   *  so the download shows the number the probe displayed. */
  expectedSize?: number
  /** Category name from settings — resolves to its output path at task start. */
  category?: string
  /** Torrent adds: 'torrent' routes to the libtorrent path — magnet links
   *  ride `url`, picked .torrent bytes ride `torrent` (base64). */
  kind?: 'torrent'
  torrent?: string
  /** 1-based aria2 select-file string ("1-3,5") of the files to fetch. */
  selectFiles?: string
  /** Playlist adds: the picked entries under the playlist's metadata — a
   *  single task downloads them into <out>/<playlist title>/. `url` here
   *  (and the request's url) is the playlist's page URL. */
  playlist?: {
    title?: string
    url?: string
    /** Full playlist size — items are numbered `count - index + 1`. */
    count?: number
    items: {
      index: number
      id?: string
      url?: string
      title?: string
      ieKey?: string
      thumbnail?: string
      durationSecs?: number
    }[]
  }
}

/** check_download_name result — a collision in the resolved output dir
 *  (disk file or another task's pending name). suggested carries the
 *  incremented rename (`name (2)`; verbatim names keep their ext). */
export interface NameCheck {
  conflict: boolean
  dir?: string
  suggested?: string
}

export interface DownloadFile {
  path: string
  /** Torrent files only: aria2's 1-based index (select-file id). */
  index?: string
  length?: string
  completedLength?: string
  selected?: string
}

/** A torrent task's aria2 view — `state` (downloadingMetadata/checking/
 *  downloading/seeding) and `fileSelectionState` (awaiting) drive the
 *  seeding and file-picker UI. */
export interface TorrentInfo {
  infoHash?: string
  name?: string
  mode?: string
  state?: string
  fileSelectionState?: string
  numPeers?: string
  numSeeds?: string
  uploadSpeed?: string
  progress?: string
  availability?: string
  seedingTime?: string
}

/** inspect_torrent result — the picker previews the file list; `torrent` is
 *  the base64 bencode echoed back on the add so the URL is fetched once. */
export interface TorrentPreview {
  name: string
  mode?: string
  infoHashV1?: string
  infoHashV2?: string
  totalLength?: string
  files: { index: string; path: string; length: string }[]
  torrent: string
}

/** Live state of one playlist entry inside a playlist task — status rides
 *  waiting → active → complete/error; the accordion shows title + progress. */
export interface PlaylistTaskItem {
  index: number
  id?: string
  ie_key?: string
  url?: string
  title?: string
  thumbnail?: string | null
  duration_secs?: number | null
  status: 'waiting' | 'active' | 'complete' | 'error'
  downloaded: number
  total: number
  filename?: string
  error?: string
}

export interface DownloadInfo {
  gid: string
  status:
    | 'active'
    | 'waiting'
    | 'paused'
    | 'error'
    | 'complete'
    | 'removed'
    | 'postprocessing'
    | 'downloading'
    /** Payload done, aria2 still seeding — counted as completed. */
    | 'seeding'
    /** Magnet parked until the user picks files. */
    | 'selecting'
  totalLength?: string
  completedLength?: string
  downloadSpeed?: string
  files?: DownloadFile[]
  errorMessage?: string
  createdAt?: number
  /** Fragment progress (HLS/DASH) — the stable bar when the byte total is
   *  unknowable (VBR streams). */
  fragments?: { done: number; total: number }
  /** Torrent tasks only. */
  bittorrent?: TorrentInfo
  /** Playlist tasks — per-entry rows the task accordion renders. */
  playlist?: {
    title?: string
    url?: string
    count?: number
    items: PlaylistTaskItem[]
  }
  /** Stored request fields merged in from the app task db. */
  dax?: {
    url?: string
    filenameHint?: string
    filename?: string
    /** The extracted item's original title and runtime. */
    title?: string
    durationSecs?: number
    /** Quality identity of the picked row. */
    quality?: {
      height?: number
      width?: number
      bandwidth?: number
      audioOnly?: boolean
    }
    /** yt-dlp format_id of the picked row. */
    reprId?: string
    /** The authoritative page the row was extracted from. */
    pageUrl?: string
    thumbnail?: string
    referer?: string
    /** Request media bucket: file | hls | dash. */
    media?: string
    createdAt?: number
  }
}

export interface GlobalStat {
  downloadSpeed: string
}

export interface DownloadsSnapshot {
  stat?: GlobalStat | null
  active: DownloadInfo[]
  waiting: DownloadInfo[]
  stopped: DownloadInfo[]
  error?: string | null
}
