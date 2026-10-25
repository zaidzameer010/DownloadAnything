// The UI's single media/file classification — mirrors the backend taxonomy
// (src-py/dax/filter.py) for the path the UI owns itself: Chrome-intercepted
// downloads. Which bucket a req routes to (hls/dash/file), whether
// postprocessing applies, and the words + icon the intercept sheet shows.
// Extraction results arrive already filtered and ranked by the backend —
// the UI never re-ranks them.

import type { ChromeDownloadMeta } from './types'
import {
  Box,
  File,
  FileCode,
  FileText,
  FileZipper,
  Filmstrip,
  HardDrive,
  MusicNote,
  Picture,
} from '@gravity-ui/icons'

const VIDEO_EXT = new Set([
  'mp4', 'webm', 'mkv', 'mov', 'm4v', 'ts', 'm3u8', 'mpd',
])
const AUDIO_EXT = new Set([
  'mp3', 'm4a', 'opus', 'wav', 'flac', 'aac', 'm3u',
])
/** Anything whose bytes are media — the postprocess gate. */
const MEDIA_EXT = new Set([...VIDEO_EXT, ...AUDIO_EXT])
/** Containers the backend no longer supports — never postprocess them. */
const RETIRED_EXT = new Set([
  'ogg', 'oga', 'avi', '3gp', 'wmv', 'flv', 'mpg', 'mpeg', 'wma', 'aiff', 'm2ts', 'alac',
])
const IMAGE_EXT = new Set([
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'avif', 'heic', 'heif', 'svg', 'bmp',
  'ico', 'tiff',
])
const ARCHIVE_EXT = new Set([
  'zip', 'rar', '7z', 'tar', 'gz', 'bz2', 'xz', 'zst', 'tgz', 'lz', 'lz4', 'cab',
])
const DISK_EXT = new Set(['dmg', 'iso', 'img', 'vhd', 'vhdx', 'vmdk'])
const INSTALLER_EXT = new Set([
  'exe', 'msi', 'msix', 'pkg', 'deb', 'rpm', 'appimage', 'apk', 'ipa',
])
const DOC_EXT = new Set([
  'pdf', 'doc', 'docx', 'odt', 'rtf', 'txt', 'md', 'epub', 'pages',
])
const SHEET_EXT = new Set(['xls', 'xlsx', 'ods', 'csv', 'tsv', 'numbers'])
const DECK_EXT = new Set(['ppt', 'pptx', 'odp', 'key'])
const FONT_EXT = new Set(['ttf', 'otf', 'woff', 'woff2'])
const CODE_EXT = new Set([
  'js', 'ts', 'jsx', 'tsx', 'py', 'rb', 'go', 'rs', 'c', 'h', 'cpp', 'hpp',
  'cs', 'java', 'kt', 'swift', 'php', 'sh', 'json', 'xml', 'yml', 'yaml',
  'toml', 'ini', 'sql', 'lua', 'vue', 'svelte',
])
const ARCHIVE_MIME = new Set([
  'application/zip', 'application/x-rar-compressed',
  'application/vnd.rar', 'application/x-7z-compressed', 'application/gzip',
  'application/x-tar', 'application/x-bzip2', 'application/x-xz',
  'application/zstd',
])
const CODE_MIME = /json|javascript|ecmascript|xml|typescript/

/** Intercepted download → the req media bucket — manifest types route the
 *  engine's stream pipeline, everything else is a plain file fetch. */
export function interceptKind(dl: ChromeDownloadMeta): 'hls' | 'dash' | 'file' {
  const mime = (dl.mime || '').toLowerCase()
  const ext = (dl.ext || '').toLowerCase()
  if (mime.includes('mpegurl') || ext === 'm3u8' || ext === 'm3u') return 'hls'
  if (mime.includes('dash') || ext === 'mpd') return 'dash'
  return 'file'
}

/** True when ffmpeg postprocessing is even meaningful — a remux/embed pass
 *  over a binary can only corrupt it. Mime wins over ext (servers lie about
 *  extensions; Content-Type less often). */
export function isMediaFile(dl: ChromeDownloadMeta): boolean {
  const mime = (dl.mime || '').toLowerCase()
  const ext = (dl.ext || '').toLowerCase()
  if (RETIRED_EXT.has(ext)) return false
  return (
    mime.startsWith('video/') ||
    mime.startsWith('audio/') ||
    mime.includes('mpegurl') ||
    mime.includes('dash') ||
    MEDIA_EXT.has(ext)
  )
}

type FileKind = {
  label: string
  Icon: React.ComponentType<React.SVGProps<SVGSVGElement>>
}

/** Mime + extension → the words and icon a person would use for this file,
 *  not the words a server would. Mime wins for broad media classes; the ext
 *  carries the load for formats servers label octet-stream. */
export function describeFile(dl: ChromeDownloadMeta): FileKind {
  const mime = (dl.mime || '').split(';')[0].trim().toLowerCase()
  const ext = (dl.ext || '').toLowerCase()
  if (mime.startsWith('video/') || mime.includes('mpegurl') || mime.includes('dash') || VIDEO_EXT.has(ext))
    return { label: 'Video', Icon: Filmstrip }
  if (mime.startsWith('audio/') || AUDIO_EXT.has(ext))
    return { label: 'Audio', Icon: MusicNote }
  if (mime.startsWith('image/') || IMAGE_EXT.has(ext))
    return { label: 'Image', Icon: Picture }
  if (ARCHIVE_MIME.has(mime) || ARCHIVE_EXT.has(ext))
    return { label: 'Archive', Icon: FileZipper }
  if (DISK_EXT.has(ext) || mime.includes('diskimage') || mime.includes('iso9660'))
    return { label: 'Disk image', Icon: HardDrive }
  if (INSTALLER_EXT.has(ext) || mime.includes('msdownload') || mime.includes('installer'))
    return { label: 'Installer', Icon: Box }
  if (FONT_EXT.has(ext) || mime.startsWith('font/'))
    return { label: 'Font', Icon: FileText }
  if (CODE_EXT.has(ext) || CODE_MIME.test(mime))
    return { label: 'Code', Icon: FileCode }
  if (DOC_EXT.has(ext) || mime.startsWith('text/'))
    return { label: 'Document', Icon: FileText }
  if (SHEET_EXT.has(ext))
    return { label: 'Spreadsheet', Icon: FileText }
  if (DECK_EXT.has(ext))
    return { label: 'Presentation', Icon: FileText }
  return { label: 'File', Icon: File }
}
