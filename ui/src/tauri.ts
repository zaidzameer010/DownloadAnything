import type {
  AppSettings,
  ChromeDownloadMeta,
  DownloadReq,
  DownloadsSnapshot,
  EngineStatus,
  ExtractOptions,
  NameCheck,
  PageResult,
  RequestContext,
  TorrentPreview,
  YtDlpInfo,
} from './types'

/** Emitted by the local API when the browser extension forwards a media page
 *  or an intercepted Chrome download (download carries the DownloadItem). */
export interface PushPayload {
  url: string
  referer?: string
  title?: string
  thumbnail?: string
  contexts?: RequestContext[]
  download?: ChromeDownloadMeta
}

declare global {
  interface Window {
    __TAURI__?: {
      core: {
        invoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T>
      }
      event?: {
        listen<T>(
          event: string,
          handler: (e: { payload: T }) => void,
        ): Promise<() => void>
      }
      window?: {
        getCurrentWindow(): {
          unminimize(): Promise<void>
          setFocus(): Promise<void>
        }
      }
    }
    /** Injected by the backend's static index.html serving. */
    __DOWNLOADANYTHING_TOKEN__?: string
  }
}

// ---- transport: Tauri IPC inside the app shell, one WebSocket elsewhere ----
//
// Inside the Tauri webview invoke() needs no credentials. Everywhere else the
// backend's /ws carries everything on a single socket:
//   request  {"id":n,"method":…,"params":{…}} → {"id":n,"result"|"error":…}
//   server push {"type":"downloadanything-push","payload":{…}}
// The bearer token rides the query string — injected into index.html
// (__DOWNLOADANYTHING_TOKEN__) or stored in localStorage.downloadanythingToken (paste once, like the
// extension options page).

function tauriInvoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  const api = window.__TAURI__?.core.invoke
  if (!api) {
    return Promise.reject(new Error('Tauri runtime unavailable'))
  }
  return api<T>(cmd, args)
}

function token(): string {
  return window.__DOWNLOADANYTHING_TOKEN__ ?? localStorage.getItem('downloadanythingToken') ?? ''
}

/** Bearer token for dev/manual contexts — paste once, persists in localStorage. */
function setApiToken(t: string) {
  localStorage.setItem('downloadanythingToken', t)
}

// Vite dev pairing: open /?token=<api token> once — stores it like the
// extension options page does, then strips it back out of the address bar.
{
  const t = new URLSearchParams(location.search).get('token')
  if (t) {
    setApiToken(t)
    history.replaceState(null, '', location.pathname)
  }
}

// API port the backend listens on — updated from backend_info inside Tauri
// (settings.apiPort is user-configurable; the settings.py default is 16801).
let wsPort = 16801

function wsUrl(): string {
  const t = token()
  // Injected token (backend-served page): same origin, token in the query.
  // Stored token (paired dev/file/Tauri webview): loopback API directly.
  // No token (vite dev): same origin — the dev proxy injects it.
  const host = window.__DOWNLOADANYTHING_TOKEN__
    ? location.host
    : t
      ? `127.0.0.1:${wsPort}`
      : location.host || `127.0.0.1:${wsPort}`
  const isLoopback = host.startsWith('127.0.0.1') || host.startsWith('localhost')
  const proto = isLoopback ? 'ws' : (location.protocol === 'https:' ? 'wss' : 'ws')
  const q = t ? `?token=${encodeURIComponent(t)}` : ''
  return `${proto}://${host}/ws${q}`
}

/** Thumbnail proxy URL — CDNs that hotlink-protect 403 the webview's own
 *  Referer; the backend fetches with the page referer instead. */
export function thumbUrl(url?: string | null, referer?: string | null): string {
  if (!url || url.startsWith('data:')) return url ?? ''
  const t = token()
  if (!t) return url
  const base = window.__DOWNLOADANYTHING_TOKEN__ ? '' : `http://127.0.0.1:${wsPort}`
  const q = new URLSearchParams({ u: url, token: t })
  if (referer) q.set('r', referer)
  return `${base}/thumb?${q.toString()}`
}

interface Pending {
  resolve: (v: unknown) => void
  reject: (e: Error) => void
}

let sock: WebSocket | null = null
let opening: Promise<void> | null = null
let nextId = 1
let retryTimer: ReturnType<typeof setTimeout> | null = null
let retries = 0
const pending = new Map<number, Pending>()
const pushListeners = new Set<(p: PushPayload) => void>()
const dlListeners = new Set<(s: DownloadsSnapshot) => void>()
const engListeners = new Set<(e: EngineStatus) => void>()
let lastDownloads: DownloadsSnapshot | null = null
let lastEngine: EngineStatus | null = null

let warnedNoToken = false

// Inside the Tauri webview nothing injects __DOWNLOADANYTHING_TOKEN__ (the page comes from
// frontendDist, not the server), so fetch token+port over IPC once —
// backend_info reads the settings row in downloadanything.db the backend owns.
let backendInfo: Promise<void> | null = null

function ensureBackendInfo(): Promise<void> {
  if (!inTauri()) return Promise.resolve()
  backendInfo ??= tauriInvoke<{ token: string; port: number }>('backend_info')
    .then((i) => {
      wsPort = i.port || 16801
      setApiToken(i.token)
    })
    .catch((e) => {
      backendInfo = null // backend still booting — retry on next connect
      throw e
    })
  return backendInfo
}

function wsOpen(): Promise<void> {
  if (sock && sock.readyState === WebSocket.OPEN) return Promise.resolve()
  if (opening) return opening
  const p = (async () => {
    try {
      await ensureBackendInfo()
      await wsConnect()
    } catch (e) {
      scheduleReconnect()
      throw e
    } finally {
      opening = null
    }
  })()
  opening = p
  return p
}

function wsConnect(): Promise<void> {
  if (!token() && !warnedNoToken && !inTauri()) {
    warnedNoToken = true
    console.error(
      '[downloadanything] no api token — open this page with ?token=<api token> once ' +
        '(it persists to localStorage), or serve the UI from ' +
        `http://127.0.0.1:${wsPort}/ which injects it automatically`,
    )
  }
  return new Promise((resolve, reject) => {
    let settled = false
    const ws = new WebSocket(wsUrl())
    sock = ws
    ws.onopen = () => {
      retries = 0
      settled = true
      resolve()
    }
    ws.onmessage = (ev) => {
      let msg: { id?: number; result?: unknown; error?: string; type?: string; payload?: unknown }
      try {
        msg = JSON.parse(ev.data as string)
      } catch {
        return
      }
      if (msg.type === 'downloadanything-push' && msg.payload) {
        pushListeners.forEach((cb) => cb(msg.payload as PushPayload))
        return
      }
      if (msg.type === 'downloads-snapshot') {
        lastDownloads = msg.payload as DownloadsSnapshot
        dlListeners.forEach((cb) => cb(lastDownloads!))
        return
      }
      if (msg.type === 'engine-status') {
        lastEngine = msg.payload as EngineStatus
        engListeners.forEach((cb) => cb(lastEngine!))
        return
      }
      if (typeof msg.id === 'number') {
        const p = pending.get(msg.id)
        if (p) {
          pending.delete(msg.id)
          if (msg.error !== undefined) p.reject(new Error(msg.error))
          else p.resolve(msg.result)
        }
      }
    }
    ws.onerror = () => {
      if (!settled) {
        settled = true
        reject(new Error('ws connect failed'))
      }
    }
    ws.onclose = (ev) => {
      if (sock === ws) sock = null
      opening = null
      if (!settled) {
        settled = true
        reject(new Error(`ws closed before open: ${ev.code}`))
      }
      // 4401 = backend rejected the token — reconnecting won't help unless in Tauri where token can refresh.
      const fatal = ev.code === 4401 && !inTauri()
      if (fatal && !warnedNoToken) {
        warnedNoToken = true
        console.error(
          `[downloadanything] websocket rejected (code ${ev.code}) — bad or missing ` +
            'api token; open this page with ?token=<api token> once',
        )
      }
      const err = new Error('backend connection lost')
      pending.forEach((p) => p.reject(err))
      pending.clear()
      if (!fatal) {
        if (inTauri() && ev.code === 4401) {
          backendInfo = null
        }
        scheduleReconnect()
      }
    }
  })
}

function scheduleReconnect() {
  const listeners = pushListeners.size + dlListeners.size + engListeners.size
  if (retryTimer || listeners === 0) return
  // Lazy: only keep the socket alive while something listens for events —
  // commands reconnect on demand.
  const delay = Math.min(1000 * 2 ** retries++, 15000)
  retryTimer = setTimeout(() => {
    retryTimer = null
    wsOpen().catch(() => {})
  }, delay)
}

async function rpc<T>(method: string, params?: Record<string, unknown>): Promise<T> {
  await wsOpen()
  const ws = sock
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    throw new Error('backend connection lost')
  }
  const id = nextId++
  return new Promise<T>((resolve, reject) => {
    pending.set(id, {
      resolve: resolve as (v: unknown) => void,
      reject,
    })
    ws.send(JSON.stringify({ id, method, params: params ?? {} }))
  })
}

export const inTauri = () => !!window.__TAURI__?.core?.invoke

export function extractMedia(
  urls: string[],
  options: ExtractOptions,
): Promise<PageResult[]> {
  return rpc<PageResult[]>('extract_media', { urls, options })
}

export const getAppSettings = () => rpc<AppSettings>('get_app_settings')

/** Sends the whole settings object — omitted keys reset to backend
 *  defaults (AppSettings.from_dict), so {} restores factory settings. */
export function saveAppSettings(settings: Partial<AppSettings>) {
  return rpc<void>('save_app_settings', { settings })
}

export async function regenerateTokens() {
  const s = await rpc<AppSettings>('regenerate_tokens')
  if (s.apiToken) {
    setApiToken(s.apiToken)
    if (window.__DOWNLOADANYTHING_TOKEN__) window.__DOWNLOADANYTHING_TOKEN__ = s.apiToken
  }
  return s
}

export const engineStatus = () => rpc<EngineStatus>('engine_status')

export const getYtDlpInfo = () => rpc<YtDlpInfo>('ytdlp_info')

export function downloadControl(gid: string, action: string) {
  return rpc<unknown>('download_control', { gid, action })
}

export function addDownload(req: DownloadReq) {
  return rpc<{ gid: string }>('add_download', { req }).then((r) => r.gid)
}

/** Pre-flight filename collision probe — output dir on disk + the task
 *  list — before a download is added. */
export function checkDownloadName(req: DownloadReq) {
  return rpc<NameCheck>('check_download_name', { req })
}

/** Describe a .torrent (URL or base64 bytes) without adding it — the picker
 *  previews the file list and echoes `torrent` back on the add. */
export function inspectTorrent(args: {
  url?: string
  data?: string
  referer?: string
  contexts?: RequestContext[]
}) {
  return rpc<TorrentPreview>('inspect_torrent', args)
}

/** Apply the picker's file selection to a magnet parked by pause-metadata
 *  and start its download. */
export function setTorrentFiles(gid: string, indices: number[]) {
  return rpc<{ ok: boolean; selectFile: string }>('set_torrent_files', {
    gid,
    indices,
  })
}

/** UI-side failures land in downloadanything.log next to the backend's. Fire-and-forget. */
export function logError(context: string, e: unknown) {
  console.error(`[${context}]`, e)
  const detail = e instanceof Error ? (e.stack || e.message) : String(e)
  rpc('log_ui_error', { message: `${context}: ${detail}` }).catch(() => {})
}

/** Server-pushed events — WS frames only, the single realtime channel the
 *  backend has. Returns the unsubscribe fn. */
async function listen<T>(
  set: Set<(v: T) => void>,
  cb: (v: T) => void,
): Promise<() => void> {
  set.add(cb)
  wsOpen().catch(() => {})
  return () => {
    set.delete(cb)
  }
}

/** Extension → ws `push` method → "downloadanything-push". */
export const onPush = (cb: (p: PushPayload) => void) => listen(pushListeners, cb)

/** Live task-state snapshots broadcast by the backend. */
export const onDownloads = (cb: (s: DownloadsSnapshot) => void) => {
  if (lastDownloads) cb(lastDownloads)
  return listen(dlListeners, cb)
}

export const onEngine = (cb: (e: EngineStatus) => void) => {
  if (lastEngine) cb(lastEngine)
  return listen(engListeners, cb)
}

/** Best-effort window raise when a push arrives while unfocused. */
export async function focusWindow() {
  try {
    const w = window.__TAURI__?.window?.getCurrentWindow()
    await w?.unminimize()
    await w?.setFocus()
  } catch {
    /* focus is best-effort */
  }
}

/** Payload of `downloadanything-update-ready` — emitted by the Rust updater task after a
 *  newer release was downloaded and installed. */
export interface UpdateReady {
  version: string
  notes?: string
}

/** Fires once per launch when an update has been installed — the banner owns
 *  the restart since it kills the Python backend. */
export function onUpdateReady(cb: (u: UpdateReady) => void): Promise<() => void> {
  const listen = window.__TAURI__?.event?.listen
  if (!listen) return Promise.resolve(() => {})
  return listen<UpdateReady>('downloadanything-update-ready', (e) => cb(e.payload))
}

/** Restarts the app to apply an installed update. */
export function restartApp() {
  return tauriInvoke<void>('restart_app')
}

export interface BrowserInfo {
  id: string
  name: string
  path: string
  running: boolean
}

export interface ExtensionSetup {
  path: string
  browsers: BrowserInfo[]
}

export interface InstallOutcome {
  sessionLoad: boolean
  needsRestart: boolean
}

/** Bundled extension folder plus every Chromium-based browser found locally. */
export function extensionSetup() {
  return tauriInvoke<ExtensionSetup>('extension_setup')
}

/** Opens the browser's extensions page and reveals the bundled folder. */
export function installExtension(id: string) {
  return tauriInvoke<InstallOutcome>('install_extension', { id })
}

/** Quits the browser, then relaunches it with the extension loaded. */
export function restartBrowserWithExtension(id: string) {
  return tauriInvoke<InstallOutcome>('restart_browser_with_extension', { id })
}

/** Reveals the bundled extension folder in Finder/Explorer. */
export function revealExtension() {
  return tauriInvoke<void>('reveal_extension')
}
