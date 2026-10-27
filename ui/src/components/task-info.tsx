import type { ReactNode } from 'react'
import { Alert, Button, Drawer, Tabs } from '@heroui/react'
import {
  ArrowRotateLeft,
  Folder,
  FolderOpen,
  Pause,
  Play,
  TriangleExclamation,
} from '@gravity-ui/icons'
import { TaskStatusChip, isLive } from '../downloads'
import {
  buildFileTree,
  fmtBytes,
  fmtDuration,
  isDone,
  taskEta,
  taskName,
  taskProgress,
  type FileTreeNode,
} from '../fmt'
import type { DownloadInfo } from '../types'

/** One label/value row inside the details drawer table. */
function InfoRow({
  label,
  value,
  mono = true,
}: {
  label: string
  value?: ReactNode
  mono?: boolean
}) {
  if (value == null || value === '') return null
  return (
    <tr className="border-b border-separator last:border-0">
      <td className="w-24 py-2 pr-3 align-baseline text-[11.5px] text-muted">{label}</td>
      <td
        className={`min-w-0 break-all py-2 align-baseline text-xs text-(--foreground-secondary) ${
          mono ? 'font-mono' : ''
        }`}
      >
        {value}
      </td>
    </tr>
  )
}

/** Grouped key/value table — the drawer's layout primitive. */
function InfoSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-1.5">
      <h3 className="px-0.5 text-[10px] font-semibold uppercase tracking-[0.08em] text-muted">
        {title}
      </h3>
      <div className="border-t border-separator px-0.5 pt-0.5">
        <table className="w-full table-fixed border-collapse">
          <tbody>{children}</tbody>
        </table>
      </div>
    </section>
  )
}

/** Right-side inspector — every field the task snapshot carries, live. */
export function TaskInfoDrawer({
  item,
  act,
  onClose,
}: {
  item?: DownloadInfo
  act: (gid: string, action: string) => void
  onClose: () => void
}) {
  const done = Number(item?.completedLength ?? 0)
  const total = Number(item?.totalLength ?? 0)
  const speed = Number(item?.downloadSpeed ?? 0)
  const eta = item ? taskEta(item) : null
  const bt = item?.bittorrent
  const url = item?.downloadanything?.url || ''
  const path = item?.files?.[0]?.path
  const fraction = item ? taskProgress(item) : null
  const progress = fraction != null ? Math.min(100, fraction * 100) : null
  const q = item?.downloadanything?.quality
  const quality = q
    ? [
        q.audioOnly ? 'audio only' : q.height ? `${q.height}p` : '',
        q.bandwidth ? `${(q.bandwidth / 1e6).toFixed(1)} Mbps` : '',
      ]
        .filter(Boolean)
        .join(' · ') || undefined
    : undefined
  const up = Number(bt?.uploadSpeed ?? 0)
  const isComplete = item ? isDone(item) : false
  const isPaused = item?.status === 'paused'
  const isError = item?.status === 'error' || !!item?.errorMessage
  const isActive = item ? isLive(item) && item.status !== 'selecting' && !isComplete : false

  return (
    <Drawer.Backdrop isOpen={!!item} onOpenChange={(o) => !o && onClose()}>
      <Drawer.Content placement="right">
        <Drawer.Dialog className="h-full w-full sm:max-w-125">
          <Drawer.CloseTrigger />
          <Drawer.Header className="flex flex-col gap-2.5">
            <Drawer.Heading
              className="truncate text-[15px] font-semibold tracking-[-0.015em] text-foreground"
              title={item ? taskName(item) : ''}
            >
              {item ? taskName(item) : 'Task'}
            </Drawer.Heading>
            {item && (
              <div className="flex flex-wrap items-center gap-2">
                <TaskStatusChip d={item} />
                {total > 0 && (
                  <span className="font-mono text-[11px] tabular-nums text-muted">
                    {fmtBytes(total)}
                  </span>
                )}
              </div>
            )}
          </Drawer.Header>
          <Drawer.Body className="flex flex-col gap-4">
            {item?.downloadanything?.thumbnail && (
              <img
                src={item.downloadanything.thumbnail}
                alt=""
                className="h-32 w-full shrink-0 rounded-xl object-cover"
                onError={(e) => {
                  e.currentTarget.style.display = 'none'
                }}
              />
            )}

            <Tabs defaultSelectedKey="details" className="flex flex-col gap-3">
              <Tabs.List
                aria-label="Task info sections"
                className="flex w-fit min-w-0 gap-1 rounded-lg bg-surface p-1"
              >
                <Tabs.Tab
                  id="details"
                  className="press-tactile h-7 w-auto gap-2 rounded-md px-3 text-xs font-medium transition-colors"
                >
                  Details
                  <Tabs.Indicator className="rounded-md bg-surface-secondary" />
                </Tabs.Tab>
                {bt && (
                  <Tabs.Tab
                    id="torrent"
                    className="press-tactile h-7 w-auto gap-2 rounded-md px-3 text-xs font-medium transition-colors"
                  >
                    Torrent
                    <Tabs.Indicator className="rounded-md bg-surface-secondary" />
                  </Tabs.Tab>
                )}
                {item?.files && item.files.length > 0 && (
                  <Tabs.Tab
                    id="files"
                    className="press-tactile h-7 w-auto gap-2 rounded-md px-3 text-xs font-medium transition-colors"
                  >
                    {item.files.length > 1 ? `Files (${item.files.length})` : 'File'}
                    <Tabs.Indicator className="rounded-md bg-surface-secondary" />
                  </Tabs.Tab>
                )}
              </Tabs.List>

              <Tabs.Panel id="details" className="mt-0 flex flex-col gap-4 p-0 outline-none">
                <InfoSection title="Overview">
                  <InfoRow label="Status" value={item?.status} />
                  <InfoRow label="Title" value={item?.downloadanything?.title} mono={false} />
                  <InfoRow
                    label="Duration"
                    value={
                      item?.downloadanything?.durationSecs
                        ? fmtDuration(item.downloadanything.durationSecs)
                        : undefined
                    }
                  />
                  <InfoRow
                    label="Created"
                    value={
                      item?.createdAt
                        ? new Date(item.createdAt * 1000).toLocaleString()
                        : undefined
                    }
                  />
                  <InfoRow label="Task ID" value={item?.gid} />
                  <InfoRow label="Path" value={path} />
                </InfoSection>

                <InfoSection title="Transfer">
                  <InfoRow
                    label="Progress"
                    value={progress != null ? `${progress.toFixed(1)}%` : undefined}
                  />
                  <InfoRow
                    label="Downloaded"
                    value={
                      done > 0 || total > 0
                        ? `${fmtBytes(done)}${total > 0 ? ` / ${fmtBytes(total)}` : ''}`
                        : undefined
                    }
                  />
                  <InfoRow
                    label="Size"
                    value={total > 0 && !item?.playlist ? fmtBytes(total) : undefined}
                  />
                  {item?.playlist && (
                    <InfoRow
                      label="Items"
                      value={`${item.playlist.items.filter((i) => i.status === 'complete').length} / ${item.playlist.items.length}`}
                    />
                  )}
                  <InfoRow
                    label="Speed"
                    value={speed > 0 ? `${fmtBytes(speed)}/s` : undefined}
                  />
                  <InfoRow label="ETA" value={eta ?? undefined} />
                  {item?.fragments && (
                    <InfoRow
                      label="Segments"
                      value={`${item.fragments.done} / ${item.fragments.total}`}
                    />
                  )}
                </InfoSection>

                {(url ||
                  item?.downloadanything?.pageUrl ||
                  item?.downloadanything?.referer ||
                  item?.downloadanything?.media ||
                  item?.downloadanything?.reprId ||
                  quality ||
                  item?.downloadanything?.filenameHint) && (
                  <InfoSection title="Source">
                    <InfoRow label="URL" value={url || undefined} />
                    <InfoRow label="Page" value={item?.downloadanything?.pageUrl} />
                    <InfoRow label="Referer" value={item?.downloadanything?.referer} />
                    <InfoRow label="Media" value={item?.downloadanything?.media} />
                    <InfoRow label="Format" value={item?.downloadanything?.reprId} />
                    <InfoRow label="Quality" value={quality} mono={false} />
                    <InfoRow label="Name hint" value={item?.downloadanything?.filenameHint} />
                  </InfoSection>
                )}
              </Tabs.Panel>

              {bt && (
                <Tabs.Panel id="torrent" className="mt-0 flex flex-col gap-4 p-0 outline-none">
                  <InfoSection title="Torrent">
                    <InfoRow label="Name" value={bt.name} />
                    <InfoRow label="Info hash" value={bt.infoHash} />
                    <InfoRow
                      label="Mode"
                      value={
                        bt.mode === 'multi'
                          ? 'multi-file'
                          : bt.mode === 'single'
                            ? 'single file'
                            : bt.mode
                      }
                    />
                    <InfoRow label="State" value={bt.state} />
                    <InfoRow
                      label="Selection"
                      value={
                        bt.fileSelectionState === 'awaiting'
                          ? 'awaiting file selection'
                          : bt.fileSelectionState === 'ready'
                            ? 'ready'
                            : undefined
                      }
                    />
                    <InfoRow
                      label="Peers"
                      value={
                        Number(bt.numPeers) > 0
                          ? `${bt.numPeers} (${bt.numSeeds ?? 0} seeds)`
                          : undefined
                      }
                    />
                    <InfoRow
                      label="Upload"
                      value={up > 0 ? `${fmtBytes(up)}/s` : undefined}
                    />
                    <InfoRow label="Availability" value={bt.availability} />
                    <InfoRow
                      label="Seeding"
                      value={
                        Number(bt.seedingTime) > 0
                          ? fmtDuration(Number(bt.seedingTime))
                          : undefined
                      }
                    />
                  </InfoSection>
                </Tabs.Panel>
              )}

              {item?.files && item.files.length > 0 && (
                <Tabs.Panel id="files" className="mt-0 flex flex-col gap-4 p-0 outline-none">
                  {item.files.length === 1 ? (
                    <InfoSection title="File">
                      <InfoRow
                        label="Name"
                        value={item.files[0].path?.split('/').pop()}
                      />
                      <InfoRow
                        label="Size"
                        value={fmtBytes(Number(item.files[0].length ?? 0))}
                      />
                      <InfoRow label="Path" value={item.files[0].path} />
                    </InfoSection>
                  ) : (
                    <InfoSection title={`Files (${item.files.length})`}>
                      {(() => {
                        const rows: ReactNode[] = []
                        const walk = (
                          node: FileTreeNode<NonNullable<typeof item.files>[number]>,
                          depth: number,
                        ) => {
                          node.dirs.forEach((d, i) => {
                            rows.push(
                              <tr key={`d${depth}:${i}:${d.name}`}>
                                <td
                                  colSpan={2}
                                  className="pb-1 pt-2"
                                  style={{ paddingLeft: depth * 16 }}
                                >
                                  <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-muted">
                                    <Folder width={11} height={11} className="shrink-0" />
                                    {d.name}
                                  </span>
                                </td>
                              </tr>,
                            )
                            walk(d, depth + 1)
                          })
                          for (const { file: f, name } of node.files) {
                            const flen = Number(f.length ?? 0)
                            const fdone = Number(f.completedLength ?? 0)
                            rows.push(
                              <tr
                                key={`${f.index}:${f.path}`}
                                className="border-b border-separator last:border-0"
                              >
                                <td
                                  className="min-w-0 truncate py-1.5 pr-3 font-mono text-xs text-(--foreground-secondary)"
                                  style={{ paddingLeft: depth * 16 }}
                                  title={f.path}
                                >
                                  {name}
                                </td>
                                <td className="w-28 py-1.5 text-right font-mono text-[10.5px] tabular-nums text-muted">
                                  {bt && flen > 0 ? `${fmtBytes(fdone)} / ` : ''}
                                  {fmtBytes(flen)}
                                </td>
                              </tr>,
                            )
                          }
                        }
                        walk(buildFileTree(item.files), 0)
                        return rows
                      })()}
                    </InfoSection>
                  )}
                </Tabs.Panel>
              )}
            </Tabs>

            {item?.errorMessage && (
              <Alert status="danger" className="w-full text-left">
                <Alert.Indicator>
                  <TriangleExclamation />
                </Alert.Indicator>
                <Alert.Content>
                  <Alert.Description>{item.errorMessage}</Alert.Description>
                </Alert.Content>
              </Alert>
            )}
          </Drawer.Body>
          <Drawer.Footer className="items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              {isActive && item && (
                <Button
                  variant="secondary"
                  size="sm"
                  className="press-tactile h-8.5 gap-1.5 px-3 text-xs font-medium"
                  onPress={() => act(item.gid, 'pause')}
                >
                  <Pause width={13} height={13} />
                  Pause
                </Button>
              )}
              {isPaused && item && (
                <Button
                  variant="secondary"
                  size="sm"
                  className="press-tactile h-8.5 gap-1.5 px-3 text-xs font-medium"
                  onPress={() => act(item.gid, 'resume')}
                >
                  <Play width={13} height={13} />
                  Resume
                </Button>
              )}
              {isError && item && (
                <Button
                  variant="secondary"
                  size="sm"
                  className="press-tactile h-8.5 gap-1.5 px-3 text-xs font-medium"
                  onPress={() => act(item.gid, 'retry')}
                >
                  <ArrowRotateLeft width={13} height={13} />
                  Retry
                </Button>
              )}
            </div>
            {path && item && (
              <Button
                variant="primary"
                size="sm"
                className="press-tactile ms-auto h-8.5 gap-1.5 px-3.5 text-xs font-medium"
                onPress={() => act(item.gid, 'showFile')}
              >
                <FolderOpen width={14} height={14} />
                Show in folder
              </Button>
            )}
          </Drawer.Footer>
        </Drawer.Dialog>
      </Drawer.Content>
    </Drawer.Backdrop>
  )
}
