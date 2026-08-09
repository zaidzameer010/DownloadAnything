# DownloadAnything

DownloadAnything is a desktop download manager for macOS and Windows. It can grab videos, playlists, audio, direct HTTP files, and torrents — usually by just pasting a link.

## What it can download

- Videos and playlists from any site [yt-dlp](https://github.com/yt-dlp/yt-dlp) supports (YouTube, TikTok, Vimeo, etc.)
- Direct media and file links
- HLS / DASH streams detected by the Chrome extension
- Magnet links and `.torrent` files (libtorrent)
- Audio-only versions of videos when available

## Install

1. Download the latest release for your platform from the [Releases](https://github.com/zaidzameer010/DownloadAnything/releases) page.
   - macOS Apple Silicon: `.dmg`
   - Windows: `-setup.exe`
2. Open the installer and move or install the app.
3. On macOS, if you see a Gatekeeper warning, right-click the app and choose **Open** the first time. (For a fully signed build, an Apple Developer ID certificate is required; the current release uses ad-hoc signing.)
4. Launch the app. The local backend starts automatically.

## First launch

- The main window has two tabs in the sidebar: **Downloads** and **Settings**.
- A status chip in the sidebar shows **Backend online** or **Backend offline**. The app cannot add or monitor downloads while the backend is offline.
- If an update is available, a banner appears at the top. Choose **Install & restart** to apply it.

## Add a download

1. Copy the URL you want to download.
2. In DownloadAnything, paste it into the URL field at the top and press **Enter** or click **Probe**.
3. The app inspects the link and shows a dialog with available formats, title, thumbnail, and file size.
4. Pick a quality and a download location, then click **Download**.
5. The job appears in the list with live progress, speed, and ETA.

For `.torrent` files, you can also double-click a `.torrent` file (if the app is associated with it) to queue it. Magnet links can be pasted directly into the URL field.

## Chrome extension

The included Chrome extension can detect videos on web pages without pasting URLs manually.

1. Open Chrome and go to `chrome://extensions`.
2. Turn on **Developer mode**.
3. Click **Load unpacked** and select the `extention` folder next to this app.
4. Pin the extension.
5. Hover over a video on a page. A download overlay appears in the top-right corner. Click it to send the link to DownloadAnything.

The extension captures direct `<video>` elements, HLS, and DASH streams.

## Manage downloads

Each job in the list shows:

- Status: queued, downloading, postprocessing, completed, paused, failed, or cancelled
- Progress bar and percentage
- Downloaded / total size
- Current speed and ETA
- Engine used (yt-dlp, aria2, or libtorrent)

Right-click or use the row actions to:

- Open the output folder
- Copy the file path
- Pause / resume
- Cancel
- Retry a failed job
- Remove a job from the list

Filters above the list let you show all, active, completed, or failed jobs.

## Settings

Open **Settings** from the sidebar to configure the app.

### General
- Default download directory
- Concurrent downloads (requires a restart)
- Rate limit
- Concurrent fragments
- Retries
- Proxy
- Cookies from a browser (Chrome, Firefox, Safari, Edge, Brave)

### Network
- aria2 connection and split settings
- Listen port
- File allocation mode
- Extra aria2 arguments

### Engines
- yt-dlp options
- libtorrent / BitTorrent settings (DHT, peer exchange, UPnP, etc.)
- aria2-next and torrent limits

### Post-processing
- Embed metadata, thumbnail, and subtitles
- Merge output format (mp4, mkv, webm, mov)

### Locations
- Preset download folders that appear in the format dialog
- Default save path

### About
- Current version
- Check for updates
- Install available updates

## Updates

DownloadAnything includes an auto-updater. On each launch it checks the latest GitHub release and prompts you to install an update when one is available. You can also check manually from **Settings → About**.

## Troubleshooting

**Backend offline**
- Restart the app.
- On Windows, check that your antivirus or firewall is not blocking the embedded backend.
- Make sure you are running a packaged release, not a raw download folder.

**Download fails immediately**
- Check that the URL is reachable in your browser.
- If it is a site that requires a login, set the matching browser in **Settings → General → Cookies from browser**.
- Some streaming sites change frequently; updating yt-dlp may help.

**No formats shown after probing**
- The URL may not be supported by yt-dlp, or the page may be geo-blocked or require cookies.
- Try a different URL or set a proxy / browser cookies in Settings.

**Chrome extension does nothing**
- Make sure the app is running and the backend is online.
- Reload the extension from `chrome://extensions`.
- Refresh the page after loading the extension.

**.torrent files do not open in the app**
- Use the URL field and paste the magnet link instead, or open the `.torrent` file from the app manually if file association is not set.

## Privacy

DownloadAnything does not phone home. The only network traffic is:

- The actual downloads you start
- Checking for app updates from GitHub Releases (if the auto-updater is enabled)
- Optional `webRequest` used by the Chrome extension to detect media on the pages you visit while the extension is active

No usage analytics or telemetry are collected.
