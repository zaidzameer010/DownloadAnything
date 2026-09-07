/* Page thumbnail extraction for sites yt-dlp cannot probe.
 *
 * Derives the most accurate poster image for the hovered/downloaded media
 * from the DOM: the video's own poster first, then structured metadata
 * (JSON-LD, Open Graph, Twitter cards, microdata), then legacy hints.
 * Every candidate is resolved to an absolute URL and filtered for icons,
 * sprites, tracking pixels and non-http schemes before being returned.
 */
(function (global) {
  'use strict';

  const { JUNK_NAME_RE } = global.DownloadAnythingConstants;
  const MAX_URL_LENGTH = 2048;

  function resolveUrl(value) {
    if (!value || typeof value !== 'string') return '';
    const trimmed = value.trim().replace(/&amp;/g, '&');
    if (!trimmed || trimmed.length > MAX_URL_LENGTH) return '';
    if (/^(?:data|blob|javascript):/i.test(trimmed)) return '';
    try {
      const resolved = new URL(trimmed, document.baseURI);
      if (resolved.protocol !== 'http:' && resolved.protocol !== 'https:') return '';
      return resolved.href;
    } catch {
      return '';
    }
  }

  function isJunkImage(url) {
    try {
      return JUNK_NAME_RE.test(new URL(url).pathname);
    } catch {
      return true;
    }
  }

  function metaContent(selector) {
    const el = document.querySelector(selector);
    const content = el?.getAttribute('content');
    return content ? content.trim() : '';
  }

  // Pick the largest thumbnail when structured data offers several. Size
  // hints are unreliable across sites, so score explicit dimensions, common
  // URL size params, and resolution tokens; ties prefer the later (usually
  // higher-quality) entry, matching yt-dlp's own preference.
  function pickLargest(urls) {
    if (urls.length <= 1) return urls[0] || '';
    const score = url => {
      let best = 0;
      const patterns = [
        /(\d{2,4})x(\d{2,4})/i,
        /[?&](?:w|width)=(\d{2,5})/i,
        /[?&](?:h|height)=(\d{2,5})/i
      ];
      for (const pattern of patterns) {
        const m = url.match(pattern);
        if (m) {
          const a = parseInt(m[1], 10);
          const b = m[2] ? parseInt(m[2], 10) : a;
          best = Math.max(best, a * b);
        }
      }
      return best;
    };
    let bestUrl = '';
    let bestScore = -1;
    for (const url of urls) {
      const s = score(url);
      if (s >= bestScore) {
        bestScore = s;
        bestUrl = url;
      }
    }
    return bestUrl;
  }

  function jsonLdThumbnails() {
    const found = [];
    const scripts = document.querySelectorAll('script[type="application/ld+json"]');
    for (const script of scripts) {
      try {
        let data = JSON.parse(script.textContent);
        if (data && Array.isArray(data['@graph'])) data = data['@graph'];
        const items = Array.isArray(data) ? data : [data];
        for (const item of items) {
          if (!item || typeof item !== 'object') continue;
          // Prefer video-typed objects; fall back to any object with a
          // thumbnail so Article/CreativeWork pages still get one.
          const isVideo = /video|movie|clip|broadcast/i.test(String(item['@type'] || ''));
          const raw = item.thumbnailUrl ?? item.thumbnailURL ?? item.image ?? item.thumbnail;
          const values = Array.isArray(raw) ? raw : [raw];
          const urls = [];
          for (const value of values) {
            const url = resolveUrl(typeof value === 'object' && value !== null ? value.url : value);
            if (url && !isJunkImage(url)) urls.push(url);
          }
          if (urls.length) {
            found.push({ isVideo, url: pickLargest(urls) });
          }
        }
      } catch { /* malformed JSON-LD is common; skip */ }
    }
    const video = found.filter(entry => entry.isVideo).map(entry => entry.url);
    if (video.length) return video[0];
    return found.map(entry => entry.url)[0] || '';
  }

  function extractThumbnail(video) {
    if (typeof document === 'undefined') return '';

    // 1. The video's own poster is the most accurate for this exact media.
    if (video && typeof video.poster === 'string' && video.poster) {
      const poster = resolveUrl(video.poster);
      if (poster && !isJunkImage(poster)) return poster;
    }

    // 2. Structured data (JSON-LD), video objects first.
    const ld = jsonLdThumbnails();
    if (ld) return ld;

    // 3. Open Graph / Twitter card metadata.
    for (const selector of [
      'meta[property="og:image:secure_url"]',
      'meta[property="og:image:url"]',
      'meta[property="og:image"]',
      'meta[name="twitter:image"]',
      'meta[name="twitter:image:src"]'
    ]) {
      const url = resolveUrl(metaContent(selector));
      if (url && !isJunkImage(url)) return url;
    }

    // 4. Microdata and legacy hints.
    for (const selector of [
      '[itemprop="thumbnailUrl"][content]',
      '[itemprop="image"][content]',
      'link[rel="image_src"]'
    ]) {
      const el = document.querySelector(selector);
      const value = el?.getAttribute('content') || el?.getAttribute('href');
      const url = resolveUrl(value);
      if (url && !isJunkImage(url)) return url;
    }

    // 5. Last resort: the largest poster attribute of any visible video.
    const posters = [];
    for (const el of document.querySelectorAll('video[poster]')) {
      const url = resolveUrl(el.getAttribute('poster'));
      if (url && !isJunkImage(url)) posters.push(url);
    }
    return pickLargest(posters);
  }

  global.DownloadAnything = Object.assign(global.DownloadAnything || {}, {
    extractThumbnail
  });
})(typeof self !== 'undefined' ? self : window);
