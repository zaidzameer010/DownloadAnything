# DownloadAnything Chrome Extension

A Manifest V3 Chrome extension that detects video URLs on the current page, overlays a download button on `<video>` elements, and captures HLS, DASH, and direct video streams.

## Loading (unpacked)

1. Open Chrome and go to `chrome://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked** and select the `extention` folder.
4. Pin the extension from the toolbar.

## Usage

- Hover over any `<video>` element to see the download overlay in the top-right corner.
- Click the overlay to probe the page with the yt-dlp backend and pick a format/location.
- The overlay shows a native browser alert if the yt-dlp backend is not connected.

## Permissions

- `webRequest` and `<all_urls>` host permission are used to observe network traffic for stream detection.
- `storage` is used to keep per-tab media lists for the current session.

## Detection layers

- **Network (background):** `webRequest` listeners capture media by URL extension, query signature, and response `Content-Type`/`Content-Disposition`; sizes come from `Content-Length` or `Content-Range` totals.
- **Page hooks (MAIN world):** `fetch`/`XHR` body sniffing (JSON deep-scan plus regex mining of HTML/text), `Blob` manifest capture (HLS/DASH emitted as data URLs), `HTMLMediaElement`/`HTMLSourceElement` `src` traps, hls.js and dash.js player hooks, and JW Player/Video.js/SoundManager probes.
- **DOM scan (content script):** `video`/`audio`/`source` elements including open shadow roots, plus `performance` resource entries.
- **Fallbacks:** Mobile Safari UA page probe when desktop sniffing finds nothing, and signed-URL refresh (re-emits a stream URL when its query token changes).
