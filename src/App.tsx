import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { 
  Download, Cpu, Terminal, ShieldAlert,
  FileDown, Settings, Trash2, FolderOpen,
  Play, Pause, FileX, Info, Clipboard, Zap, Puzzle
} from 'lucide-react'
import {
  useReactTable,
  getCoreRowModel,
  flexRender
} from '@tanstack/react-table'
import type { ColumnDef } from '@tanstack/react-table'
import './App.css'
import Modal from './components/Modal'

interface Job {
  job_id: string
  url: string
  status: string
  progress: number
  downloaded_bytes: number
  total_bytes: number
  audio_downloaded_bytes: number
  audio_total_bytes: number
  combined_downloaded_bytes: number
  combined_total_bytes: number
  stream_phase: string  // 'single' | 'video' | 'audio'
  speed: number
  eta: number
  format_id?: string
  output_dir?: string
  error?: string
  title?: string
  duration?: number
  thumbnail?: string
  uploader?: string
  file_path?: string
  fragment_index?: number
  fragment_count?: number
}



interface Category {
  name: string
  path: string
}

interface FormatOption {
  label: string
  height: number
  fps: number
  codecFamily: string
  ext: string
  tbr?: number
  estSizeBytes?: number
  formatId: string
  isCombined: boolean
  hdr: boolean
  isStream: boolean
  streamType?: string
}

function formatBytes(bytes: number): string {
  if (bytes <= 0) return '\u2014'
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(3)} GB`
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(3)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(3)} KB`
  return `${bytes} B`
}

function formatDuration(sec?: number): string {
  if (!sec || sec <= 0) return '00:00'
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = Math.round(sec % 60)
  
  const pad = (n: number) => n.toString().padStart(2, '0')
  
  if (h > 0) {
    return `${h}:${pad(m)}:${pad(s)}`
  }
  return `${m}:${pad(s)}`
}// Generate a unique client tab ID for this browser tab session to prevent WebSocket collisions
const getSessionTabId = () => {
  const key = 'downloadanything_tab_id'
  try {
    let id = sessionStorage.getItem(key)
    if (!id) {
      id = Math.floor(100000 + Math.random() * 900000).toString()
      sessionStorage.setItem(key, id)
    }
    return parseInt(id, 10)
  } catch (e) {
    return Math.floor(100000 + Math.random() * 900000)
  }
}
const CLIENT_VERSION = import.meta.env.APP_VERSION || '0.0.0'
const DEFAULT_SERVER_URL = 'ws://127.0.0.1:8765/ws'
const TAB_ID = getSessionTabId()

function App() {
  const [activeTab, setActiveTab] = useState<'downloads' | 'settings'>('downloads')
  const [activeSettingsSection, setActiveSettingsSection] = useState<'general' | 'categories' | 'engines' | 'integrations'>('general')
  const [filterTab, setFilterTab] = useState<'all' | 'downloading' | 'completed' | 'paused' | 'failed'>('all')

  // Front-end Connection Configs
  const [serverUrl, setServerUrl] = useState(DEFAULT_SERVER_URL)

  // Back-end Dynamic Configs
  const [mergeFormat, setMergeFormat] = useState('mkv')
  const [embedThumbnail, setEmbedThumbnail] = useState(true)
  const [embedSubs, setEmbedSubs] = useState(false)
  const [cookiesFromBrowser, setCookiesFromBrowser] = useState('none')
  const [useAria2, setUseAria2] = useState(true)
  const [aria2MaxConnections, setAria2MaxConnections] = useState(16)
  const [aria2ConcurrentDownloads, setAria2ConcurrentDownloads] = useState(5)
  const [aria2Split, setAria2Split] = useState(16)
  const [aria2MinSplitSize, setAria2MinSplitSize] = useState('1M')
  const [aria2Preallocate, setAria2Preallocate] = useState(true)
  const [aria2CheckCertificate, setAria2CheckCertificate] = useState(true)
  const [aria2AlwaysResume, setAria2AlwaysResume] = useState(true)

  // Extra yt-dlp settings
  const [concurrentFragmentDownloads, setConcurrentFragmentDownloads] = useState(4)
  const [downloadRetries, setDownloadRetries] = useState(10)
  const [fragmentRetries, setFragmentRetries] = useState(10)
  const [rateLimit, setRateLimit] = useState('')
  const [subtitlesLangs, setSubtitlesLangs] = useState('all')
  const [ffmpegLocation, setFfmpegLocation] = useState('')

  // Categories list
  const [categories, setCategories] = useState<Category[]>([])
  const [newCatName, setNewCatName] = useState('')
  const [newCatPath, setNewCatPath] = useState('')
  // Status and System Info
  const [isConnected, setIsConnected] = useState(false)
  const [serverInfo, setServerInfo] = useState({
    ytDlpVersion: 'Unknown',
    ffmpegAvailable: false,
    poTokenPluginLoaded: false
  })

  // Extension Installer Modal State
  const [isExtModalOpen, setIsExtModalOpen] = useState(false)
  const [browsersList, setBrowsersList] = useState<any[]>([])
  const [isLoadingBrowsers, setIsLoadingBrowsers] = useState(false)

  const handleInstallExtensionClick = async () => {
    setIsLoadingBrowsers(true)
    setIsExtModalOpen(true)
    
    try {
      if (typeof window !== 'undefined' && (window as any).__TAURI__) {
        const list = await (window as any).__TAURI__.core.invoke('detect_installed_browsers')
        setBrowsersList(list)
      } else {
        // Fallback for non-tauri testing
        setBrowsersList([
          { name: "Google Chrome", key: "chrome", installed: true, extensions_url: "chrome://extensions" },
          { name: "Microsoft Edge", key: "edge", installed: false, extensions_url: "edge://extensions" },
          { name: "Brave Browser", key: "brave", installed: true, extensions_url: "brave://extensions" }
        ])
      }
    } catch (err) {
      console.error("Failed to detect browsers:", err)
    } finally {
      setIsLoadingBrowsers(false)
    }
  }

  const handleInstallForBrowser = async (browser: any) => {
    try {
      if (typeof window !== 'undefined' && (window as any).__TAURI__) {
        await (window as any).__TAURI__.core.invoke('install_extension_for_browser', { browserKey: browser.key })
      }
      setIsExtModalOpen(false)
      alert(`DownloadAnything extension loaded into ${browser.name} successfully!`)
    } catch (err) {
      alert(`Installation failed: ${err}`)
    }
  }

  // Active downloads and completed history
  const [jobs, setJobs] = useState<{ [key: string]: Job }>({})
  const [contextMenu, setContextMenu] = useState<{ x: number, y: number, jobId: string } | null>(null)
  const [propertiesJobId, setPropertiesJobId] = useState<string | null>(null)

  // Direct Downloader States
  const [inputUrl, setInputUrl] = useState('')
  const myProbingJobIdRef = useRef('')
  const amProbingUrlRef = useRef('')

  const setMyProbingJobId = (val: string) => {
    myProbingJobIdRef.current = val
  }

  const setAmProbingUrl = (val: string) => {
    amProbingUrlRef.current = val
  }

  const [isProbing, setIsProbing] = useState(false)
  const [showFormatDrawer, setShowFormatDrawer] = useState(false)
  const [probedInfo, setProbedInfo] = useState<{
    jobId: string
    title: string
    duration?: number
    thumbnail?: string
    uploader?: string
    formats: FormatOption[]
  } | null>(null)
  const [selectedFormatId, setSelectedFormatId] = useState('')
  const [selectedCategoryPath, _setSelectedCategoryPath] = useState('')
  const selectedCategoryPathRef = useRef('')
  const setSelectedCategoryPath = (val: string) => {
    selectedCategoryPathRef.current = val
    _setSelectedCategoryPath(val)
  }
  const [drawerCustomPath, setDrawerCustomPath] = useState('')

  // Duplicate alerts & confirm states
  const [duplicateJobAlert, setDuplicateJobAlert] = useState<{
    jobId: string
    url: string
    title: string
    status: string
  } | null>(null)

  const [duplicateFileAlert, setDuplicateFileAlert] = useState<{
    filename: string
    path: string
    jobId: string
  } | null>(null)

  const [deleteFileConfirm, setDeleteFileConfirm] = useState<string | null>(null)
  const [genericAlert, setGenericAlert] = useState<{
    title: string
    message: string
    suggestion?: string
  } | null>(null)

  const selectedFormatIdRef = useRef('')
  const selectedOutputDirRef = useRef('')


  const wsRef = useRef<WebSocket | null>(null)
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const heartbeatIntervalRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Suppresses re-save when settings are loaded from the server broadcast
  const isLoadingSettings = useRef(false)

  useEffect(() => {
    const handleGlobalClick = () => {
      setContextMenu(null)
    }
    window.addEventListener('click', handleGlobalClick)
    return () => window.removeEventListener('click', handleGlobalClick)
  }, [])
  
  // --- REST CALLS & HELPERS ---
  const updateLocalJob = useCallback((jobId: string, updates: Partial<Job>) => {
    setJobs(prev => {
      const existing = prev[jobId] || {
        job_id: jobId, url: '', status: 'queued', progress: 0,
        downloaded_bytes: 0, total_bytes: 0,
        audio_downloaded_bytes: 0, audio_total_bytes: 0,
        combined_downloaded_bytes: 0, combined_total_bytes: 0,
        stream_phase: 'single',
        speed: 0, eta: 0
      }

      const merged = { ...existing, ...updates }
      return { ...prev, [jobId]: merged }
    })
  }, [])

  const fetchJobsList = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'get_jobs' }))
    }
  }, [])

  const fetchCategories = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'get_categories' }))
    }
  }, [])

  const saveCategoriesList = useCallback((list: Category[]) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'save_categories', categories: list }))
    }
  }, [])

  const fetchDirectory = useCallback((path: string = '', forField?: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ 
        type: 'browse_directory', 
        path: path || null,
        forField: forField || null
      }))
    }
  }, [])

  const handleRevealFile = useCallback((jobId: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'reveal_file', jobId }))
    }
  }, [])

  const fetchSettings = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'get_settings' }))
    }
  }, [])

  const saveSettingsOnBackend = useCallback((updated: {
    mergeFormat: string
    embedThumbnail: boolean
    embedSubs: boolean
    cookiesFromBrowser: string
    useAria2: boolean
    aria2MaxConnections: number
    aria2ConcurrentDownloads: number
    aria2Split: number
    aria2MinSplitSize: string
    aria2Preallocate: boolean
    aria2CheckCertificate: boolean
    aria2AlwaysResume: boolean
    concurrentFragmentDownloads: number
    downloadRetries: number
    fragmentRetries: number
    rateLimit: string
    subtitlesLangs: string
    ffmpegLocation: string
  }) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'save_settings',
        settings: {
          mergeFormat: updated.mergeFormat,
          embedThumbnail: updated.embedThumbnail,
          embedSubs: updated.embedSubs,
          cookiesFromBrowser: updated.cookiesFromBrowser === 'none' ? null : updated.cookiesFromBrowser,
          useAria2: updated.useAria2,
          aria2MaxConnections: updated.aria2MaxConnections,
          aria2ConcurrentDownloads: updated.aria2ConcurrentDownloads,
          aria2Split: updated.aria2Split,
          aria2MinSplitSize: updated.aria2MinSplitSize,
          aria2Preallocate: updated.aria2Preallocate,
          aria2CheckCertificate: updated.aria2CheckCertificate,
          aria2AlwaysResume: updated.aria2AlwaysResume,
          concurrentFragmentDownloads: updated.concurrentFragmentDownloads,
          downloadRetries: updated.downloadRetries,
          fragmentRetries: updated.fragmentRetries,
          rateLimit: updated.rateLimit === '' ? null : updated.rateLimit,
          subtitlesLangs: updated.subtitlesLangs,
          ffmpegLocation: updated.ffmpegLocation === '' ? null : updated.ffmpegLocation
        }
      }))
    }
  }, [])

  // --- LOCALSTORAGE & API SYNC ---
  useEffect(() => {
    const savedServerUrl = localStorage.getItem('serverUrl')
    if (savedServerUrl) setServerUrl(savedServerUrl)
  }, [])

  useEffect(() => {
    localStorage.setItem('serverUrl', serverUrl)
  }, [serverUrl])

  /**
   * Build a settings snapshot from current state and send to backend.
   * Called directly from each onChange handler — no useEffect needed.
   * Reads latest values via a ref to avoid stale closures.
   */
  const currentSettingsRef = useRef({
    mergeFormat, embedThumbnail, embedSubs, cookiesFromBrowser,
    useAria2, aria2MaxConnections, aria2ConcurrentDownloads, aria2Split,
    aria2MinSplitSize, aria2Preallocate, aria2CheckCertificate, aria2AlwaysResume,
    concurrentFragmentDownloads, downloadRetries, fragmentRetries,
    rateLimit, subtitlesLangs, ffmpegLocation
  })
  // Keep the ref in sync with every render
  useEffect(() => {
    currentSettingsRef.current = {
      mergeFormat, embedThumbnail, embedSubs, cookiesFromBrowser,
      useAria2, aria2MaxConnections, aria2ConcurrentDownloads, aria2Split,
      aria2MinSplitSize, aria2Preallocate, aria2CheckCertificate, aria2AlwaysResume,
      concurrentFragmentDownloads, downloadRetries, fragmentRetries,
      rateLimit, subtitlesLangs, ffmpegLocation
    }
  })

  const pushSettings = useCallback((overrides: Partial<typeof currentSettingsRef.current> = {}) => {
    if (isLoadingSettings.current) return
    const s = { ...currentSettingsRef.current, ...overrides }
    saveSettingsOnBackend(s)
  }, [saveSettingsOnBackend])

  // --- WEBSOCKET CLIENT ---
  const serverUrlRef = useRef(serverUrl)
  useEffect(() => {
    serverUrlRef.current = serverUrl
  }, [serverUrl])

  const connectWebSocket = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.onclose = null
      wsRef.current.onerror = null
      wsRef.current.close()
    }

    console.log(`Connecting WebSocket to ${serverUrlRef.current}...`)
    try {
      const ws = new WebSocket(serverUrlRef.current)
      wsRef.current = ws

      ws.onopen = () => {
        console.log('WebSocket connected')
        setIsConnected(true)
        
        ws.send(JSON.stringify({
          type: 'hello',
          clientVersion: CLIENT_VERSION,
          tabId: TAB_ID
        }))

        if (heartbeatIntervalRef.current) clearInterval(heartbeatIntervalRef.current)
        heartbeatIntervalRef.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'ping', ts: Date.now() }))
          }
        }, 20 * 1000)

        fetchJobsList()
        fetchCategories()
        fetchSettings()
      }

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data)
          
          switch (msg.type) {
            case 'hello':
              setServerInfo({
                ytDlpVersion: msg.ytDlpVersion,
                ffmpegAvailable: msg.ffmpegAvailable,
                poTokenPluginLoaded: msg.poTokenPluginLoaded || false
              })
              break

            case 'probe_started':
              if (msg.url === amProbingUrlRef.current) {
                setMyProbingJobId(msg.jobId)
                setIsProbing(true)
              }
              break

            case 'probe_result':
              if (msg.jobId === myProbingJobIdRef.current) {
                setIsProbing(false)
                setProbedInfo({
                  jobId: msg.jobId,
                  title: msg.title,
                  duration: msg.duration,
                  thumbnail: msg.thumbnail,
                  uploader: msg.uploader,
                  formats: msg.formats || []
                })
                if (msg.formats && msg.formats.length > 0) {
                  setSelectedFormatId(msg.formats[0].formatId)
                }
                setShowFormatDrawer(true)
                setMyProbingJobId('')
                setAmProbingUrl('')
              }
              break

            case 'probe_failed':
              if (msg.jobId === myProbingJobIdRef.current) {
                setIsProbing(false)
                setGenericAlert({
                  title: 'Analysis Failed',
                  message: msg.error,
                  suggestion: msg.suggestion || 'Verify the link and try again.'
                })
                setMyProbingJobId('')
                setAmProbingUrl('')
              }
              break
              
            case 'download_queued':
              updateLocalJob(msg.jobId, {
                job_id: msg.jobId,
                url: msg.url || '',
                status: 'downloading',
                speed: 0,
                eta: 0,
                output_dir: msg.outputPath,
                title: msg.title || undefined,
                duration: msg.duration || undefined,
                thumbnail: msg.thumbnail || undefined,
                uploader: msg.uploader || undefined
              })
              break
              
            case 'download_progress': {
              const combinedDl = msg.combinedDownloadedBytes ?? (msg.downloadedBytes || 0) + (msg.audioDownloadedBytes || 0)
              const combinedTotal = msg.combinedTotalBytes ?? (msg.totalBytes || msg.totalBytesEstimate || 0) + (msg.audioTotalBytes || 0)
              const pct = combinedTotal > 0 ? (combinedDl / combinedTotal) * 100 : 0
              updateLocalJob(msg.jobId, {
                status: msg.status,
                progress: pct,
                downloaded_bytes: msg.downloadedBytes || 0,
                total_bytes: msg.totalBytes || msg.totalBytesEstimate || 0,
                audio_downloaded_bytes: msg.audioDownloadedBytes || 0,
                audio_total_bytes: msg.audioTotalBytes || 0,
                combined_downloaded_bytes: combinedDl,
                combined_total_bytes: combinedTotal,
                stream_phase: msg.streamPhase || 'single',
                speed: msg.speed || 0,
                eta: msg.eta || 0,
                fragment_index: msg.fragmentIndex,
                fragment_count: msg.fragmentCount,
                file_path: msg.filePath || undefined
              })
              break
            }
              
            case 'download_completed':
              updateLocalJob(msg.jobId, {
                status: 'completed',
                file_path: msg.filePath,
                progress: 100
              })
              break
              
            case 'download_failed':
              updateLocalJob(msg.jobId, {
                status: 'failed',
                error: msg.error
              })
              break
              
            case 'download_canceled':
              updateLocalJob(msg.jobId, {
                status: 'canceled'
              })
              break

            case 'jobs_list': {
              const jobsMap: { [key: string]: Job } = {}
              msg.jobs.forEach((job: Job) => {
                jobsMap[job.job_id] = job
              })
              setJobs(jobsMap)
              break
            }
              
            case 'categories_list': {
              setCategories(msg.categories)
              if (msg.categories && msg.categories.length > 0 && !selectedCategoryPathRef.current) {
                const def = msg.categories.find((c: Category) => c.name === 'Default') || msg.categories[0]
                setSelectedCategoryPath(def.path)
              }
              break
            }
              
            case 'directory_selected': {
              if (msg.forField === 'new_category') {
                setNewCatPath(msg.path)
              } else if (msg.forField === 'drawer') {
                setDrawerCustomPath(msg.path)
              }
              break
            }

            case 'duplicate_job_alert': {
              setIsProbing(false)
              setMyProbingJobId('')
              setAmProbingUrl('')
              setDuplicateJobAlert({
                jobId: msg.jobId,
                url: msg.url,
                title: msg.title,
                status: msg.status
              })
              break
            }

            case 'file_exists_result': {
              if (msg.exists) {
                setDuplicateFileAlert({
                  filename: msg.filename,
                  path: msg.path,
                  jobId: msg.jobId
                })
              } else {
                proceedWithDownload(msg.jobId, selectedFormatIdRef.current, msg.path, 'replace')
              }
              break
            }
              
            case 'settings_data': {
              // Suppress re-save while we apply server values to local state
              isLoadingSettings.current = true
              
              setMergeFormat(msg.settings.mergeFormat)
              setEmbedThumbnail(msg.settings.embedThumbnail)
              setEmbedSubs(msg.settings.embedSubs)
              setCookiesFromBrowser(msg.settings.cookiesFromBrowser || 'none')
              setUseAria2(msg.settings.useAria2 ?? true)
              setAria2MaxConnections(msg.settings.aria2MaxConnections ?? 16)
              setAria2ConcurrentDownloads(msg.settings.aria2ConcurrentDownloads ?? 5)
              setAria2Split(msg.settings.aria2Split ?? 16)
              setAria2MinSplitSize(msg.settings.aria2MinSplitSize || '1M')
              setAria2Preallocate(msg.settings.aria2Preallocate ?? true)
              setAria2CheckCertificate(msg.settings.aria2CheckCertificate ?? true)
              setAria2AlwaysResume(msg.settings.aria2AlwaysResume ?? true)
              
              // Custom yt-dlp settings
              setConcurrentFragmentDownloads(msg.settings.concurrentFragmentDownloads ?? 4)
              setDownloadRetries(msg.settings.downloadRetries ?? 10)
              setFragmentRetries(msg.settings.fragmentRetries ?? 10)
              setRateLimit(msg.settings.rateLimit || '')
              setSubtitlesLangs(msg.settings.subtitlesLangs || 'all')
              setFfmpegLocation(msg.settings.ffmpegLocation || '')
              
              // Release the guard after React has committed the state
              setTimeout(() => { isLoadingSettings.current = false }, 0)
              break
            }

            case 'browse_failed': {
              console.error('Directory browse failed:', msg.error)
              break
            }
          }
        } catch (err) {
          console.error('Failed to parse WS message:', err)
        }
      }

      ws.onclose = () => {
        console.log('WebSocket closed')
        setIsConnected(false)
        if (heartbeatIntervalRef.current) {
          clearInterval(heartbeatIntervalRef.current)
        }
        
        if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current)
        reconnectTimeoutRef.current = setTimeout(connectWebSocket, 3000)
      }

      ws.onerror = (err) => {
        console.error('WebSocket error:', err)
      }
    } catch (e) {
      console.error('Failed to connect WebSocket:', e)
      setIsConnected(false)
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current)
      reconnectTimeoutRef.current = setTimeout(connectWebSocket, 3000)
    }
  }, [])

  useEffect(() => {
    connectWebSocket()

    return () => {
      if (wsRef.current) wsRef.current.close()
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current)
      if (heartbeatIntervalRef.current) clearInterval(heartbeatIntervalRef.current)
    }
  }, [connectWebSocket])

  // --- ACTIONS ---
  const handleProbeUrl = () => {
    const trimmed = inputUrl.trim()
    if (!trimmed) return

    // Check duplicate locally
    const existingJob = Object.values(jobs).find(j => j.url === trimmed)
    if (existingJob) {
      setDuplicateJobAlert({
        jobId: existingJob.job_id,
        url: existingJob.url,
        title: existingJob.title || 'Unknown Title',
        status: existingJob.status
      })
      return
    }

    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'probe',
        url: trimmed
      }))
      setAmProbingUrl(trimmed)
      setIsProbing(true)
    }
  }

  const handlePasteUrl = async () => {
    try {
      const text = await navigator.clipboard.readText()
      if (text) setInputUrl(text)
    } catch (err) {
      console.error('Clipboard paste blocked or unsupported:', err)
    }
  }

  const handleChooseFormat = () => {
    if (!probedInfo || !selectedFormatId) return
    const finalDest = drawerCustomPath || selectedCategoryPath
    const chosenFormatObj = probedInfo.formats.find(f => f.formatId === selectedFormatId)
    const estimatedExt = chosenFormatObj?.ext || 'mp4'
    let cleanTitle = probedInfo.title.replace(/[\x00-\x1F\x7F]/g, '')
    const mappings: Record<string, string> = {
      '/': '／',
      '\\': '＼',
      ':': '：',
      '*': '＊',
      '?': '？',
      '"': '＂',
      '<': '＜',
      '>': '＞',
      '|': '｜'
    }
    for (const [char, replacement] of Object.entries(mappings)) {
      cleanTitle = cleanTitle.replaceAll(char, replacement)
    }
    cleanTitle = cleanTitle.replace(/\s+/g, ' ')
    cleanTitle = cleanTitle.trim()
    while (cleanTitle.endsWith('.')) {
      cleanTitle = cleanTitle.slice(0, -1).trim()
    }
    if (!cleanTitle) cleanTitle = 'video'
    const filename = `${cleanTitle}.${estimatedExt}`

    selectedFormatIdRef.current = selectedFormatId
    selectedOutputDirRef.current = finalDest

    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'check_file_exists',
        path: finalDest,
        filename: filename,
        jobId: probedInfo.jobId
      }))
    }
  }

  const proceedWithDownload = (jobId: string, formatId: string, outputDir: string, conflictResolution: 'replace' | 'rename') => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'choose',
        jobId,
        formatId,
        outputDir,
        conflictResolution
      }))
      setShowFormatDrawer(false)
      setProbedInfo(null)
      setDuplicateFileAlert(null)
      setInputUrl('')
    }
  }

  const handleAddCategory = () => {
    if (!newCatName.trim() || !newCatPath.trim()) return
    
    if (categories.find(c => c.name.toLowerCase() === newCatName.trim().toLowerCase())) {
      setGenericAlert({
        title: 'Category Validation',
        message: 'A category with this name already exists.'
      })
      return
    }

    const updated = [...categories, { name: newCatName.trim(), path: newCatPath.trim() }]
    saveCategoriesList(updated)
    setNewCatName('')
  }

  const handleDeleteCategory = (name: string) => {
    if (name === 'Default') return
    const updated = categories.filter(c => c.name !== name)
    saveCategoriesList(updated)
  }

  const counts = useMemo(() => {
    const list = Object.values(jobs).filter(j => j.status !== 'probing')
    return {
      all: list.length,
      downloading: list.filter(j => ['downloading', 'queued', 'postprocessing'].includes(j.status)).length,
      completed: list.filter(j => j.status === 'completed').length,
      paused: list.filter(j => j.status === 'paused').length,
      failed: list.filter(j => ['failed', 'canceled'].includes(j.status)).length,
    }
  }, [jobs])

  const displayJobs = useMemo(() => {
    const list = Object.values(jobs).filter(j => j.status !== 'probing')
    if (filterTab === 'all') return list
    if (filterTab === 'downloading') {
      return list.filter(j => ['downloading', 'queued', 'postprocessing'].includes(j.status))
    }
    if (filterTab === 'completed') {
      return list.filter(j => j.status === 'completed')
    }
    if (filterTab === 'paused') {
      return list.filter(j => j.status === 'paused')
    }
    if (filterTab === 'failed') {
      return list.filter(j => ['failed', 'canceled'].includes(j.status))
    }
    return list
  }, [jobs, filterTab])

  const hasCompletedJobs = useMemo(() => {
    return Object.values(jobs).some(j => ['completed', 'failed', 'canceled'].includes(j.status))
  }, [jobs])

  const handleClearCompleted = () => {
    Object.values(jobs).forEach(j => {
      if (['completed', 'failed', 'canceled'].includes(j.status)) {
        handleRemoveJob(j.job_id)
      }
    })
  }

  const handlePauseJob = (jobId: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'pause',
        jobId: jobId
      }))
    }
  }

  const handleResumeJob = (jobId: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'resume',
        jobId: jobId
      }))
    }
  }

  const handleRemoveJob = (jobId: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'remove_job',
        jobId: jobId
      }))
    }
  }

  const handleDeleteFile = (jobId: string) => {
    setDeleteFileConfirm(jobId)
  }

  const columns = useMemo<ColumnDef<Job>[]>(() => [
    {
      header: 'Filename',
      accessorKey: 'title',
      cell: ({ row }) => {
        const job = row.original
        const isActive = ['downloading', 'queued', 'postprocessing'].includes(job.status)
        const isPostProcessing = job.status === 'postprocessing'

        // Extract filename
        let displayName = 'Unknown filename'
        if (job.file_path) {
          const parts = job.file_path.split(/[/\\]/)
          const name = parts[parts.length - 1]
          if (name) displayName = name
        } else if (job.title && !/^https?:\/\//i.test(job.title)) {
          displayName = job.title
        } else {
          try {
            const urlObj = new URL(job.url)
            const base = urlObj.pathname.split('/').pop()
            if (base && base.trim() !== '') {
              displayName = decodeURIComponent(base)
            } else {
              displayName = job.url
            }
          } catch {
            const base = job.url.split('/').pop()
            displayName = base || job.url
          }
        }

        // Sub-info indicators
        let subInfo: React.ReactNode = null
        if (isPostProcessing) {
          subInfo = <span style={{ color: 'var(--status-postprocessing)' }}>⚙ Post-processing...</span>
        } else if (isActive && job.status === 'downloading') {
          const phase = job.stream_phase
          const phaseLabel = phase === 'video' ? '▶ Video' : phase === 'audio' ? '♪ Audio' : '⬇ Download'
          const phaseColor = phase === 'video' ? 'var(--status-downloading)' : phase === 'audio' ? 'var(--status-postprocessing)' : 'var(--text-secondary)'
          const etaStr = job.eta > 0 
            ? ` · ETA ${job.eta < 60 ? `${Math.round(job.eta)}s` : `${Math.floor(job.eta / 60)}m${Math.round(job.eta % 60)}s`}` 
            : ''
          subInfo = (
            <span>
              <span style={{ color: phaseColor, fontWeight: 700 }}>{phaseLabel}</span>
              <span style={{ color: 'var(--text-secondary)' }}>{etaStr}</span>
            </span>
          )
        } else if (job.status === 'queued') {
          subInfo = <span style={{ color: 'var(--text-muted)' }}>Waiting in queue...</span>
        } else if (job.status === 'paused') {
          subInfo = <span style={{ color: 'var(--status-paused)' }}>⏸ Paused</span>
        } else if (job.status === 'failed') {
          subInfo = <span style={{ color: 'var(--status-failed)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '320px', display: 'inline-block' }} title={job.error}>{job.error || 'Download failed'}</span>
        } else if (job.status === 'completed') {
          subInfo = <span style={{ color: 'var(--status-completed)' }}>✓ Completed</span>
        }

        return (
          <div className="job-title-cell">
            <div className="job-thumbnail-box">
              {job.thumbnail ? (
                <img src={job.thumbnail} alt="preview" />
              ) : (
                <FileDown size={18} className="job-thumbnail-fallback" />
              )}
            </div>
            <div className="job-title-info">
              <div className="job-title-name" title={displayName}>
                {displayName}
              </div>
              <div className="job-subinfo-row">
                {subInfo}
              </div>
            </div>
          </div>
        )
      }
    },
    {
      header: 'Status',
      accessorKey: 'status',
      cell: ({ getValue }) => {
        const val = getValue() as string
        return (
          <span className={`status-badge ${val}`}>
            {val}
          </span>
        )
      }
    },
    {
      header: 'Progress',
      accessorKey: 'progress',
      cell: ({ row }) => {
        const job = row.original
        const isPostProcessing = job.status === 'postprocessing'
        const progressPct = job.progress
        const progressDisplay = progressPct.toFixed(2)
        
        let barFillClass = ''
        if (job.status === 'downloading') barFillClass = 'downloading'
        else if (job.status === 'postprocessing') barFillClass = 'postprocessing'
        else if (job.status === 'completed') barFillClass = 'completed'
        else if (job.status === 'failed') barFillClass = 'failed'
        else if (job.status === 'paused') barFillClass = 'paused'

        return (
          <div className="job-progress-cell">
            <div className="progress-bar-bg">
              <div 
                className={`progress-bar-fill ${barFillClass} ${isPostProcessing ? 'indeterminate' : ''}`}
                style={{ width: isPostProcessing ? '100%' : `${progressPct}%` }}
              ></div>
            </div>
            <div className="progress-stats-row tabular-nums">
              <span className="progress-speed">
                {isPostProcessing ? (
                  'Post-processing...'
                ) : job.speed > 0 ? (
                  `${(job.speed / 1024 / 1024).toFixed(1)} MB/s`
                ) : (
                  '0.0 MB/s'
                )}
              </span>
              <span className="progress-percent">
                {progressDisplay}%{job.status === 'downloading' && job.fragment_index ? ` (${job.fragment_index}/${job.fragment_count || '?'})` : ''}
              </span>
            </div>
          </div>
        )
      }
    },
    {
      header: 'Size',
      id: 'size',
      cell: ({ row }) => {
        const job = row.original
        // Use server-computed combined bytes directly
        const combinedDl = job.combined_downloaded_bytes ?? (job.downloaded_bytes + job.audio_downloaded_bytes)
        const combinedTotal = job.combined_total_bytes ?? (job.total_bytes + job.audio_total_bytes)

        const hasTotal = combinedTotal > 0
        const dlStr = formatBytes(combinedDl)
        const totalStr = hasTotal ? formatBytes(combinedTotal) : '?'

        if (job.status === 'completed') {
          return (
            <span className="job-size-cell tabular-nums" style={{ color: 'var(--status-completed)', fontWeight: 700 }}>
              {formatBytes(combinedTotal || combinedDl)}
            </span>
          )
        }

        if (combinedDl === 0 && !hasTotal) {
          return <span className="job-size-cell job-size-total">—</span>
        }

        return (
          <div className="job-size-cell tabular-nums">
            <span>{dlStr}</span>
            <span className="job-size-total"> / {totalStr}</span>
          </div>
        )
      }
    }
  ], [])

  const table = useReactTable({
    data: displayJobs,
    columns,
    getCoreRowModel: getCoreRowModel(),
    getRowId: (row) => row.job_id,
  })

  return (
    <div className="dashboard-container">
      
      {/* SIDEBAR */}
      <aside className="sidebar">
        <div className="sidebar-top">
          {/* LOGO */}
          <div className="logo-section">
            <div className="logo-icon-grid">
              <div className="logo-dot active"></div>
              <div className="logo-dot"></div>
              <div className="logo-dot"></div>
              <div className="logo-dot active"></div>
            </div>
            <h1>DownloadAnything</h1>
          </div>

          {/* NAVIGATION TABS */}
          <nav className="sidebar-section">
            <h2 className="sidebar-title">Downloads</h2>
            <button 
              className={`tab-btn ${activeTab === 'downloads' ? 'active' : ''}`} 
              onClick={() => setActiveTab('downloads')}
            >
              <FileDown size={16} />
              <span>Dashboard</span>
            </button>
            
            <h2 className="sidebar-title" style={{ marginTop: '16px' }}>Configuration</h2>
            <button 
              className={`tab-btn ${activeTab === 'settings' ? 'active' : ''}`} 
              onClick={() => {
                setActiveTab('settings')
                fetchSettings()
                fetchCategories()
              }}
            >
              <Settings size={16} />
              <span>Settings</span>
            </button>
          </nav>
        </div>

        {/* SYSTEM STATUS FOOTER */}
        <div className="sidebar-footer">
          <div className="server-status-pill">
            <div className="status-indicator-row-minimal">
              <span className={`pulse-dot ${isConnected ? 'online' : 'offline'}`}></span>
              <span>{isConnected ? 'Server Connected' : 'Server Offline'}</span>
            </div>
            {isConnected && (
              <div className="system-specs-mini">
                <span title={`yt-dlp version: ${serverInfo.ytDlpVersion}`}>yt-dlp v{serverInfo.ytDlpVersion.substring(0, 8)}</span>
                <span>FFmpeg: {serverInfo.ffmpegAvailable ? 'OK' : 'ERR'}</span>
              </div>
            )}
          </div>
        </div>
      </aside>

      {/* MAIN CONTENT AREA */}
      <main className="main-area">

        {/* DOWNLOADS TAB */}
        {activeTab === 'downloads' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }} className="animate-fade-in-up">
            
            {/* DYNAMIC MAGIC INPUT BAR */}
            <section className="magic-input-card">
              {isProbing && (
                <div className="scanner-overlay">
                  <div className="scanner-line"></div>
                  <div className="scanner-loader">
                    <Zap size={28} className="animate-spin" style={{ color: '#ffffff' }} />
                    <span className="scanner-text">PROBING REMOTE TARGET...<span className="tui-cursor"></span></span>
                  </div>
                </div>
              )}
              
              <div className="magic-input-title-row">
                <h3>Direct Downloader</h3>
                <span>Paste any media link (YouTube, Twitter, Vimeo, etc.) to analyze and select formats</span>
              </div>

              <div className="magic-input-wrapper">
                <GlobeIcon className="magic-input-icon" />
                <input 
                  type="text" 
                  className="magic-input" 
                  placeholder="https://www.youtube.com/watch?v=..."
                  value={inputUrl}
                  onChange={(e) => setInputUrl(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') handleProbeUrl()
                  }}
                  disabled={isProbing}
                />
                <div className="magic-btn-group">
                  <button 
                    className="action-btn-secondary" 
                    onClick={handlePasteUrl} 
                    title="Paste from Clipboard"
                    disabled={isProbing}
                  >
                    <Clipboard size={14} />
                  </button>
                  <button 
                    className="action-btn" 
                    onClick={handleProbeUrl}
                    disabled={!inputUrl.trim() || isProbing}
                  >
                    <span>Analyze Link</span>
                    <Zap size={14} />
                  </button>
                </div>
              </div>
            </section>

            {/* ACTIVE DOWNLOADS QUEUE */}
            <section className="downloads-panel">
              <div className="queue-filter-toolbar">
                <div className="filter-tabs">
                  <button className={`filter-tab ${filterTab === 'all' ? 'active' : ''}`} onClick={() => setFilterTab('all')}>
                    All <span className="filter-count tabular-nums">{counts.all}</span>
                  </button>
                  <button className={`filter-tab ${filterTab === 'downloading' ? 'active' : ''}`} onClick={() => setFilterTab('downloading')}>
                    Active <span className="filter-count tabular-nums">{counts.downloading}</span>
                  </button>
                  <button className={`filter-tab ${filterTab === 'completed' ? 'active' : ''}`} onClick={() => setFilterTab('completed')}>
                    Completed <span className="filter-count tabular-nums">{counts.completed}</span>
                  </button>
                  <button className={`filter-tab ${filterTab === 'paused' ? 'active' : ''}`} onClick={() => setFilterTab('paused')}>
                    Paused <span className="filter-count tabular-nums">{counts.paused}</span>
                  </button>
                  <button className={`filter-tab ${filterTab === 'failed' ? 'active' : ''}`} onClick={() => setFilterTab('failed')}>
                    Failed <span className="filter-count tabular-nums">{counts.failed}</span>
                  </button>
                </div>
                
                {hasCompletedJobs && (
                  <button className="action-btn-secondary clear-completed-btn" onClick={handleClearCompleted}>
                    <span>Clear Completed</span>
                  </button>
                )}
              </div>
              
              <div className="job-table-wrapper">
                <table className="job-table">
                  <thead>
                    {table.getHeaderGroups().map((headerGroup) => (
                      <tr key={headerGroup.id}>
                        {headerGroup.headers.map((header) => (
                          <th key={header.id} style={{ 
                            width: header.id === 'title' ? '40%' : 
                                   header.id === 'status' ? '12%' : 
                                   header.id === 'progress' ? '23%' : 
                                   header.id === 'size' ? '13%' : 
                                   '12%' 
                          }}>
                            {header.isPlaceholder
                              ? null
                              : flexRender(
                                  header.column.columnDef.header,
                                  header.getContext()
                                )}
                          </th>
                        ))}
                      </tr>
                    ))}
                  </thead>
                  <tbody>
                    {table.getRowModel().rows.map((row) => {
                      const job = row.original
                      return (
                        <tr 
                          key={row.id} 
                          className="animate-fade-in-up"
                          style={{ cursor: 'context-menu' }}
                          onContextMenu={(e) => {
                            e.preventDefault()
                            setContextMenu({
                              x: e.clientX,
                              y: e.clientY,
                              jobId: job.job_id
                            })
                          }}
                        >
                          {row.getVisibleCells().map((cell) => (
                            <td key={cell.id}>
                              {flexRender(
                                cell.column.columnDef.cell,
                                cell.getContext()
                              )}
                            </td>
                          ))}
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
                
                {displayJobs.length === 0 && (
                  <div className="empty-queue-placeholder">
                    <FileDown size={40} />
                    <p>No downloads in the pipeline.<br/>Paste a link above or trigger downloads via the browser extension.</p>
                  </div>
                )}
              </div>
            </section>
          </div>
        )}

        {/* UNIFIED SETTINGS HUB */}
        {activeTab === 'settings' && (
          <div className="unified-settings-container animate-fade-in-up">
            {/* Settings Sub-navigation Sidebar */}
            <aside className="settings-sub-sidebar">
              <button 
                className={`settings-sub-btn ${activeSettingsSection === 'general' ? 'active' : ''}`}
                onClick={() => setActiveSettingsSection('general')}
              >
                <Settings size={14} />
                <span>General Preferences</span>
              </button>
              <button 
                className={`settings-sub-btn ${activeSettingsSection === 'categories' ? 'active' : ''}`}
                onClick={() => setActiveSettingsSection('categories')}
              >
                <FolderOpen size={14} />
                <span>Preset Paths</span>
              </button>
              <button 
                className={`settings-sub-btn ${activeSettingsSection === 'engines' ? 'active' : ''}`}
                onClick={() => setActiveSettingsSection('engines')}
              >
                <Cpu size={14} />
                <span>Downloader Engines</span>
              </button>
              <button 
                className={`settings-sub-btn ${activeSettingsSection === 'integrations' ? 'active' : ''}`}
                onClick={() => setActiveSettingsSection('integrations')}
              >
                <Puzzle size={14} />
                <span>Browser Extensions</span>
              </button>
            </aside>

            {/* Settings Content Pane */}
            <div className="settings-content-pane">
              {activeSettingsSection === 'general' && (
                <div className="general-preferences-layout">
                  <section className="settings-card">
                    <div className="section-header-row">
                      <h2 className="section-title-large">
                        <Settings size={18} />
                        <span>Output Preferences</span>
                      </h2>
                    </div>

                    <div className="settings-group">
                      <div className="form-field">
                        <label htmlFor="mergeFormat">Merged Output Format</label>
                        <select 
                          id="mergeFormat" 
                          className="form-select"
                          value={mergeFormat}
                          onChange={(e) => { setMergeFormat(e.target.value); pushSettings({ mergeFormat: e.target.value }) }}
                        >
                          <option value="mkv">MKV (Broad Codec Support)</option>
                          <option value="mp4">MP4 (Merge MP4 container)</option>
                        </select>
                      </div>

                      <div className="form-field">
                        <label htmlFor="cookiesFromBrowser">Native Cookies Browser Source</label>
                        <select 
                          id="cookiesFromBrowser" 
                          className="form-select"
                          value={cookiesFromBrowser}
                          onChange={(e) => { setCookiesFromBrowser(e.target.value); pushSettings({ cookiesFromBrowser: e.target.value }) }}
                        >
                          <option value="none">None (Bypassed)</option>
                          <option value="chrome">Chrome</option>
                          <option value="firefox">Firefox</option>
                          <option value="safari">Safari</option>
                          <option value="edge">Edge</option>
                          <option value="brave">Brave</option>
                          <option value="opera">Opera</option>
                          <option value="vivaldi">Vivaldi</option>
                        </select>
                      </div>

                      <div className="checkboxes-vertical-group">
                        <label className="checkbox-row custom-toggle-row">
                          <input 
                            type="checkbox" 
                            checked={embedThumbnail}
                            onChange={(e) => { setEmbedThumbnail(e.target.checked); pushSettings({ embedThumbnail: e.target.checked }) }}
                          />
                          <span>Embed Album Art / Thumbnail</span>
                        </label>

                        <label className="checkbox-row custom-toggle-row">
                          <input 
                            type="checkbox" 
                            checked={embedSubs}
                            onChange={(e) => { setEmbedSubs(e.target.checked); pushSettings({ embedSubs: e.target.checked }) }}
                          />
                          <span>Extract & Embed Subtitles</span>
                        </label>
                      </div>

                      {embedSubs && (
                        <div className="form-field subtitle-langs-field animate-fade-in-up">
                          <label htmlFor="subtitlesLangs">Embed Subtitles Languages (comma separated, e.g. en,es or all)</label>
                          <input 
                            id="subtitlesLangs"
                            type="text" 
                            className="form-input" 
                            value={subtitlesLangs} 
                            onChange={(e) => setSubtitlesLangs(e.target.value)}
                            onBlur={(e) => pushSettings({ subtitlesLangs: e.target.value })}
                            placeholder="all"
                          />
                        </div>
                      )}
                    </div>
                  </section>
                </div>
              )}

              {activeSettingsSection === 'categories' && (
                <div className="preset-paths-layout">
                  <div className="settings-card form-section">
                    <div className="section-header-row">
                      <h2 className="section-title-large">
                        <FolderOpen size={18} />
                        <span>Create Preset Path</span>
                      </h2>
                    </div>
                    <div className="add-cat-form-fields">
                      <div className="form-field">
                        <label htmlFor="catNameInput">Category Name</label>
                        <input 
                          id="catNameInput"
                          type="text" 
                          placeholder="e.g. Movies, Music, Lectures" 
                          className="form-input" 
                          value={newCatName}
                          onChange={(e) => setNewCatName(e.target.value)}
                        />
                      </div>

                      <div className="form-field">
                        <label>Selected Destination Path</label>
                        <div style={{ display: 'flex', gap: '8px' }}>
                          <input 
                            type="text" 
                            className="form-input" 
                            value={newCatPath}
                            onChange={(e) => setNewCatPath(e.target.value)}
                            placeholder="Select a folder path..."
                            style={{ flex: 1 }}
                          />
                          <button 
                            className="action-btn-secondary" 
                            style={{ padding: '0 12px' }}
                            onClick={() => {
                              fetchDirectory(newCatPath, 'new_category')
                            }}
                          >
                            <FolderOpen size={14} />
                          </button>
                        </div>
                      </div>

                      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: '8px' }}>
                        <button 
                          className="action-btn"
                          onClick={handleAddCategory}
                          disabled={!newCatName.trim() || !newCatPath.trim()}
                        >
                          <span>Save Category</span>
                        </button>
                      </div>
                    </div>
                  </div>

                  <div className="settings-card list-section">
                    <div className="section-header-row">
                      <h2 className="section-title-large">
                        <FolderOpen size={18} />
                        <span>Preset Directories</span>
                      </h2>
                    </div>
                    <p className="section-subtitle">
                      Define default download paths. These presets populate the dashboard drawer and browser extension dropdown.
                    </p>

                    <div className="categories-list">
                      {categories.map((cat, idx) => (
                        <div key={idx} className="category-item-row animate-fade-in-up">
                          <div className="category-details">
                            <span className="category-name">{cat.name}</span>
                            <span className="category-path" title={cat.path}>{cat.path}</span>
                          </div>
                          {cat.name !== 'Default' ? (
                            <button 
                              className="delete-cat-btn"
                              onClick={() => handleDeleteCategory(cat.name)}
                              title="Delete Category"
                            >
                              <Trash2 size={13} />
                            </button>
                          ) : (
                            <span style={{ fontSize: '9px', color: 'var(--text-muted)', fontWeight: 800, paddingRight: '8px', letterSpacing: '0.05em' }}>SYSTEM</span>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              )}

              {activeSettingsSection === 'engines' && (
                <div className="engines-layout">
                  <section className="settings-card">
                    <div className="section-header-row">
                      <h2 className="section-title-large">
                        <GlobeIcon width={18} height={18} />
                        <span>yt-dlp Core Tuning</span>
                      </h2>
                    </div>

                    <div className="settings-group">
                      <div className="sliders-grid">
                        <div className="form-field">
                          <div className="label-row">
                            <label htmlFor="concurrentFragmentDownloads">Concurrent Fragment Downloads</label>
                            <span className="slider-value tabular-nums">{concurrentFragmentDownloads}</span>
                          </div>
                          <input 
                            id="concurrentFragmentDownloads"
                            type="range" 
                            min="1" 
                            max="16" 
                            className="form-input-slider" 
                            value={concurrentFragmentDownloads} 
                            onChange={(e) => { const v = parseInt(e.target.value); setConcurrentFragmentDownloads(v); pushSettings({ concurrentFragmentDownloads: v }) }}
                          />
                        </div>
                        <div className="form-field">
                          <div className="label-row">
                            <label htmlFor="downloadRetries">Download Retries</label>
                            <span className="slider-value tabular-nums">{downloadRetries}</span>
                          </div>
                          <input 
                            id="downloadRetries"
                            type="range" 
                            min="0" 
                            max="30" 
                            className="form-input-slider" 
                            value={downloadRetries} 
                            onChange={(e) => { const v = parseInt(e.target.value); setDownloadRetries(v); pushSettings({ downloadRetries: v }) }}
                          />
                        </div>
                        <div className="form-field">
                          <div className="label-row">
                            <label htmlFor="fragmentRetries">Fragment Retries</label>
                            <span className="slider-value tabular-nums">{fragmentRetries}</span>
                          </div>
                          <input 
                            id="fragmentRetries"
                            type="range" 
                            min="0" 
                            max="30" 
                            className="form-input-slider" 
                            value={fragmentRetries} 
                            onChange={(e) => { const v = parseInt(e.target.value); setFragmentRetries(v); pushSettings({ fragmentRetries: v }) }}
                          />
                        </div>
                      </div>

                      <div className="form-field">
                        <label htmlFor="rateLimit">Download Speed Limit</label>
                        <input 
                          id="rateLimit"
                          type="text" 
                          className="form-input" 
                          value={rateLimit} 
                          onChange={(e) => setRateLimit(e.target.value)}
                          onBlur={(e) => pushSettings({ rateLimit: e.target.value })}
                          placeholder="Unlimited (e.g. 50K, 1M, 5M)"
                        />
                      </div>

                      <div className="form-field">
                        <label htmlFor="ffmpegLocation">Custom FFmpeg Binary Path</label>
                        <input 
                          id="ffmpegLocation"
                          type="text" 
                          className="form-input" 
                          value={ffmpegLocation} 
                          onChange={(e) => setFfmpegLocation(e.target.value)}
                          onBlur={(e) => pushSettings({ ffmpegLocation: e.target.value })}
                          placeholder="System default path (leave empty)"
                        />
                      </div>
                    </div>
                  </section>

                  <section className="settings-card" style={{ marginTop: '24px' }}>
                    <div className="section-header-row">
                      <h2 className="section-title-large">
                        <Play size={18} />
                        <span>aria2-next Configuration</span>
                      </h2>
                    </div>
                    <div className="settings-group">
                      <label className="checkbox-row custom-toggle-row">
                        <input 
                          type="checkbox" 
                          checked={useAria2}
                          onChange={(e) => { setUseAria2(e.target.checked); pushSettings({ useAria2: e.target.checked }) }}
                        />
                        <span>Enable aria2-next External Downloader</span>
                      </label>

                      {useAria2 && (
                        <div className="aria2-subpanel animate-fade-in-up">
                          <div className="sliders-grid">
                            <div className="form-field">
                              <div className="label-row">
                                <label htmlFor="aria2ConcurrentDownloads">Max Concurrent Downloads</label>
                                <span className="slider-value tabular-nums">{aria2ConcurrentDownloads}</span>
                              </div>
                              <input 
                                id="aria2ConcurrentDownloads"
                                type="range" 
                                min="1" 
                                max="10" 
                                className="form-input-slider" 
                                value={aria2ConcurrentDownloads} 
                                onChange={(e) => { const v = parseInt(e.target.value); setAria2ConcurrentDownloads(v); pushSettings({ aria2ConcurrentDownloads: v }) }}
                              />
                            </div>
                            <div className="form-field">
                              <div className="label-row">
                                <label htmlFor="aria2MaxConnections">Max Connections Per Server</label>
                                <span className="slider-value tabular-nums">{aria2MaxConnections}</span>
                              </div>
                              <input 
                                id="aria2MaxConnections"
                                type="range" 
                                min="1" 
                                max="32" 
                                className="form-input-slider" 
                                value={aria2MaxConnections} 
                                onChange={(e) => { const v = parseInt(e.target.value); setAria2MaxConnections(v); pushSettings({ aria2MaxConnections: v }) }}
                              />
                            </div>
                            <div className="form-field">
                              <div className="label-row">
                                <label htmlFor="aria2Split">Max Split Connections Per File</label>
                                <span className="slider-value tabular-nums">{aria2Split}</span>
                              </div>
                              <input 
                                id="aria2Split"
                                type="range" 
                                min="1" 
                                max="32" 
                                className="form-input-slider" 
                                value={aria2Split} 
                                onChange={(e) => { const v = parseInt(e.target.value); setAria2Split(v); pushSettings({ aria2Split: v }) }}
                              />
                            </div>
                          </div>

                          <div className="form-field" style={{ marginTop: '8px' }}>
                            <label htmlFor="aria2MinSplitSize">Minimum Split Size</label>
                            <select 
                              id="aria2MinSplitSize"
                              className="form-select"
                              value={aria2MinSplitSize}
                              onChange={(e) => { setAria2MinSplitSize(e.target.value); pushSettings({ aria2MinSplitSize: e.target.value }) }}
                            >
                              <option value="1M">1 MB (Aggressive splitting)</option>
                              <option value="5M">5 MB</option>
                              <option value="10M">10 MB</option>
                              <option value="20M">20 MB</option>
                              <option value="50M">50 MB</option>
                            </select>
                          </div>

                          <div className="checkboxes-grid">
                            <label className="checkbox-row custom-toggle-row">
                              <input 
                                type="checkbox" 
                                checked={aria2Preallocate}
                                onChange={(e) => { setAria2Preallocate(e.target.checked); pushSettings({ aria2Preallocate: e.target.checked }) }}
                              />
                              <span>Pre-allocate File Space</span>
                            </label>
                            <label className="checkbox-row custom-toggle-row">
                              <input 
                                type="checkbox" 
                                checked={aria2CheckCertificate}
                                onChange={(e) => { setAria2CheckCertificate(e.target.checked); pushSettings({ aria2CheckCertificate: e.target.checked }) }}
                              />
                              <span>Validate SSL Certificates</span>
                            </label>
                            <label className="checkbox-row custom-toggle-row">
                              <input 
                                type="checkbox" 
                                checked={aria2AlwaysResume}
                                onChange={(e) => { setAria2AlwaysResume(e.target.checked); pushSettings({ aria2AlwaysResume: e.target.checked }) }}
                              />
                              <span>Always Resume Downloads</span>
                            </label>
                          </div>
                        </div>
                      )}
                    </div>
                  </section>
                </div>
              )}

              {activeSettingsSection === 'integrations' && (
                <div className="integrations-layout">
                  <section className="settings-card">
                    <div className="section-header-row">
                      <h2 className="section-title-large">
                        <Puzzle size={18} />
                        <span>Browser Extension Setup</span>
                      </h2>
                    </div>
                    <div className="settings-group">
                      <p className="section-subtitle">
                        Intercept standard browser downloads and sniff media streams automatically by installing the DownloadAnything browser extension.
                      </p>
                      
                      <div className="browser-instructions">
                        <div className="step-item">
                          <span className="step-badge">1</span>
                          <p>Click the button below to initiate extension detection on your system.</p>
                        </div>
                        <div className="step-item">
                          <span className="step-badge">2</span>
                          <p>Select your active browser profile from the detected browsers list to automatically load the extension bundle.</p>
                        </div>
                      </div>

                      <button 
                        className="action-btn extension-primary-btn" 
                        onClick={handleInstallExtensionClick}
                      >
                        <span>Detect and Install Extension</span>
                        <Puzzle size={14} />
                      </button>
                    </div>
                  </section>
                </div>
              )}
            </div>
          </div>
        )}

      </main>

      {/* DYNAMIC FORMAT SELECTION DRAWER / MODAL */}
      <Modal
        isOpen={showFormatDrawer && !!probedInfo}
        title="Configure Download Option"
        onClose={() => {
          setShowFormatDrawer(false)
          setProbedInfo(null)
        }}
        size="lg"
        footer={
          <>
            <button className="action-btn-secondary" onClick={() => {
              setShowFormatDrawer(false)
              setProbedInfo(null)
            }}>
              Cancel
            </button>
            <button className="action-btn" onClick={handleChooseFormat}>
              <span>Download Now</span>
              <Download size={14} />
            </button>
          </>
        }
      >
        {probedInfo && (
          <div className="format-drawer-body">
            {/* Media Card */}
            <div className="media-meta-card">
              <div className="meta-thumbnail-wrapper">
                {probedInfo.thumbnail ? (
                  <img src={probedInfo.thumbnail} alt="Cover" />
                ) : (
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: 'var(--text-muted)' }}>
                    <FileDown size={32} />
                  </div>
                )}
                {probedInfo.duration ? (
                  <span className="duration-badge">{formatDuration(probedInfo.duration)}</span>
                ) : null}
              </div>
              <div className="meta-info">
                <div className="meta-title" title={probedInfo.title}>{probedInfo.title}</div>
                {probedInfo.uploader && <div className="meta-uploader">Uploaded by: {probedInfo.uploader}</div>}
                <div className="meta-url">{inputUrl}</div>
              </div>
            </div>

             {/* Formats Grouping */}
             <div style={{ marginTop: '20px' }}>
               <div className="format-section-title" style={{ marginBottom: '10px', fontSize: '11px', fontWeight: 700, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                 Available Target Streams
               </div>
               <div className="format-list">
                 {probedInfo.formats.map((fmt) => {
                   const isSelected = selectedFormatId === fmt.formatId
                   return (
                     <div 
                       key={fmt.formatId} 
                       className={`format-list-row ${isSelected ? 'selected' : ''}`}
                       onClick={() => setSelectedFormatId(fmt.formatId)}
                     >
                       <div className="format-list-radio-col">
                         <input 
                           type="radio" 
                           name="format-choice"
                           checked={isSelected}
                           onChange={() => setSelectedFormatId(fmt.formatId)}
                         />
                       </div>
                       <div className="format-list-label-col">
                         <span className="format-list-label" title={fmt.label}>
                           {fmt.label}
                         </span>
                       </div>
                       <div className="format-list-ext-col">
                         <span className="format-list-ext">{fmt.ext.toUpperCase()}</span>
                       </div>
                       <div className="format-list-size-col">
                         <span className="format-list-size">
                           {fmt.estSizeBytes ? formatBytes(fmt.estSizeBytes) : '\u2014'}
                         </span>
                       </div>
                       <div className="format-list-type-col">
                         {fmt.streamType && (
                           <span className={`format-badge-pill ${fmt.streamType.toLowerCase()}`}>
                             {fmt.streamType.toUpperCase()}
                           </span>
                         )}
                       </div>
                       <div className="format-list-badges-col">
                         {fmt.hdr && (
                           <span className="format-badge hdr">HDR</span>
                         )}
                         {fmt.isCombined && (
                           <span className="format-badge muxed">Combined</span>
                         )}
                       </div>
                     </div>
                   )
                 })}
                 {probedInfo.formats.length === 0 && (
                   <div style={{ padding: '24px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '13px' }}>
                     No formats discovered. Defaulting to best.
                   </div>
                 )}
               </div>
             </div>

            {/* Destination folder presets */}
            <div className="drawer-options-row" style={{ marginTop: '16px' }}>
              <div className="form-field">
                <label htmlFor="drawerCategory">Save to Preset Category</label>
                <select 
                  id="drawerCategory"
                  className="form-select"
                  value={categories.find(c => c.path === selectedCategoryPath)?.path || ''}
                  onChange={(e) => {
                    setSelectedCategoryPath(e.target.value)
                    setDrawerCustomPath('') // reset custom path
                  }}
                >
                  {categories.map((c, i) => (
                    <option key={i} value={c.path}>{c.name} ({c.path})</option>
                  ))}
                </select>
              </div>
              
              <div className="form-field">
                <label>Or Choose Custom Output Directory</label>
                <div style={{ display: 'flex', gap: '8px' }}>
                  <input 
                    type="text" 
                    className="form-input" 
                    value={drawerCustomPath}
                    onChange={(e) => setDrawerCustomPath(e.target.value)}
                    placeholder="Custom directory path..."
                    style={{ flex: 1 }}
                  />
                  <button 
                    className="action-btn-secondary" 
                    onClick={() => {
                      fetchDirectory(drawerCustomPath || selectedCategoryPath, 'drawer')
                    }}
                  >
                    <FolderOpen size={14} />
                  </button>
                </div>
              </div>
            </div>
          </div>
        )}
      </Modal>

      {/* CONTEXT MENU */}
      {contextMenu && (
        <div 
          className="context-menu"
          style={{ 
            top: `${contextMenu.y}px`, 
            left: `${contextMenu.x}px`
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {(() => {
            const job = jobs[contextMenu.jobId]
            if (!job) return null
            const isActive = ['downloading', 'queued', 'postprocessing'].includes(job.status)
            const isMac = navigator.userAgent.toLowerCase().includes('mac')
            const revealLabel = isMac ? "Reveal in Finder" : "Reveal in Explorer"
            
            return (
              <>
                {isActive && (
                  <div className="context-menu-item" onClick={() => { handlePauseJob(job.job_id); setContextMenu(null); }}>
                    <Pause size={14} />
                    <span>Pause Task</span>
                  </div>
                )}
                {job.status === 'paused' && (
                  <div className="context-menu-item" onClick={() => { handleResumeJob(job.job_id); setContextMenu(null); }}>
                    <Play size={14} />
                    <span>Resume Task</span>
                  </div>
                )}
                {!isActive && (
                  <div className="context-menu-item" onClick={() => { handleRemoveJob(job.job_id); setContextMenu(null); }}>
                    <Trash2 size={14} />
                    <span>Remove from List</span>
                  </div>
                )}
                {job.status === 'completed' && job.file_path && (
                  <div className="context-menu-item" onClick={() => { handleRevealFile(job.job_id); setContextMenu(null); }}>
                    <FolderOpen size={14} />
                    <span>{revealLabel}</span>
                  </div>
                )}
                {job.status === 'completed' && (
                  <div className="context-menu-item" onClick={() => { handleDeleteFile(job.job_id); setContextMenu(null); }}>
                    <FileX size={14} style={{ color: 'var(--status-failed)' }} />
                    <span style={{ color: 'var(--status-failed)' }}>Delete File</span>
                  </div>
                )}
                <div className="context-menu-item" onClick={() => { setPropertiesJobId(job.job_id); setContextMenu(null); }}>
                  <Info size={14} />
                  <span>Properties</span>
                </div>
              </>
            )
          })()}
        </div>
      )}

      {/* 1. PROPERTIES MODAL */}
      <Modal
        isOpen={!!propertiesJobId}
        title="Task Properties"
        onClose={() => setPropertiesJobId(null)}
        footer={
          <button className="action-btn" onClick={() => setPropertiesJobId(null)}>Close</button>
        }
        size="md"
      >
        {propertiesJobId && (() => {
          const job = jobs[propertiesJobId]
          if (!job) return <p>Task details not found.</p>
          
          return (
            <div className="properties-grid">
              <div className="properties-label">Title</div>
              <div className="properties-value" style={{ fontWeight: 700 }}>{job.title || 'Unknown Title'}</div>

              <div className="properties-label">Job ID</div>
              <div className="properties-value" style={{ fontFamily: 'var(--font-mono)' }}>{job.job_id}</div>

              <div className="properties-label">Source URL</div>
              <div className="properties-value" style={{ color: 'var(--accent-secondary)' }}>
                <a href={job.url} target="_blank" rel="noreferrer" style={{ color: 'inherit', textDecoration: 'none' }}>
                  {job.url}
                </a>
              </div>

              <div className="properties-label">Status</div>
              <div className="properties-value" style={{ textTransform: 'uppercase', fontWeight: 800 }}>
                <span className={`status-badge ${job.status}`} style={{ border: 'none', padding: 0 }}>
                  {job.status}
                </span>
              </div>

              <div className="properties-label">Progress</div>
              <div className="properties-value">
                {job.progress.toFixed(2)}% ({formatBytes(job.combined_downloaded_bytes ?? (job.downloaded_bytes + job.audio_downloaded_bytes))} / {formatBytes(job.combined_total_bytes ?? (job.total_bytes + job.audio_total_bytes))})
              </div>

              {job.file_path && (
                <>
                  <div className="properties-label">Saved Path</div>
                  <div className="properties-value" style={{ color: 'var(--status-completed)', fontFamily: 'var(--font-mono)' }}>
                    {job.file_path}
                  </div>
                </>
              )}

              {job.output_dir && (
                <>
                  <div className="properties-label">Output Dir</div>
                  <div className="properties-value" style={{ fontFamily: 'var(--font-mono)' }}>{job.output_dir}</div>
                </>
              )}

              {job.error && (
                <>
                  <div className="properties-label" style={{ color: 'var(--status-failed)' }}>Error</div>
                  <div className="properties-value" style={{ color: 'var(--status-failed)' }}>{job.error}</div>
                </>
              )}
            </div>
          )
        })()}
      </Modal>

      {/* 2. DUPLICATE JOB INFO ALERT MODAL */}
      <Modal
        isOpen={!!duplicateJobAlert}
        title="Link Already In List"
        onClose={() => setDuplicateJobAlert(null)}
        footer={
          <>
            <button className="action-btn-secondary" onClick={() => setDuplicateJobAlert(null)}>
              Dismiss
            </button>
            {duplicateJobAlert && (
              <button 
                className="action-btn" 
                onClick={() => {
                  setPropertiesJobId(duplicateJobAlert.jobId)
                  setDuplicateJobAlert(null)
                }}
              >
                View Task Details
              </button>
            )}
          </>
        }
        size="sm"
      >
        {duplicateJobAlert && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '14px', lineHeight: 1.4 }}>
            <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
              This media link has already been submitted and exists in the task registry.
            </div>
            <div className="media-info" style={{ background: 'rgba(255,255,255,0.01)', border: '1px solid var(--border-dim)', padding: '12px', borderRadius: 'var(--radius-md)', display: 'flex', flexDirection: 'column', gap: '8px' }}>
              <div style={{ fontWeight: 700, fontSize: '12.5px' }}>{duplicateJobAlert.title}</div>
              <div style={{ fontSize: '11px', color: 'var(--text-muted)', wordBreak: 'break-all' }}>{duplicateJobAlert.url}</div>
              <div style={{ fontSize: '11px', textTransform: 'uppercase', fontWeight: 800 }}>
                Status: <span className={`status-badge ${duplicateJobAlert.status}`}>{duplicateJobAlert.status}</span>
              </div>
            </div>
          </div>
        )}
      </Modal>

      {/* 3. DUPLICATE FILENAME ACTION MODAL */}
      <Modal
        isOpen={!!duplicateFileAlert}
        title="Duplicate File Detected"
        onClose={() => setDuplicateFileAlert(null)}
        footer={
          <>
            <button className="action-btn-secondary" onClick={() => setDuplicateFileAlert(null)}>
              Cancel
            </button>
            {duplicateFileAlert && (
              <>
                <button 
                  className="action-btn-secondary" 
                  onClick={() => {
                    proceedWithDownload(
                      duplicateFileAlert.jobId,
                      selectedFormatIdRef.current,
                      duplicateFileAlert.path,
                      'rename'
                    )
                  }}
                >
                  Add Anyway (Auto-Rename)
                </button>
                <button 
                  className="action-btn" 
                  onClick={() => {
                    proceedWithDownload(
                      duplicateFileAlert.jobId,
                      selectedFormatIdRef.current,
                      duplicateFileAlert.path,
                      'replace'
                    )
                  }}
                >
                  Replace / Overwrite
                </button>
              </>
            )}
          </>
        }
        size="md"
      >
        {duplicateFileAlert && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '14px', lineHeight: 1.4 }}>
            <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
              A file with the same name already exists in the target save destination.
            </div>
            <div style={{ background: 'rgba(255,255,255,0.01)', border: '1px solid var(--border-dim)', padding: '12px', borderRadius: 'var(--radius-md)', display: 'flex', flexDirection: 'column', gap: '6px' }}>
              <div style={{ fontWeight: 700, fontSize: '12.5px', wordBreak: 'break-all' }}>{duplicateFileAlert.filename}</div>
              <div style={{ fontSize: '11px', color: 'var(--text-muted)', wordBreak: 'break-all' }}>Path: {duplicateFileAlert.path}</div>
            </div>
            <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
              Would you like to overwrite the existing file or download it as a new copy with an incremented name?
            </div>
          </div>
        )}
      </Modal>

      {/* 4. FILE DELETION CONFIRMATION MODAL */}
      <Modal
        isOpen={!!deleteFileConfirm}
        title="Confirm File Deletion"
        onClose={() => setDeleteFileConfirm(null)}
        footer={
          <>
            <button className="action-btn-secondary" onClick={() => setDeleteFileConfirm(null)}>
              Cancel
            </button>
            <button 
              className="action-btn" 
              style={{ background: 'var(--status-failed)' }}
              onClick={() => {
                if (deleteFileConfirm) {
                  if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                    wsRef.current.send(JSON.stringify({
                      type: 'delete_file',
                      jobId: deleteFileConfirm
                    }))
                  }
                  setDeleteFileConfirm(null)
                }
              }}
            >
              Delete File
            </button>
          </>
        }
        size="sm"
      >
        <p style={{ fontSize: '13px', lineHeight: 1.4, color: 'var(--text-secondary)' }}>
          Are you sure you want to delete the downloaded file from your disk? This action is irreversible.
        </p>
      </Modal>

      {/* 5. GENERIC ALERT / DIALOG MODAL */}
      <Modal
        isOpen={!!genericAlert}
        title={genericAlert?.title || "Notification"}
        onClose={() => setGenericAlert(null)}
        footer={
          <button className="action-btn" onClick={() => setGenericAlert(null)}>
            OK
          </button>
        }
        size="sm"
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', lineHeight: 1.4, fontSize: '13px', color: 'var(--text-secondary)' }}>
          <p>{genericAlert?.message}</p>
          {genericAlert?.suggestion && (
            <div style={{ background: 'rgba(255,255,255,0.01)', border: '1px solid var(--border-dim)', padding: '10px', borderRadius: 'var(--radius-md)', fontSize: '11.5px', color: 'var(--text-muted)' }}>
              <strong>Suggestion:</strong> {genericAlert.suggestion}
            </div>
          )}
        </div>
      </Modal>

      {/* 6. BROWSER EXTENSION INTEGRATION MODAL */}
      <Modal 
        isOpen={isExtModalOpen} 
        title="Install Browser Extension" 
        onClose={() => setIsExtModalOpen(false)}
        size="md"
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          <p style={{ color: 'var(--text-secondary)', fontSize: '13px', lineHeight: '1.5' }}>
            Select an installed browser to integrate the DownloadAnything media sniffer extension:
          </p>
          
          {isLoadingBrowsers ? (
            <div style={{ display: 'flex', justifyContent: 'center', padding: '24px 0' }}>
              <span style={{ fontSize: '14px', color: 'var(--text-muted)' }}>Detecting installed browsers...</span>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
              {browsersList.map((browser) => (
                <div 
                  key={browser.key}
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    padding: '12px 16px',
                    background: 'rgba(255,255,255,0.02)',
                    border: '1px solid var(--border)',
                    borderRadius: '8px'
                  }}
                >
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                    <span style={{ fontWeight: '600', fontSize: '14px', color: 'var(--text)' }}>{browser.name}</span>
                    <span style={{ fontSize: '11px', color: browser.installed ? '#10b981' : '#ef4444', fontWeight: 600 }}>
                      {browser.installed ? 'Detected' : 'Not Detected'}
                    </span>
                  </div>
                  
                  <button
                    className="action-btn"
                    style={{ padding: '6px 12px', fontSize: '12px' }}
                    onClick={() => handleInstallForBrowser(browser)}
                    disabled={!browser.installed}
                  >
                    Install
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </Modal>
    </div>
  )
}


function GlobeIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      {...props}
      xmlns="http://www.w3.org/2000/svg"
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="12" cy="12" r="10" />
      <path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20" />
      <path d="M2 12h20" />
    </svg>
  )
}

export default App
