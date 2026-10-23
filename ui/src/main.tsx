import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { logError } from './tauri'

window.addEventListener('unhandledrejection', (e) => logError('unhandled rejection', e.reason))
window.addEventListener('error', (e) => logError('uncaught', e.message))

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
