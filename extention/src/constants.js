/**
 * DownloadAnything - shared constant data.
 *
 * Single home for the media-extension maps/sets and the URL/filename pattern
 * lists that more than one content script reasons about, so the definitions
 * are never duplicated (extractor.js and inject.js previously each carried
 * their own copy) and cannot drift apart. Loaded before every other content
 * script in both manifest worlds; behavior stays with the consumers.
 */
(function (global) {
  'use strict';

  const constants = {
    // Stream-manifest extensions mapped to their protocol family.
    STREAM_EXTS: new Map([
      ['m3u8', 'hls'],
      ['m3u', 'hls'],
      ['mpd', 'dash'],
      ['f4m', 'hds'],
      ['ism', 'smooth']
    ]),

    VIDEO_EXTS: new Set([
      'mp4', 'webm', 'ogg', 'ogv', 'mov', 'mkv', 'm4v', 'flv', 'avi',
      'wmv', 'mpg', 'mpeg', '3gp', 'f4v'
    ]),

    // Transport/fragment chunks that must never be offered as downloads.
    SEGMENT_EXTS: new Set([
      'm4s', 'mp4a', 'mp4v', 'ts', 'm2ts', 'ismv', 'isma', 'seg', 'm4f'
    ]),

    AUDIO_EXTS: new Set([
      'mp3', 'aac', 'ogg', 'oga', 'wav', 'flac', 'm4a', 'wma', 'opus', 'weba'
    ]),

    // Ephemeral chunk/slice/sequence query parameters stripped when a media
    // URL is canonicalized, so every fragment of one stream dedupes to one entry.
    SLICE_PARAM_REGEX: /^(?:range|rn|sq|segment|seg|chunk|slice|frag|fragment|cpn|byterange|rbuf|alr|sgoap|bypass_hfr|keepalive)$/i,

    // Generic video-embed iframes: src path hints and known player hosts.
    EMBED_URL_HINT: /(?:^|[/_.-])(?:embed|players?|video(?:s)?|stream(?:ing)?|media|player_v2)(?:[/_.?-]|$)/i,
    EMBED_HOST_HINTS: /(?:youtube|youtube-nocookie|vimeo|dailymotion|twitch|bilibili|ok\.ru|vk\.com|dailymotion|jwplayer|brightcove|kaltura|vidyard|wistia|loom|peertube|odysee|rumble|bitchute|streamable)\./i,

    // Filename stems that identify nothing (a page called "video.mp4" is not a title).
    GENERIC_NAMES: new Set([
      'video', 'movie', 'playlist', 'manifest', 'master', 'index', 'media', 'file',
      'download', 'stream', 'segment', 'chunk', 'intro', 'outro', 'sample',
      'trailer', 'clip', 'default', 'output', 'mediafile', 'videofile'
    ]),

    // First hostname labels that are infrastructure, never a site brand
    // ("player.vimeo.com" is Vimeo).
    HOST_PREFIX_NOISE: new Set([
      'www', 'm', 'mobile', 'player', 'media', 'cdn', 'static', 'v', 'video', 'watch'
    ]),

    // Windows reserves these device names with or without an extension.
    WINDOWS_RESERVED: /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i,

    // Filenames that indicate page chrome rather than media artwork.
    JUNK_NAME_RE: /(?:^|[/_.-])(?:sprite|logo|icon|favicon|avatar|placeholder|banner|ads?|pixel|blank|transparent)(?:[/_.-]|$)/i
  };

  // Freeze the container so consumers cannot rebind the shared definitions.
  global.DownloadAnythingConstants = Object.freeze(constants);
})(typeof self !== 'undefined' ? self : window);
