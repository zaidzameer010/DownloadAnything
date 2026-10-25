import { Fragment, useState } from 'react'
import { Chip, Disclosure, ProgressBar } from '@heroui/react'
import type { DownloadInfo, PlaylistTaskItem } from './types'
import { fmtBytes, fmtDuration, isDone, taskEta, taskName, taskProgress } from './fmt'
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
  Pause,
  Play,
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

export function DownloadsView({
  items,
  act,
}: {
  items: DownloadInfo[]
  act: (gid: string, action: string) => void
}) {
  return (
    <ul className="flex flex-col divide-y divide-separator">
      {items.map((d) => (
        <DlRow key={d.gid} d={d} act={act} />
      ))}
    </ul>
  )
}

export function getTaskChip(d: DownloadInfo): {
  label: string
  className: string
  dotClass: string
  barClass: string
} {
  if (d.status === 'error' || d.errorMessage) {
    return {
      label: 'Error',
      className: 'chip--error',
      dotClass: 'bg-(--state-error)',
      barClass: 'progress-bar--error',
    }
  }
  if (d.status === 'seeding') {
    return {
      label: 'Seeding',
      className: 'chip--seeding',
      dotClass: 'bg-(--state-postprocessing)',
      barClass: 'progress-bar--postprocessing',
    }
  }
  if (d.status === 'selecting') {
    return {
      label: 'Choose files',
      className: 'chip--selecting',
      dotClass: 'bg-(--state-paused)',
      barClass: 'progress-bar--paused',
    }
  }
  if (d.status === 'postprocessing') {
    return {
      label: 'Processing',
      className: 'chip--postprocessing',
      dotClass: 'bg-(--state-postprocessing) animate-pulse',
      barClass: 'progress-bar--postprocessing',
    }
  }
  if (d.status === 'paused') {
    return {
      label: 'Paused',
      className: 'chip--paused',
      dotClass: 'bg-(--state-paused)',
      barClass: 'progress-bar--paused',
    }
  }
  if (d.status === 'active' || d.status === 'downloading') {
    return {
      label: 'Downloading',
      className: 'chip--downloading',
      dotClass: 'bg-(--state-downloading) animate-pulse',
      barClass: 'progress-bar--downloading',
    }
  }
  if (d.status === 'complete') {
    return {
      label: 'Completed',
      className: 'chip--complete',
      dotClass: 'bg-(--state-complete)',
      barClass: 'progress-bar--complete',
    }
  }
  if (d.status === 'waiting') {
    return {
      label: 'Queued',
      className: 'chip--queued',
      dotClass: 'bg-(--state-queued)',
      barClass: 'progress-bar--queued',
    }
  }
  return {
    label: d.status || 'Unknown',
    className: 'chip--queued',
    dotClass: 'bg-(--state-queued)',
    barClass: 'progress-bar--queued',
  }
}

export function TaskStatusChip({ d }: { d: DownloadInfo }) {
  const chip = getTaskChip(d)
  return (
    <Chip
      size="sm"
      variant="soft"
      className={`${chip.className} h-5.5 shrink-0 gap-1.5 px-2 font-medium`}
    >
      <span className={`size-1.5 shrink-0 rounded-full ${chip.dotClass}`} aria-hidden="true" />
      <span>{chip.label}</span>
    </Chip>
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
  const q = d.dax?.quality
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
  const chip = getTaskChip(d)
  const bt = d.bittorrent
  const pl = d.playlist
  const plItems = pl?.items ?? []
  const plDone = plItems.filter((i) => i.status === 'complete').length


  const stats: { t: string; w?: number }[] = []
  if (plItems.length > 0) {
    stats.push({
      t: `${plDone}/${plItems.length} items`,
      w: String(plItems.length).length * 2 + 8,
    })
    if (done > 0) stats.push({ t: fmtBytes(done), w: 7 })
  } else if (frags) {
    if (frags.total > 0)
      stats.push({
        t: `${frags.done}/${frags.total} segments`,
        w: String(frags.total).length * 2 + 10,
      })
    if (done > 0) stats.push({ t: fmtBytes(done), w: 7 })
  } else {
    if (progress != null && !isComplete) {
      stats.push({ t: `${Math.min(100, progress * 100).toFixed(1)}%`, w: 6 })
    }
    if (isComplete && total > 0) {
      stats.push({ t: fmtBytes(total), w: 7 })
    } else if (total > 0) {
      const totalStr = fmtBytes(total)
      stats.push({ t: `${fmtBytes(done)} / ${totalStr}`, w: totalStr.length * 2 + 3 })
    } else if (done > 0) {
      stats.push({ t: fmtBytes(done), w: 7 })
    }
  }
  if (eta) stats.push({ t: `ETA ${eta}`, w: 11 })
  if (bt) {
    if (bt.state === 'downloadingMetadata') stats.push({ t: 'Fetching metadata' })
    else if (bt.state === 'checking') stats.push({ t: 'Checking data' })
    if (Number(bt.numPeers) > 0) {
      stats.push({ t: `${bt.numPeers} peers`, w: 9 })
      if (Number(bt.numSeeds) > 0) stats.push({ t: `${bt.numSeeds} seeds`, w: 9 })
    }
  }
  const upSpeed = Number(bt?.uploadSpeed ?? 0)

  const [thumbFailed, setThumbFailed] = useState(false)
  const [plOpen, setPlOpen] = useState(false)
  const showThumb = !!d.dax?.thumbnail && !thumbFailed

  return (
    <li className="rise-in group relative -mx-2 flex flex-col gap-2 rounded-lg px-2 py-3 transition-colors duration-150 hover:bg-(--state-hover-bg) focus-within:bg-(--state-hover-bg)">
      <Disclosure
        isExpanded={plOpen}
        onExpandedChange={setPlOpen}
        className="flex flex-col gap-2"
      >
      <div className="flex items-center gap-3.5">
        {plItems.length > 0 && (
          <Disclosure.Heading className="shrink-0 self-center">
            <Disclosure.Trigger
              aria-label="Show playlist items"
              className="flex size-6 items-center justify-center rounded-md text-muted outline-none transition-colors hover:bg-surface-secondary hover:text-foreground focus-visible:ring-2 focus-visible:ring-focus"
            >
              <Disclosure.Indicator>
                <ChevronDown width={14} height={14} />
              </Disclosure.Indicator>
            </Disclosure.Trigger>
          </Disclosure.Heading>
        )}
        {/* Media preview / source icon */}
        <div className="relative flex h-9.5 w-14 shrink-0 items-center justify-center overflow-hidden rounded-md bg-surface-secondary text-muted transition-colors group-hover:bg-surface-tertiary group-focus-within:bg-surface-tertiary">
          {showThumb ? (
            <img
              src={d.dax!.thumbnail}
              alt=""
              className="absolute inset-0 size-full object-cover"
              loading="lazy"
              onError={() => setThumbFailed(true)}
            />
          ) : (
            <ArrowDownToLine width={15} height={15} aria-hidden="true" />
          )}
        </div>

        {/* Title & telemetry */}
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex min-w-0 items-center gap-2.5">
            <p
              className="min-w-0 flex-1 truncate text-[13.5px] font-medium leading-5 tracking-[-0.01em] text-foreground"
              title={d.files?.[0]?.path || d.dax?.title || d.gid}
            >
              {name}
            </p>
            {quality && (
              <span className="shrink-0 rounded bg-surface-secondary px-1.5 py-px font-mono text-[10.5px] tabular-nums text-muted">
                {quality}
              </span>
            )}
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1">
            {stats.length > 0 && (
              <span className="flex min-w-0 flex-wrap items-center font-mono text-[11.5px] tabular-nums text-muted">
                {stats.map((s, i) => (
                  <Fragment key={i}>
                    {i > 0 && (
                      <span
                        aria-hidden="true"
                        className="w-[3ch] shrink-0 text-center text-(--muted-subtle)"
                      >
                        ·
                      </span>
                    )}
                    <span
                      className="shrink-0 whitespace-nowrap text-center"
                      style={{ width: `${s.w ?? s.t.length}ch` }}
                    >
                      {s.t}
                    </span>
                  </Fragment>
                ))}
              </span>
            )}
            {speed > 0 && (
              <span className="inline-flex items-center gap-1 font-mono text-[11.5px] font-medium tabular-nums text-(--state-downloading-fg)">
                ↓ <span className="inline-block w-[9ch] text-center">{fmtBytes(speed)}/s</span>
              </span>
            )}
            {upSpeed > 0 && (
              <span className="inline-flex items-center gap-1 font-mono text-[11.5px] tabular-nums text-(--state-postprocessing-fg)">
                ↑ <span className="inline-block w-[9ch] text-center">{fmtBytes(upSpeed)}/s</span>
              </span>
            )}
            {d.errorMessage && (
              <span
                className="truncate text-[11.5px] font-medium text-(--state-error-fg)"
                title={d.errorMessage}
              >
                {d.errorMessage}
              </span>
            )}
          </div>
        </div>

        <TaskStatusChip d={d} />

        {/* Row actions — always visible for immediate affordance and consistent row geometry */}
        <div className="flex shrink-0 items-center gap-1">
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
            onPress={() => act(d.gid, 'remove')}
            className="hover:bg-danger-soft hover:text-(--state-error-fg)"
          >
            <Xmark width={14} height={14} />
          </IconBtn>
        </div>
      </div>

      {plItems.length > 0 && (
        <Disclosure.Content>
          <Disclosure.Body className="p-0">
            <PlaylistItemList items={plItems} count={pl?.count} />
          </Disclosure.Body>
        </Disclosure.Content>
      )}

      {!isComplete && (
        <ProgressBar
          aria-label={name}
          size="sm"
          value={progress != null ? Math.min(100, progress * 100) : 0}
          isIndeterminate={
            (!progress && (d.status === 'active' || d.status === 'downloading')) ||
            d.status === 'postprocessing' ||
            bt?.state === 'downloadingMetadata'
          }
          className={`${chip.barClass} gap-0`}
        >
          <ProgressBar.Track className="h-1 bg-surface-secondary">
            <ProgressBar.Fill />
          </ProgressBar.Track>
        </ProgressBar>
      )}
      </Disclosure>
    </li>
  )
}

/** Per-entry rows under a playlist task — reversed index, status dot,
 *  title, and a size/progress tail. Scrolls past ~8 rows so long playlists
 *  don't flood. */
function PlaylistItemList({
  items,
  count,
}: {
  items: PlaylistTaskItem[]
  count?: number
}) {
  const total = Math.max(
    count || 0,
    items.length,
    ...items.map((i) => i.index),
  )
  return (
    <ul className="flex max-h-60 flex-col gap-px overflow-y-auto rounded-lg bg-surface-secondary p-1">
      {items.map((it) => {
        const pct =
          it.total > 0 ? Math.min(100, (it.downloaded / it.total) * 100) : null
        const icon =
          it.status === 'complete' ? (
            <CircleCheck width={13} height={13} className="shrink-0 text-(--state-complete-fg)" />
          ) : it.status === 'error' ? (
            <CircleXmark width={13} height={13} className="shrink-0 text-(--state-error-fg)" />
          ) : it.status === 'active' ? (
            <CircleDashed
              width={13}
              height={13}
              className="shrink-0 animate-pulse text-(--state-downloading-fg)"
            />
          ) : (
            <CircleDashed width={13} height={13} className="shrink-0 text-muted" />
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
          ) : (
            <span className="text-muted">queued</span>
          )
        return (
          <li
            key={it.index}
            className="flex min-w-0 items-center gap-2.5 rounded-md px-2 py-1"
            title={it.error || it.filename || it.url || it.title}
          >
            <span className="w-[4ch] shrink-0 text-right font-mono text-[10.5px] tabular-nums text-muted">
              {total - it.index + 1}
            </span>
            {icon}
            <span className="min-w-0 flex-1 truncate text-xs text-foreground">
              {it.title || it.url || `Item ${it.index}`}
            </span>
            {it.duration_secs ? (
              <span className="shrink-0 font-mono text-[10.5px] tabular-nums text-(--muted-subtle)">
                {fmtDuration(it.duration_secs)}
              </span>
            ) : null}
            <span className="shrink-0 font-mono text-[10.5px] tabular-nums text-muted">
              {tail}
            </span>
          </li>
        )
      })}
    </ul>
  )
}
