/* Level-aware logger for the DownloadAnything extension.
 *
 * Sends info/debug/warning/error logs to the yt-dlp backend websocket and
 * also mirrors them to the browser/service-worker console.  Works in the
 * service worker, in content scripts, and in any other extension context.
 */
/* global backendWs */
/* exported DownloadAnythingLogger */
DownloadAnythingLogger = (function () {
  'use strict';

  var BACKEND_LEVELS = ['debug', 'info', 'warning', 'error'];
  var isBackground = typeof window === 'undefined' && typeof importScripts === 'function';
  var source = isBackground ? 'extension/background' : 'extension/content';

  function toLevel(level) {
    var s = String(level || 'info').toLowerCase();
    if (s === 'warn') s = 'warning';
    if (s === 'crit' || s === 'critical') s = 'error';
    return BACKEND_LEVELS.indexOf(s) >= 0 ? s : 'info';
  }

  function toConsoleArgs(message, context) {
    var args = [message];
    if (context && Object.keys(context).length) {
      args.push(context);
    }
    return args;
  }

  function consoleLog(level, message, context) {
    var args = toConsoleArgs(message, context);
    if (level === 'debug' && typeof console.debug === 'function') {
      console.debug.apply(console, args);
    } else if (level === 'info') {
      console.info.apply(console, args);
    } else if (level === 'warning') {
      console.warn.apply(console, args);
    } else if (level === 'error') {
      console.error.apply(console, args);
    } else {
      console.log.apply(console, args);
    }
  }

  function sendToBackend(level, message, context) {
    var payload = {
      type: 'log',
      level: level,
      message: message,
      context: context || {},
      client: source,
      ts: Date.now(),
    };
    var json;
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
          chrome.runtime.sendMessage(payload);
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
    debug: function (message, context) { log('debug', message, context); },
    info: function (message, context) { log('info', message, context); },
    warning: function (message, context) { log('warning', message, context); },
    warn: function (message, context) { log('warning', message, context); },
    error: function (message, context) { log('error', message, context); },
    critical: function (message, context) { log('error', message, context); },
    log: log,
  };
})();
