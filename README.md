# DownloadAnything — Enterprise Media Acquisition Engine

A high-performance media downloading ecosystem powered by **FastAPI**, **yt-dlp**, **asyncio**, and **WebSockets**, complete with a beautiful modern dark-mode dashboard and a Chrome extension helper for seamless stream capturing.

---

## 🌟 Key Features

- **Real-Time Stream Updates**: Powered by WebSockets to broadcast download progress, speeds, ETAs, file sizes, and status events instantly to the client.
- **Concurrent Download Queue**: Managed via `asyncio.Queue` with a configurable pool of concurrent download workers.
- **Stream Snatcher Chrome Extension**: Silently sniffs HLS (`.m3u8`), DASH (`.mpd`), and direct video streams (`.mp4`, etc.) from webpages, overlaying quick-action download buttons or routing streams directly to the local backend.
- **Robust yt-dlp Options**: Auto-prioritizes modern codecs (like AV1, VP9, AVC) and automatically resolves and merges streams (using `ffmpeg`).
- **Disk Persistence**: Tasks state is continuously persisted to `tmp/tasks.json` so you never lose your queue after restarts.
- **Bulk Actions**: Control several downloads simultaneously with options to pause, resume, cancel, clean up from queue, or fully delete associated files from disk.
- **Directory Routing**: Map categories (e.g., *Videos*, *Courses*, *Music*, *Cinematic*) to specific filesystem destinations or define custom paths.

---

## 🛠️ Tech Stack

- **Backend**: FastAPI, Python 3.10+, `yt-dlp`, `pydantic`, `websockets`, `asyncio`
- **Frontend**: Vanilla HTML5, Custom CSS Variables, JavaScript (ES6 Modules), Lucide Icons
- **Browser Extension**: Chrome Extension Manifest V3, Content scripts (Stream sniffer), Background Service Worker

---

## 🚀 Getting Started

### 1. Backend Installation & Startup
Clone the repository and install the Python dependencies:

```bash
uv pip install fastapi uvicorn yt-dlp pydantic
```

Start the FastAPI backend server on port 8000:

```bash
fastapi run
```
The dashboard will be accessible at [http://127.0.0.1:8000](http://127.0.0.1:8000).

### 2. Load the Chrome Extension ("Stream Snatcher")
1. Open Google Chrome and navigate to `chrome://extensions/`.
2. Enable **Developer mode** using the toggle in the top-right corner.
3. Click **Load unpacked** in the top-left corner.
4. Select the `extension` directory inside this project folder.
5. Stream Snatcher will now run in the background, sniffing network traffic for stream links and showing helpful download overlays on stream-capable pages.

---

## ⚙️ Configuration & Settings

All settings are configured through the backend dashboard or stored in `settings.json`:
- **Max Concurrent Downloads**: Control CPU/network utilization by limiting active parallel worker tasks.
- **Merge Container**: Format option (`mp4`, `mkv`, `webm`) to automatically stitch downloaded audio/video streams together.
- **Categories**: Map specific folders on your system to download categories so files are automatically sorted upon download.

---

## 📂 Project Structure

- `main.py`: FastAPI server containing worker queues, WebSocket publishers, API routes, and yt-dlp configurations.
- `index.html`: Modern, single-page dashboard layout.
- `static/`: Contains dashboard-specific JS logic (`js/app.js`) and UI layouts (`css/style.css`, `css/components.css`).
- `extension/`: Chrome Extension implementation files (`manifest.json`, background sniffer, page content injector).
- `settings.json`: Local project configuration settings.
- `tmp/`: Stores fragment temp download cache files and the persistent queue list (`tasks.json`).
