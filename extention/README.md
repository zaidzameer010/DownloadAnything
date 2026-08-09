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
- The overlay shows a toast if the yt-dlp backend is not connected.

## Permissions

- `webRequest` and `<all_urls>` host permission are used to observe network traffic for stream detection.
- `storage` is used to keep per-tab media lists for the current session.
