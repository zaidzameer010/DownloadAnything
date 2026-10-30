import { Fragment, useState, type CSSProperties } from 'react'
import { Disclosure, ProgressBar } from '@heroui/react'
import type { DownloadInfo, PlaylistTaskItem } from './types'
import { fmtBytes, fmtDuration, isDone, taskEta, taskName, taskProgress } from './fmt'
import { describeFile } from './filter'
import {
  ArrowDownToLine,
  ArrowRotateLeft,
  ChevronDown,
  CircleCheck,
  CircleDashed,
  CircleInfo,
  CircleXmark,
  FolderOpen,
  ListCheck,
  Magnet,
  Pause,
  Play,
  TrashBin,
  Xmark,
} from '@gravity-ui/icons'
import { IconBtn } from './icon-btn'

/** Live work — everything that is moving, queued, or waiting on the user. */
export const isLive = (d: DownloadInfo) =>
  d.status === 'active' ||
  d.status === 'downloading' ||
  d.status === 'waiting' ||
  d.status === 'postprocessing' ||
  d.status === 'selecting' ||
  d.status === 'seeding'

type TaskState = {
  label: string
  /** CSS custom property carrying the state hue — used by the LED and rail. */
  tone: string
  barClass: string
  /** Attention states carry their hue on the label; settled states stay neutral. */
  attention: boolean
  error?: boolean
}

/** One source of truth for every task state's words and colour. */
function getTaskState(d: DownloadInfo): TaskState {
  if (d.status === 'error' || d.errorMessage) {
    return {
      label: 'Error',
      tone: 'var(--state-error)',
      barClass: 'progress-bar--error',
      attention: true,
      error: true,
    }
  }
  if (d.status === 'seeding') {
    return {
      label: 'Seeding',
      tone: 'var(--state-postprocessing)',
      barClass: 'progress-bar--postprocessing',
      attention: true,
    }
  }
  if (d.status === 'selecting') {
    return {
      label: 'Choose files',
      tone: 'var(--state-paused)',
      barClass: 'progress-bar--paused',
      attention: true,
    }
  }
  if (d.status === 'postprocessing') {
    return {
      label: 'Processing',
      tone: 'var(--state-postprocessing)',
      barClass: 'progress-bar--postprocessing',
      attention: true,
    }
  }
  if (d.status === 'paused') {
    return {
      label: 'Paused',
      tone: 'var(--state-paused)',
      barClass: 'progress-bar--paused',
      attention: true,
    }
  }
  if (d.status === 'active' || d.status === 'downloading') {
    return {
      label: 'Downloading',
      tone: 'var(--state-downloading)',
      barClass: 'progress-bar--downloading',
      attention: true,
    }
  }
  if (d.status === 'complete') {
    return {
      label: 'Completed',
      tone: 'var(--state-complete)',
      barClass: 'progress-bar--complete',
      attention: false,
    }
  }
  if (d.status === 'waiting') {
    return {
      label: 'Queued',
      tone: 'var(--state-queued)',
      barClass: 'progress-bar--queued',
      attention: false,
    }
  }
  return {
    label: d.status || 'Unknown',
    tone: 'var(--state-queued)',
    barClass: 'progress-bar--queued',
    attention: false,
  }
}

/** A 6px LED and its label — the row's only state colour. */
export function TaskStatusMark({
  d,
  className = '',
}: {
  d: DownloadInfo
  className?: string
}) {
  const state = getTaskState(d)
  return (
    <span
      className={`status-mark ${className}`}
      data-tone={state.error ? 'error' : state.attention ? 'attention' : 'neutral'}
      style={{ '--status-tone': state.tone } as CSSProperties}
    >
      <span
        className="status-mark__dot"
        style={{ color: state.tone }}
        aria-hidden="true"
      />
      <span className="status-mark__label">{state.label}</span>
    </span>
  )
}

/** A hairline middot between two pieces of metadata. */
function Dot() {
  return (
    <span aria-hidden="true" className="shrink-0 text-muted-subtle">
      ·
    </span>
  )
}

export function DownloadsView({
  items,
  act,
}: {
  items: DownloadInfo[]
  act: (gid: string, action: string) => void
}) {
  return (
    <ul className="flex flex-col gap-1">
      {items.map((d) => (
        <DlRow key={d.gid} d={d} act={act} />
      ))}
    </ul>
  )
}

function DlRow({
  d,
  act,
}: {
  d: DownloadInfo
  act: (gid: string, action: string) => void
}) {
  const name = taskName(d)
  const q = d.downloadanything?.quality
  const quality = q?.audioOnly ? 'audio' : q?.height ? `${q.height}p` : ''
  const frags = d.fragments
  const total = Number(d.totalLength ?? 0)
  const done = Number(d.completedLength ?? 0)
  const progress = taskProgress(d)
  const speed = Number(d.downloadSpeed ?? 0)
  const eta = taskEta(d)
  const isComplete = isDone(d)
  const isPaused = d.status === 'paused'
  const isError = d.status === 'error' || !!d.errorMessage
  const isSelecting = d.status === 'selecting'
  const isActive =
    d.status === 'active' ||
    d.status === 'downloading' ||
    d.status === 'waiting' ||
    d.status === 'postprocessing'
  const state = getTaskState(d)
  const bt = d.bittorrent
  const pl = d.playlist
  const plItems = pl?.items ?? []
  const plDone = plItems.filter((i) => i.status === 'complete').length

  const stats: string[] = []
  // `total` is seeded from the probe's expectedSize — the size the user saw
  // in the probe modal. For hls/dash it stays a bandwidth estimate (VBR
  // streams have no byte total), so it keeps the modal's ~ and drops out
  // once real bytes pass it.
  const approxTotal =
    d.downloadanything?.media === 'hls' || d.downloadanything?.media === 'dash'
  const size =
    total > 0
      ? isComplete || (approxTotal && done >= total)
        ? fmtBytes(done > 0 ? done : total)
        : `${fmtBytes(done)} / ${approxTotal ? '~' : ''}${fmtBytes(total)}`
      : done > 0
        ? fmtBytes(done)
        : ''
  if (plItems.length > 0) {
    stats.push(`${plDone}/${plItems.length} items`)
    if (done > 0) stats.push(fmtBytes(done))
  } else if (frags) {
    if (frags.total > 0) stats.push(`${frags.done}/${frags.total} segments`)
    if (size) stats.push(size)
  } else {
    if (progress != null && !isComplete) {
      stats.push(`${Math.min(100, progress * 100).toFixed(1)}%`)
    }
    if (size) stats.push(size)
  }
  if (eta) stats.push(`ETA ${eta}`)
  if (bt) {
    if (bt.state === 'downloadingMetadata') stats.push('Fetching metadata')
    else if (bt.state === 'checking') stats.push('Checking data')
    if (Number(bt.numPeers) > 0) {
      stats.push(`${bt.numPeers} peers`)
      if (Number(bt.numSeeds) > 0) stats.push(`${bt.numSeeds} seeds`)
    }
  }
  const upSpeed = Number(bt?.uploadSpeed ?? 0)

  const [thumbFailed, setThumbFailed] = useState(false)
  const [plOpen, setPlOpen] = useState(false)
  const showThumb = !!d.downloadanything?.thumbnail && !thumbFailed
  const kind = bt
    ? { label: 'Torrent', Icon: Magnet }
    : pl
      ? { label: 'Playlist', Icon: Play }
      : describeFile({
          url: d.downloadanything?.url ?? '',
          ext: name.includes('.') ? name.split('.').pop() : '',
        })
  const KindIcon = kind.Icon

  const indeterminate =
    (progress == null && (d.status === 'active' || d.status === 'downloading')) ||
    d.status === 'postprocessing' ||
    bt?.state === 'downloadingMetadata'
  const showRail = !isComplete && !isSelecting

  return (
    <li className="band flex flex-col overflow-hidden">
      <Disclosure
        isExpanded={plOpen}
        onExpandedChange={setPlOpen}
        className="flex flex-col"
      >
        <div className="flex items-center gap-3.5 px-2.5 py-3">
          <div className="relative flex h-10 w-16 shrink-0 items-center justify-center overflow-hidden rounded-md bg-surface-secondary text-muted-subtle">
            {showThumb ? (
              <img
                src={d.downloadanything!.thumbnail}
                alt=""
                className="thumb-outline absolute inset-0 size-full object-cover"
                loading="lazy"
                onError={() => setThumbFailed(true)}
              />
            ) : (
              <KindIcon width={16} height={16} aria-hidden="true" />
            )}
          </div>

          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <p
              className="truncate text-body font-medium leading-5 text-foreground"
              title={d.files?.[0]?.path || d.downloadanything?.title || d.gid}
            >
              {name}
            </p>
            <div className="flex min-w-0 items-center gap-1.5 overflow-hidden font-mono text-meta tabular-nums text-muted">
              {quality && (
                <>
                  <span className="shrink-0 text-foreground-secondary">{quality}</span>
                  <Dot />
                </>
              )}
              {d.errorMessage ? (
                <span
                  className="truncate text-(--state-error-fg)"
                  title={d.errorMessage}
                >
                  {d.errorMessage}
                </span>
              ) : (
                stats.map((s, i) => (
                  <Fragment key={`${s}:${i}`}>
                    {i > 0 && <Dot />}
                    <span className="shrink-0 whitespace-nowrap">{s}</span>
                  </Fragment>
                ))
              )}
              {speed > 0 && (
                <>
                  <Dot />
                  <span className="shrink-0 whitespace-nowrap text-(--state-downloading-fg)">
                    ↓ {fmtBytes(speed)}/s
                  </span>
                </>
              )}
              {upSpeed > 0 && (
                <>
                  <Dot />
                  <span className="shrink-0 whitespace-nowrap text-(--state-postprocessing-fg)">
                    ↑ {fmtBytes(upSpeed)}/s
                  </span>
                </>
              )}
            </div>
          </div>

          <TaskStatusMark d={d} className="hidden w-26 sm:inline-flex" />

          <div className="flex shrink-0 items-center gap-0.5">
            {plItems.length > 0 && (
              <Disclosure.Heading>
                <Disclosure.Trigger
                  aria-label="Show playlist items"
                  className="press-tactile flex size-7 items-center justify-center rounded-md text-muted outline-none transition-colors hover:bg-(--state-hover-bg) hover:text-foreground focus-visible:ring-1 focus-visible:ring-focus"
                >
                  <Disclosure.Indicator>
                    <ChevronDown width={14} height={14} />
                  </Disclosure.Indicator>
                </Disclosure.Trigger>
              </Disclosure.Heading>
            )}
            {isSelecting && (
              <IconBtn
                label="Choose files to download"
                variant="secondary"
                onPress={() => act(d.gid, 'selectFiles')}
              >
                <ListCheck width={14} height={14} />
              </IconBtn>
            )}
            {isActive && (
              <IconBtn label="Pause" onPress={() => act(d.gid, 'pause')}>
                <Pause width={14} height={14} />
              </IconBtn>
            )}
            {isPaused && (
              <>
                <IconBtn label="Resume" onPress={() => act(d.gid, 'resume')}>
                  <Play width={14} height={14} />
                </IconBtn>
                <IconBtn label="Restart download" onPress={() => act(d.gid, 'download')}>
                  <ArrowDownToLine width={14} height={14} />
                </IconBtn>
              </>
            )}
            {isComplete && (
              <IconBtn label="Show in folder" onPress={() => act(d.gid, 'showFile')}>
                <FolderOpen width={14} height={14} />
              </IconBtn>
            )}
            {isError && (
              <IconBtn label="Retry download" onPress={() => act(d.gid, 'retry')}>
                <ArrowRotateLeft width={14} height={14} />
              </IconBtn>
            )}
            <IconBtn label="Details" onPress={() => act(d.gid, 'info')}>
              <CircleInfo width={14} height={14} />
            </IconBtn>
            <IconBtn
              label={isComplete ? 'Remove from list' : 'Cancel'}
              danger
              onPress={() => act(d.gid, 'remove')}
            >
              {isComplete ? <TrashBin width={14} height={14} /> : <Xmark width={14} height={14} />}
            </IconBtn>
          </div>
        </div>

        {plItems.length > 0 && (
          <Disclosure.Content>
            <Disclosure.Body className="p-0 pb-3">
              <PlaylistItemList items={plItems} />
            </Disclosure.Body>
          </Disclosure.Content>
        )}

        {showRail && (
          <ProgressBar
            aria-label={`${name} progress`}
            value={progress != null ? Math.min(100, progress * 100) : 0}
            isIndeterminate={indeterminate}
            className={`${state.barClass} absolute inset-x-(--radius-xl) bottom-0 w-auto`}
          >
            <ProgressBar.Track className="h-0.5 rounded-none bg-transparent">
              <ProgressBar.Fill className="rounded-none" />
            </ProgressBar.Track>
          </ProgressBar>
        )}
      </Disclosure>
    </li>
  )
}

/** Per-entry rows under a playlist task — playlist index, status dot,
 *  title, and a size/progress tail. Scrolls past ~8 rows so long playlists
 *  don't flood. */
function PlaylistItemList({ items }: { items: PlaylistTaskItem[] }) {
  return (
    <ul className="flex max-h-60 flex-col overflow-y-auto rounded-lg bg-surface-secondary p-1">
      {items.map((it) => {
        const pct =
          it.total > 0 ? Math.min(100, (it.downloaded / it.total) * 100) : null
        const icon =
          it.status === 'complete' ? (
            <CircleCheck width={12} height={12} className="shrink-0 text-(--state-complete)" />
          ) : it.status === 'error' ? (
            <CircleXmark width={12} height={12} className="shrink-0 text-(--state-error)" />
          ) : it.status === 'active' ? (
            <CircleDashed
              width={12}
              height={12}
              className="shrink-0 text-(--state-downloading)"
            />
          ) : (
            <CircleDashed width={12} height={12} className="shrink-0 text-muted-subtle" />
          )
        const tail =
          it.status === 'error' ? (
            <span className="text-(--state-error-fg)">{it.error || 'failed'}</span>
          ) : it.status === 'complete' ? (
            it.total || it.downloaded ? (
              <span>{fmtBytes(it.total || it.downloaded)}</span>
            ) : null
          ) : it.status === 'active' ? (
            pct != null ? (
              <span>{pct.toFixed(0)}%</span>
            ) : (
              <span>{fmtBytes(it.downloaded)}</span>
            )
          ) : it.downloaded > 0 ? (
            <span>{fmtBytes(it.downloaded)}</span>
          ) : (
            <span className="text-muted-subtle">queued</span>
          )
        return (
          <li
            key={it.index}
            className="flex min-w-0 items-center gap-2.5 rounded-md px-2 py-1"
            title={
              it.error ||
              (it.status === 'complete' ? it.filename : '') ||
              it.url ||
              it.title
            }
          >
            <span className="w-[3ch] shrink-0 text-right font-mono text-micro tabular-nums text-muted-subtle">
              {it.index}
            </span>
            {icon}
            <span className="min-w-0 flex-1 truncate text-meta text-foreground-secondary">
              {it.title || it.url || `Item ${it.index}`}
            </span>
            {it.duration_secs ? (
              <span className="shrink-0 font-mono text-micro tabular-nums text-muted-subtle">
                {fmtDuration(it.duration_secs)}
              </span>
            ) : null}
            <span className="shrink-0 font-mono text-micro tabular-nums text-muted">
              {tail}
            </span>
          </li>
        )
      })}
    </ul>
  )
}
