/* Level-aware logger for the DownloadAnything extension.
 *
 * Sends info/debug/warning/error logs to the yt-dlp backend websocket and
 * also mirrors them to the browser/service-worker console.  Works in the
 * service worker, in content scripts, and in any other extension context.
 */
/* global backendWs */
const DownloadAnythingLogger = (() => {
  'use strict';

  const BACKEND_LEVELS = ['debug', 'info', 'warning', 'error'];
  const isBackground = typeof window === 'undefined' && typeof importScripts === 'function';
  const source = isBackground ? 'extension/background' : 'extension/content';

  function toLevel(level) {
    let s = String(level || 'info').toLowerCase();
    if (s === 'warn') s = 'warning';
    if (s === 'crit' || s === 'critical') s = 'error';
    return BACKEND_LEVELS.includes(s) ? s : 'info';
  }

  function toConsoleArgs(message, context) {
    const args = [message];
    if (context && Object.keys(context).length) {
      args.push(context);
    }
    return args;
  }

  function consoleLog(level, message, context) {
    const args = toConsoleArgs(message, context);
    if (level === 'debug' && typeof console.debug === 'function') {
      console.debug(...args);
    } else if (level === 'info') {
      console.info(...args);
    } else if (level === 'warning') {
      console.warn(...args);
    } else if (level === 'error') {
      console.error(...args);
    } else {
      console.log(...args);
    }
  }

  function sendToBackend(level, message, context) {
    const payload = {
      type: 'log',
      level: level,
      message: message,
      context: context || {},
      client: source,
      ts: Date.now(),
    };
    let json;
    try {
      json = JSON.stringify(payload);
    } catch (e) {
      console.error('DownloadAnythingLogger: failed to serialize log', e);
      return;
    }

    if (isBackground) {
      if (typeof backendWs !== 'undefined' && backendWs && backendWs.readyState === WebSocket.OPEN) {
        try {
          backendWs.send(json);
        } catch {
          // Network gone; console is enough.
        }
      }
    } else {
      if (typeof chrome !== 'undefined' && chrome.runtime && typeof chrome.runtime.sendMessage === 'function') {
        try {
          chrome.runtime.sendMessage(payload).catch(() => {});
        } catch {
          // Extension context unavailable.
        }
      }
    }
  }

  function log(level, message, context) {
    if (message == null) message = '';
    message = String(message);
    level = toLevel(level);
    consoleLog(level, message, context);
    sendToBackend(level, message, context);
  }

  return {
    debug: (message, context) => log('debug', message, context),
    info: (message, context) => log('info', message, context),
    warning: (message, context) => log('warning', message, context),
    warn: (message, context) => log('warning', message, context),
    error: (message, context) => log('error', message, context),
    critical: (message, context) => log('error', message, context),
    log,
  };
})();
