import react, { reactCompilerPreset } from '@vitejs/plugin-react'
import babel from '@rolldown/plugin-babel'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'
import { execSync } from 'node:child_process'
import { homedir } from 'node:os'
import { join } from 'node:path'

// Dev-mode WS pairing: the app talks to the backend over a single /ws socket.
// On the backend's own origin the token is injected into index.html; in vite
// dev the page has no token, so the proxy appends it server-side — read from
// the settings row in downloadanything.db the backend owns. The token never reaches the
// page, and no manual pairing step is needed.
function apiSettings(): { apiToken: string; apiPort: number } {
  try {
    const db = join(
      homedir(),
      'Library/Application Support/DownloadAnything/downloadanything.db',
    )
    const raw = execSync(
      `sqlite3 -readonly "${db}" "SELECT api_token, api_port FROM settings WHERE id=1"`,
      { stdio: ['ignore', 'pipe', 'ignore'] },
    ).toString()
    const [token, port] = raw.trim().split('|')
    return { apiToken: token ?? '', apiPort: Number(port) || 16801 }
  } catch {
    return { apiToken: '', apiPort: 16801 }
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    babel({ presets: [reactCompilerPreset()] }),
    tailwindcss(),
  ],
  server: {
    proxy: {
      '/ws': {
        target: `ws://127.0.0.1:${apiSettings().apiPort}`,
        ws: true,
        rewrite: (p) =>
          `${p}${p.includes('?') ? '&' : '?'}token=${encodeURIComponent(apiSettings().apiToken)}`,
      },
    },
  },
})
