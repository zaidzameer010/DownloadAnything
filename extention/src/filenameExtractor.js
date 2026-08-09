(function (global) {
  const INVALID_CHARS = /[\\/:*?"<>|]/g;
  // eslint-disable-next-line no-control-regex
  const CONTROL_CHARS = new RegExp('[\\u0000-\\u001f\\u007f]', 'g');
  const MULTI_SPACE = /\s+/g;
  const SUFFIX_SEPARATORS = /\s+(?:[-–—|»·:]|on)\s+/;

  function siteBrand(url) {
    let hostname = '';
    if (url) {
      try { hostname = new URL(url).hostname; } catch { /* no-op */ }
    }
    if (!hostname && typeof location !== 'undefined' && location.hostname) {
      hostname = location.hostname;
    }
    if (!hostname) return '';
    const host = hostname.replace(/^www\./, '');
    const name = host.split('.')[0] || '';
    return name.toLowerCase();
  }

  function looksLikeBrand(text, url) {
    const brand = siteBrand(url);
    if (!text) return false;
    const squashed = text.toLowerCase().replace(/[^a-z0-9]/g, '');
    if (!squashed) return false;
    return !!brand && squashed === brand.replace(/[^a-z0-9]/g, '');
  }

  function stripBrandSuffix(title, url) {
    const parts = title.split(SUFFIX_SEPARATORS);
    while (parts.length > 1 && looksLikeBrand(parts[parts.length - 1], url)) {
      parts.pop();
    }
    return parts.join(' ');
  }

  const GENERIC_NAMES = new Set([
    'video', 'movie', 'playlist', 'manifest', 'master', 'index', 'media', 'file',
    'download', 'stream', 'segment', 'chunk', 'intro', 'outro', 'sample',
    'trailer', 'clip', 'default', 'output', 'mediafile', 'videofile'
  ]);

  function jsonLdVideoName() {
    if (typeof document === 'undefined') return '';
    const scripts = document.querySelectorAll('script[type="application/ld+json"]');
    for (const script of scripts) {
      try {
        let data = JSON.parse(script.textContent);
        if (data && Array.isArray(data['@graph'])) data = data['@graph'];
        const items = Array.isArray(data) ? data : [data];
        for (const item of items) {
          if (item && /video/i.test(String(item['@type'])) && item.name) {
            return String(item.name).trim();
          }
        }
      } catch { /* no-op */ }
    }
    return '';
  }

  function getVideoPageTitle(url) {
    if (typeof document === 'undefined') return '';
    const og = document.querySelector('meta[property="og:title"]');
    if (og?.content) return og.content.trim();
    const tw = document.querySelector('meta[name="twitter:title"]');
    if (tw?.content) return tw.content.trim();
    const ld = jsonLdVideoName();
    if (ld) return ld;
    const itemprop = document.querySelector('[itemprop="name"][content]');
    if (itemprop?.content) return itemprop.content.trim();
    const h1 = document.querySelector('h1');
    if (h1?.textContent) return h1.textContent.trim();
    const title = (document.title || '').trim();
    if (looksLikeBrand(title, url)) return '';
    return title;
  }

  function cleanTitle(raw, url) {
    if (!raw) return '';
    return stripBrandSuffix(raw, url)
      .replace(INVALID_CHARS, '-')
      .replace(MULTI_SPACE, ' ')
      .replace(/[-–—|»·: ]+$/, '')
      .trim()
      .slice(0, 80)
      .trim();
  }

  function isGenericName(name) {
    if (!name) return true;
    const lower = name.toLowerCase();
    return GENERIC_NAMES.has(lower) ||
      /^video_?\d*$/i.test(lower) ||
      /^movie_?\d*$/i.test(lower) ||
      /^-?[0-9a-f]{8,}$/i.test(lower) ||
      /^-?\d+$/.test(lower);
  }

  function pathFilename(url) {
    try {
      const clean = new URL(url).href.split('?')[0].split('#')[0].replace(/\/+$/, '');
      const path = new URL(clean).pathname.split('/').pop() || '';
      return decodeURIComponent(path);
    } catch { /* no-op */ }
    return '';
  }

  function sanitizeFilename(name) {
    return (name || '')
      .replace(CONTROL_CHARS, '')
      .replace(INVALID_CHARS, '-')
      .replace(MULTI_SPACE, ' ')
      .replace(/^[. ]+|[. ]+$/g, '')
      .trim();
  }

  function sanitizeExtension(ext) {
    return (ext || '')
      .replace(CONTROL_CHARS, '')
      .replace(INVALID_CHARS, '')
      .replace(/\s+/g, '')
      .slice(0, 10);
  }

  const RESOLUTION_PATTERNS = [
    {
      pattern: /(?:^|[^A-Za-z0-9])(\d{3,5})[xX×](\d{3,5})(?=[^A-Za-z0-9]|$)/,
      toLabel: m => {
        const h = parseInt(m[2], 10);
        return h >= 144 && h <= 8640 ? `${h}p` : '';
      }
    },
    {
      pattern: /(?:^|[^A-Za-z0-9])(\d{2,4})p(\d{2,3})?(?=\D|$)/i,
      toLabel: m => {
        const h = parseInt(m[1], 10);
        return h >= 144 && h <= 8640 ? `${h}p${m[2] || ''}` : '';
      }
    },
    {
      pattern: /(?:^|[^A-Za-z0-9])(4k|8k|uhd|fhd|qhd|2k)(?=[^A-Za-z0-9]|$)/i,
      toLabel: m => normalizeResolutionWord(m[1])
    }
  ];

  function normalizeResolutionWord(word) {
    switch (word.toLowerCase()) {
      case '4k':
      case 'uhd':
        return '4K';
      case '8k':
        return '8K';
      case 'fhd':
        return '1080p';
      case 'qhd':
        return '1440p';
      case 'hd':
        return '720p';
      case '2k':
        return '2K';
      default:
        return word.toLowerCase();
    }
  }

  function extractResolution(text) {
    if (!text) return '';
    const s = String(text);
    for (const { pattern, toLabel } of RESOLUTION_PATTERNS) {
      const m = s.match(pattern);
      if (m) {
        const label = toLabel(m);
        if (label) return label;
      }
    }
    return '';
  }

  function extractResolutionFromSources(...sources) {
    for (const source of sources) {
      const label = extractResolution(source);
      if (label) return label;
    }
    return '';
  }

  function suggestFilename(url, type, pageTitle, mergeOutputFormat) {
    const raw = pathFilename(url);
    let base = '';
    if (raw) {
      const dot = raw.lastIndexOf('.');
      if (dot > 0) {
        base = raw.slice(0, dot);
      } else if (dot !== 0) {
        base = raw;
      }
    }
    const cleanBase = sanitizeFilename(base);
    const targetExt = sanitizeExtension(mergeOutputFormat?.toLowerCase());

    // 1. Prefer meaningful page title if available
    const title = cleanTitle(pageTitle || getVideoPageTitle(url), url);
    if (title && !isGenericName(title)) {
      return targetExt ? `${title}.${targetExt}` : title;
    }

    // 2. Prefer non-generic URL path filename base
    if (cleanBase && !isGenericName(cleanBase)) {
      return targetExt ? `${cleanBase}.${targetExt}` : cleanBase;
    }

    // 3. Fallback
    const finalBase = title || 'media';
    return targetExt ? `${finalBase}.${targetExt}` : finalBase;
  }

  if (!global.DownloadAnything) global.DownloadAnything = {};
  global.DownloadAnything.suggestFilename = suggestFilename;
  global.DownloadAnything.sanitizeFilename = sanitizeFilename;
  global.DownloadAnything.extractResolution = extractResolution;
  global.DownloadAnything.extractResolutionFromSources = extractResolutionFromSources;
})(typeof self !== 'undefined' ? self : window);
