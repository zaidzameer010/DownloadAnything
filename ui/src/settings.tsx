import { useEffect, useRef, useState } from 'react'
import {
  AlertDialog,
  Button,
  Description,
  Input,
  Label,
  ListBox,
  ScrollShadow,
  Select,
  Switch,
  Tabs,
  TextField,
  toast,
} from '@heroui/react'
import {
  engineStatus,
  extensionSetup,
  getAppSettings,
  getYtDlpInfo,
  inTauri,
  installExtension,
  logError,
  regenerateTokens,
  restartBrowserWithExtension,
  revealExtension,
  saveAppSettings,
} from './tauri'
import type { AppSettings, Category, EngineStatus, YtDlpInfo } from './types'
import type { BrowserInfo, ExtensionSetup } from './tauri'
import { errMsg } from './fmt'
import {
  ArrowDownToLine,
  ArrowLeft,
  Check,
  Copy,
  Filmstrip,
  FolderOpen,
  Gear,
  Globe,
  Magnet,
  Plus,
  Puzzle,
  TrashBin,
} from '@gravity-ui/icons'
import { IconBtn } from './icon-btn'

type SettingsTab =
  | 'extraction'
  | 'downloads'
  | 'torrents'
  | 'media'
  | 'output'
  | 'extension'
  | 'engine'

const TABS: { id: SettingsTab; label: string; desc: string; icon: React.ReactNode }[] = [
  {
    id: 'extraction',
    label: 'Extraction',
    desc: 'Pipeline, clients & cookies',
    icon: <Globe width={15} height={15} />,
  },
  {
    id: 'downloads',
    label: 'Downloads',
    desc: 'Engine, concurrency & speed',
    icon: <ArrowDownToLine width={15} height={15} />,
  },
  {
    id: 'torrents',
    label: 'Torrents',
    desc: 'Seeding, peers & discovery',
    icon: <Magnet width={15} height={15} />,
  },
  {
    id: 'media',
    label: 'Media',
    desc: 'Container & metadata',
    icon: <Filmstrip width={15} height={15} />,
  },
  {
    id: 'output',
    label: 'Output',
    desc: 'Named output folders',
    icon: <FolderOpen width={15} height={15} />,
  },
  {
    id: 'extension',
    label: 'Extension',
    desc: 'Browser integration',
    icon: <Puzzle width={15} height={15} />,
  },
  {
    id: 'engine',
    label: 'Engine & RPC',
    desc: 'API server, daemon & TLS',
    icon: <Gear width={15} height={15} />,
  },
]

/** Numeric field parser — empty clears to undefined, invalid input never
 *  propagates NaN into settings (JSON would serialize it as null). */
const numOpt = (v: string) => {
  if (v === '') return undefined
  const n = Number(v)
  return Number.isFinite(n) ? n : undefined
}

/** Backend settings defaults — "Reset to defaults" restores this draft
 *  (nothing persists until Save). Mirrors the py AppSettings defaults. */
const DEFAULT_SETTINGS: AppSettings = {
  categories: [],
  apiPort: 16801,
  apiToken: '',
  maxConcurrentDownloads: 3,
  extractorEngine: 'auto',
  ytdlpGeneric: false,
  downloaderEngine: 'aria2_next',
  parallelSegments: 16,
  playerClient: 'default',
  ffmpegPath: '',
  aria2NextPath: '',
  aria2RpcPort: 16802,
  aria2Split: 16,
  aria2MaxConnectionPerServer: 16,
  aria2MinSplitSize: '1M',
  aria2FileAllocation: 'none',
  aria2MinTlsVersion: 'TLSv1.2',
  aria2MaxOverallDownloadLimit: '0',
  aria2MaxDownloadLimit: '0',
  aria2LowestSpeedLimit: '0',
  aria2RetryWait: 0,
  aria2StateSaveInterval: 10,
  btSeedRatio: 1,
  btSeedTime: 0,
  btMaxPeers: 100,
  btListenPort: 6881,
  btEnableDht: true,
  btEnablePex: true,
  btEnableLpd: true,
  btSequential: false,
  btTrackers: '',
  mediaFormat: '',
  embedThumbnail: true,
  embedSubtitles: false,
  embedChapters: true,
  embedMetadata: false,
  checkCertificate: true,
  cookiesFile: '',
  cookiesBrowser: 'off',
  timeoutSecs: 30,
  retries: 3,
}

/** One settings group: title/description rail on the left, controls on the right. */
function Section({
  title,
  description,
  children,
}: {
  title: string
  description?: string
  children: React.ReactNode
}) {
  return (
    <section className="settings-section">
      <div className="flex flex-col gap-1.5">
        <h2 className="text-body font-semibold leading-[1.35] tracking-[-0.01em] text-foreground">
          {title}
        </h2>
        {description && (
          <p className="max-w-[34ch] text-pretty text-meta leading-relaxed text-muted">
            {description}
          </p>
        )}
      </div>
      <div className="flex min-w-0 flex-col gap-5">
        {children}
      </div>
    </section>
  )
}

type Option = readonly [id: string, label: string]

/** Labelled select with an optional hint — every choice field uses this. */
function Choice({
  label,
  value,
  onChange,
  options,
  hint,
  width = 'w-full',
}: {
  label: string
  value: string
  onChange: (v: string) => void
  options: ReadonlyArray<Option>
  hint?: string
  width?: string
}) {
  return (
    <div className={`flex flex-col gap-1.5 ${width}`}>
      <Label>{label}</Label>
      <Select
        aria-label={label}
        variant="secondary"
        fullWidth
        value={value}
        onChange={(k) => onChange(String(k))}
      >
        <Select.Trigger>
          <Select.Value />
          <Select.Indicator />
        </Select.Trigger>
        <Select.Popover>
          <ListBox>
            {options.map(([id, text]) => (
              <ListBox.Item key={id} id={id} textValue={text}>
                {text}
                <ListBox.ItemIndicator />
              </ListBox.Item>
            ))}
          </ListBox>
        </Select.Popover>
      </Select>
      {hint && <Description>{hint}</Description>}
    </div>
  )
}

/** Labelled input with an optional hint — the only text/number control. */
function Field({
  label,
  value,
  onChange,
  hint,
  placeholder,
  mono,
  type,
  min,
  max,
  step,
  width = 'w-full',
}: {
  label: string
  value: string
  onChange: (v: string) => void
  hint?: string
  placeholder?: string
  mono?: boolean
  type?: 'text' | 'number'
  min?: number
  max?: number
  step?: number
  width?: string
}) {
  return (
    <TextField value={value} onChange={onChange} className={width}>
      <Label>{label}</Label>
      <Input
        variant="secondary"
        type={type}
        min={min}
        max={max}
        step={step}
        placeholder={placeholder}
        className={`w-full ${mono ? 'font-mono text-meta' : ''}`}
      />
      {hint && <Description>{hint}</Description>}
    </TextField>
  )
}

/** A switch row: label + hint on the left, control on the right. */
function ToggleRow({
  label,
  hint,
  selected,
  onChange,
}: {
  label: string
  hint: string
  selected: boolean
  onChange: (v: boolean) => void
}) {
  return (
    <Switch
      isSelected={selected}
      onChange={onChange}
      className="flex-row-reverse items-start justify-between gap-6 py-3"
    >
      <Switch.Control className="mt-0.5 shrink-0">
        <Switch.Thumb />
      </Switch.Control>
      <Switch.Content className="min-w-0 flex-1 flex-col items-start gap-0.5">
        <Label className="text-body font-medium text-foreground">{label}</Label>
        <Description className="text-pretty text-meta leading-relaxed text-muted">
          {hint}
        </Description>
      </Switch.Content>
    </Switch>
  )
}

/** Recessed list of switch rows, separated by hairlines. */
function ToggleList({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-col rounded-xl bg-surface px-4">
      {children}
    </div>
  )
}

/** Read-only status: the same LED language as a task row. */
function StatusLine({
  tone = 'success',
  children,
}: {
  tone?: 'success' | 'warning' | 'danger'
  children: React.ReactNode
}) {
  return (
    <span className="status-mark" data-tone={tone === 'danger' ? 'error' : 'neutral'}>
      <span
        className={`status-mark__dot ${
          tone === 'success'
            ? 'text-success'
            : tone === 'warning'
              ? 'text-warning'
              : 'text-danger'
        }`}
      />
      <span className="status-mark__label">{children}</span>
    </span>
  )
}

/** Destructive action behind a confirmation dialog — both confirmations on
 *  this page share one shape. */
function ConfirmDialog({
  trigger,
  heading,
  body,
  confirm,
  onConfirm,
}: {
  trigger: string
  heading: string
  body: string
  confirm: string
  onConfirm: () => void
}) {
  return (
    <AlertDialog>
      <Button variant="secondary" size="sm">
        {trigger}
      </Button>
      <AlertDialog.Backdrop>
        <AlertDialog.Container>
          <AlertDialog.Dialog className="sm:max-w-100">
            <AlertDialog.CloseTrigger />
            <AlertDialog.Header>
              <AlertDialog.Icon status="warning" />
              <AlertDialog.Heading>{heading}</AlertDialog.Heading>
            </AlertDialog.Header>
            <AlertDialog.Body>
              <p>{body}</p>
            </AlertDialog.Body>
            <AlertDialog.Footer>
              <Button slot="close" variant="secondary" size="sm">
                Cancel
              </Button>
              <Button slot="close" variant="danger" size="sm" onPress={onConfirm}>
                {confirm}
              </Button>
            </AlertDialog.Footer>
          </AlertDialog.Dialog>
        </AlertDialog.Container>
      </AlertDialog.Backdrop>
    </AlertDialog>
  )
}

export function SettingsPage({ onBack }: { onBack: () => void }) {
  const [tab, setTab] = useState<SettingsTab>('extraction')
  const [engine, setEngine] = useState<AppSettings | null>(null)
  const [status, setStatus] = useState<EngineStatus | null>(null)
  const [ytdlpInfo, setYtdlpInfo] = useState<YtDlpInfo | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedTick, setSavedTick] = useState(false)
  const [tokenCopied, setTokenCopied] = useState(false)

  useEffect(() => {
    getAppSettings()
      .then(setEngine)
      .catch((e) => { logError('settings load', e); setEngine(null) })
    engineStatus()
      .then(setStatus)
      .catch((e) => { logError('engine status', e); setStatus(null) })
    getYtDlpInfo()
      .then(setYtdlpInfo)
      .catch((e) => { logError('ytdlp info', e); setYtdlpInfo(null) })
  }, [])

  const setF = <K extends keyof AppSettings>(k: K, v: AppSettings[K]) =>
    setEngine((s) => (s ? { ...s, [k]: v } : s))

  const save = async () => {
    if (!engine || saving) return
    setSaving(true)
    try {
      await saveAppSettings(engine)
      const [st, yi] = await Promise.all([engineStatus(), getYtDlpInfo()])
      setStatus(st)
      setYtdlpInfo(yi)
      setSavedTick(true)
      setTimeout(() => setSavedTick(false), 1500)
    } catch (e) {
      logError('settings save', e)
      toast.danger('Settings not saved', { description: errMsg(e) })
    }
    setSaving(false)
  }

  // Cmd/Ctrl+S saves — the window only exists while settings is the page.
  const saveRef = useRef(save)
  saveRef.current = save
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
        e.preventDefault()
        void saveRef.current()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div className="page-enter flex h-full flex-col overflow-hidden bg-background">
      <header className="chrome flex h-13 shrink-0 items-center gap-4 px-6">
        <Button
          variant="ghost"
          size="sm"
          className="-ms-2 gap-1.5 text-muted"
          onPress={onBack}
        >
          <ArrowLeft width={13} height={13} />
          <span>Downloads</span>
        </Button>
        <h1 className="text-title font-semibold tracking-[-0.014em] text-foreground">
          Settings
        </h1>
        {status && (
          <StatusLine tone={status.running ? 'success' : 'danger'}>
            {status.running ? 'Engine ready' : 'Engine offline'}
          </StatusLine>
        )}

        <div className="ms-auto flex items-center gap-2">
          <ConfirmDialog
            trigger="Reset to defaults"
            heading="Reset all settings?"
            body="Every field returns to its default. Nothing is written until you press Save; leave this page to keep the current values."
            confirm="Reset defaults"
            onConfirm={() =>
              setEngine((prev) => ({
                ...DEFAULT_SETTINGS,
                apiToken: prev?.apiToken ?? '',
              }))
            }
          />
          <Button
            size="sm"
            variant="primary"
            className="gap-1.5"
            onPress={save}
            isDisabled={!engine || saving}
          >
            {savedTick && <Check width={13} height={13} />}
            <span>
              {savedTick ? 'Saved' : saving ? 'Saving…' : 'Save changes'}
            </span>
          </Button>
        </div>
      </header>

      <Tabs
        orientation="vertical"
        selectedKey={tab}
        onSelectionChange={(k) => setTab(String(k) as SettingsTab)}
        className="flex min-h-0 flex-1 flex-col gap-0 lg:flex-row"
      >
        <div className="chrome flex shrink-0 items-center gap-1 overflow-x-auto px-3 py-2 lg:hidden">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              aria-pressed={tab === t.id}
              onClick={() => setTab(t.id)}
              className={`press-tactile flex h-8 shrink-0 items-center gap-1.5 rounded-lg px-2.5 text-meta font-medium outline-none transition-colors focus-visible:ring-1 focus-visible:ring-focus ${
                tab === t.id
                  ? 'bg-(--state-selected-bg) text-foreground shadow-[inset_0_0_0_1px_oklch(1_0_0/0.08)]'
                  : 'text-muted hover:bg-(--state-hover-bg) hover:text-foreground'
              }`}
            >
              {t.icon}
              {t.label}
            </button>
          ))}
        </div>

        <aside className="hidden w-58 shrink-0 flex-col bg-background-secondary p-3 lg:flex">
          <Tabs.ListContainer className="rounded-none bg-transparent">
            <Tabs.List aria-label="Settings sections" className="w-full gap-0.5 p-0">
              {TABS.map((t) => (
                <Tabs.Tab
                  key={t.id}
                  id={t.id}
                  aria-label={t.label}
                  className="press-tactile h-8.5 w-full min-w-0 justify-start gap-2.5 rounded-lg px-2.5 text-left text-body font-medium text-muted transition-colors data-selected:text-foreground"
                >
                  <span className="shrink-0">{t.icon}</span>
                  <span className="truncate">{t.label}</span>
                  <Tabs.Indicator className="rounded-lg" />
                </Tabs.Tab>
              ))}
            </Tabs.List>
          </Tabs.ListContainer>

          <div className="mt-auto flex flex-col gap-1 px-2 pt-4 pb-1">
            <div className="flex items-center justify-between gap-2">
              <span className="overline">Runtime</span>
              <span
                className={`size-1.5 shrink-0 rounded-full ${
                  status?.running ? 'bg-success' : 'bg-danger'
                }`}
              />
            </div>
            <span className="truncate font-mono text-micro text-foreground-secondary">
              {status?.version?.split(' · ')[0] ?? 'Engine offline'}
            </span>
            {status?.version?.includes(' · ') && (
              <span className="truncate font-mono text-micro text-muted-subtle">
                {status.version.split(' · ')[1]}
              </span>
            )}
          </div>
        </aside>

        <ScrollShadow className="min-h-0 flex-1 overflow-y-auto">
          <div className="settings-content mx-auto w-full max-w-248 px-6 py-8 lg:px-10 lg:py-9">
            <Tabs.Panel id="extraction" className="ms-0 p-0 outline-none">
              <ExtractionPane engine={engine} setF={setF} ytdlpInfo={ytdlpInfo} />
            </Tabs.Panel>
            <Tabs.Panel id="downloads" className="ms-0 p-0 outline-none">
              <DownloadsPane engine={engine} setF={setF} />
            </Tabs.Panel>
            <Tabs.Panel id="torrents" className="ms-0 p-0 outline-none">
              <TorrentsPane engine={engine} setF={setF} />
            </Tabs.Panel>
            <Tabs.Panel id="media" className="ms-0 p-0 outline-none">
              <MediaPane engine={engine} setF={setF} />
            </Tabs.Panel>
            <Tabs.Panel id="output" className="ms-0 p-0 outline-none">
              <OutputPane engine={engine} setF={setF} />
            </Tabs.Panel>
            <Tabs.Panel id="extension" className="ms-0 p-0 outline-none">
              <ExtensionPane />
            </Tabs.Panel>
            <Tabs.Panel id="engine" className="ms-0 p-0 outline-none">
              <EnginePane
                engine={engine}
                status={status}
                ytdlpInfo={ytdlpInfo}
                setF={setF}
                setEngine={setEngine}
                tokenCopied={tokenCopied}
                setTokenCopied={setTokenCopied}
              />
            </Tabs.Panel>
          </div>
        </ScrollShadow>
      </Tabs>
    </div>
  )
}

function ExtractionPane({
  engine,
  setF,
  ytdlpInfo,
}: {
  engine: AppSettings | null
  setF: <K extends keyof AppSettings>(k: K, v: AppSettings[K]) => void
  ytdlpInfo: YtDlpInfo | null
}) {
  return (
    <div className="flex flex-col gap-12">
      <Section
        title="Extractor pipeline"
        description="Which engine handles a link, and how it falls back."
      >
        <div className="grid grid-cols-2 gap-4">
          <Choice
            label="Pipeline priority"
            value={engine?.extractorEngine ?? 'auto'}
            onChange={(v) =>
              setF('extractorEngine', v as AppSettings['extractorEngine'])
            }
            options={[
              ['auto', 'Auto (yt-dlp → Rust extractor)'],
              ['python_ytdlp', 'Python yt-dlp only'],
              ['custom', 'Rust extractor only'],
            ]}
          />
          <Choice
            label="YouTube player client"
            value={engine?.playerClient ?? 'default'}
            onChange={(v) => setF('playerClient', v as AppSettings['playerClient'])}
            options={[
              ['default', 'Default (tv_samsung, visionos, web)'],
              ['web', 'Web (desktop browser)'],
              ['web_safari', 'Web Safari'],
              ['web_embedded', 'Web Embedded (age-gate workaround)'],
              ['android', 'Android (direct streams, high speed)'],
              ['android_vr', 'Android VR'],
              ['ios', 'iOS (clean mobile streams)'],
              ['visionos', 'visionOS'],
              ['tv', 'TV'],
              ['tv_samsung', 'TV Samsung'],
              ['tv_simply', 'TV Simply'],
              ['mweb', 'Mobile Web'],
            ]}
          />
        </div>

        <ToggleList>
          <ToggleRow
            label="Generic page extractor"
            hint="Let yt-dlp handle pages with no dedicated extractor. Slower, but finds more media."
            selected={engine?.ytdlpGeneric ?? false}
            onChange={(v) => setF('ytdlpGeneric', v)}
          />
        </ToggleList>

        {ytdlpInfo && (
          <div className="flex flex-col gap-2.5 rounded-xl bg-surface p-4">
            <div className="flex items-center justify-between gap-3">
              <span className="text-meta font-medium text-foreground-secondary">
                Detected binaries
              </span>
              <StatusLine tone={ytdlpInfo.available ? 'success' : 'danger'}>
                {ytdlpInfo.available ? 'Ready' : 'Unavailable'}
              </StatusLine>
            </div>
            <dl className="grid grid-cols-[84px_minmax(0,1fr)] gap-x-3 gap-y-1.5 font-mono text-micro">
              {ytdlpInfo.ytdlp_path && (
                <>
                  <dt className="text-muted-subtle">yt-dlp</dt>
                  <dd className="truncate text-foreground-secondary" title={ytdlpInfo.ytdlp_path}>
                    {ytdlpInfo.ytdlp_path}
                  </dd>
                </>
              )}
              {ytdlpInfo.ffmpeg_path && (
                <>
                  <dt className="text-muted-subtle">ffmpeg</dt>
                  <dd className="truncate text-foreground-secondary" title={ytdlpInfo.ffmpeg_path}>
                    {ytdlpInfo.ffmpeg_path}
                  </dd>
                </>
              )}
              {ytdlpInfo.aria2_next_path && (
                <>
                  <dt className="text-muted-subtle">aria2-next</dt>
                  <dd
                    className="truncate text-foreground-secondary"
                    title={ytdlpInfo.aria2_next_path}
                  >
                    {ytdlpInfo.aria2_next_version
                      ? `v${ytdlpInfo.aria2_next_version} · `
                      : ''}
                    {ytdlpInfo.aria2_next_path}
                  </dd>
                </>
              )}
            </dl>
          </div>
        )}

        <Field
          label="ffmpeg path override"
          value={engine?.ffmpegPath ?? ''}
          onChange={(v) => setF('ffmpegPath', v)}
          placeholder="Auto-detected"
          mono
        />
      </Section>

      <Section
        title="Network & authentication"
        description="Cookies for private or age-gated pages, plus request timeouts."
      >
        <div className="grid grid-cols-2 gap-4">
          <Field
            label="Cookies file"
            value={engine?.cookiesFile ?? ''}
            onChange={(v) => setF('cookiesFile', v)}
            placeholder="/path/to/cookies.txt"
            hint="Netscape-format cookies file."
            mono
          />
          <Choice
            label="Cookies from browser"
            value={engine?.cookiesBrowser ?? 'off'}
            onChange={(v) => setF('cookiesBrowser', v)}
            options={[
              ['off', 'Off'],
              ['chrome', 'Chrome'],
              ['firefox', 'Firefox'],
              ['safari', 'Safari'],
              ['edge', 'Edge'],
              ['brave', 'Brave'],
              ['chromium', 'Chromium'],
              ['opera', 'Opera'],
              ['vivaldi', 'Vivaldi'],
              ['whale', 'Whale'],
            ]}
            hint="Read cookies directly from the browser's local store."
          />
        </div>
        <div className="grid grid-cols-2 gap-4">
          <Field
            label="Timeout (s)"
            type="number"
            min={1}
            value={(engine?.timeoutSecs ?? 30).toString()}
            onChange={(v) => {
              const n = numOpt(v)
              if (n !== undefined) setF('timeoutSecs', n)
            }}
          />
          <Field
            label="Retries"
            type="number"
            min={0}
            value={(engine?.retries ?? 3).toString()}
            onChange={(v) => {
              const n = numOpt(v)
              if (n !== undefined) setF('retries', n)
            }}
          />
        </div>
      </Section>
    </div>
  )
}

function EnginePane({
  engine,
  status,
  ytdlpInfo,
  setF,
  setEngine,
  tokenCopied,
  setTokenCopied,
}: {
  engine: AppSettings | null
  status: EngineStatus | null
  ytdlpInfo: YtDlpInfo | null
  setF: <K extends keyof AppSettings>(k: K, v: AppSettings[K]) => void
  setEngine: (s: AppSettings | null) => void
  tokenCopied: boolean
  setTokenCopied: (v: boolean) => void
}) {
  if (!engine) {
    return (
      <p className="py-6 text-center text-body text-muted">
        Settings unavailable. Reconnect to the engine and try again.
      </p>
    )
  }
  const regen = async () => {
    try {
      setEngine(await regenerateTokens())
      setTokenCopied(false)
    } catch (e) {
      logError('token rotate', e)
      toast.danger('Token rotation failed', { description: errMsg(e) })
    }
  }
  return (
    <div className="flex flex-col gap-12">
      <Section
        title="Engine & server"
        description="The local API the browser extension talks to."
      >
        <div className="flex items-center justify-between gap-3 rounded-xl bg-surface px-4 py-2.5">
          <span className="text-meta font-medium text-foreground-secondary">Backend status</span>
          {status && (
            <StatusLine tone={status.running ? 'success' : 'danger'}>
              {status.running ? `${status.version ?? 'yt-dlp'}` : 'offline'}
            </StatusLine>
          )}
        </div>
        {status?.error && <p className="text-meta text-(--state-error-fg)">{status.error}</p>}

        <Field
          label="Extension API port"
          type="number"
          min={1}
          max={65535}
          width="w-48"
          value={engine.apiPort.toString()}
          onChange={(v) => setF('apiPort', numOpt(v) ?? 16801)}
        />

        <div className="flex flex-col gap-1.5">
          <Label>Extension token</Label>
          <div className="flex items-center gap-2">
            <code className="flex h-8.5 min-w-0 flex-1 items-center truncate rounded-lg bg-surface px-3 font-mono text-meta text-foreground-secondary">
              {engine.apiToken}
            </code>
            <IconBtn
              size="md"
              variant="secondary"
              label={tokenCopied ? 'Copied' : 'Copy token'}
              onPress={() => {
                navigator.clipboard.writeText(engine.apiToken)
                setTokenCopied(true)
                setTimeout(() => setTokenCopied(false), 1400)
              }}
              className={tokenCopied ? 'text-success' : ''}
            >
              {tokenCopied ? <Check width={14} height={14} /> : <Copy width={14} height={14} />}
            </IconBtn>
            <ConfirmDialog
              trigger="Regenerate"
              heading="Regenerate tokens?"
              body="The extension paired with this app stops working until you paste the new token into its options page."
              confirm="Regenerate"
              onConfirm={regen}
            />
          </div>
          <Description>Paste into the DownloadAnything extension's options page.</Description>
        </div>
      </Section>

      <Section
        title="aria2-next daemon"
        description="The persistent RPC downloader. Ports and state checkpoints."
      >
        <div className="flex items-center justify-between gap-3 rounded-xl bg-surface px-4 py-2.5">
          <span className="text-meta font-medium text-foreground-secondary">Daemon status</span>
          {ytdlpInfo && (
            <StatusLine
              tone={ytdlpInfo.aria2_next_available ? 'success' : 'danger'}
            >
              {ytdlpInfo.aria2_next_running
                ? `RPC active · :${ytdlpInfo.aria2_next_rpc_port ?? engine.aria2RpcPort}`
                : ytdlpInfo.aria2_next_available
                  ? `Ready${ytdlpInfo.aria2_next_version ? ` · v${ytdlpInfo.aria2_next_version}` : ''}`
                  : 'Binary not found'}
            </StatusLine>
          )}
        </div>
        <Field
          label="aria2-next binary path"
          value={engine.aria2NextPath ?? ''}
          onChange={(v) => setF('aria2NextPath', v)}
          placeholder="/path/to/aria2-next"
          hint="Standalone aria2-next executable, distinct from legacy aria2c. Required for the aria2-next engine — there is no auto-detection."
          mono
        />
        <div className="grid grid-cols-2 gap-4">
          <Field
            label="RPC listen port"
            type="number"
            min={1024}
            max={65535}
            value={(engine.aria2RpcPort ?? 16802).toString()}
            onChange={(v) => setF('aria2RpcPort', numOpt(v) ?? 16802)}
          />
          <Field
            label="State checkpoint (s)"
            type="number"
            min={1}
            max={600}
            value={(engine.aria2StateSaveInterval ?? 10).toString()}
            onChange={(v) => setF('aria2StateSaveInterval', numOpt(v) ?? 10)}
          />
        </div>
      </Section>

      <Section
        title="Transport security"
        description="TLS verification for every request."
      >
        <ToggleList>
          <ToggleRow
            label="Check TLS certificate"
            hint="Verify peer certificate validity. Turning this off disables certificate checks."
            selected={engine.checkCertificate}
            onChange={(v) => setF('checkCertificate', v)}
          />
        </ToggleList>
        {(engine.downloaderEngine ?? 'aria2_next') === 'aria2_next' && (
          <Choice
            label="Minimum TLS version"
            width="w-60"
            value={engine.aria2MinTlsVersion ?? 'TLSv1.2'}
            onChange={(v) =>
              setF('aria2MinTlsVersion', v as AppSettings['aria2MinTlsVersion'])
            }
            options={[
              ['TLSv1.2', 'TLS 1.2 (recommended)'],
              ['TLSv1.3', 'TLS 1.3 (strict)'],
              ['TLSv1.1', 'TLS 1.1 (legacy)'],
            ]}
          />
        )}
      </Section>
    </div>
  )
}

function DownloadsPane({
  engine,
  setF,
}: {
  engine: AppSettings | null
  setF: <K extends keyof AppSettings>(k: K, v: AppSettings[K]) => void
}) {
  if (!engine) {
    return (
      <p className="py-6 text-center text-body text-muted">
        Settings unavailable. Reconnect to the engine and try again.
      </p>
    )
  }
  const isAria2 = (engine.downloaderEngine ?? 'aria2_next') === 'aria2_next'
  return (
    <div className="flex flex-col gap-12">
      <Section
        title="Downloader engine"
        description="What yt-dlp hands finished streams to."
      >
        <Choice
          label="External downloader"
          value={engine.downloaderEngine ?? 'aria2_next'}
          onChange={(v) =>
            setF('downloaderEngine', v as AppSettings['downloaderEngine'])
          }
          options={[
            ['aria2_next', 'aria2-next (WebSocket RPC)'],
            ['ytdlp', 'yt-dlp native downloader'],
          ]}
          hint="aria2-next handles HTTP(S) range splitting, HLS, DASH, and media collections."
        />
      </Section>

      <Section
        title="Concurrency"
        description="How many tasks and fragment workers run at once."
      >
        <div className="grid grid-cols-2 gap-4">
          <Field
            label="Concurrent downloads"
            type="number"
            min={1}
            max={16}
            value={engine.maxConcurrentDownloads.toString()}
            onChange={(v) => setF('maxConcurrentDownloads', numOpt(v) ?? 3)}
            hint="Extra downloads wait in the queue."
          />
          <Field
            label="Parallel segments"
            type="number"
            min={1}
            max={64}
            value={engine.parallelSegments.toString()}
            onChange={(v) => setF('parallelSegments', numOpt(v) ?? 16)}
            hint="Fragment workers per segmented stream."
          />
        </div>
      </Section>

      {isAria2 && (
        <Section
          title="Connections & segmentation"
          description="How aria2-next splits a file across connections."
        >
          <div className="grid grid-cols-2 gap-4">
            <Field
              label="Connections per download"
              type="number"
              min={1}
              max={256}
              value={(engine.aria2Split ?? 16).toString()}
              onChange={(v) => setF('aria2Split', numOpt(v) ?? 16)}
              hint="Range connections per HTTP(S) file."
            />
            <Field
              label="Max connections per host"
              type="number"
              min={1}
              max={256}
              value={(engine.aria2MaxConnectionPerServer ?? 16).toString()}
              onChange={(v) =>
                setF('aria2MaxConnectionPerServer', numOpt(v) ?? 16)
              }
              hint="Parallel connections to one origin."
            />
            <Field
              label="Minimum split size"
              value={engine.aria2MinSplitSize ?? '1M'}
              onChange={(v) => setF('aria2MinSplitSize', v || '1M')}
              placeholder="1M"
              hint="Smallest range worth a new connection."
              mono
            />
            <Choice
              label="Disk file allocation"
              value={engine.aria2FileAllocation ?? 'none'}
              onChange={(v) =>
                setF('aria2FileAllocation', v as AppSettings['aria2FileAllocation'])
              }
              options={[
                ['none', 'None (sparse, instant start)'],
                ['falloc', 'falloc (fast reservation)'],
                ['prealloc', 'preallocate space'],
                ['trunc', 'trunc (truncate to length)'],
              ]}
            />
          </div>
        </Section>
      )}

      {isAria2 && (
        <Section
          title="Speed & retries"
          description="Bandwidth caps and the retry backoff floor."
        >
          <div className="grid grid-cols-2 gap-4">
            <Field
              label="Overall speed limit"
              value={engine.aria2MaxOverallDownloadLimit ?? '0'}
              onChange={(v) => setF('aria2MaxOverallDownloadLimit', v || '0')}
              placeholder="0 (unlimited)"
              hint="Bytes per second, e.g. 20M."
              mono
            />
            <Field
              label="Per-task speed limit"
              value={engine.aria2MaxDownloadLimit ?? '0'}
              onChange={(v) => setF('aria2MaxDownloadLimit', v || '0')}
              placeholder="0 (unlimited)"
              hint="Bytes per second, e.g. 5M."
              mono
            />
            <Field
              label="Lowest speed limit"
              value={engine.aria2LowestSpeedLimit ?? '0'}
              onChange={(v) => setF('aria2LowestSpeedLimit', v || '0')}
              placeholder="0 (off)"
              hint="Abort below this rate, e.g. 64K."
              mono
            />
            <Field
              label="Retry wait (s)"
              type="number"
              min={0}
              max={600}
              value={(engine.aria2RetryWait ?? 0).toString()}
              onChange={(v) => setF('aria2RetryWait', numOpt(v) ?? 0)}
              hint="0 uses automatic backoff."
            />
          </div>
        </Section>
      )}
    </div>
  )
}

function TorrentsPane({
  engine,
  setF,
}: {
  engine: AppSettings | null
  setF: <K extends keyof AppSettings>(k: K, v: AppSettings[K]) => void
}) {
  if (!engine) {
    return (
      <p className="py-6 text-center text-body text-muted">
        Settings unavailable. Reconnect to the engine and try again.
      </p>
    )
  }
  if ((engine.downloaderEngine ?? 'aria2_next') !== 'aria2_next') {
    return (
      <p className="py-6 text-center text-body text-muted">
        Torrent settings apply to the aria2-next downloader. Switch the
        downloader engine under Downloads to configure them.
      </p>
    )
  }
  return (
    <div className="flex flex-col gap-12">
      <Section
        title="Seeding"
        description="How long finished torrents keep uploading."
      >
        <div className="grid grid-cols-2 gap-4">
          <Field
            label="Seed ratio"
            type="number"
            min={0}
            step={0.1}
            value={(engine.btSeedRatio ?? 1).toString()}
            onChange={(v) => setF('btSeedRatio', numOpt(v) ?? 1)}
            hint="Stop after uploading this multiple. 0 = no limit."
          />
          <Field
            label="Seed time (min)"
            type="number"
            min={0}
            value={(engine.btSeedTime ?? 0).toString()}
            onChange={(v) => setF('btSeedTime', numOpt(v) ?? 0)}
            hint="0 = no time limit."
          />
        </div>
      </Section>

      <Section
        title="Peers"
        description="Connection limits, the listen port, and peer discovery."
      >
        <div className="grid grid-cols-2 gap-4">
          <Field
            label="Peers per torrent"
            type="number"
            min={0}
            max={65535}
            value={(engine.btMaxPeers ?? 100).toString()}
            onChange={(v) => setF('btMaxPeers', numOpt(v) ?? 100)}
            hint="0 = unlimited."
          />
          <Field
            label="Peer listen port"
            type="number"
            min={1}
            max={65535}
            value={(engine.btListenPort ?? 6881).toString()}
            onChange={(v) => setF('btListenPort', numOpt(v) ?? 6881)}
            hint="TCP and uTP port."
          />
        </div>
        <Field
          label="Extra trackers"
          value={engine.btTrackers ?? ''}
          onChange={(v) => setF('btTrackers', v)}
          placeholder="udp://tracker.example:6969,https://tracker.example/announce"
          hint="Comma-separated tracker URIs added to every torrent."
          mono
        />
        <ToggleList>
          <ToggleRow
            label="DHT"
            hint="Find peers through the distributed hash table."
            selected={engine.btEnableDht}
            onChange={(v) => setF('btEnableDht', v)}
          />
          <ToggleRow
            label="Peer exchange"
            hint="Discover peers through connected peers."
            selected={engine.btEnablePex}
            onChange={(v) => setF('btEnablePex', v)}
          />
          <ToggleRow
            label="Local peer discovery"
            hint="Find peers on the local network."
            selected={engine.btEnableLpd}
            onChange={(v) => setF('btEnableLpd', v)}
          />
          <ToggleRow
            label="Sequential download"
            hint="Fetch pieces in order so media can play while it downloads."
            selected={engine.btSequential}
            onChange={(v) => setF('btSequential', v)}
          />
        </ToggleList>
      </Section>
    </div>
  )
}

function MediaPane({
  engine,
  setF,
}: {
  engine: AppSettings | null
  setF: <K extends keyof AppSettings>(k: K, v: AppSettings[K]) => void
}) {
  if (!engine) {
    return (
      <p className="py-6 text-center text-body text-muted">
        Settings unavailable. Reconnect to the engine and try again.
      </p>
    )
  }
  return (
    <div className="flex flex-col gap-12">
      <Section
        title="Media container"
        description="Container and metadata written into finished files."
      >
        <Choice
          label="Output format"
          value={engine.mediaFormat === 'mkv' ? 'mkv' : 'mp4'}
          onChange={(v) => setF('mediaFormat', v === 'mkv' ? 'mkv' : 'mp4')}
          options={[
            ['mp4', 'MP4 (broadest compatibility)'],
            ['mkv', 'MKV (all codecs, multi-track)'],
          ]}
        />
        <ToggleList>
          <ToggleRow
            label="Embed thumbnail"
            hint="Save cover art inside the finished file."
            selected={engine.embedThumbnail}
            onChange={(v) => setF('embedThumbnail', v)}
          />
          <ToggleRow
            label="Embed subtitles"
            hint="Mux subtitle and caption streams into the container."
            selected={engine.embedSubtitles}
            onChange={(v) => setF('embedSubtitles', v)}
          />
          <ToggleRow
            label="Embed metadata"
            hint="Write title, uploader, date, and source tags."
            selected={engine.embedMetadata}
            onChange={(v) => setF('embedMetadata', v)}
          />
          <ToggleRow
            label="Embed chapters"
            hint="Add chapter markers when the page provides them."
            selected={engine.embedChapters}
            onChange={(v) => setF('embedChapters', v)}
          />
        </ToggleList>
      </Section>
    </div>
  )
}

function OutputPane({
  engine,
  setF,
}: {
  engine: AppSettings | null
  setF: <K extends keyof AppSettings>(k: K, v: AppSettings[K]) => void
}) {
  if (!engine) {
    return (
      <p className="py-6 text-center text-body text-muted">
        Settings unavailable. Reconnect to the engine and try again.
      </p>
    )
  }
  return (
    <div className="flex flex-col gap-12">
      <CategoriesSection engine={engine} setF={setF} />
    </div>
  )
}

function ExtensionPane() {
  const [setup, setSetup] = useState<ExtensionSetup | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [installing, setInstalling] = useState<string | null>(null)
  const [installed, setInstalled] = useState<{
    browser: BrowserInfo
    sessionLoad: boolean
  } | null>(null)
  const [pendingRestart, setPendingRestart] = useState<BrowserInfo | null>(null)
  const [restarting, setRestarting] = useState(false)
  const [pathCopied, setPathCopied] = useState(false)

  useEffect(() => {
    if (!inTauri()) {
      setError('Open the desktop app to install the browser extension.')
      return
    }
    extensionSetup()
      .then(setSetup)
      .catch((e) => {
        logError('extension setup', e)
        setError(errMsg(e))
      })
  }, [])

  const install = async (browser: BrowserInfo) => {
    setInstalling(browser.id)
    if (setup) {
      navigator.clipboard
        .writeText(setup.path)
        .then(() => {
          setPathCopied(true)
          setTimeout(() => setPathCopied(false), 1400)
        })
        .catch(() => {})
    }
    try {
      const outcome = await installExtension(browser.id)
      if (outcome.needsRestart) {
        setPendingRestart(browser)
        setInstalled(null)
      } else {
        setPendingRestart(null)
        setInstalled({ browser, sessionLoad: outcome.sessionLoad })
      }
    } catch (e) {
      logError('extension install', e)
      toast.danger('Could not open the install page', { description: errMsg(e) })
    }
    setInstalling(null)
  }

  const restart = async () => {
    if (!pendingRestart) return
    setRestarting(true)
    try {
      const outcome = await restartBrowserWithExtension(pendingRestart.id)
      setInstalled({ browser: pendingRestart, sessionLoad: outcome.sessionLoad })
      setPendingRestart(null)
    } catch (e) {
      logError('extension restart', e)
      toast.danger(`Could not restart ${pendingRestart.name}`, { description: errMsg(e) })
    }
    setRestarting(false)
  }

  const copyPath = () => {
    if (!setup) return
    navigator.clipboard.writeText(setup.path)
    setPathCopied(true)
    setTimeout(() => setPathCopied(false), 1400)
  }

  return (
    <div className="flex flex-col gap-12">
      <Section
        title="Browser extension"
        description="Send pages and media from your browser to DownloadAnything."
      >
        <div className="flex flex-col gap-3 rounded-xl bg-surface p-4">
          <div className="flex items-center justify-between gap-3">
            <span className="text-meta font-medium text-foreground-secondary">
              Bundled extension
            </span>
            {setup ? (
              <StatusLine>Ready</StatusLine>
            ) : error ? (
              <StatusLine tone="danger">Unavailable</StatusLine>
            ) : (
              <StatusLine tone="warning">Loading</StatusLine>
            )}
          </div>
          {setup && (
            <div className="flex items-center gap-2">
              <code className="flex h-8.5 min-w-0 flex-1 items-center truncate rounded-lg bg-surface-secondary px-3 font-mono text-micro text-foreground-secondary">
                {setup.path}
              </code>
              <IconBtn
                size="md"
                variant="secondary"
                label={pathCopied ? 'Copied' : 'Copy extension path'}
                onPress={copyPath}
                className={pathCopied ? 'text-success' : ''}
              >
                {pathCopied ? <Check width={14} height={14} /> : <Copy width={14} height={14} />}
              </IconBtn>
              <IconBtn
                size="md"
                variant="secondary"
                label="Show extension folder"
                onPress={() => {
                  revealExtension().catch((e) => {
                    logError('extension reveal', e)
                    toast.danger('Could not open the folder', { description: errMsg(e) })
                  })
                }}
              >
                <FolderOpen width={14} height={14} />
              </IconBtn>
            </div>
          )}
          {error && <p className="text-meta leading-relaxed text-muted">{error}</p>}
        </div>

        <div className="flex flex-col gap-1.5">
          <Label>Installed browsers</Label>
          {setup?.browsers.length ? (
            <div className="flex flex-col rounded-xl bg-surface px-4">
              {setup.browsers.map((browser) => (
                <div key={browser.id} className="flex items-center justify-between gap-3 py-3">
                  <div className="flex min-w-0 items-center gap-2.5">
                    <Globe width={14} height={14} className="shrink-0 text-muted-subtle" />
                    <span className="truncate text-meta font-medium text-foreground">
                      {browser.name}
                    </span>
                    {browser.running && (
                      <span className="rounded-sm bg-warning-soft px-1.5 py-0.5 font-mono text-micro text-warning-soft-foreground">
                        Running
                      </span>
                    )}
                  </div>
                  <Button
                    size="sm"
                    variant="secondary"
                    isDisabled={!!installing}
                    onPress={() => install(browser)}
                  >
                    {installing === browser.id ? 'Opening…' : 'Install'}
                  </Button>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-meta text-muted">
              {setup
                ? 'No Chromium-based browsers found.'
                : error
                  ? 'Browser install is only available in the desktop app.'
                  : 'Looking for browsers…'}
            </p>
          )}
        </div>

        {pendingRestart && (
          <div className="rounded-xl bg-surface px-4 py-3.5">
            <p className="text-meta font-semibold text-foreground">
              {pendingRestart.name} is running
            </p>
            <p className="mt-1 max-w-[60ch] text-pretty text-micro leading-relaxed text-muted">
              Restart it to load the extension. Open tabs are usually restored, but unsaved
              work may be lost.
            </p>
            <div className="mt-3 flex items-center gap-2">
              <Button
                size="sm"
                variant="primary"
                isDisabled={restarting}
                onPress={restart}
              >
                {restarting ? 'Restarting…' : `Restart ${pendingRestart.name}`}
              </Button>
              <Button
                size="sm"
                variant="secondary"
                isDisabled={restarting}
                onPress={() => setPendingRestart(null)}
              >
                Cancel
              </Button>
            </div>
          </div>
        )}

        {installed && (
          <div className="rounded-xl bg-surface px-4 py-3.5">
            <p className="text-meta font-semibold text-foreground">
              {installed.sessionLoad
                ? `Opened in ${installed.browser.name}`
                : `Finish in ${installed.browser.name}`}
            </p>
            <ol className="mt-2 list-decimal space-y-1 ps-4 text-meta leading-relaxed text-muted">
              <li>Turn on Developer mode.</li>
              <li>Click Load unpacked.</li>
              <li>Select the extension folder shown above (its path is on your clipboard).</li>
            </ol>
            <p className="mt-2 max-w-[64ch] text-pretty text-micro leading-relaxed text-muted">
              {installed.sessionLoad
                ? `The app passed the extension to ${installed.browser.name} for this session. If the browser was already running, it ignored it — quit and try again. Complete the steps above to keep the extension after restarting.`
                : 'Chrome blocks silent installs of unpacked extensions, so this final selection has to happen in the browser. The folder is already open in your file manager.'}
            </p>
          </div>
        )}

        <Description>
          Works with Chrome, Chrome Beta, Chrome Canary, Chromium, Brave, Edge, Vivaldi, and
          Opera. After loading, open the extension's options and paste the token from Engine
          &amp; RPC.
        </Description>
      </Section>
    </div>
  )
}

function CategoriesSection({
  engine,
  setF,
}: {
  engine: AppSettings
  setF: <K extends keyof AppSettings>(k: K, v: AppSettings[K]) => void
}) {
  const cats = engine.categories ?? []
  const setCats = (next: Category[]) => setF('categories', next)
  const update = (i: number, patch: Partial<Category>) =>
    setCats(cats.map((c, j) => (j === i ? { ...c, ...patch } : c)))
  return (
    <Section
      title="Output categories"
      description="Named output folders offered in the download dialog. Downloads with no category go to your Downloads folder."
    >
      {cats.length > 0 && (
        <div className="flex flex-col overflow-hidden rounded-xl bg-surface">
          <div className="grid grid-cols-[180px_minmax(0,1fr)_28px] items-center gap-3 px-3.5 py-2">
            <span className="overline">Name</span>
            <span className="overline">Folder path</span>
            <span aria-hidden="true" />
          </div>
          {cats.map((c, i) => (
            <div
              key={i}
              className="grid grid-cols-[180px_minmax(0,1fr)_28px] items-center gap-3 px-3.5 py-2 last:border-0"
            >
              <TextField
                aria-label="Category name"
                value={c.name}
                onChange={(v) => update(i, { name: v })}
                className="min-w-0"
              >
                <Input variant="secondary" className="w-full" placeholder="Category name" />
              </TextField>
              <TextField
                aria-label="Category folder"
                value={c.path}
                onChange={(v) => update(i, { path: v })}
                className="min-w-0"
              >
                <Input variant="secondary" className="w-full font-mono text-meta" placeholder="/path/to/folder" />
              </TextField>
              <IconBtn
                label="Remove category"
                danger
                onPress={() => setCats(cats.filter((_, j) => j !== i))}
              >
                <TrashBin width={14} height={14} />
              </IconBtn>
            </div>
          ))}
        </div>
      )}
      {cats.length === 0 && (
        <p className="text-meta text-muted">
          No categories yet. Downloads land in your Downloads folder.
        </p>
      )}
      <button
        type="button"
        onClick={() => setCats([...cats, { name: '', path: '' }])}
        className="press-tactile flex h-9 w-full items-center gap-1.5 rounded-lg px-3 text-meta font-medium text-muted outline-none transition-colors hover:bg-(--state-hover-bg) hover:text-foreground focus-visible:ring-1 focus-visible:ring-focus"
      >
        <Plus width={12} height={12} />
        Add category
      </button>
    </Section>
  )
}
