/**
 * DownloadAnything - MAIN World Injector
 * Runs at document_start in the MAIN world to intercept in-memory streams,
 * player instances, dynamic media source setters, Blobs, Fetch and XHR.
 */
(function () {
  'use strict';

  if (window.__DA_INJECTED__) return;
  window.__DA_INJECTED__ = true;

  // Inlined copy of the shared constants: this file runs in the MAIN world,
  // which cannot see the isolated world's globals, and listing constants.js in
  // both content-script entries prevents it from executing in the isolated
  // world (Chrome does not re-run the same file path across entries).
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
  const AUDIO_EXTS = new Set([
    'mp3', 'aac', 'ogg', 'oga', 'wav', 'flac', 'm4a', 'wma', 'opus', 'weba'
  ]);
  const SEGMENT_EXTS = new Set([
    'm4s', 'mp4a', 'mp4v', 'ts', 'm2ts', 'ismv', 'isma', 'seg', 'm4f'
  ]);
  const SLICE_PARAM_REGEX = /^(?:range|rn|sq|segment|seg|chunk|slice|frag|fragment|cpn|byterange|rbuf|alr|sgoap|bypass_hfr|keepalive)$/i;

  // JSON-embedded URLs arrive with escaped separators ("https:\/\/x\u0026y");
  // raw backslashes never occur in real URLs, so unescaping them is safe.
  function decodeEscapedUrl(url) {
    if (typeof url !== 'string' || !url.includes('\\')) return url;
    return url
      .replace(/\\u0026/gi, '&')
      .replace(/\\\//g, '/')
      .replace(/\\+$/, '');
  }

  const emittedUrls = new Map();

  function stripUrlQueryAndHash(url) {
    return (url || '').split('?')[0].split('#')[0].replace(/\/+$/, '');
  }

  function isSegmentUrl(url) {
    if (!url) return false;
    const withoutQuery = stripUrlQueryAndHash(url).toLowerCase();
    const match = withoutQuery.match(/\.([a-zA-Z0-9]+)$/);
    if (match && SEGMENT_EXTS.has(match[1])) return true;
    const filename = withoutQuery.split('/').pop() || '';
    return /^(?:seg(?:ment)?|chunk|frag|init)(?:[-_.]?(?:\d+|v\d+|a\d+))(?:[-_.].*)?$/i.test(filename);
  }

  function cleanSliceParams(rawUrl) {
    try {
      const parsed = new URL(rawUrl, window.location.href);
      const keys = Array.from(parsed.searchParams.keys());
      for (const key of keys) {
        if (SLICE_PARAM_REGEX.test(key)) {
          parsed.searchParams.delete(key);
        }
      }
      return parsed.href;
    } catch {
      return rawUrl;
    }
  }

  function classifyMedia(url, contentType) {
    if (!url || isSegmentUrl(url)) return null;

    const withoutQuery = stripUrlQueryAndHash(decodeEscapedUrl(url));
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
      // MPEG-TS is transport-segment content, never a standalone download.
      if (ct === 'video/mp2t' || ct === 'video/mp2ts') return null;
      if (ct.startsWith('video/')) return 'video';
      if (ct.startsWith('audio/')) return 'audio';
    }

    // Query signature inspection
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

    return null;
  }

  function isMasterUrl(url) {
    if (!url) return false;
    const lower = url.toLowerCase();
    if (lower.includes('multi=') || lower.includes('/_tpl_/')) return true;
    if (/\.mpd(?:[?#]|$)/i.test(url)) return true;
    const filename = stripUrlQueryAndHash(url).split('/').pop() || '';
    if (/^(?:master|main|manifest|index|playlist)\.m3u8$/i.test(filename)) return true;
    return /(?:^|[/_.-])master(?:[/_.-]|$)/i.test(lower);
  }

  function postSniffedMedia(entry) {
    if (!entry || !entry.url) return;
    const url = cleanSliceParams(decodeEscapedUrl(entry.url));
    if (!url || isSegmentUrl(url)) return;

    // Dedupe by path, but re-emit when the query changes: CDNs rotate signed
    // tokens on the same path, and the freshest URL is the one that still works.
    const dedupeKey = `${entry.type || ''}:${stripUrlQueryAndHash(url)}`;
    if (emittedUrls.get(dedupeKey) === url) return;
    emittedUrls.set(dedupeKey, url);

    try {
      window.postMessage({
        type: '__DA_SNIFFED_MEDIA__',
        detail: {
          url,
          type: entry.type || classifyMedia(url, entry.contentType) || 'video',
          contentType: entry.contentType || '',
          source: entry.source || 'inject',
          isMaster: isMasterUrl(url),
          label: entry.label || '',
          referer: typeof window !== 'undefined' ? window.location.href : '',
          origin: typeof window !== 'undefined' && window.location.origin ? window.location.origin : '',
          timestamp: Date.now()
        }
      }, '*');
    } catch {
      // ignore serialization or window errors
    }
  }

  // --- Deep URL / Stream Scanner inside JSON objects ---
  function scanObjectForStreams(obj, depth = 0) {
    if (!obj || depth > 6 || typeof obj !== 'object') return;

    if (Array.isArray(obj)) {
      for (const item of obj) scanObjectForStreams(item, depth + 1);
      return;
    }

    // YouTube streamingData check
    if (obj.streamingData) {
      if (obj.streamingData.hlsManifestUrl) {
        postSniffedMedia({ url: obj.streamingData.hlsManifestUrl, type: 'hls', source: 'youtube:streamingData' });
      }
      if (obj.streamingData.dashManifestUrl) {
        postSniffedMedia({ url: obj.streamingData.dashManifestUrl, type: 'dash', source: 'youtube:streamingData' });
      }
      const formats = [...(obj.streamingData.formats || []), ...(obj.streamingData.adaptiveFormats || [])];
      for (const fmt of formats) {
        if (fmt && fmt.url) {
          postSniffedMedia({
            url: fmt.url,
            type: fmt.mimeType?.startsWith('audio') ? 'audio' : 'video',
            contentType: fmt.mimeType || '',
            label: fmt.qualityLabel || (fmt.height ? `${fmt.height}p` : ''),
            source: 'youtube:format'
          });
        }
      }
    }

    for (const [key, value] of Object.entries(obj)) {
      const lowerKey = key.toLowerCase();
      if (typeof value === 'string' && value.length > 5 && value.length < 4096) {
        if (
          lowerKey.includes('m3u8') || lowerKey.includes('hls') ||
          lowerKey.includes('mpd') || lowerKey.includes('dash') ||
          lowerKey.includes('manifest') || lowerKey.includes('stream') ||
          lowerKey.includes('video') || lowerKey.includes('audio') ||
          lowerKey === 'url' || lowerKey === 'src' || lowerKey === 'file'
        ) {
          const type = classifyMedia(value, null);
          if (type) {
            postSniffedMedia({ url: value, type, source: `json:${key}` });
          }
        }
      } else if (typeof value === 'object' && value !== null) {
        scanObjectForStreams(value, depth + 1);
      }
    }
  }

  // --- Regex mining of arbitrary text (HTML fragments, JSON-LD, plain text) ---
  const MEDIA_URL_REGEX = /https?:\/\/[^\s"'<>]+/gi;
  function scanTextForMedia(text) {
    if (!text || typeof text !== 'string' || text.length > 5000000) return;
    for (const match of text.matchAll(MEDIA_URL_REGEX)) {
      const type = classifyMedia(match[0], null);
      if (type) {
        postSniffedMedia({ url: match[0], type, source: 'text:regex' });
      }
    }
  }

  function scanResponseText(text, reqUrl, tag) {
    if (!text) return;
    const trimmed = text.trim();
    if (trimmed.startsWith('#EXTM3U')) {
      postSniffedMedia({ url: reqUrl, type: 'hls', contentType: 'application/vnd.apple.mpegurl', source: `${tag}:hls` });
      return;
    }
    if (trimmed.startsWith('<?xml') && trimmed.includes('<MPD')) {
      postSniffedMedia({ url: reqUrl, type: 'dash', contentType: 'application/dash+xml', source: `${tag}:dash` });
      return;
    }
    if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
      try {
        scanObjectForStreams(JSON.parse(trimmed));
        return;
      } catch {}
    }
    scanTextForMedia(text);
  }

  // --- 1. In-Memory Blob Hook ---
  // Proxy keeps Blob subclassing/instanceof intact and lets us inspect the raw
  // constructor args synchronously — a manifest blob can be revoked before any
  // async text() read completes.
  if (typeof window.Blob === 'function') {
    const OriginalBlob = window.Blob;
    const emitManifestText = (text, type) => {
      const lowered = (type || '').toLowerCase();
      const isDash = lowered.includes('dash+xml');
      try {
        const base64 = btoa(unescape(encodeURIComponent(text)));
        const mime = isDash ? 'application/dash+xml' : 'application/vnd.apple.mpegurl';
        postSniffedMedia({
          url: `data:${mime};base64,${base64}`,
          type: isDash ? 'dash' : 'hls',
          contentType: mime,
          source: 'blob:text'
        });
      } catch {}
    };
    window.Blob = new Proxy(OriginalBlob, {
      construct(Target, args) {
        const blob = new Target(...args);
        try {
          const type = (args[1] && args[1].type) ? args[1].type : '';
          const lowered = type.toLowerCase();
          if (lowered.includes('mpegurl') || lowered.includes('dash+xml')) {
            if (blob.size <= 5 * 1024 * 1024 && typeof blob.text === 'function') {
              blob.text().then(text => {
                if (text.includes('#EXTM3U') || text.includes('<MPD')) emitManifestText(text, type);
              }).catch(() => {});
            }
          } else {
            // Untyped/small text blobs: sniff synchronously for manifest content
            let text = '';
            for (const part of (args[0] || [])) {
              if (typeof part === 'string') text += part;
              if (text.length > 512 * 1024) break;
            }
            if (text && (text.includes('#EXTM3U') || text.includes('<MPD'))) {
              emitManifestText(text, type);
            }
          }
        } catch {}
        return blob;
      }
    });
  }

  // --- 2. Window.fetch Interception ---
  if (typeof window.fetch === 'function') {
    const originalFetch = window.fetch;
    window.fetch = async function (...args) {
      const request = args[0];
      let reqUrl = '';
      if (typeof request === 'string') reqUrl = request;
      else if (request && typeof request.url === 'string') reqUrl = request.url;

      if (reqUrl) {
        const directType = classifyMedia(reqUrl, null);
        if (directType) {
          postSniffedMedia({ url: reqUrl, type: directType, source: 'fetch:url' });
        }
      }

      const response = await originalFetch.apply(this, args);

      try {
        const clone = response.clone();
        const contentType = clone.headers.get('content-type') || '';
        const lowerCt = contentType.toLowerCase();

        if (lowerCt.includes('mpegurl') || lowerCt.includes('dash+xml') || lowerCt.includes('video/') || lowerCt.includes('audio/')) {
          const type = classifyMedia(reqUrl, contentType);
          if (type) {
            postSniffedMedia({ url: reqUrl, type, contentType, source: 'fetch:headers' });
          }
        }

        if (lowerCt.includes('application/json') || lowerCt.includes('text/plain') || lowerCt.includes('javascript') || lowerCt.includes('text/html')) {
          clone.text().then(text => {
            scanResponseText(text, reqUrl, 'fetch:text');
          }).catch(() => {});
        }
      } catch {}

      return response;
    };
  }

  // --- 3. XMLHttpRequest Interception ---
  if (typeof window.XMLHttpRequest === 'function') {
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      this.__da_req_url = typeof url === 'string' ? url : (url && url.href ? url.href : '');
      if (this.__da_req_url) {
        const type = classifyMedia(this.__da_req_url, null);
        if (type) {
          postSniffedMedia({ url: this.__da_req_url, type, source: 'xhr:open' });
        }
      }
      return originalOpen.call(this, method, url, ...rest);
    };

    XMLHttpRequest.prototype.send = function (...args) {
      this.addEventListener('load', function () {
        try {
          const reqUrl = this.responseURL || this.__da_req_url;
          const contentType = this.getResponseHeader('content-type') || '';
          const lowerCt = contentType.toLowerCase();

          if (lowerCt.includes('mpegurl') || lowerCt.includes('dash+xml') || lowerCt.includes('video/') || lowerCt.includes('audio/')) {
            const type = classifyMedia(reqUrl, contentType);
            if (type) {
              postSniffedMedia({ url: reqUrl, type, contentType, source: 'xhr:headers' });
            }
          }

          if (this.responseType === '' || this.responseType === 'text') {
            const text = this.responseText;
            if (typeof text === 'string') {
              scanResponseText(text, reqUrl, 'xhr:text');
            }
          } else if (this.responseType === 'json' && this.response) {
            scanObjectForStreams(this.response);
          }
        } catch {}
      });

      return originalSend.apply(this, args);
    };
  }

  // --- 4. HTMLMediaElement & HTMLSourceElement .src Trap ---
  function trapProperty(proto, propName, sourceTag) {
    if (!proto) return;
    const desc = Object.getOwnPropertyDescriptor(proto, propName);
    if (!desc || !desc.set || !desc.get) return;

    Object.defineProperty(proto, propName, {
      get() {
        return desc.get.call(this);
      },
      set(val) {
        if (typeof val === 'string' && val) {
          const type = classifyMedia(val, null);
          if (type) {
            postSniffedMedia({ url: val, type, source: `${sourceTag}:src_setter` });
          }
        }
        return desc.set.call(this, val);
      },
      configurable: true,
      enumerable: true
    });
  }

  if (typeof HTMLMediaElement !== 'undefined') {
    trapProperty(HTMLMediaElement.prototype, 'src', 'media');
  }
  if (typeof HTMLSourceElement !== 'undefined') {
    trapProperty(HTMLSourceElement.prototype, 'src', 'source');
  }

  // --- 5. Player Library Prototype & Global Hooks ---
  function hookHlsConstructor(HlsConstructor) {
    if (!HlsConstructor || HlsConstructor.__da_hooked__) return;
    HlsConstructor.__da_hooked__ = true;
    if (HlsConstructor.prototype && typeof HlsConstructor.prototype.loadSource === 'function') {
      const origLoad = HlsConstructor.prototype.loadSource;
      HlsConstructor.prototype.loadSource = function (url) {
        if (typeof url === 'string') {
          postSniffedMedia({ url, type: 'hls', source: 'hlsjs:loadSource' });
        }
        return origLoad.apply(this, arguments);
      };
    }
  }

  function hookDashjs(dashInstance) {
    if (!dashInstance || dashInstance.__da_hooked__) return;
    dashInstance.__da_hooked__ = true;
    if (typeof dashInstance.attachSource === 'function') {
      const origAttach = dashInstance.attachSource;
      dashInstance.attachSource = function (url) {
        if (typeof url === 'string') {
          postSniffedMedia({ url, type: 'dash', source: 'dashjs:attachSource' });
        }
        return origAttach.apply(this, arguments);
      };
    }
  }

  // Hook existing globals if present
  if (window.Hls) hookHlsConstructor(window.Hls);
  if (window.dashjs) {
    if (window.dashjs.MediaPlayer && window.dashjs.MediaPlayer.prototype) {
      hookDashjs(window.dashjs.MediaPlayer.prototype);
    }
  }

  // Trap dynamic definition of Hls / dashjs
  let localHls = window.Hls;
  try {
    Object.defineProperty(window, 'Hls', {
      get() { return localHls; },
      set(val) {
        localHls = val;
        hookHlsConstructor(val);
      },
      configurable: true,
      enumerable: true
    });
  } catch {}

  let localDashjs = window.dashjs;
  try {
    Object.defineProperty(window, 'dashjs', {
      get() { return localDashjs; },
      set(val) {
        localDashjs = val;
        if (val && val.MediaPlayer && val.MediaPlayer.prototype) {
          hookDashjs(val.MediaPlayer.prototype);
        }
      },
      configurable: true,
      enumerable: true
    });
  } catch {}

  // --- 6. Runtime Player Instances Scanner (JWPlayer, VideoJS, SoundManager) ---
  function scanPlayerGlobals() {
    try {
      // JW Player
      if (typeof window.jwplayer === 'function') {
        const jw = window.jwplayer();
        if (jw && typeof jw.getPlaylist === 'function') {
          const playlist = jw.getPlaylist();
          if (Array.isArray(playlist)) {
            for (const item of playlist) {
              if (item.file) {
                const type = classifyMedia(item.file, null);
                if (type) postSniffedMedia({ url: item.file, type, label: item.title || '', source: 'jwplayer:file' });
              }
              if (Array.isArray(item.sources)) {
                for (const s of item.sources) {
                  if (s && s.file) {
                    const type = classifyMedia(s.file, s.type);
                    if (type) postSniffedMedia({ url: s.file, type, label: s.label || '', source: 'jwplayer:sources' });
                  }
                }
              }
            }
          }
        }
      }

      // Video.js
      if (window.videojs && typeof window.videojs.getAllPlayers === 'function') {
        const players = window.videojs.getAllPlayers();
        if (Array.isArray(players)) {
          for (const player of players) {
            const src = player.currentSrc ? player.currentSrc() : (player.src ? player.src() : null);
            if (src && typeof src === 'string') {
              const type = classifyMedia(src, null);
              if (type) postSniffedMedia({ url: src, type, source: 'videojs:currentSrc' });
            }
          }
        }
      }

      // Video.js custom elements registered on the DOM but absent from getAllPlayers
      for (const el of document.querySelectorAll('video-js, .video-js')) {
        const player = el.player;
        if (!player) continue;
        const source = player.tech && player.tech().currentSource_;
        if (source && source.src) {
          const type = classifyMedia(source.src, source.type);
          if (type) postSniffedMedia({ url: source.src, type, contentType: source.type || '', source: 'videojs:element' });
        }
      }

      // SoundManager
      if (window.soundManager && window.soundManager.sounds) {
        for (const sound of Object.values(window.soundManager.sounds)) {
          if (sound && sound.url) {
            postSniffedMedia({ url: sound.url, type: 'audio', source: 'soundmanager' });
          }
        }
      }
    } catch {}
  }

  // Scan immediately and after DOM events
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', scanPlayerGlobals);
    window.addEventListener('load', scanPlayerGlobals);
  } else {
    scanPlayerGlobals();
    window.addEventListener('load', scanPlayerGlobals);
  }

  // Also listen for trigger from content script if needed
  window.addEventListener('message', event => {
    if (event.data && event.data.type === '__DA_TRIGGER_PLAYER_SCAN__') {
      scanPlayerGlobals();
    }
  });

})();
