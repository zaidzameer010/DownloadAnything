(function (global) {
  const STREAM_EXTS = new Map([
    ['m3u8', 'hls'],
    ['m3u', 'hls'],
    ['mpd', 'dash'],
    ['f4m', 'hds'],
    ['ism', 'smooth']
  ]);

  const VIDEO_EXTS = new Set([
    'mp4', 'webm', 'ogg', 'ogv', 'mov', 'mkv', 'm4v', 'flv', 'avi',
    'wmv', 'mpg', 'mpeg', '3gp', 'f4v'
  ]);

  const SEGMENT_EXTS = new Set(['m4s', 'mp4a', 'mp4v', 'ts', 'm2ts', 'ismv', 'isma']);

  const AUDIO_EXTS = new Set([
    'mp3', 'aac', 'ogg', 'oga', 'wav', 'flac', 'm4a', 'wma', 'opus', 'weba'
  ]);

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
    const withoutQuery = stripUrlQueryAndHash(url);
    const lower = withoutQuery.toLowerCase();
    const extMatch = lower.match(/\.([a-zA-Z0-9]+)$/);
    if (extMatch && SEGMENT_EXTS.has(extMatch[1])) return true;
    const filename = lower.split('/').pop() || '';
    return /\b(?:seg|segment|chunk|frag|init)\d*\.[^.]+$/i.test(filename);
  }

  function classifyMedia(url, contentType) {
    if (isSegmentUrl(url)) return null;

    const withoutQuery = stripUrlQueryAndHash(url);
    const match = withoutQuery.match(/\.([a-zA-Z0-9]+)$/);
    const ext = match ? match[1].toLowerCase() : null;

    if (ext) {
      if (STREAM_EXTS.has(ext)) return STREAM_EXTS.get(ext);
      if (VIDEO_EXTS.has(ext)) return 'video';
    }

    if (contentType) {
      const ct = contentType.toLowerCase().split(';')[0].trim();
      if (ct === 'application/vnd.apple.mpegurl' || ct === 'application/x-mpegurl') return 'hls';
      if (ct === 'application/dash+xml') return 'dash';
      if (ct.startsWith('video/')) return 'video';
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

  function isMasterUrl(url) {
    const lower = url.toLowerCase();
    if (lower.endsWith('.mpd')) return true;
    if (/\.m3u8(?:[?#]|$)/i.test(url) && /\b(master|main|manifest|index|playlist)\b/i.test(url)) return true;
    return /\bmaster\b/i.test(url);
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
    const add = (value, label = '') => {
      const url = resolvePageUrl(value);
      if (!url || !classifyMedia(url, null)) return;
      const normalizedLabel = String(label || '').trim();
      const key = stripUrlQueryAndHash(url);
      const filename = pathFilename(url).toLowerCase();
      const existing = byUrl.get(key) || (
        filename
          ? [...byUrl.values()].find(item => pathFilename(item.url).toLowerCase() === filename)
          : null
      );
      if (existing) {
        if (!existing.label && normalizedLabel) {
          existing.label = normalizedLabel;
          existing.url = url;
        } else if (!existing.url.includes('?') && url.includes('?')) {
          existing.url = url;
        }
        return;
      }
      byUrl.set(key, { url, label: normalizedLabel });
    };

    for (const element of document.querySelectorAll('video, audio, source')) {
      add(element.currentSrc);
      add(element.src);
      add(element.getAttribute('data-src'));
      add(element.getAttribute('data-video'));
      add(element.getAttribute('data-url'));
    }

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

  function collectPageMediaSources() {
    return collectPageMediaVariants().map(variant => variant.url);
  }

  global.DownloadAnything = Object.assign(global.DownloadAnything || {}, {
    getHeader,
    classifyMedia,
    classifyDownload,
    pathFilename,
    extFromName,
    isStream,
    isMasterUrl,
    collectVideoSources,
    collectPageMediaSources,
    collectPageMediaVariants,
    getVideoUrl
  });
})(typeof self !== 'undefined' ? self : window);
