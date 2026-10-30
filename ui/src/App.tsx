import {
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from 'react'
import {
  Alert,
  Button,
  Checkbox,
  CloseButton,
  Disclosure,
  Modal,
  Radio,
  RadioGroup,
  ScrollShadow,
  SearchField,
  Skeleton,
  Spinner,
  Tabs,
  Toast,
  toast,
} from '@heroui/react'
import {
  addDownload,
  checkDownloadName,
  downloadControl,
  extractMedia,
  focusWindow,
  inspectTorrent,
  logError,
  onDownloads,
  onEngine,
  onPush,
  onTorrentFiles,
  onUpdateReady,
  readTorrentFile,
  restartApp,
  takePendingTorrents,
  thumbUrl,
  type PushPayload,
} from './tauri'
import { DownloadsView } from './downloads'
import { describeFile, interceptKind, isMediaFile } from './filter'
import {
  errMsg,
  estBytes,
  fmtBytes,
  fmtDuration,
  formatSelection,
  isActive,
  isDone,
  isMagnet,
  isTorrentDownload,
  isTorrentUrl,
  safeFilename,
  taskName,
  tasksFor,
} from './fmt'
import { SettingsPage } from './settings'
import { LogoMark } from './components/brand'
import { CategorySelect, useCategories } from './components/categories'
import { ExtractDialog } from './components/extract-dialog'
import { Tag } from './components/tag'
import { TaskInfoDrawer } from './components/task-info'
import {
  pickMany,
  TorrentFileList,
  TorrentJobCard,
  TorrentPickerDialog,
} from './components/torrent'
import type {
  ChromeDownloadMeta,
  DownloadInfo,
  DownloadReq,
  DownloadsSnapshot,
  EngineStatus,
  ExtractOptions,
  MediaItem,
  MediaLink,
  NameCheck,
  PageResult,
  RequestContext,
  TorrentPreview,
} from './types'
import {
  ArrowDownToLine,
  ChevronDown,
  Gear,
  Magnet,
  Pause,
  Play,
  Plus,
  TrashBin,
  Tray,
  TriangleExclamation,
} from '@gravity-ui/icons'
import { IconBtn } from './icon-btn'


type Job = {
  id: number
  input: string
  /** Captured request contexts from the extension push — replayed to the
   *  download so its requests carry the extractor's session. */
  contexts?: RequestContext[]
  /** The push's referer — private-tracker .torrent fetches need it. */
  referer?: string
  /** Picked .torrent file bytes (base64) — `input` is its filename. */
  torrent?: string
} & (
  | { state: 'running' }
  | { state: 'done'; result: PageResult }
  | { state: 'failed'; fatal: string }
  /** Magnet / .torrent input — handled by the torrent card, never probed. */
  | { state: 'torrent' }
)

type DoneJob = Extract<Job, { state: 'done' }>

type StatusFilter = 'all' | 'active' | 'completed' | 'paused' | 'failed'

/** How many format rows an item shows before collapsing behind a toggle. */
const VISIBLE_FORMATS = 6


let jobSeq = 0
const probingNow = new Set<string>()

/** btoa chokes on a spread over ~64KB — chunk it. */
function bytesToB64(bytes: Uint8Array) {
  let bin = ''
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  }
  return btoa(bin)
}

export default function App() {
  const [page, setPage] = useState<'downloads' | 'settings'>('downloads')
  const [raw, setRaw] = useState('')
  const [jobs, setJobs] = useState<Job[]>([])
  const [busy, setBusy] = useState(false)
  const [snap, setSnap] = useState<DownloadsSnapshot | null>(null)
  const [engine, setEngine] = useState<EngineStatus | null>(null)
  const [query, setQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [probeOpen, setProbeOpen] = useState(false)
  const [extractOpen, setExtractOpen] = useState(false)
  /** A Chrome download the extension cancelled — shown raw in a modal, never
   *  probed; the task is only added when the user confirms. */
  const [intercept, setIntercept] = useState<{
    dl: ChromeDownloadMeta
    referer?: string
    contexts?: RequestContext[]
  } | null>(null)
  /** Gid whose torrent file picker is open (metadata arrived / row action). */
  const [picker, setPicker] = useState<string | null>(null)
  /** Gid whose details drawer is open. */
  const [infoGid, setInfoGid] = useState<string | null>(null)
  /** Version of the update already installed by the Rust updater task —
   *  the banner offers the restart since it kills the Python backend. */
  const [updateReady, setUpdateReady] = useState<{ version: string; notes?: string } | null>(null)

  const urls = [...new Set(raw.split('\n').map((s) => s.trim()).filter(Boolean))]

  // Live updates stream continuously over WebSocket via server-pushed
  // downloads-snapshot and engine-status frames on connect, progress, and actions.
  useEffect(() => {
    let dead = false
    const offs: (() => void)[] = []
    onDownloads((s) => setSnap(s)).then((f) => (dead ? f() : offs.push(f)))
    onEngine((e) => setEngine(e)).then((f) => (dead ? f() : offs.push(f)))
    return () => {
      dead = true
      offs.forEach((f) => f())
    }
  }, [])

  const act = async (gid: string, action: string) => {
    if (action === 'selectFiles') {
      setPicker(gid)
      return
    }
    if (action === 'info') {
      setInfoGid(gid)
      return
    }
    try {
      await downloadControl(gid, action)
    } catch (e) {
      toast.danger(`${action} failed`, { description: errMsg(e) })
    }
  }

  const probe = async (
    input: string,
    extra?: {
      referer?: string
      contexts?: RequestContext[]
      title?: string
      thumbnail?: string
    },
  ) => {
    if (probingNow.has(input)) return
    probingNow.add(input)
    focusWindow()
    const id = ++jobSeq
    // Magnets and .torrent URLs are not yt-dlp material — the torrent card
    // inspects them and adds them through the libtorrent path.
    if (isMagnet(input) || isTorrentUrl(input)) {
      setJobs((js) => [
        ...js,
        { id, input, contexts: extra?.contexts, referer: extra?.referer, state: 'torrent' as const },
      ])
      probingNow.delete(input)
      return
    }
    setJobs((js) => [
      ...js,
      { id, input, contexts: extra?.contexts, referer: extra?.referer, state: 'running' as const },
    ])
    // Saved settings are merged server-side — only per-call data rides here.
    const opts: ExtractOptions = {
      ...(extra?.referer ? { referer: extra.referer } : {}),
      ...(extra?.contexts?.length ? { contexts: extra.contexts } : {}),
      ...(extra?.title ? { title: extra.title } : {}),
      ...(extra?.thumbnail ? { thumbnail: extra.thumbnail } : {}),
    }
    try {
      const [result] = await extractMedia([input], opts)
      if (!result) throw new Error('no result')
      setJobs((js) =>
        js.map((j) =>
          j.id === id
            ? { ...j, state: 'done' as const, result }
            : j,
        ),
      )
    } catch (e) {
      const fatal = errMsg(e)
      logError(`probe ${input}`, e)
      setJobs((js) => js.map((j) => (j.id === id ? { ...j, state: 'failed', fatal } : j)))
    } finally {
      probingNow.delete(input)
      focusWindow()
    }
  }

  const run = async () => {
    if (urls.length === 0 || busy) return
    setBusy(true)
    setJobs([])
    setProbeOpen(true)
    await Promise.all(urls.map((u) => probe(u)))
    setBusy(false)
  }

  /** A locally picked .torrent file — inspected (and added) via base64. */
  const openTorrentFile = async (file: File) => {
    focusWindow()
    // Real .torrent files are KBs to a few MB; the base64 frame is ~1.4x.
    if (file.size > 8 * 1024 * 1024) {
      toast.danger('Torrent file too large')
      return
    }
    try {
      const bytes = new Uint8Array(await file.arrayBuffer())
      setJobs([
        { id: ++jobSeq, input: file.name, torrent: bytesToB64(bytes), state: 'torrent' as const },
      ])
      setProbeOpen(true)
    } catch (e) {
      toast.danger('Could not read torrent file', { description: errMsg(e) })
    }
  }

  /** .torrent files the OS handed to the app (double-click, icon drop) —
   *  same inspect card as a picked file, one job per path. */
  const openTorrentPaths = async (paths: string[]) => {
    if (!paths.length) return
    focusWindow()
    const next: Job[] = []
    for (const p of paths) {
      try {
        const bytes = new Uint8Array(await readTorrentFile(p))
        next.push({
          id: ++jobSeq,
          input: p.split(/[\\/]/).pop() || p,
          torrent: bytesToB64(bytes),
          state: 'torrent' as const,
        })
      } catch (e) {
        toast.danger('Could not read torrent file', { description: errMsg(e) })
      }
    }
    if (next.length) {
      setJobs(next)
      setProbeOpen(true)
    }
  }

  // Extension pushes: raise the window, open the probe modal, probe in-app.
  // The unsubscribe is async — StrictMode remounts before it resolves, so
  // guard with a dead flag or the listener leaks and pushes duplicate.
  useEffect(() => {
    let dead = false
    let off: (() => void) | undefined
    onPush((p: PushPayload) => {
      focusWindow()
      // Intercepted Chrome downloads are shown as-is — the browser already
      // resolved name/size; nothing to probe, just confirm.
      if (p.download) {
        setIntercept({ dl: p.download, referer: p.referer, contexts: p.contexts })
        return
      }
      setProbeOpen(true)
      // Drop previous probes — keep only a still-running job for this exact
      // input so the dedupe in probe() leaves a visible card behind.
      setJobs((js) => js.filter((j) => j.input === p.url && j.state === 'running'))
      probe(p.url, {
        referer: p.referer,
        contexts: p.contexts,
        title: p.title,
        thumbnail: p.thumbnail,
      })
    }).then((u) => {
      if (dead) u()
      else off = u
    })
    return () => {
      dead = true
      off?.()
    }
  }, [])

  // OS-opened .torrent files: drain anything queued before this listener
  // existed (cold start), then subscribe for opens on the running app.
  useEffect(() => {
    let dead = false
    let off: (() => void) | undefined
    takePendingTorrents()
      .then((paths) => {
        if (!dead) openTorrentPaths(paths)
      })
      .catch(() => {})
    onTorrentFiles((paths) => {
      if (!dead) openTorrentPaths(paths)
    }).then((u) => {
      if (dead) u()
      else off = u
    })
    return () => {
      dead = true
      off?.()
    }
  }, [])

  // Rust updater task: fires once when a new release finished installing.
  useEffect(() => {
    let dead = false
    let off: (() => void) | undefined
    onUpdateReady(setUpdateReady).then((u) => {
      if (dead) u()
      else off = u
    })
    return () => {
      dead = true
      off?.()
    }
  }, [])

  const q = query.trim().toLowerCase()
  const tasks = snap ? tasksFor(snap) : []

  const counts = {
    all: tasks.length,
    active: tasks.filter(isActive).length,
    completed: tasks.filter((d) => isDone(d)).length,
    paused: tasks.filter((d) => d.status === 'paused').length,
    failed: tasks.filter((d) => d.status === 'error' || !!d.errorMessage).length,
  }

  const matchesStatus = (d: DownloadInfo, f: StatusFilter) => {
    if (f === 'all') return true
    if (f === 'active') return isActive(d)
    if (f === 'completed') return isDone(d)
    if (f === 'paused') return d.status === 'paused'
    if (f === 'failed') return d.status === 'error' || !!d.errorMessage
    return true
  }

  const filtered = tasks.filter(
    (d) =>
      matchesStatus(d, statusFilter) &&
      (!q || taskName(d).toLowerCase().includes(q) || d.gid.includes(q)),
  )
  const sorted = [...filtered].sort(
    (a, b) => (b.createdAt ?? b.downloadanything?.createdAt ?? 0) - (a.createdAt ?? a.downloadanything?.createdAt ?? 0),
  )

  // A magnet parked after its metadata arrived (status 'selecting') opens
  // the file picker once — the row keeps a "select files" button for later.
  const pickerSeen = useRef(new Set<string>())
  useEffect(() => {
    if (!snap) return
    for (const d of [...snap.active, ...snap.waiting, ...snap.stopped]) {
      if (
        d.status === 'selecting' &&
        (d.files?.length ?? 0) > 1 &&
        !pickerSeen.current.has(d.gid)
      ) {
        pickerSeen.current.add(d.gid)
        setPicker(d.gid)
      }
    }
  }, [snap])

  const hasActive = (snap?.active.length ?? 0) > 0 || (snap?.waiting.length ?? 0) > 0
  const hasPaused = snap?.stopped.some((d) => d.status === 'paused') ?? false
  const hasStopped = snap?.stopped.some((d) => d.status === 'complete') ?? false
  const dlSpeed = Number(snap?.stat?.downloadSpeed ?? 0)
  const filteredBytes = sorted.reduce(
    (sum, d) => sum + (Number(d.totalLength) || Number(d.completedLength) || 0),
    0,
  )

  const filterTabs: {
    id: StatusFilter
    label: string
    count: number
  }[] = [
    { id: 'all', label: 'All', count: counts.all },
    { id: 'active', label: 'Active', count: counts.active },
    { id: 'completed', label: 'Completed', count: counts.completed },
    { id: 'paused', label: 'Paused', count: counts.paused },
    { id: 'failed', label: 'Failed', count: counts.failed },
  ]

  return (
    <div className="relative flex h-screen w-screen overflow-hidden bg-background">
      <Toast.Provider placement="bottom end" />

      {page === 'settings' ? (
        <div className="flex-1 min-w-0">
          <SettingsPage onBack={() => setPage('downloads')} />
        </div>
      ) : (
        <main className="flex min-w-0 flex-1 flex-col">
          <h1 className="sr-only">DownloadAnything downloads</h1>
          <header className="chrome flex h-13 shrink-0 items-center px-6">
            {/* Left: brand + engine telemetry */}
            <div className="flex min-w-0 flex-1 items-center gap-4">
              <div className="flex shrink-0 items-center gap-2.5">
                <LogoMark size={19} />
                <span className="text-title font-semibold tracking-[-0.02em] text-foreground">
                  DownloadAnything
                </span>
              </div>
              <span
                className="inline-flex min-w-0 items-center gap-2"
                title={
                  engine?.error ||
                  (engine?.running
                    ? `Engine active: ${engine.version ?? 'yt-dlp'}`
                    : 'Engine stopped')
                }
              >
                <span
                  className={`size-1.5 shrink-0 rounded-full transition-colors ${
                    engine == null
                      ? 'bg-(--state-queued)'
                      : engine.running
                        ? 'bg-success'
                        : 'bg-danger'
                  }`}
                />
                <span className="hidden truncate text-meta text-muted min-[860px]:inline">
                  {engine == null
                    ? 'Connecting…'
                    : engine.running
                      ? 'Engine ready'
                      : 'Engine offline'}
                </span>
              </span>

              {dlSpeed > 0 && (
                <span className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap font-mono text-meta tabular-nums text-(--info-soft-foreground)">
                  <ArrowDownToLine
                    width={11}
                    height={11}
                    className="shrink-0 text-(--info)"
                  />
                  <span className="inline-block w-[9ch] text-center">
                    {fmtBytes(dlSpeed)}/s
                  </span>
                </span>
              )}
            </div>

            {/* Right: filter search, bulk controls, settings, primary action */}
            <div className="flex shrink-0 items-center gap-2">
              {tasks.length > 0 && (
                <SearchField
                  aria-label="Filter downloads"
                  value={query}
                  onChange={setQuery}
                  className="w-52 lg:w-56"
                >
                  <SearchField.Group className="rounded-lg">
                    <SearchField.SearchIcon />
                    <SearchField.Input
                      className="w-full text-meta"
                      placeholder="Filter downloads…"
                    />
                    <SearchField.ClearButton aria-label="Clear filter" />
                  </SearchField.Group>
                </SearchField>
              )}

              {tasks.length > 0 && (
                <div className="hidden shrink-0 items-center gap-0.5 min-[900px]:flex">
                  <IconBtn
                    label="Pause all"
                    onPress={() => act('-', 'pauseAll')}
                    isDisabled={!hasActive}
                  >
                    <Pause width={14} height={14} />
                  </IconBtn>
                  <IconBtn
                    label="Resume all"
                    onPress={() => act('-', 'unpauseAll')}
                    isDisabled={!hasPaused}
                  >
                    <Play width={14} height={14} />
                  </IconBtn>
                  <IconBtn
                    label="Clear finished"
                    onPress={() => act('-', 'purge')}
                    isDisabled={!hasStopped}
                    danger
                  >
                    <TrashBin width={14} height={14} />
                  </IconBtn>
                </div>
              )}

              <IconBtn
                size="md"
                label="Settings"
                onPress={() => setPage('settings')}
              >
                <Gear width={15} height={15} />
              </IconBtn>

              <Button
                variant="primary"
                size="sm"
                className="gap-1.5"
                onPress={() => setExtractOpen(true)}
              >
                <Plus width={14} height={14} />
                <span>Extract</span>
              </Button>
            </div>
          </header>

          {/* Status filter & summary bar */}
          {tasks.length > 0 && (
            <div className="flex h-11 shrink-0 items-center justify-between gap-4 px-6">
              <div
                role="group"
                aria-label="Filter by download status"
                className="inline-flex items-center gap-0.5"
              >
                {filterTabs.map((t) => {
                  const active = statusFilter === t.id
                  return (
                    <button
                      key={t.id}
                      type="button"
                      aria-pressed={active}
                      onClick={() => setStatusFilter(t.id)}
                      className={`press-tactile inline-flex h-7 items-center rounded-md px-2.5 text-meta font-medium outline-none transition-colors focus-visible:ring-1 focus-visible:ring-focus ${
                        active
                          ? 'bg-(--state-selected-bg) text-foreground'
                          : 'text-muted hover:bg-(--state-hover-bg) hover:text-foreground'
                      }`}
                    >
                      <span>{t.label}</span>
                      <span
                        className={`ms-1.5 font-mono text-micro tabular-nums ${
                          active ? 'text-foreground-secondary' : 'text-muted-subtle'
                        }`}
                      >
                        {t.count}
                      </span>
                    </button>
                  )
                })}
              </div>

              <div className="flex items-center gap-2 font-mono text-meta tabular-nums text-muted">
                <span>
                  {sorted.length} {sorted.length === 1 ? 'task' : 'tasks'}
                </span>
                {filteredBytes > 0 && (
                  <>
                    <span aria-hidden="true" className="text-muted-subtle">
                      ·
                    </span>
                    <span>{fmtBytes(filteredBytes)}</span>
                  </>
                )}
              </div>
            </div>
          )}

          {snap?.error && (
            <div className="px-6 pt-4">
              <Alert status="warning">
                <Alert.Indicator>
                  <TriangleExclamation />
                </Alert.Indicator>
                <Alert.Content>
                  <Alert.Description>{snap.error}</Alert.Description>
                </Alert.Content>
              </Alert>
            </div>
          )}

          {updateReady && (
            <div className="px-6 pt-4">
              <Alert status="success">
                <Alert.Indicator />
                <Alert.Content>
                  <Alert.Title>Update installed — v{updateReady.version}</Alert.Title>
                  <Alert.Description>
                    Restart DownloadAnything to finish updating. Active downloads keep running until then.
                  </Alert.Description>
                </Alert.Content>
                <Button
                  size="sm"
                  variant="primary"
                  className="shrink-0"
                  onPress={() => restartApp().catch((e) => logError('restart after update', e))}
                >
                  Restart
                </Button>
                <CloseButton onPress={() => setUpdateReady(null)} />
              </Alert>
            </div>
          )}

          <ScrollShadow className="min-h-0 flex-1 overflow-y-auto">
            <div className="flex min-h-full flex-col px-6 pt-1 pb-16">
              {sorted.length === 0 ? (
                <div className="m-auto w-full">
                  {snap === null ? (
                    <EmptyState
                      icon={<Spinner size="sm" />}
                      text="Connecting to the engine"
                      hint="Waiting for the local backend to come up."
                    />
                  ) : snap.error && tasks.length === 0 ? (
                    <EmptyState
                      icon={<TriangleExclamation width={20} height={20} />}
                      text="Engine unreachable"
                      hint={snap.error}
                    />
                  ) : (
                    <EmptyState
                      icon={<Tray width={20} height={20} />}
                      text={
                        query || statusFilter !== 'all'
                          ? 'No matching tasks'
                          : 'No downloads yet'
                      }
                      hint={
                        query || statusFilter !== 'all'
                          ? `No tasks match ${
                              query ? `“${query}”` : `the ${statusFilter} filter`
                            }. Reset the filter to view all downloads.`
                          : 'Paste a media link to extract it, or send a page from the browser extension.'
                      }
                      meta={
                        query || statusFilter !== 'all'
                          ? undefined
                          : 'Web pages · HLS / DASH · Torrents · Direct files'
                      }
                      action={
                        query || statusFilter !== 'all' ? (
                          <Button
                            variant="secondary"
                            size="sm"
                            className="mt-1"
                            onPress={() => {
                              setQuery('')
                              setStatusFilter('all')
                            }}
                          >
                            Reset filters
                          </Button>
                        ) : (
                          <Button
                            variant="primary"
                            size="sm"
                            className="mt-1 gap-1.5"
                            onPress={() => setExtractOpen(true)}
                          >
                            <Plus width={14} height={14} />
                            Extract media
                          </Button>
                        )
                      }
                    />
                  )}
                </div>
              ) : (
                <DownloadsView items={sorted} act={act} />
              )}
            </div>
          </ScrollShadow>
        </main>
      )}

      <ExtractDialog
        raw={raw}
        setRaw={setRaw}
        busy={busy}
        urls={urls.length}
        run={run}
        open={extractOpen}
        setOpen={setExtractOpen}
        onTorrentFile={openTorrentFile}
      />
      <ProbeModal open={probeOpen} onOpenChange={setProbeOpen} jobs={jobs} />
      <InterceptModal job={intercept} onClose={() => setIntercept(null)} />
      <TorrentPickerDialog
        key={picker ?? 'none'}
        item={picker ? findTask(snap, picker) : undefined}
        onClose={() => setPicker(null)}
      />
      <TaskInfoDrawer
        item={infoGid ? findTask(snap, infoGid) : undefined}
        act={act}
        onClose={() => setInfoGid(null)}
      />
    </div>
  )
}

function findTask(
  snap: DownloadsSnapshot | null,
  gid: string,
): DownloadInfo | undefined {
  if (!snap) return undefined
  return [...snap.active, ...snap.waiting, ...snap.stopped].find((d) => d.gid === gid)
}


type ProbeSelection = {
  /** Owning probe job — selections from dropped/finished runs must never
   *  submit again. */
  jobId: number
  item: MediaItem
  link: MediaLink
  page: string
  contexts?: RequestContext[]
}







function buildReq({ item, link, page, contexts, category }: ProbeSelection & { category?: string }): DownloadReq {
  const referer = link.headers?.referer ?? page
  const viaManifest = link.kind === 'hls' && !!link.manifest_url
  const url = viaManifest ? link.manifest_url! : link.url
  const media = link.kind === 'hls' ? 'hls' : link.kind === 'dash' ? 'dash' : 'file'
  return {
    url,
    media,
    title: item.title,
    durationSecs: item.duration_secs,
    filenameHint: safeFilename(item.title ?? '') || undefined,
    referer,
    thumbnail: item.thumbnail,
    reprId: link.repr_id,
    pageUrl: link.page_url || item.page_url || page,
    expectedSize: link.probed_size || estBytes(link, item.duration_secs) || undefined,
    createdAt: Math.floor(Date.now() / 1000),
    contexts,
    category: category || undefined,
    quality: link.kind === 'audio'
      ? { audioOnly: true }
      : link.quality?.height
        ? { height: link.quality.height, bandwidth: link.quality.bandwidth }
        : link.quality?.bandwidth
          ? { bandwidth: link.quality.bandwidth }
          : undefined,
  }
}

async function submitDownload(req: DownloadReq) {
  await addDownload(req)
}

function renameReq(req: DownloadReq, suggested: string): DownloadReq {
  return req.filename
    ? { ...req, filename: suggested }
    : { ...req, filenameHint: suggested }
}

function NameConflictModal({
  conflicts,
  onAction,
}: {
  conflicts: { req: DownloadReq; check: NameCheck }[] | null
  onAction: (a: 'cancel' | 'rename' | 'override') => void
}) {
  const names = conflicts?.map(
    ({ req }) => req.filename ?? req.filenameHint ?? req.url,
  ) ?? []
  const dir = conflicts?.[0]?.check.dir
  return (
    <Modal.Backdrop isOpen={!!conflicts} onOpenChange={(o) => !o && onAction('cancel')}>
      <Modal.Container>
        <Modal.Dialog className="sm:max-w-104">
          <Modal.CloseTrigger />
          <Modal.Header>
            <Modal.Heading>File already exists</Modal.Heading>
          </Modal.Header>
          <Modal.Body>
            <p className="text-pretty text-body leading-relaxed text-muted">
              {names.length === 1
                ? 'This name is already taken'
                : `${names.length} names are already taken`}
              {dir ? ` in ${dir}` : ''}:
            </p>
            <ul className="mt-3 flex max-h-32 flex-col gap-1 overflow-y-auto rounded-lg bg-surface-secondary p-2.5 font-mono text-meta text-foreground-secondary">
              {names.map((n) => (
                <li key={n} className="truncate" title={n}>{n}</li>
              ))}
            </ul>
          </Modal.Body>
          <Modal.Footer className="justify-end gap-2">
            <Button
              slot="close" variant="secondary" size="sm"
              onPress={() => onAction('cancel')}
            >
              Cancel
            </Button>
            <Button
              slot="close" variant="secondary" size="sm"
              onPress={() => onAction('rename')}
            >
              Rename
            </Button>
            <Button
              slot="close" variant="danger" size="sm"
              onPress={() => onAction('override')}
            >
              Replace
            </Button>
          </Modal.Footer>
        </Modal.Dialog>
      </Modal.Container>
    </Modal.Backdrop>
  )
}

const DANGER_COPY: Record<string, string> = {
  uncommon: 'files like this are rarely downloaded',
  potentiallyUnwanted: 'it may be unwanted software',
  file: 'it may harm your computer',
  content: 'its contents may be harmful',
  url: 'its source looks suspicious',
  passwordProtected: "it's password-protected, so Chrome couldn't check it",
  sensitiveDataWarning: 'it may contain sensitive data',
  sensitiveDataBlock: 'Chrome blocked it over sensitive data',
  blockedTooLarge: 'it was too large for Chrome to check',
  accountCompromise: 'its source may be compromised',
  asyncScanning: 'Chrome is still checking it',
  asyncPasswordScanning: 'Chrome is still checking it',
  promptForScanning: "Chrome hasn't checked it yet",
  promptForLocalPasswordScanning: "Chrome hasn't checked it yet",
  deepScannedFailed: "Chrome couldn't finish checking it",
}

function urlHost(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '')
  } catch {
    return ''
  }
}

function InterceptModal({
  job,
  onClose,
}: {
  job: { dl: ChromeDownloadMeta; referer?: string; contexts?: RequestContext[] } | null
  onClose: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [cat, setCat] = useState('')
  const cats = useCategories(!!job)
  const [conflict, setConflict] = useState<{ req: DownloadReq; check: NameCheck } | null>(null)
  const dl = job?.dl
  const url = dl ? dl.finalUrl || dl.url : ''
  const name =
    dl?.name ||
    (() => {
      try {
        return decodeURIComponent(new URL(url).pathname.split('/').pop() || '')
      } catch {
        return ''
      }
    })()
  const size =
    dl && dl.fileSize && dl.fileSize > 0
      ? dl.fileSize
      : dl && dl.totalBytes && dl.totalBytes > 0
        ? dl.totalBytes
        : undefined
  const risky = dl?.danger && dl.danger !== 'safe' && dl.danger !== 'accepted'
  const torrent = dl ? isTorrentDownload(dl) : false
  const kind = dl
    ? torrent
      ? { label: 'Torrent', Icon: Magnet }
      : describeFile(dl)
    : null
  const host = urlHost(url)
  const fromHost = urlHost(job?.referer || dl?.referrer || '')
  const [preview, setPreview] = useState<TorrentPreview | null>(null)
  const [picked, setPicked] = useState<Set<number>>(new Set())
  const [torrentErr, setTorrentErr] = useState('')

  useEffect(() => {
    if (!job || !isTorrentDownload(job.dl)) return
    let dead = false
    setPreview(null)
    setTorrentErr('')
    inspectTorrent({
      url: job.dl.finalUrl || job.dl.url,
      referer: job.referer,
      contexts: job.contexts,
    })
      .then((p) => {
        if (dead) return
        setPreview(p)
        setPicked(new Set(p.files.map((f) => Number(f.index))))
      })
      .catch((e) => !dead && setTorrentErr(errMsg(e)))
    return () => {
      dead = true
    }
  }, [job])

  const togglePicked = (idx: number) =>
    setPicked((prev) => {
      const next = new Set(prev)
      if (next.has(idx)) next.delete(idx)
      else next.add(idx)
      return next
    })

  const toggleDirPicked = (indices: number[], select: boolean) =>
    setPicked((prev) => pickMany(prev, indices, select))

  const setAllPicked = (select: boolean) =>
    setPicked(select ? new Set(preview?.files.map((f) => Number(f.index)) ?? []) : new Set())

  const buildInterceptReq = (): DownloadReq | null => {
    if (!dl) return null
    if (torrent) {
      return {
        url,
        kind: 'torrent',
        torrent: preview?.torrent,
        filenameHint: preview?.name,
        selectFiles: formatSelection([...picked]),
        referer: job?.referer || dl.referrer || undefined,
        category: cat || undefined,
        createdAt: Math.floor(Date.now() / 1000),
      }
    }
    return {
      url,
      media: interceptKind(dl),
      filename: name || undefined,
      postprocess: isMediaFile(dl),
      referer: job?.referer || dl.referrer || undefined,
      expectedSize: size,
      createdAt: Math.floor(Date.now() / 1000),
      contexts: job?.contexts,
      category: cat || undefined,
    }
  }

  const submit = async (req: DownloadReq) => {
    await addDownload(req)
    onClose()
  }

  const download = async () => {
    const req = buildInterceptReq()
    if (!req || busy) return
    setBusy(true)
    try {
      if (torrent) {
        await submit(req)
        return
      }
      const c = await checkDownloadName(req).catch(() => null)
      if (c?.conflict) {
        setConflict({ req, check: c })
        return
      }
      await submit(req)
    } catch (e) {
      toast.danger('Download failed', { description: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  const onConflict = (a: 'cancel' | 'rename' | 'override') => {
    const c = conflict
    setConflict(null)
    if (a === 'cancel' || !c) return
    const req = a === 'rename' && c.check.suggested
      ? renameReq(c.req, c.check.suggested)
      : c.req
    setBusy(true)
    submit(req)
      .catch((e) => toast.danger('Download failed', { description: errMsg(e) }))
      .finally(() => setBusy(false))
  }

  const displayName = torrent && preview ? preview.name : name
  const meta = torrent
    ? [
        preview
          ? `${preview.files.length} file${preview.files.length === 1 ? '' : 's'}`
          : 'reading torrent…',
        preview ? fmtBytes(Number(preview.totalLength ?? 0)) : '',
        host,
      ].filter(Boolean)
    : [size ? fmtBytes(size) : 'Size unknown', kind?.label, host].filter(Boolean)

  const details: [string, string][] = dl
    ? ([
        ['URL', url],
        dl.mime ? ['Type', dl.mime] : null,
        job?.referer || dl.referrer ? ['From', job?.referer || dl.referrer!] : null,
        dl.startTime ? ['Started', new Date(dl.startTime).toLocaleString()] : null,
        dl.danger && dl.danger !== 'safe' && dl.danger !== 'accepted'
          ? ['Flag', dl.danger]
          : null,
        dl.incognito ? ['Incognito', 'yes'] : null,
      ].filter((r): r is [string, string] => r !== null))
    : []

  return (
    <>
      <Modal.Backdrop isOpen={!!job} onOpenChange={(o) => !o && onClose()}>
        <Modal.Container>
          <Modal.Dialog className="sm:max-w-130">
            <Modal.CloseTrigger />
            <Modal.Header>
              <Modal.Heading>Incoming download</Modal.Heading>
            </Modal.Header>
            <Modal.Body className="flex flex-col items-center gap-4 overflow-y-auto text-center">
              {dl && kind && (
                <>
                  <div className="relative mt-1">
                    <div className="flex size-14 items-center justify-center rounded-xl bg-surface-secondary text-muted">
                      <kind.Icon width={26} height={26} />
                    </div>
                    {dl.ext && (
                      <span className="absolute -right-2 -bottom-1.5 rounded-md bg-surface-tertiary px-1.5 py-0.5 font-mono text-micro font-semibold uppercase tracking-wide text-foreground">
                        {dl.ext}
                      </span>
                    )}
                  </div>
                  <div className="min-w-0">
                    <p className="line-clamp-2 break-all text-title font-semibold leading-snug text-foreground">
                      {displayName || 'Unnamed file'}
                    </p>
                    <p className="mt-1 font-mono text-meta text-muted">{meta.join(' · ')}</p>
                    {fromHost && fromHost !== host && (
                      <p className="mt-0.5 text-micro text-muted-subtle">
                        via {fromHost}
                      </p>
                    )}
                  </div>
                  {torrent && torrentErr && (
                    <Alert status="danger" className="rise-in w-full text-left">
                      <Alert.Indicator>
                        <TriangleExclamation />
                      </Alert.Indicator>
                      <Alert.Content>
                        <Alert.Description>{torrentErr}</Alert.Description>
                      </Alert.Content>
                    </Alert>
                  )}
                  {torrent && !torrentErr && !preview && (
                    <div className="flex items-center gap-2 text-meta text-muted">
                      <Spinner size="sm" />
                      Reading torrent…
                    </div>
                  )}
                  {torrent && preview && (
                    <TorrentFileList
                      files={preview.files}
                      picked={picked}
                      onToggle={togglePicked}
                      onToggleDir={toggleDirPicked}
                      onAll={setAllPicked}
                    />
                  )}
                  {risky && (
                    <Alert status="danger" className="rise-in w-full text-left">
                      <Alert.Indicator>
                        <TriangleExclamation />
                      </Alert.Indicator>
                      <Alert.Content>
                        <Alert.Description>
                          Chrome flagged this download: {DANGER_COPY[dl.danger!] ?? 'it may not be safe'}.
                        </Alert.Description>
                      </Alert.Content>
                    </Alert>
                  )}
                  <Disclosure className="w-full">
                    <Disclosure.Heading>
                      <Disclosure.Trigger className="press-tactile mx-auto flex h-7 items-center gap-1 rounded-md px-2.5 text-meta font-medium text-muted outline-none hover:bg-(--state-hover-bg) hover:text-foreground">
                        Details
                        <Disclosure.Indicator />
                      </Disclosure.Trigger>
                    </Disclosure.Heading>
                    <Disclosure.Content>
                      <Disclosure.Body>
                        <div className="flex flex-col gap-1.5 rounded-lg bg-surface-secondary p-3 text-left">
                          {details.map(([k, v]) => (
                            <div key={k} className="grid grid-cols-[72px_1fr] gap-3 text-meta">
                              <span className="text-muted">{k}</span>
                              <span className="min-w-0 wrap-break-word font-mono text-foreground-secondary">{v}</span>
                            </div>
                          ))}
                        </div>
                      </Disclosure.Body>
                    </Disclosure.Content>
                  </Disclosure>
                </>
              )}
            </Modal.Body>
            <Modal.Footer className="items-center gap-2.5">
              <CategorySelect cats={cats} value={cat} onChange={setCat} />
              <Button
                slot="close"
                variant="primary"
                size="sm"
                className="flex-1 gap-2"
                isDisabled={busy || (torrent && (!preview || picked.size === 0))}
                onPress={() => void download()}
              >
                <ArrowDownToLine width={14} height={14} />
                {busy ? 'Adding…' : torrent ? 'Add torrent' : 'Download'}
              </Button>
            </Modal.Footer>
          </Modal.Dialog>
        </Modal.Container>
      </Modal.Backdrop>
      <NameConflictModal
        conflicts={conflict ? [conflict] : null}
        onAction={onConflict}
      />
    </>
  )
}

function ProbeModal({
  open,
  onOpenChange,
  jobs,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  jobs: Job[]
}) {
  const selections = useRef(new Map<string, ProbeSelection>())
  const cats = useCategories(open)
  const [cat, setCat] = useState('')
  const [conflicts, setConflicts] = useState<{ req: DownloadReq; check: NameCheck }[] | null>(null)
  const probing = jobs.some((j) => j.state === 'running')
  useEffect(() => {
    const live = new Set(jobs.map((j) => j.id))
    for (const [key, sel] of selections.current)
      if (!live.has(sel.jobId)) selections.current.delete(key)
  }, [jobs])
  const report =
    (job: Job) => (itemKey: string, sel: Omit<ProbeSelection, 'jobId'> | null) => {
      const key = `${job.id}:${itemKey}`
      if (sel)
        selections.current.set(key, { ...sel, jobId: job.id, contexts: job.contexts })
      else selections.current.delete(key)
    }
  const submitAll = async (reqs: DownloadReq[]) => {
    const results = await Promise.allSettled(reqs.map(submitDownload))
    const failed = results.filter((r) => r.status === 'rejected')
    if (failed.length)
      toast.danger(`${failed.length} download${failed.length === 1 ? '' : 's'} failed`, {
        description:
          failed[0].status === 'rejected' ? String(failed[0].reason) : undefined,
      })
  }
  const downloadSelected = async () => {
    const live = new Set(jobs.map((j) => j.id))
    const reqs = [...selections.current.values()]
      .filter((s) => live.has(s.jobId))
      .map((s) => buildReq({ ...s, category: cat }))
    const checks = await Promise.all(
      reqs.map((r) => checkDownloadName(r).catch(() => null)),
    )
    const good: DownloadReq[] = []
    const bad: { req: DownloadReq; check: NameCheck }[] = []
    reqs.forEach((r, i) => {
      const c = checks[i]
      if (c?.conflict) bad.push({ req: r, check: c })
      else good.push(r)
    })
    void submitAll(good)
    if (bad.length) setConflicts(bad)
  }
  const onConflict = (a: 'cancel' | 'rename' | 'override') => {
    const c = conflicts
    setConflicts(null)
    if (a === 'cancel' || !c) return
    void submitAll(
      c.map(({ req, check }) =>
        a === 'rename' && check.suggested ? renameReq(req, check.suggested) : req,
      ),
    )
  }
  const ready = jobs.reduce(
    (n, j) =>
      n +
      (j.state === 'done'
        ? j.result.items.filter((i) => i.formats.length > 0).length
        : 0),
    0,
  )
  const torrentOnly = jobs.length > 0 && jobs.every((j) => j.state === 'torrent')
  const hasPlaylist = jobs.some(
    (j) => j.state === 'done' && (j.result.playlist?.entries.length ?? 0) > 0,
  )
  // Nothing to choose format-wise: every result is a playlist listing.
  const playlistOnly =
    jobs.length > 0 &&
    jobs.every(
      (j) =>
        j.state === 'done' &&
        j.result.items.length === 0 &&
        (j.result.playlist?.entries.length ?? 0) > 0,
    )
  // The Playlist tab owns its submit row — the shared footer (it serves
  // item selections, which playlists don't produce) must hide while every
  // done job is on it, or the modal shows two download rows at once.
  const [plTabs, setPlTabs] = useState<Set<number>>(new Set())
  const allPlTab =
    jobs.length > 0 &&
    jobs.every(
      (j) =>
        j.state === 'done' && j.result.playlist != null && plTabs.has(j.id),
    )
  const reportTab = (job: Job) => (tab: string) =>
    setPlTabs((prev) => {
      const next = new Set(prev)
      if (tab === 'playlist') next.add(job.id)
      else next.delete(job.id)
      return next
    })
  const jobFailed = (j: Job) =>
    j.state === 'failed' ||
    (j.state === 'done' && !!j.result.error && !j.result.playlist)
  const allFailed = jobs.length > 0 && jobs.every(jobFailed)
  const anyFailed = jobs.some(jobFailed)
  return (
    <>
      <Modal.Backdrop isOpen={open} onOpenChange={onOpenChange}>
        <Modal.Container>
          <Modal.Dialog className="sm:max-w-176">
            <Modal.CloseTrigger />
            <Modal.Header className="gap-1.5">
              <Modal.Heading>
                <span className="flex items-center gap-2.5">
                  {jobs.length > 0 && (
                    <span
                      className={`size-1.5 rounded-full ${
                        probing
                          ? 'bg-(--info)'
                          : allFailed
                            ? 'bg-danger'
                            : anyFailed
                              ? 'bg-warning'
                              : 'bg-success'
                      }`}
                    />
                  )}
                  {probing
                    ? 'Finding media…'
                    : allFailed
                      ? 'Extraction failed'
                      : torrentOnly
                        ? 'Torrent'
                        : playlistOnly
                          ? 'Choose playlist items'
                          : ready > 0 || hasPlaylist
                            ? 'Choose a format'
                            : 'No media found'}
                </span>
              </Modal.Heading>
              {!probing && (ready > 0 || playlistOnly) && (
                <p className="text-pretty text-meta leading-relaxed text-muted">
                  {playlistOnly
                    ? 'Picked items download at the best available quality.'
                    : 'The best available format is preselected for each item.'}
                </p>
              )}
            </Modal.Header>
            <Modal.Body className="flex max-h-[65vh] flex-col gap-5 overflow-y-auto">
              {jobs.length === 0 && (
                <p className="py-8 text-center text-body text-muted">
                  Nothing to inspect yet.
                </p>
              )}
              {jobs.map((job) => (
                <div key={job.id}>
                  <JobCard
                    job={job}
                    report={report(job)}
                    onTabChange={reportTab(job)}
                    onAdded={() => {
                      if (ready === 0) onOpenChange(false)
                    }}
                  />
                </div>
              ))}
            </Modal.Body>
            {!probing && ready > 0 && !allPlTab && (
              <Modal.Footer className="items-center justify-between">
                <CategorySelect cats={cats} value={cat} onChange={setCat} />
                <Button
                  slot="close"
                  variant="primary"
                  size="sm"
                  className="gap-2"
                  onPress={() => void downloadSelected()}
                >
                  <ArrowDownToLine width={14} height={14} />
                  Download
                </Button>
              </Modal.Footer>
            )}
          </Modal.Dialog>
        </Modal.Container>
      </Modal.Backdrop>
      <NameConflictModal conflicts={conflicts} onAction={onConflict} />
    </>
  )
}

function EmptyState({
  icon,
  text,
  hint,
  meta,
  action,
}: {
  icon: React.ReactNode
  text: string
  hint: string
  meta?: string
  action?: React.ReactNode
}) {
  return (
    <div className="mx-auto flex w-full max-w-108 flex-col items-center justify-center gap-3.5 px-6 py-12 text-center">
      <div className="flex size-10 items-center justify-center rounded-full bg-(--state-hover-bg) text-muted-subtle">
        {icon}
      </div>
      <div className="flex flex-col gap-1.5">
        <h2 className="text-balance text-display font-semibold text-foreground">
          {text}
        </h2>
        <p className="mx-auto max-w-[42ch] text-pretty text-meta leading-relaxed text-muted">
          {hint}
        </p>
      </div>
      {action}
      {meta && (
        <p className="font-mono text-micro text-muted-subtle">{meta}</p>
      )}
    </div>
  )
}

function JobCard({
  job,
  report,
  onAdded,
  onTabChange,
}: {
  job: Job
  report?: (itemKey: string, sel: Omit<ProbeSelection, 'jobId'> | null) => void
  onAdded?: () => void
  onTabChange?: (tab: string) => void
}) {
  if (job.state === 'torrent') {
    return <TorrentJobCard job={job} onAdded={onAdded} />
  }
  if (job.state === 'running') {
    return (
      <div className="rise-in flex flex-col gap-3 py-1">
        <div className="flex items-center gap-2.5">
          <Spinner size="sm" />
          <span className="min-w-0 flex-1 truncate font-mono text-meta text-muted">{job.input}</span>
          <Tag className="px-2 font-medium">Finding media</Tag>
        </div>
        <div className="flex flex-col gap-2">
          <Skeleton className="h-3.5 w-2/3 rounded-md" />
          <Skeleton className="h-3.5 w-1/2 rounded-md" />
        </div>
      </div>
    )
  }
  if (job.state === 'failed') {
    return (
      <Alert status="danger">
        <Alert.Indicator>
          <TriangleExclamation />
        </Alert.Indicator>
        <Alert.Content>
          <Alert.Title className="font-mono text-meta font-normal">{job.input}</Alert.Title>
          <Alert.Description>{job.fatal}</Alert.Description>
        </Alert.Content>
      </Alert>
    )
  }
  return (
    <ResultCard job={job} report={report} onAdded={onAdded} onTabChange={onTabChange} />
  )
}

function ResultCard({
  job,
  report,
  onAdded,
  onTabChange,
}: {
  job: DoneJob
  report?: (itemKey: string, sel: Omit<ProbeSelection, 'jobId'> | null) => void
  onAdded?: () => void
  onTabChange?: (tab: string) => void
}) {
  const result = job.result
  const pl = result.playlist
  // Lives here, not in PlaylistPanel: the tab unmounts its panel, and a
  // user's picks must survive a look at the Video tab.
  const [picked, setPicked] = useState<Set<number>>(
    () => new Set(pl?.entries.map((e) => e.index) ?? []),
  )
  const mediaItems = (
    <>
      {result.items.map((item, i) => (
        <div key={item.id} className={i > 0 ? 'mt-4 border-t border-separator pt-4' : ''}>
          <ItemSection
            item={item}
            extractor={result.extractor}
            onSelected={(link) =>
              report?.(
                String(i),
                link
                  ? {
                      item,
                      link,
                      page: result.final_url ?? result.input,
                    }
                  : null,
              )
            }
          />
        </div>
      ))}
    </>
  )
  return (
    <div className="rise-in flex flex-col">
      {result.error && (
        <Alert status={result.error_code === 4 ? 'warning' : 'danger'}>
          <Alert.Indicator>
            <TriangleExclamation />
          </Alert.Indicator>
          <Alert.Content>
            <Alert.Title className="text-body font-medium">
              Couldn't extract this URL
            </Alert.Title>
            <Alert.Description>{result.error}</Alert.Description>
          </Alert.Content>
        </Alert>
      )}
      {pl ? (
        <Tabs
          defaultSelectedKey={result.items.length > 0 ? 'video' : 'playlist'}
          onSelectionChange={(k) => onTabChange?.(String(k))}
          className="mt-2 flex flex-col gap-3"
        >
          <Tabs.List
            aria-label="Result type"
            className="flex w-fit min-w-0 gap-0.5 rounded-lg bg-(--state-hover-bg) p-1"
          >
            {result.items.length > 0 && (
              <Tabs.Tab
                id="video"
                className="press-tactile h-7 w-auto gap-2 rounded-md px-2.5 text-meta font-medium transition-colors"
              >
                Video {result.items.length}
                <Tabs.Indicator className="rounded-md bg-(--state-selected-bg)" />
              </Tabs.Tab>
            )}
            <Tabs.Tab
              id="playlist"
              className="press-tactile h-7 w-auto gap-2 rounded-md px-2.5 text-meta font-medium transition-colors"
            >
              Playlist{pl.count ? ` ${pl.count}` : ''}
              <Tabs.Indicator className="rounded-md bg-(--state-selected-bg)" />
            </Tabs.Tab>
          </Tabs.List>
          {result.items.length > 0 && (
            <Tabs.Panel id="video" className="mt-0 flex flex-col p-0 outline-none">
              {mediaItems}
            </Tabs.Panel>
          )}
          <Tabs.Panel id="playlist" className="mt-0 p-0 outline-none">
            <PlaylistPanel
              job={job}
              playlist={pl}
              picked={picked}
              setPicked={setPicked}
              onAdded={onAdded}
            />
          </Tabs.Panel>
        </Tabs>
      ) : (
        <>
          {result.items.length === 0 && !result.error && (
            <p className="py-6 text-center text-meta text-muted">
              No media found on this page.
            </p>
          )}
          {mediaItems}
        </>
      )}
    </div>
  )
}

/** Playlist tab: checkbox-picked entries submitted as ONE task — the
 *  backend downloads the picked indices at the best available quality
 *  into <out>/<playlist title>/. */
function PlaylistPanel({
  job,
  playlist,
  picked,
  setPicked,
  onAdded,
}: {
  job: DoneJob
  playlist: NonNullable<PageResult['playlist']>
  picked: Set<number>
  setPicked: Dispatch<SetStateAction<Set<number>>>
  onAdded?: () => void
}) {
  const entries = playlist.entries
  const [cat, setCat] = useState('')
  const [busy, setBusy] = useState(false)
  const cats = useCategories(true)

  const toggle = (idx: number) =>
    setPicked((prev) => {
      const next = new Set(prev)
      if (next.has(idx)) next.delete(idx)
      else next.add(idx)
      return next
    })

  const setAll = (select: boolean) =>
    setPicked(select ? new Set(entries.map((e) => e.index)) : new Set())

  const submit = async () => {
    if (busy || picked.size === 0) return
    setBusy(true)
    try {
      const items = entries
        .filter((e) => picked.has(e.index))
        .map((e) => ({
          index: e.index,
          id: e.id,
          url: e.url,
          title: e.title,
          ieKey: e.ie_key,
          thumbnail: e.thumbnail,
          durationSecs: e.duration_secs,
        }))
      const plUrl = playlist.url || job.input
      const req: DownloadReq = {
        url: plUrl,
        title: playlist.title,
        referer: job.referer,
        thumbnail: items[0]?.thumbnail,
        contexts: job.contexts,
        category: cat || undefined,
        createdAt: Math.floor(Date.now() / 1000),
        playlist: {
          title: playlist.title,
          url: plUrl,
          count: playlist.count,
          items,
        },
      }
      await addDownload(req)
      toast.success('Playlist added', {
        description: `${items.length} item${items.length === 1 ? '' : 's'} → ${playlist.title || plUrl}`,
      })
      onAdded?.()
    } catch (e) {
      toast.danger('Adding playlist failed', { description: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="flex flex-col gap-3">
      <div className="flex min-w-0 items-center gap-2.5">
        <span className="min-w-0 flex-1 truncate text-body font-medium" title={playlist.title}>
          {playlist.title || 'Playlist'}
        </span>
        <Tag className="shrink-0">
          {entries.length} item{entries.length === 1 ? '' : 's'}
        </Tag>
        <div className="flex shrink-0 items-center gap-0.5">
          <button
            type="button"
            onClick={() => setAll(true)}
            className="flex h-6 items-center rounded px-2 text-micro font-medium text-muted outline-none transition-colors hover:bg-(--state-hover-bg) hover:text-foreground focus-visible:ring-1 focus-visible:ring-focus"
          >
            All
          </button>
          <button
            type="button"
            onClick={() => setAll(false)}
            className="flex h-6 items-center rounded px-2 text-micro font-medium text-muted outline-none transition-colors hover:bg-(--state-hover-bg) hover:text-foreground focus-visible:ring-1 focus-visible:ring-focus"
          >
            None
          </button>
        </div>
      </div>
      <ul className="flex max-h-72 flex-col overflow-y-auto overscroll-contain rounded-lg bg-surface-secondary p-1">
        {entries.map((e) => (
          <li key={e.index} className="min-w-0">
            <Checkbox
              isSelected={picked.has(e.index)}
              onChange={() => toggle(e.index)}
              className="w-full rounded-md px-2 py-1.5 transition-colors hover:bg-(--state-hover-bg)"
            >
              <Checkbox.Content className="flex w-full min-w-0 items-center gap-2.5">
                <Checkbox.Control className="shrink-0">
                  <Checkbox.Indicator />
                </Checkbox.Control>
                <span className="w-[3ch] shrink-0 text-right font-mono text-micro tabular-nums text-muted-subtle">
                  {e.index}
                </span>
                {e.thumbnail ? (
                  <img
                    src={thumbUrl(e.thumbnail, playlist.url || job.input)}
                    alt=""
                    className="thumb-outline h-6 w-10 shrink-0 rounded-sm object-cover"
                    loading="lazy"
                    onError={(ev) => {
                      ev.currentTarget.style.display = 'none'
                    }}
                  />
                ) : (
                  <div className="flex h-6 w-10 shrink-0 items-center justify-center rounded-sm bg-surface-tertiary text-muted-subtle">
                    <Play width={10} height={10} />
                  </div>
                )}
                <span
                  className="min-w-0 flex-1 truncate text-meta text-foreground"
                  title={e.title || e.url}
                >
                  {e.title || e.url || `Item ${e.index}`}
                </span>
                {e.duration_secs != null && (
                  <span className="shrink-0 font-mono text-micro tabular-nums text-muted-subtle">
                    {fmtDuration(e.duration_secs)}
                  </span>
                )}
              </Checkbox.Content>
            </Checkbox>
          </li>
        ))}
      </ul>
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <CategorySelect cats={cats} value={cat} onChange={setCat} />
        </div>
        <Button
          size="sm"
          variant="primary"
          className="gap-1.5"
          isDisabled={busy || picked.size === 0}
          onPress={() => void submit()}
        >
          <ArrowDownToLine width={14} height={14} />
          {busy
            ? 'Adding…'
            : `Download ${picked.size} item${picked.size === 1 ? '' : 's'}`}
        </Button>
      </div>
    </section>
  )
}

const rowKey = (l?: MediaLink) => (l ? `${l.url}|${l.repr_id ?? ''}` : '')

function ItemSection({
  item,
  extractor,
  onSelected,
}: {
  item: MediaItem
  extractor?: string
  onSelected?: (link: MediaLink | null) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const formatsList = item.formats
  const [sel, setSel] = useState(() => rowKey(formatsList[0]))
  const onSelRef = useRef(onSelected)
  useEffect(() => {
    onSelRef.current = onSelected
  })
  useEffect(() => {
    if (!formatsList.some((f) => rowKey(f) === sel)) {
      setSel(rowKey(formatsList[0]))
    }
  }, [formatsList, sel])
  useEffect(() => {
    onSelRef.current?.(formatsList.find((f) => rowKey(f) === sel) ?? null)
  }, [sel, formatsList])
  const formats = expanded ? formatsList : formatsList.slice(0, VISIBLE_FORMATS)
  const hidden = formatsList.length - VISIBLE_FORMATS
  return (
    <section className="flex flex-col">
      <div className="flex gap-3 pb-3">
        {item.thumbnail ? (
          <img
            src={thumbUrl(item.thumbnail, item.page_url)}
            alt=""
            className="thumb-outline mt-0.5 h-10 w-16 shrink-0 rounded-md object-cover"
            loading="lazy"
            onError={(e) => {
              e.currentTarget.style.display = 'none'
            }}
          />
        ) : (
          <div className="mt-0.5 flex h-10 w-16 shrink-0 items-center justify-center rounded-md bg-surface-secondary text-muted-subtle">
            <Play width={14} height={14} />
          </div>
        )}
        <div className="flex min-w-0 flex-1 flex-col">
          <p className="text-balance text-body font-medium leading-snug text-foreground">
            {item.title ?? (item.id === 'page' ? 'Page media' : item.id)}
          </p>
          {(item.duration_secs != null || extractor) && (
            <div className="mt-1.5 flex items-center gap-1.5">
              {item.duration_secs != null && (
                <Tag className="font-mono tabular-nums">
                  {fmtDuration(item.duration_secs)}
                </Tag>
              )}
              {extractor && (
                <Tag className="font-mono">
                  {extractor}
                </Tag>
              )}
            </div>
          )}
        </div>
      </div>
      {formatsList.length > 0 && (
        <RadioGroup
          aria-label="Format selection"
          className="flex flex-col gap-1"
          value={sel}
          onChange={(k) => setSel(String(k))}
        >
          {formats.map((l) => (
            <FormatRow
              key={rowKey(l)}
              link={l}
              durSecs={item.duration_secs}
            />
          ))}
        </RadioGroup>
      )}
      {formatsList.length === 0 && (
        <p className="px-1 pb-1.5 text-meta text-muted">No downloadable formats found.</p>
      )}
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setExpanded((e) => !e)}
          className="mt-1.5 flex items-center gap-1.5 rounded-md px-1.5 py-1.5 text-left text-meta font-medium text-muted outline-none transition-colors hover:bg-(--state-hover-bg) hover:text-foreground focus-visible:ring-1 focus-visible:ring-focus"
        >
          <ChevronDown
            width={12}
            height={12}
            className={`transition-transform duration-150 ${
              expanded ? 'rotate-180' : ''
            }`}
          />
          {expanded ? 'Show fewer' : `Show ${hidden} more format${hidden === 1 ? '' : 's'}`}
        </button>
      )}
    </section>
  )
}


function FormatRow({
  link,
  durSecs,
}: {
  link: MediaLink
  durSecs?: number
}) {
  const q = link.quality
  const [now] = useState(() => Date.now() / 1000)
  const expIn = link.expires_at != null ? link.expires_at - now : null
  const rawLabel = q?.label && !/unknown/i.test(q.label) ? q.label : ''
  const label =
    q?.height != null
      ? `${q.height}p`
      : rawLabel ||
        (link.kind === 'audio' ? 'Audio' : link.kind === 'hls' || link.kind === 'dash' ? 'Stream' : 'Video')
  const container = link.container?.split('-')[0]
  const codec = link.codecs?.split('.')[0]
  const detail = [link.kind, container, codec].filter(Boolean).join(' · ')
  const est = estBytes(link, durSecs)
  const size =
    link.probed_size != null
      ? fmtBytes(link.probed_size)
      : est != null
        ? `~${fmtBytes(est)}`
        : null
  const expiring = expIn != null && expIn < 900
  return (
    <Radio
      value={rowKey(link)}
      className="mt-0! min-w-0 flex-1 cursor-pointer rounded-lg px-3 py-2.5 transition-colors not-data-selected:hover:bg-(--state-hover-bg) data-selected:bg-(--state-selected-bg)"
    >
      <Radio.Content className="flex w-full items-center gap-3">
        <Radio.Control className="shrink-0">
          <Radio.Indicator />
        </Radio.Control>
        <span className="shrink-0 text-body font-medium text-foreground">
          {label}
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-meta text-muted" title={detail}>
          {detail}
        </span>
        {expiring && (
          <span
            className={`shrink-0 font-mono text-micro tabular-nums ${
              expIn <= 0 ? 'text-(--state-error-fg)' : 'text-(--state-paused-fg)'
            }`}
            title="Link expiry — re-extract when it passes"
          >
            {expIn <= 0
              ? 'expired'
              : `exp ${expIn < 3600 ? `${Math.ceil(expIn / 60)}m` : `${(expIn / 3600).toFixed(1)}h`}`}
          </span>
        )}
        {size && (
          <span className="w-[8ch] shrink-0 text-right font-mono text-meta tabular-nums text-foreground-secondary">
            {size}
          </span>
        )}
      </Radio.Content>
    </Radio>
  )
}
