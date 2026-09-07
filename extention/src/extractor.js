(function (global) {
  const { STREAM_EXTS, VIDEO_EXTS, AUDIO_EXTS, SEGMENT_EXTS, SLICE_PARAM_REGEX } =
    global.DownloadAnythingConstants;

  // JSON-embedded URLs arrive with escaped separators ("https:\/\/x\u0026y");
  // raw backslashes never occur in real URLs, so unescaping them is safe.
  function decodeEscapedUrl(url) {
    if (typeof url !== 'string' || !url.includes('\\')) return url;
    return url
      .replace(/\\u0026/gi, '&')
      .replace(/\\\//g, '/')
      .replace(/\\+$/, '');
  }

  function getHeader(headers, name) {
    if (!headers || !name) return undefined;
    const needle = name.toLowerCase();
    const found = headers.find(h => h.name.toLowerCase() === needle);
    return found ? found.value : undefined;
  }

  function stripUrlQueryAndHash(url) {
    return (url || '').split('?')[0].split('#')[0].replace(/\/+$/, '');
  }

  function isSegmentUrl(url) {
    if (!url) return false;
    const withoutQuery = stripUrlQueryAndHash(url);
    const lower = withoutQuery.toLowerCase();
    const extMatch = lower.match(/\.([a-zA-Z0-9]+)$/);
    if (extMatch && SEGMENT_EXTS.has(extMatch[1])) return true;
    const filename = lower.split('/').pop() || '';
    return /^(?:seg(?:ment)?|chunk|frag|init)(?:[-_.]?(?:\d+|v\d+|a\d+))(?:[-_.].*)?$/i.test(filename);
  }

  function normalizeMediaUrl(rawUrl, baseUri) {
    if (!rawUrl || typeof rawUrl !== 'string') return '';
    const decoded = decodeEscapedUrl(rawUrl);
    try {
      const parsed = new URL(
        decoded,
        baseUri || (typeof document !== 'undefined' ? document.baseURI : 'http://localhost')
      );
      let changed = decoded !== rawUrl;
      if (parsed.hash) {
        parsed.hash = '';
        changed = true;
      }
      const keys = Array.from(parsed.searchParams.keys());
      for (const key of keys) {
        if (SLICE_PARAM_REGEX.test(key)) {
          parsed.searchParams.delete(key);
          changed = true;
        }
      }
      return changed ? parsed.href : rawUrl;
    } catch {
      return decoded;
    }
  }

  function calculateMediaSize(url, headers) {
    const cl = getHeader(headers, 'content-length');
    if (cl) {
      const parsed = parseInt(cl, 10);
      if (Number.isFinite(parsed) && parsed > 0) return { size: parsed, isEstimate: false };
    }

    // Chunked/ranged responses carry the total in Content-Range instead of
    // Content-Length ("bytes 100-199/12345").
    const cr = getHeader(headers, 'content-range');
    if (cr) {
      const match = cr.match(/bytes\s+\d+-\d+\/(\d+)/i);
      if (match) {
        const total = parseInt(match[1], 10);
        if (Number.isFinite(total) && total > 0) return { size: total, isEstimate: false };
      }
    }

    if (url && typeof url === 'string') {
      try {
        const u = new URL(url);
        const clen = u.searchParams.get('clen') || u.searchParams.get('contentlength');
        if (clen) {
          const parsed = parseInt(clen, 10);
          if (Number.isFinite(parsed) && parsed > 0) return { size: parsed, isEstimate: false };
        }
        const dur = u.searchParams.get('dur') || u.searchParams.get('duration');
        const bitrate = u.searchParams.get('bitrate');
        if (dur && bitrate) {
          const d = parseFloat(dur);
          const b = parseInt(bitrate, 10);
          if (Number.isFinite(d) && Number.isFinite(b) && d > 0 && b > 0) {
            return { size: Math.round((d * b) / 8), isEstimate: true };
          }
        }
      } catch {}
    }

    return { size: null, isEstimate: false };
  }

  function classifyMedia(url, contentType) {
    if (isSegmentUrl(url)) return null;

    const withoutQuery = stripUrlQueryAndHash(url);
    const match = withoutQuery.match(/\.([a-zA-Z0-9]+)$/);
    const ext = match ? match[1].toLowerCase() : null;

    if (ext) {
      if (STREAM_EXTS.has(ext)) return STREAM_EXTS.get(ext);
      if (VIDEO_EXTS.has(ext)) return 'video';
      if (AUDIO_EXTS.has(ext)) return 'audio';
    }

    if (contentType) {
      const ct = contentType.toLowerCase().split(';')[0].trim();
      if (ct === 'application/vnd.apple.mpegurl' || ct === 'application/x-mpegurl' || ct === 'audio/mpegurl') return 'hls';
      if (ct === 'application/dash+xml') return 'dash';
      if (ct.startsWith('video/')) return 'video';
      if (ct.startsWith('audio/')) return 'audio';
    }

    // Query signature inspection
    if (url && typeof url === 'string') {
      const lower = decodeEscapedUrl(url).toLowerCase();
      if (
        lower.includes('format=m3u8') || lower.includes('type=m3u8') ||
        lower.includes('output=m3u8') || lower.includes('stream_type=hls') ||
        lower.includes('/hls/') || lower.includes('protocol=hls') || lower.includes('.m3u8')
      ) return 'hls';
      if (
        lower.includes('format=mpd') || lower.includes('type=mpd') ||
        lower.includes('stream_type=dash') || lower.includes('/dash/') ||
        lower.includes('protocol=dash') || lower.includes('.mpd')
      ) return 'dash';
      if (lower.includes('/videoplayback?') || lower.includes('mime=video') || lower.includes('&video=1')) return 'video';
      if (lower.includes('mime=audio') || lower.includes('&audio=1')) return 'audio';
    }

    return null;
  }

  function pathFilename(url) {
    try {
      const name = new URL(stripUrlQueryAndHash(url)).pathname.split('/').pop() || '';
      return decodeURIComponent(name);
    } catch { /* no-op */ }
    return '';
  }

  function extFromName(name) {
    const m = String(name || '').match(/\.([a-zA-Z0-9]+)$/);
    return m ? m[1].toLowerCase() : '';
  }

  function stemFromName(name) {
    const ext = extFromName(name);
    if (!ext) return String(name || '');
    return String(name || '').slice(0, -(ext.length + 1));
  }

  function classifyDownload(url, contentType, filename) {
    const name = filename || pathFilename(url);
    const ext = extFromName(name).toLowerCase();
    if (filename && ext && (AUDIO_EXTS.has(ext) || VIDEO_EXTS.has(ext) || STREAM_EXTS.has(ext))) {
      return 'file';
    }

    const media = classifyMedia(url, contentType);
    if (media) return media;

    if (ext) {
      if (AUDIO_EXTS.has(ext)) return 'file';
      if (VIDEO_EXTS.has(ext) || STREAM_EXTS.has(ext)) return 'video';
    }

    if (contentType) {
      const ct = contentType.toLowerCase().split(';')[0].trim();
      if (ct.startsWith('audio/')) return 'file';
      if (ct.startsWith('video/')) return 'video';
      if (ct === 'application/vnd.apple.mpegurl' || ct === 'application/x-mpegurl') return 'hls';
      if (ct === 'application/dash+xml') return 'dash';
    }

    return 'file';
  }

  function isStream(type) {
    return type === 'hls' || type === 'dash' || type === 'hds' || type === 'smooth';
  }

  // Multi-rendition master manifests that encode every variant in one URL
  // Strictly preferable to any single-rendition URL: one probe lists all
  // formats for the picker.
  function isMultiUrl(url) {
    if (!url) return false;
    return /[?&/]multi=/.test(url) || url.includes('/_TPL_/');
  }

  function isMasterUrl(url) {
    if (!url) return false;
    if (isMultiUrl(url)) return true;
    const lower = url.toLowerCase();
    const filename = stripUrlQueryAndHash(url).split('/').pop() || '';
    if (/\.mpd(?:[?#]|$)/i.test(url)) return true;
    if (/^(?:master|main|manifest|index|playlist)\.m3u8$/i.test(filename)) return true;
    return /(?:^|[/_.-])master(?:[/_.-]|$)/i.test(lower);
  }

  function collectVideoSources(video) {
    const seen = new Set();
    const sources = [];
    const add = url => {
      if (!url || url.startsWith('blob:') || seen.has(url)) return;
      seen.add(url);
      sources.push(url);
    };
    add(video.currentSrc);
    add(video.src);
    for (const source of video.querySelectorAll('source')) add(source.src);
    return sources;
  }

  function getVideoUrl(video) {
    const sources = collectVideoSources(video);
    let firstStream = '';
    for (const url of sources) {
      const type = classifyMedia(url, null);
      if (!isStream(type)) continue;
      if (isMasterUrl(url)) return url;
      if (!firstStream) firstStream = url;
    }
    return firstStream || sources[0] || '';
  }

  function resolvePageUrl(value) {
    if (!value || typeof value !== 'string') return '';
    const normalized = value
      .replace(/\\\\\//g, '/')
      .replace(/&amp;/g, '&')
      .trim()
      .replace(/[),;]+$/, '');
    try {
      return new URL(normalized, document.baseURI).href;
    } catch {
      return '';
    }
  }

  function collectPageMediaVariants() {
    if (typeof document === 'undefined') return [];
    const byUrl = new Map();
    const byFilename = new Map();
    const add = (value, label = '') => {
      const rawUrl = resolvePageUrl(value);
      if (!rawUrl || !classifyMedia(rawUrl, null)) return;
      const url = normalizeMediaUrl(rawUrl);
      const normalizedLabel = String(label || '').trim()
        || (DownloadAnything.extractResolutionFromSources ? DownloadAnything.extractResolutionFromSources(url) : '');
      const key = stripUrlQueryAndHash(url);
      const filename = pathFilename(url).toLowerCase();
      const existing = byUrl.get(key) || (filename ? byFilename.get(filename) : null);
      if (existing) {
        if (!existing.label && normalizedLabel) {
          existing.label = normalizedLabel;
          existing.url = url;
        } else if (!existing.url.includes('?') && url.includes('?')) {
          existing.url = url;
        }
        return;
      }
      const variant = { url, label: normalizedLabel };
      byUrl.set(key, variant);
      if (filename) byFilename.set(filename, variant);
    };

    // Recursive traversal through open Shadow DOM roots
    function scanRoot(root) {
      if (!root) return;
      for (const element of root.querySelectorAll('video, audio, source')) {
        add(element.currentSrc);
        add(element.src);
        add(element.getAttribute('data-src'));
        add(element.getAttribute('data-video'));
        add(element.getAttribute('data-url'));
      }

      const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
      while (walker.nextNode()) {
        const node = walker.currentNode;
        if (node.shadowRoot && node.shadowRoot !== root) {
          scanRoot(node.shadowRoot);
        }
      }
    }

    scanRoot(document);

    const urlPattern = /(?:https?:\/\/|\/\/)[^'"\s<>\\]+/g;
    for (const script of document.scripts) {
      const text = script.textContent || '';
      for (const match of text.matchAll(urlPattern)) {
        const context = text.slice(match.index + match[0].length, match.index + match[0].length + 500);
        const labelMatch = context.match(/(?:video_url_text|video_alt_url2?_text)\s*:\s*['"]([^'"]+)['"]/i);
        add(match[0], labelMatch?.[1] || '');
      }
    }
    return [...byUrl.values()];
  }

  function extractPerformanceResources() {
    if (typeof performance === 'undefined' || typeof performance.getEntriesByType !== 'function') return [];
    const entries = performance.getEntriesByType('resource');
    const results = [];
    const seen = new Set();

    for (const entry of entries) {
      const rawUrl = entry.name;
      if (!rawUrl || seen.has(rawUrl)) continue;
      const type = classifyMedia(rawUrl, null);
      if (!type) continue;
      seen.add(rawUrl);
      const url = normalizeMediaUrl(rawUrl);
      results.push({
        url,
        type,
        isMaster: isMasterUrl(url),
        source: 'performance',
        duration: entry.duration || 0,
        transferSize: entry.transferSize || 0
      });
    }
    return results;
  }

  global.DownloadAnything = Object.assign(global.DownloadAnything || {}, {
    getHeader,
    decodeEscapedUrl,
    stripUrlQueryAndHash,
    normalizeMediaUrl,
    calculateMediaSize,
    classifyMedia,
    classifyDownload,
    pathFilename,
    extFromName,
    stemFromName,
    isStream,
    isMasterUrl,
    isMultiUrl,
    isSegmentUrl,
    collectVideoSources,
    collectPageMediaVariants,
    getVideoUrl,
    extractPerformanceResources
  });
})(typeof self !== 'undefined' ? self : window);
