import { useRef } from 'react'
import { Button, Modal, TextArea, TextField } from '@heroui/react'
import { Magnet } from '@gravity-ui/icons'

/** Platform modifier for the inline shortcut hint. */
const MOD_KEY = navigator.userAgent.includes('Mac') ? '⌘' : 'Ctrl'

export function ExtractDialog({
  raw,
  setRaw,
  busy,
  urls,
  run,
  open,
  setOpen,
  onTorrentFile,
}: {
  raw: string
  setRaw: (v: string) => void
  busy: boolean
  urls: number
  run: () => void
  open: boolean
  setOpen: (o: boolean) => void
  onTorrentFile: (f: File) => void
}) {
  const fileRef = useRef<HTMLInputElement>(null)
  const go = () => {
    if (busy || urls === 0) return
    setOpen(false)
    run()
  }
  return (
    <Modal.Backdrop isOpen={open} onOpenChange={setOpen}>
      <Modal.Container>
        <Modal.Dialog className="sm:max-w-140">
          <Modal.CloseTrigger />
          <Modal.Header className="gap-1">
            <Modal.Heading>Extract media</Modal.Heading>
            <p className="text-pretty text-xs leading-relaxed text-muted">
              One URL per line. Web pages, playlists, HLS / DASH manifests,
              direct files, and magnet links.
            </p>
          </Modal.Header>
          <Modal.Body className="flex flex-col gap-2.5">
            <TextField aria-label="URLs" value={raw} onChange={setRaw} className="w-full">
              <TextArea
                variant="secondary"
                rows={5}
                autoFocus
                className="w-full resize-none p-3 font-mono text-xs leading-relaxed"
                placeholder={
                  'https://example.com/watch?v=…\nmagnet:?xt=urn:btih:…'
                }
                onKeyDown={(e) => {
                  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') go()
                }}
              />
            </TextField>
            <div className="flex items-center justify-between gap-3 px-0.5">
              <span className="text-[11px] font-medium text-muted">
                {urls > 0
                  ? `${urls} URL${urls === 1 ? '' : 's'} ready`
                  : 'Paste one or more URLs above'}
              </span>
              <span className="font-mono text-[10.5px] text-(--muted-subtle)">
                {MOD_KEY}↵ to extract
              </span>
            </div>
          </Modal.Body>
          <Modal.Footer className="items-center justify-between">
            <input
              ref={fileRef}
              type="file"
              accept=".torrent,application/x-bittorrent"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0]
                e.target.value = ''
                if (f) onTorrentFile(f)
              }}
            />
            <Button
              size="sm"
              variant="secondary"
              className="press-tactile h-8.5 gap-1.5 px-3 text-xs font-medium"
              onPress={() => fileRef.current?.click()}
            >
              <Magnet width={14} height={14} />
              Torrent file…
            </Button>
            <Button
              size="sm"
              variant="primary"
              className="press-tactile h-8.5 gap-1.5 px-4 text-xs font-medium"
              onPress={go}
              isDisabled={busy || urls === 0}
            >
              {busy ? 'Extracting…' : 'Extract'}
            </Button>
          </Modal.Footer>
        </Modal.Dialog>
      </Modal.Container>
    </Modal.Backdrop>
  )
}
