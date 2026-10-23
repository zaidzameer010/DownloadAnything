// dax extension settings — token + port pairing with the desktop engine.

const $ = (id) => document.getElementById(id);

const statusBadge = $('status-badge');
const statusLabel = $('status-label');
const statusLine = $('status-line');
const tokenInput = $('token');
const portInput = $('port');
const btnSave = $('btn-save');
const btnToggleToken = $('btn-toggle-token');
const btnPasteToken = $('btn-paste-token');

function setStatus(state, label, detail = '') {
  statusBadge.className = `status-badge${state ? ` ${state}` : ''}`;
  statusLabel.textContent = label;
  statusLine.textContent = detail;
}

async function checkConnection() {
  setStatus('', 'Checking…', 'Handshaking…');
  try {
    const res = await chrome.runtime.sendMessage({ type: 'testConnection' });
    if (res?.ok) {
      const latency = res.data?.latencyMs != null ? `${res.data.latencyMs}ms` : '';
      const version = res.data?.engine?.version || 'yt-dlp';
      setStatus('connected', 'Connected', `${version} running${latency ? ` · ${latency}` : ''}`);
    } else {
      const err = res?.error || 'Unreachable — is dax running?';
      setStatus('error', err.toLowerCase().includes('token') ? 'Bad Token' : 'Offline', err);
    }
  } catch (e) {
    setStatus('error', 'Offline', String(e?.message || e));
  }
}

async function save() {
  await chrome.storage.local.set({
    apiToken: tokenInput.value.trim(),
    apiPort: Number(portInput.value) || 16801,
  });

  btnSave.classList.add('saved');
  btnSave.textContent = 'Saved ✓';
  setTimeout(() => {
    btnSave.classList.remove('saved');
    btnSave.textContent = 'Save';
  }, 1500);

  checkConnection();
}

async function load() {
  const c = await chrome.storage.local.get({ apiToken: '', apiPort: 16801 });
  tokenInput.value = (c.apiToken || '').trim();
  portInput.value = Number(c.apiPort) || 16801;
  if (tokenInput.value) checkConnection();
  else setStatus('', 'No Token', 'Paste the token from dax → Options → Engine');
}

btnToggleToken.addEventListener('click', (e) => {
  e.preventDefault();
  const show = tokenInput.type === 'password';
  tokenInput.type = show ? 'text' : 'password';
  btnToggleToken.textContent = show ? 'Hide' : 'Show';
});

btnPasteToken.addEventListener('click', async (e) => {
  e.preventDefault();
  try {
    const text = await navigator.clipboard.readText();
    if (text) tokenInput.value = text.trim();
  } catch {
    tokenInput.focus();
    tokenInput.select();
  }
});

btnSave.addEventListener('click', save);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.target === tokenInput || e.target === portInput)) save();
});

load();
