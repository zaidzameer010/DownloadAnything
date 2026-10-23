import type { ChromeDownloadMeta, DownloadInfo, DownloadsSnapshot } from './types'

/** Payload-done torrents still seeding count as completed everywhere. */
export function isDone(d: DownloadInfo): boolean {
  return d.status === 'complete' || d.status === 'seeding'
}

/** The merged task list — `removed` entries are ghosts the engine keeps
 *  reporting until purge; hide them everywhere. */
export function tasksFor(snap: DownloadsSnapshot): DownloadInfo[] {
  return [...snap.active, ...snap.waiting, ...snap.stopped].filter(
    (d) => d.status !== 'removed',
  )
}

export function taskName(d: DownloadInfo): string {
  const p = d.files?.[0]?.path
  const base = p?.split(/[\\/]/).pop()
  return (
    d.bittorrent?.name ||
    base ||
    d.dax?.filename ||
    d.dax?.filenameHint ||
    d.playlist?.title ||
    d.gid
  )
}

// ---- torrents ----------------------------------------------------------------

export function isMagnet(url: string): boolean {
  return url.trim().toLowerCase().startsWith('magnet:')
}

/** The magnet's dn parameter — the display name before metadata arrives. */
export function magnetName(url: string): string {
  const i = url.indexOf('?')
  if (i < 0) return ''
  try {
    return new URLSearchParams(url.slice(i + 1)).get('dn')?.trim() || ''
  } catch {
    return ''
  }
}

export function magnetInfoHash(url: string): string {
  const m = /urn:btih:([0-9a-zA-Z]{32,40})/i.exec(url)
  return m ? m[1].toLowerCase() : ''
}

/** An http(s) URL that names a .torrent file. */
export function isTorrentUrl(url: string): boolean {
  try {
    return /\.torrent$/i.test(new URL(url.trim()).pathname)
  } catch {
    return false
  }
}

/** A Chrome-intercepted download that is really a .torrent file. */
export function isTorrentDownload(dl: ChromeDownloadMeta): boolean {
  return (
    (dl.ext || '').toLowerCase() === 'torrent' ||
    (dl.mime || '').toLowerCase().includes('bittorrent')
  )
}

/** Longest common directory of a set of absolute paths — the torrent root,
 *  used to show file rows relative to it ('' when nothing is shared). */
export function commonDir(paths: string[]): string {
  const clean = paths.filter(Boolean)
  if (clean.length < 2) return ''
  const cut = (p: string) => {
    const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'))
    return i > 0 ? p.slice(0, i) : ''
  }
  let root = cut(clean[0])
  for (const p of clean.slice(1)) {
    const dir = cut(p)
    while (root && !dir.startsWith(root)) {
      const i = Math.max(root.lastIndexOf('/'), root.lastIndexOf('\\'))
      if (i <= 0) return ''
      root = root.slice(0, i)
    }
    if (!root) return ''
  }
  return root
}

/** A node in the folder tree — '' name is the torrent root itself. */
export interface FileTreeNode<T> {
  name: string
  dirs: FileTreeNode<T>[]
  files: { file: T; name: string }[]
}

/** Files nested into a directory tree under their common root — the folder
 *  view for torrent file lists. Order within each level is preserved. */
export function buildFileTree<T extends { path: string }>(files: T[]): FileTreeNode<T> {
  const root = commonDir(files.map((f) => f.path))
  const rel = (p: string) =>
    root && p.startsWith(root) ? p.slice(root.length).replace(/^[\\/]/, '') : p
  const tree: FileTreeNode<T> = { name: '', dirs: [], files: [] }
  const byPath = new Map<string, FileTreeNode<T>>([['', tree]])
  for (const file of files) {
    const segs = rel(file.path).split(/[\\/]/)
    let node = tree
    let prefix = ''
    for (const seg of segs.slice(0, -1)) {
      prefix = prefix ? `${prefix}/${seg}` : seg
      let child = byPath.get(prefix)
      if (!child) {
        child = { name: seg, dirs: [], files: [] }
        byPath.set(prefix, child)
        node.dirs.push(child)
      }
      node = child
    }
    node.files.push({ file, name: segs[segs.length - 1] })
  }
  return tree
}

/** 1-based indices → aria2's select-file syntax ("1-3,5"). */
export function formatSelection(indices: number[]): string {
  const nums = [...new Set(indices.filter((i) => i > 0))].sort((a, b) => a - b)
  const parts: string[] = []
  let start = 0
  let prev = 0
  for (const n of nums) {
    if (start === 0) {
      start = prev = n
    } else if (n === prev + 1) {
      prev = n
    } else {
      parts.push(start === prev ? String(start) : `${start}-${prev}`)
      start = prev = n
    }
  }
  if (start) parts.push(start === prev ? String(start) : `${start}-${prev}`)
  return parts.join(',')
}

export function fmtBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '0 B'
  // Decimal units — matches Finder/most tools (11.78MB shows as 11.8 MB).
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  while (n >= 1000 && i < units.length - 1) {
    n /= 1000
    i++
  }
  return `${n >= 100 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`
}

/** Seconds → h:mm:ss (or m:ss under an hour). */
export function fmtDuration(s: number): string {
  const t = Math.max(0, Math.round(s))
  const h = Math.floor(t / 3600)
  const m = Math.floor((t % 3600) / 60)
  const sec = t % 60
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
    : `${m}:${String(sec).padStart(2, '0')}`
}

/** Remaining-time estimate from bytes left and speed; null when unknown. */
export function fmtEta(completed: number, total: number, speed: number): string | null {
  if (total <= 0 || speed <= 0 || completed >= total) return null
  const s = Math.ceil((total - completed) / speed)
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
}

export function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

/** Filesystem-safe filename stem from a page title. */
export function safeFilename(t: string): string {
  return t
    .replace(/[\\/:*?"<>|\u0000-\u001f]/g, ' ')
    .replace(/\s+/g, ' ')
    .replace(/[. ]+$/, '')
    .trim()
    .slice(0, 150)
    .trim()
}
