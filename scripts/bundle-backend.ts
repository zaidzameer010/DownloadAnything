// Stages everything `tauri build` bundles for the Python backend:
//   src-tauri/resources/backend  the `dax` package, including dax_rs.so
//   src-tauri/resources/venv     the uv venv, pyvenv.cfg relocated to `../python`
//   src-tauri/resources/python   the uv-managed CPython install
//   src-tauri/binaries/aria2-next-<triple>  the external binary Tauri renames
//
// The venv is not relocatable on its own (pyvenv.cfg and bin/python point at
// the build machine), so the staged copy gets a relative `home`. On Windows
// `Scripts/python.exe` is the venv redirector, which resolves that `home`
// against its cwd — Tauri launches it from `venv/Scripts`. On Unix the
// launcher is a symlink, replaced with a real binary and PYTHONHOME is set.

import { spawnSync } from 'node:child_process'
import {
  chmodSync,
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  readlinkSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from 'node:fs'
import { basename, dirname, isAbsolute, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(dirname(fileURLToPath(import.meta.url)))
const srcPy = join(root, 'src-py')
const resources = join(root, 'src-tauri', 'resources')
const binaries = join(root, 'src-tauri', 'binaries')
const isWindows = process.platform === 'win32'
const isMac = process.platform === 'darwin'

function run(
  cmd: string,
  args: string[],
  opts: { cwd?: string; env?: Record<string, string> } = {},
): string {
  const res = spawnSync(cmd, args, {
    cwd: opts.cwd ?? root,
    env: { ...process.env, ...opts.env },
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'inherit'],
  })
  if (res.error) throw res.error
  if (res.status !== 0) throw new Error(`${cmd} ${args.join(' ')} exited ${res.status}`)
  return (res.stdout ?? '').trim()
}

const skipCache = (src: string) =>
  !src.split(/[\\/]/).includes('__pycache__') && !src.endsWith('.pyc')

// uv's install and the venv link python3 to the build machine's absolute
// paths. They are unused (we launch venv/bin/python) and would either dangle
// or be duplicated by Tauri's resource copy, so drop them.
function dropAbsoluteSymlinks(dir: string) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name)
    if (entry.isSymbolicLink()) {
      if (isAbsolute(readlinkSync(path))) rmSync(path, { force: true })
    } else if (entry.isDirectory()) {
      dropAbsoluteSymlinks(path)
    }
  }
}

// 1. venv + the CPython version pinned by src-py/.python-version
run('uv', ['sync'], { cwd: srcPy })
const venv = join(srcPy, '.venv')
const venvPython = isWindows
  ? join(venv, 'Scripts', 'python.exe')
  : join(venv, 'bin', 'python')
if (!existsSync(venvPython)) throw new Error(`venv python missing: ${venvPython}`)

// 2. dax_rs PyO3 extension, built against the interpreter it ships with
run('cargo', ['build', '--release', '-p', 'dax_rs'], { env: { PYO3_PYTHON: venvPython } })
const dylib = isWindows ? 'dax_rs.dll' : isMac ? 'libdax_rs.dylib' : 'libdax_rs.so'
cpSync(join(root, 'target', 'release', dylib), join(srcPy, 'dax', isWindows ? 'dax_rs.pyd' : 'dax_rs.so'))

// 3. Stage the package, the venv, and the interpreter it was built from
rmSync(resources, { recursive: true, force: true })
mkdirSync(resources, { recursive: true })
cpSync(join(srcPy, 'dax'), join(resources, 'backend', 'dax'), {
  recursive: true,
  filter: skipCache,
})
cpSync(venv, join(resources, 'venv'), {
  recursive: true,
  dereference: false,
  filter: skipCache,
})
dropAbsoluteSymlinks(join(resources, 'venv'))

const pyvenv = readFileSync(join(venv, 'pyvenv.cfg'), 'utf8')
const home = pyvenv.match(/^home\s*=\s*(.+)$/m)?.[1]?.trim()
if (!home) throw new Error('pyvenv.cfg has no home entry')
// uv's Python dir is a version symlink on macOS; copy the real install.
cpSync(realpathSync(isWindows ? home : dirname(home)), join(resources, 'python'), {
  recursive: true,
})
dropAbsoluteSymlinks(join(resources, 'python'))

// 4. Relocate the staged venv: relative home. Windows keeps its redirector;
//    Unix gets a real launcher (Tauri may not preserve symlinks in resources).
writeFileSync(
  join(resources, 'venv', 'pyvenv.cfg'),
  pyvenv.replace(/^home\s*=.*$/m, `home = ${isWindows ? '..\\..\\python' : '../../python/bin'}`),
)
if (!isWindows) {
  const version = pyvenv.match(/^version_info\s*=\s*(\d+\.\d+)/m)?.[1]
  const pythonName = (() => {
    try {
      return basename(readlinkSync(venvPython))
    } catch {
      return version ? `python${version}` : 'python3.14'
    }
  })()
  const stagedPython = join(resources, 'venv', 'bin', 'python')
  rmSync(stagedPython, { force: true })
  cpSync(join(resources, 'python', 'bin', pythonName), stagedPython)
  chmodSync(stagedPython, 0o755)
}

// 5. aria2-next sidecar, named for the target triple Tauri expects
const triple =
  process.env.TAURI_ENV_TARGET_TRIPLE ||
  process.env.TAURI_TARGET_TRIPLE ||
  run('rustc', ['--print', 'host-tuple'])
const wanted = isWindows ? /windows/i : isMac ? /macos|darwin/i : /linux/i
const arch = process.arch === 'arm64' ? /arm64|aarch64/i : /x86_64|amd64/i
const aria2 = readdirSync(root)
  .filter((name) => name.startsWith('aria2-next-') && wanted.test(name) && arch.test(name))
  .filter((name) => (isWindows ? name.toLowerCase().endsWith('.exe') : !name.toLowerCase().endsWith('.exe')))
  .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
  .at(-1)
if (!aria2) throw new Error('aria2-next binary not found in the repo root — download it first')

mkdirSync(binaries, { recursive: true })
const aria2Dest = join(binaries, `aria2-next-${triple}${isWindows ? '.exe' : ''}`)
cpSync(join(root, aria2), aria2Dest)
chmodSync(aria2Dest, 0o755)

console.log(`staged backend  -> ${resources}`)
console.log(`staged aria2-next ${aria2} -> ${aria2Dest}`)
