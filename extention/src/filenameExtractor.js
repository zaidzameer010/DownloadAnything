(function (global) {
  const INVALID_CHARS = /[\\/:*?"<>|]/g;
  // eslint-disable-next-line no-control-regex
  const CONTROL_CHARS = new RegExp('[\\u0000-\\u001f\\u007f]', 'g');
  const MULTI_SPACE = /\s+/g;
  const SUFFIX_SEPARATORS = /\s+(?:[-–—|»·:]|on)\s+/;
  // Trailing "<sep><word(s)>" anchored at the end of a title, e.g.
  // "My Video - Vimeo" -> ["My Video", "Vimeo"].
  const SUFFIX_TAIL_RE = /^(.*)\s+(?:[-–—|»·:]|on)\s+(\S.*)$/;
  global.DownloadAnything = global.DownloadAnything || {};
  // extractor.js loads first (manifest order) and defines these; destructure
  // the shared implementations instead of carrying fallback copies.
  const { extFromName, pathFilename, stemFromName } = global.DownloadAnything;

  const { GENERIC_NAMES, HOST_PREFIX_NOISE, WINDOWS_RESERVED } = global.DownloadAnythingConstants;

  function siteBrand(url) {
    let hostname = '';
    if (url) {
      try { hostname = new URL(url).hostname; } catch { /* no-op */ }
    }
    if (!hostname && typeof location !== 'undefined' && location.hostname) {
      hostname = location.hostname;
    }
    if (!hostname) return '';
    const labels = hostname.replace(/^www\./, '').split('.');
    const name = labels.find(label => !HOST_PREFIX_NOISE.has(label.toLowerCase())) || labels[0] || '';
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
    // Trim trailing "<sep><brand>" from the original string (rather than
    // splitting and rejoining) so separators inside the title survive.
    let result = title.trim();
    let prev = '';
    while (result !== prev) {
      prev = result;
      const match = result.match(SUFFIX_TAIL_RE);
      if (match && looksLikeBrand(match[2], url)) {
        result = match[1].trim();
      }
    }
    return result;
  }

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
    // document.title is usually the media name plus a brand suffix (which
    // stripBrandSuffix removes); a bare <h1> is more often site chrome.
    const title = (document.title || '').trim();
    if (title && !looksLikeBrand(title, url)) return title;
    const h1 = document.querySelector('h1');
    if (h1?.textContent) return h1.textContent.trim();
    return '';
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

  // Windows reserves these device names with or without an extension and in
  // any case style ("con.mp4" is as invalid as "con").
  const MAX_FILENAME_LENGTH = 150;

  function sanitizeFilename(name) {
    let clean = (name || '')
      .replace(CONTROL_CHARS, '')
      .replace(INVALID_CHARS, '-')
      .replace(MULTI_SPACE, ' ')
      .replace(/^[. ]+|[. ]+$/g, '')
      .trim();
    if (WINDOWS_RESERVED.test(clean)) {
      clean = `_${clean}`;
    }
    if (clean.length > MAX_FILENAME_LENGTH) {
      const ext = extFromName(clean);
      const stem = ext ? clean.slice(0, clean.length - ext.length - 1) : clean;
      clean = (ext ? `${stem.slice(0, MAX_FILENAME_LENGTH - ext.length - 1)}.${ext}` : stem.slice(0, MAX_FILENAME_LENGTH));
    }
    return clean;
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
      // 1920x1080 / 1280X720 / 1920×1080 — the height drives the label.
      // Boundaries exclude only digits, so "abc1920x1080" still matches
      // while "11920x1080" (a longer number) does not.
      pattern: /(?:^|[^0-9])(\d{3,5})[xX×](\d{3,5})(?![0-9])/,
      toLabel: m => {
        const h = parseInt(m[2], 10);
        return h >= 144 && h <= 8640 ? `${h}p` : '';
      }
    },
    {
      // 720p / 1080p60 / 1080p60fps / 1080i (interlaced) — a letter may butt
      // against the digits ("video1080p.mp4"); a trailing letter is rejected
      // so image widths ("360px") never read as heights.
      pattern: /(?:^|[^0-9])(\d{2,4})[pi](\d{2,3})?(?:fps)?(?![a-z0-9])/i,
      toLabel: m => {
        const h = parseInt(m[1], 10);
        return h >= 144 && h <= 8640 ? `${h}p${m[2] || ''}` : '';
      }
    },
    {
      // 4k / 8k / uhd / fhd / qhd / 2k — "4kb" (file size) is rejected.
      pattern: /(?:^|[^0-9])(4k|8k|uhd|fhd|qhd|2k)(?![a-z0-9])/i,
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
    const base = raw ? stemFromName(raw) : '';
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
  global.DownloadAnything.isGenericName = isGenericName;
  global.DownloadAnything.extractResolution = extractResolution;
  global.DownloadAnything.extractResolutionFromSources = extractResolutionFromSources;
})(typeof self !== 'undefined' ? self : window);
