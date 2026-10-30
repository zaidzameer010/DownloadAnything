import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Alert, Button, Checkbox, Modal, Spinner, toast } from '@heroui/react'
import {
  ArrowDown,
  ArrowDownToLine,
  ArrowUp,
  ChevronRight,
  Folder,
  FolderOpen,
  Magnet,
  TriangleExclamation,
} from '@gravity-ui/icons'
import { addDownload, inspectTorrent, setTorrentFiles } from '../tauri'
import {
  buildFileTree,
  errMsg,
  fmtBytes,
  formatSelection,
  isMagnet,
  magnetInfoHash,
  magnetName,
  type FileTreeNode,
} from '../fmt'
import type {
  DownloadInfo,
  DownloadReq,
  RequestContext,
  TorrentPreview,
} from '../types'
import { describeFile } from '../filter'
import { CategorySelect, useCategories } from './categories'
import { Tag } from './tag'

type TorrentFileEntry = { index?: string; path: string; length?: string }

type TorrentJob = {
  id: number
  input: string
  contexts?: RequestContext[]
  referer?: string
  torrent?: string
}

/** Copy `set` with `indices` added/removed — the picked-set update callers
 *  pass as onToggleDir. */
export const pickMany = (set: Set<number>, indices: number[], select: boolean) => {
  const next = new Set(set)
  for (const i of indices) select ? next.add(i) : next.delete(i)
  return next
}

/** Copy `set` with one index flipped. */
const toggleIndex = (set: Set<number>, idx: number) => {
  const next = new Set(set)
  if (next.has(idx)) next.delete(idx)
  else next.add(idx)
  return next
}

/** Torrent files as a nested folder tree with checkboxes — shared by the
 *  probe card, the intercept modal and the picker dialog. Folder rows carry
 *  a select-all checkbox when onToggleDir is provided. */
export function TorrentFileList({
  files,
  picked,
  onToggle,
  onToggleDir,
  onAll,
  maxH = 'max-h-52',
}: {
  files: TorrentFileEntry[]
  picked: Set<number>
  onToggle: (index: number) => void
  /** Folder-level select: (descendant indices, select). */
  onToggleDir?: (indices: number[], select: boolean) => void
  /** Renders an "All / None" header row when provided. */
  onAll?: (select: boolean) => void
  /** Scroll height of the list — dialogs with room pass a taller value. */
  maxH?: string
}) {
  const tree = buildFileTree(files)
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [sort, setSort] = useState<{ key: 'name' | 'size'; dir: 1 | -1 }>({
    key: 'name',
    dir: 1,
  })
  const toggleCollapse = (path: string) =>
    setCollapsed((c) => {
      const next = new Set(c)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  const sortBy = (key: 'name' | 'size') =>
    setSort((s) =>
      s.key === key ? { key, dir: (s.dir * -1) as 1 | -1 } : { key, dir: 1 },
    )

  const collect = (
    n: FileTreeNode<TorrentFileEntry>,
    out: { idxs: number[]; size: number } = { idxs: [], size: 0 },
  ) => {
    for (const { file } of n.files) {
      out.idxs.push(Number(file.index ?? 0))
      out.size += Number(file.length ?? 0)
    }
    for (const d of n.dirs) collect(d, out)
    return out
  }

  /** Explorer-style ordering: applied within each tree level — dirs sort by
   *  aggregate size, files by their own length. */
  const sortedDirs = (dirs: FileTreeNode<TorrentFileEntry>[]) =>
    [...dirs].sort((a, b) =>
      sort.key === 'size'
        ? (collect(a).size - collect(b).size) * sort.dir
        : a.name.localeCompare(b.name) * sort.dir,
    )
  const sortedFiles = (list: { file: TorrentFileEntry; name: string }[]) =>
    [...list].sort((a, b) =>
      sort.key === 'size'
        ? (Number(a.file.length ?? 0) - Number(b.file.length ?? 0)) * sort.dir
        : a.name.localeCompare(b.name) * sort.dir,
    )

  const fileRow = (f: TorrentFileEntry, name: string) => {
    const idx = Number(f.index ?? 0)
    const { Icon } = describeFile({
      url: '',
      ext: name.includes('.') ? name.split('.').pop() : '',
    })
    return (
      <li key={`${f.index}:${f.path}`} className="min-w-0">
        <Checkbox
          isSelected={picked.has(idx)}
          onChange={() => onToggle(idx)}
          className="w-full rounded-md px-1.5 py-1 transition-colors hover:bg-(--state-hover-bg)"
        >
          <Checkbox.Content className="flex w-full min-w-0 items-center gap-2">
            <Checkbox.Control className="shrink-0">
              <Checkbox.Indicator />
            </Checkbox.Control>
            <Icon width={12} height={12} className="shrink-0 text-muted-subtle" />
            <span
              className="min-w-0 flex-1 truncate font-mono text-meta text-foreground-secondary"
              title={f.path}
            >
              {name}
            </span>
            {f.length && (
              <span className="shrink-0 font-mono text-micro tabular-nums text-muted">
                {fmtBytes(Number(f.length))}
              </span>
            )}
          </Checkbox.Content>
        </Checkbox>
      </li>
    )
  }

  const dirNode = (node: FileTreeNode<TorrentFileEntry>): ReactNode => {
    const { idxs, size } = collect(node)
    const sel = idxs.filter((i) => picked.has(i)).length
    const open = !collapsed.has(node.path)
    return (
      <div key={node.path} className="flex min-w-0 flex-col">
        <div className="flex items-center rounded-md px-1 transition-colors hover:bg-(--state-hover-bg)">
          {onToggleDir && (
            <Checkbox
              isSelected={idxs.length > 0 && sel === idxs.length}
              onChange={() => onToggleDir(idxs, sel !== idxs.length)}
              className="shrink-0 ps-1"
            >
              <Checkbox.Content className="flex items-center">
                <Checkbox.Control className="shrink-0">
                  <Checkbox.Indicator />
                </Checkbox.Control>
              </Checkbox.Content>
            </Checkbox>
          )}
          <button
            type="button"
            onClick={() => toggleCollapse(node.path)}
            aria-expanded={open}
            aria-label={`${open ? 'Collapse' : 'Expand'} ${node.name}`}
            className="flex min-w-0 flex-1 items-center gap-1.5 rounded-md px-1 py-1 text-left outline-none focus-visible:ring-1 focus-visible:ring-focus"
          >
            <ChevronRight
              width={10}
              height={10}
              className={`shrink-0 text-muted-subtle transition-transform duration-150 ${open ? 'rotate-90' : ''}`}
            />
            {open ? (
              <FolderOpen width={12} height={12} className="shrink-0 text-muted-subtle" />
            ) : (
              <Folder width={12} height={12} className="shrink-0 text-muted-subtle" />
            )}
            <span className="min-w-0 flex-1 truncate text-meta font-medium text-foreground-secondary">
              {node.name}
            </span>
            {size > 0 && (
              <span className="shrink-0 font-mono text-micro tabular-nums text-muted">
                {fmtBytes(size)}
              </span>
            )}
          </button>
        </div>
        {open && (
          <div className="ms-3.5 flex min-w-0 flex-col border-s border-separator ps-1.5">
            <ul className="flex flex-col">
              {sortedFiles(node.files).map(({ file, name }) => fileRow(file, name))}
            </ul>
            {sortedDirs(node.dirs).map(dirNode)}
          </div>
        )}
      </div>
    )
  }

  const rootFiles = (
    <ul className="flex flex-col">
      {sortedFiles(tree.files).map(({ file, name }) => fileRow(file, name))}
    </ul>
  )

  const SortHeader = ({ id, label }: { id: 'name' | 'size'; label: string }) => {
    const active = sort.key === id
    return (
      <button
        type="button"
        onClick={() => sortBy(id)}
        className={`flex h-6 items-center gap-0.5 rounded px-1.5 text-micro font-semibold uppercase tracking-[0.08em] outline-none transition-colors hover:text-foreground focus-visible:ring-1 focus-visible:ring-focus ${
          active ? 'text-foreground-secondary' : 'text-muted-subtle'
        }`}
      >
        {label}
        {active &&
          (sort.dir === 1 ? (
            <ArrowUp width={9} height={9} />
          ) : (
            <ArrowDown width={9} height={9} />
          ))}
      </button>
    )
  }

  return (
    <div className="flex w-full flex-col gap-2 rounded-lg bg-surface-secondary p-2 text-left">
      {onAll && (
        <div className="flex items-center justify-between px-1">
          <span className="overline">
            {files.length} file{files.length === 1 ? '' : 's'}
          </span>
          <div className="flex items-center gap-0.5">
            <button
              type="button"
              onClick={() => onAll(true)}
              className="flex h-6 items-center rounded px-2 text-micro font-medium text-muted outline-none transition-colors hover:bg-(--state-hover-bg) hover:text-foreground focus-visible:ring-1 focus-visible:ring-focus"
            >
              All
            </button>
            <button
              type="button"
              onClick={() => onAll(false)}
              className="flex h-6 items-center rounded px-2 text-micro font-medium text-muted outline-none transition-colors hover:bg-(--state-hover-bg) hover:text-foreground focus-visible:ring-1 focus-visible:ring-focus"
            >
              None
            </button>
          </div>
        </div>
      )}
      <div className="flex items-center justify-between px-0.5 pb-1.5">
        <SortHeader id="name" label="Filename" />
        <SortHeader id="size" label="Size" />
      </div>
      <div className={`flex ${maxH} flex-col overflow-y-auto`}>
        {sortedDirs(tree.dirs).map(dirNode)}
        {rootFiles}
      </div>
    </div>
  )
}

/** Magnet / .torrent probe card. Magnets go straight to the download (the
 *  picker opens when metadata lands); a .torrent URL is inspected first so
 *  the file list is picked before anything is added. */
export function TorrentJobCard({
  job,
  onAdded,
}: {
  job: TorrentJob
  /** Fired after the task was accepted — the probe modal closes itself when
   *  no media rows remain, so the picker isn't stacked on top of it. */
  onAdded?: () => void
}) {
  const [cat, setCat] = useState('')
  const cats = useCategories(true)
  const [preview, setPreview] = useState<TorrentPreview | null>(null)
  const [picked, setPicked] = useState<Set<number>>(new Set())
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const magnet = isMagnet(job.input)
  const data = job.torrent
  const hash = magnetInfoHash(job.input)
  const name = magnet ? magnetName(job.input) || hash : preview?.name || ''

  useEffect(() => {
    if (magnet) return
    let dead = false
    inspectTorrent(
      data ? { data } : { url: job.input, referer: job.referer, contexts: job.contexts },
    )
      .then((p) => {
        if (dead) return
        setPreview(p)
        setPicked(new Set(p.files.map((f) => Number(f.index))))
      })
      .catch((e) => !dead && setError(errMsg(e)))
    return () => {
      dead = true
    }
  }, [job.input, data])

  const toggle = (idx: number) => setPicked((prev) => toggleIndex(prev, idx))

  const toggleDir = (indices: number[], select: boolean) =>
    setPicked((prev) => pickMany(prev, indices, select))

  const submit = async () => {
    if (busy) return
    setBusy(true)
    try {
      const req: DownloadReq = magnet
        ? {
            url: job.input,
            kind: 'torrent',
            filenameHint: magnetName(job.input) || undefined,
            referer: job.referer,
            category: cat || undefined,
            createdAt: Math.floor(Date.now() / 1000),
          }
        : {
            url: data ? '' : job.input,
            kind: 'torrent',
            torrent: preview?.torrent,
            filenameHint: preview?.name,
            selectFiles: formatSelection([...picked]),
            referer: job.referer,
            category: cat || undefined,
            createdAt: Math.floor(Date.now() / 1000),
          }
      await addDownload(req)
      toast.success('Torrent added', {
        description: magnet
          ? 'Pick files when the metadata arrives'
          : `${picked.size} file${picked.size === 1 ? '' : 's'} selected`,
      })
      onAdded?.()
    } catch (e) {
      toast.danger('Adding torrent failed', { description: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  const canSubmit = magnet || (!!preview && picked.size > 0)
  const setAll = (select: boolean) =>
    setPicked(select ? new Set(preview?.files.map((f) => Number(f.index)) ?? []) : new Set())

  return (
    <section className="rise-in flex flex-col gap-3">
      <div className="flex items-center gap-2.5">
        <Magnet width={14} height={14} className="shrink-0 text-muted" />
        <span className="min-w-0 flex-1 truncate text-body font-medium" title={job.input}>
          {name || (magnet ? 'Magnet link' : 'Torrent file')}
        </span>
        <Tag className="shrink-0">
          {magnet ? 'magnet' : data ? '.torrent' : 'torrent'}
        </Tag>
      </div>
      {magnet ? (
        <p className="truncate font-mono text-micro text-muted" title={job.input}>
          {hash || job.input}
        </p>
      ) : error ? (
        <Alert status="danger">
          <Alert.Indicator>
            <TriangleExclamation />
          </Alert.Indicator>
          <Alert.Content>
            <Alert.Description>{error}</Alert.Description>
          </Alert.Content>
        </Alert>
      ) : !preview ? (
        <div className="flex items-center gap-2 text-meta text-muted">
          <Spinner size="sm" />
          Reading torrent…
        </div>
      ) : (
        <>
          <p className="font-mono text-meta tabular-nums text-muted">
            {fmtBytes(Number(preview.totalLength ?? 0))}
            {preview.mode === 'multi' ? ' · multi-file' : ' · single file'}
          </p>
          <TorrentFileList
            files={preview.files}
            picked={picked}
            onToggle={toggle}
            onToggleDir={toggleDir}
            onAll={setAll}
          />
        </>
      )}
      <div className="flex items-center justify-between gap-2">
        <CategorySelect cats={cats} value={cat} onChange={setCat} />
        <Button
          size="sm"
          variant="primary"
          className="gap-1.5"
          isDisabled={busy || !canSubmit}
          onPress={() => void submit()}
        >
          <ArrowDownToLine width={14} height={14} />
          {busy
            ? 'Adding…'
            : magnet
              ? 'Download'
              : `Download ${picked.size} file${picked.size === 1 ? '' : 's'}`}
        </Button>
      </div>
    </section>
  )
}

/** File picker for a magnet parked by pause-metadata — auto-opened when the
 *  metadata arrives and reachable from the row's "select files" action. */
export function TorrentPickerDialog({
  item,
  onClose,
}: {
  item?: DownloadInfo
  onClose: () => void
}) {
  const files = item?.files ?? []
  const [picked, setPicked] = useState<Set<number>>(new Set())
  const [busy, setBusy] = useState(false)
  const seeded = useRef(false)
  useEffect(() => {
    if (seeded.current || files.length === 0) return
    seeded.current = true
    setPicked(new Set(files.map((f) => Number(f.index))))
  }, [files])

  const toggle = (idx: number) => setPicked((prev) => toggleIndex(prev, idx))

  const toggleDir = (indices: number[], select: boolean) =>
    setPicked((prev) => pickMany(prev, indices, select))

  const setAll = (select: boolean) =>
    setPicked(select ? new Set(files.map((f) => Number(f.index))) : new Set())

  const selectedBytes = files
    .filter((f) => picked.has(Number(f.index)))
    .reduce((n, f) => n + Number(f.length ?? 0), 0)

  const submit = async () => {
    if (!item || busy || picked.size === 0) return
    setBusy(true)
    try {
      await setTorrentFiles(item.gid, [...picked])
      toast.success('Download started', {
        description: `${picked.size} of ${files.length} files`,
      })
      onClose()
    } catch (e) {
      toast.danger('File selection failed', { description: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal.Backdrop isOpen={!!item} onOpenChange={(o) => !o && onClose()}>
      <Modal.Container>
        <Modal.Dialog className="sm:max-w-136">
          <Modal.CloseTrigger />
          <Modal.Header className="gap-1.5">
            <Modal.Heading>
              <span className="flex items-center gap-2">
                <Magnet width={14} height={14} className="text-muted" />
                Choose files
              </span>
            </Modal.Heading>
            <p className="max-w-[48ch] text-pretty text-meta leading-relaxed text-muted">
              Only the checked files download. You can add the rest later by
              resuming this task.
            </p>
          </Modal.Header>
          <Modal.Body className="flex flex-col gap-3">
            <p className="truncate text-body font-medium text-foreground" title={item?.bittorrent?.name}>
              {item?.bittorrent?.name || 'Torrent'}
            </p>
            <TorrentFileList
              files={files}
              picked={picked}
              onToggle={toggle}
              onToggleDir={toggleDir}
              onAll={setAll}
              maxH="max-h-[45vh]"
            />
            <div className="flex items-center justify-between font-mono text-meta tabular-nums text-muted">
              <span>
                {picked.size} of {files.length} selected
              </span>
              <span>{fmtBytes(selectedBytes)}</span>
            </div>
          </Modal.Body>
          <Modal.Footer className="justify-end gap-2">
            <Button
              slot="close"
              variant="secondary"
              size="sm"
              onPress={onClose}
            >
              Cancel
            </Button>
            <Button
              slot="close"
              variant="primary"
              size="sm"
              isDisabled={busy || picked.size === 0}
              onPress={() => void submit()}
            >
              {busy
                ? 'Starting…'
                : `Download ${picked.size} file${picked.size === 1 ? '' : 's'}`}
            </Button>
          </Modal.Footer>
        </Modal.Dialog>
      </Modal.Container>
    </Modal.Backdrop>
  )
}
