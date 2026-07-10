import { execSync } from 'child_process';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Determine target triple
let targetTriple = '';
const platform = process.platform;

if (platform === 'darwin') {
  targetTriple = 'aarch64-apple-darwin';
} else if (platform === 'win32') {
  targetTriple = 'x86_64-pc-windows-msvc';
} else {
  console.error(`Unsupported platform: ${platform}`);
  process.exit(1);
}

const binariesDir = path.join(__dirname, 'src-tauri', 'binaries');
const destPath = path.join(binariesDir, 'backend');
const destExecutable = path.join(destPath, platform === 'win32' ? 'main.exe' : 'main');
// Incremental build check: skip if sidecar exists and is newer than any source .py files
let maxMtime = 0;
const getPythonFiles = (dir) => {
  let results = [];
  if (!fs.existsSync(dir)) return results;
  const list = fs.readdirSync(dir);
  list.forEach((file) => {
    const fullPath = path.join(dir, file);
    const stat = fs.statSync(fullPath);
    if (stat && stat.isDirectory()) {
      if (file !== 'node_modules' && file !== '.git' && file !== 'dist' && file !== 'build' && file !== 'src-tauri') {
        results = results.concat(getPythonFiles(fullPath));
      }
    } else if (file.endsWith('.py')) {
      results.push(fullPath);
    }
  });
  return results;
};

if (fs.existsSync(destExecutable)) {
  const pyFiles = getPythonFiles(__dirname);
  pyFiles.forEach((file) => {
    const stat = fs.statSync(file);
    if (stat.mtimeMs > maxMtime) {
      maxMtime = stat.mtimeMs;
    }
  });

  const destStat = fs.statSync(destExecutable);
  if (destStat.mtimeMs > maxMtime) {
    console.log('Python backend is up to date (incremental check of all python files). Skipping PyInstaller build.');
    process.exit(0);
  }
}

console.log('Changes detected or backend missing. Compiling Python backend...');

try {
  // Run PyInstaller
  execSync('pyinstaller --onedir --clean --noupx --paths=server server/app/main.py', { stdio: 'inherit' });
} catch (error) {
  console.error('Failed to run PyInstaller:', error.message);
  process.exit(1);
}

if (!fs.existsSync(binariesDir)) {
  fs.mkdirSync(binariesDir, { recursive: true });
}

console.log(`Copying backend directory: dist/main -> ${destPath}`);
try {
  if (fs.existsSync(destPath)) {
    // Only clean up python backend files to preserve other bundled binaries (like aria2-next)
    const mainExe = path.join(destPath, 'main.exe');
    const mainBin = path.join(destPath, 'main');
    const internalDir = path.join(destPath, '_internal');
    if (fs.existsSync(mainExe)) fs.rmSync(mainExe, { force: true });
    if (fs.existsSync(mainBin)) fs.rmSync(mainBin, { force: true });
    if (fs.existsSync(internalDir)) fs.rmSync(internalDir, { recursive: true, force: true });

    // Remove legacy unrenamed aria2-next-* files and old aria2c / aria2c.exe to avoid duplicates
    const destFiles = fs.readdirSync(destPath);
    destFiles.forEach((file) => {
      if (file.startsWith('aria2-next-') || file === 'aria2c' || file === 'aria2c.exe') {
        fs.rmSync(path.join(destPath, file), { force: true });
      }
    });
  } else {
    fs.mkdirSync(destPath, { recursive: true });
  }
  fs.cpSync(path.join(__dirname, 'dist', 'main'), destPath, { recursive: true });

  // Copy aria2-next binaries into the backend directory directly as renamed files
  const files = fs.readdirSync(__dirname);
  files.forEach((file) => {
    if (file.startsWith('aria2-next-')) {
      if (file.includes('macos') || file.includes('darwin')) {
        const destFile = path.join(destPath, 'aria2-next');
        fs.copyFileSync(path.join(__dirname, file), destFile);
        fs.chmodSync(destFile, '755');
      } else if (file.includes('windows') || file.endsWith('.exe')) {
        const destFile = path.join(destPath, 'aria2-next.exe');
        fs.copyFileSync(path.join(__dirname, file), destFile);
      }
    }
  });
} catch (error) {
  console.error('Failed to copy backend directory:', error.message);
  process.exit(1);
}

// Make sure it is executable on Unix
if (platform !== 'win32') {
  try {
    fs.chmodSync(destExecutable, '755');
  } catch (error) {
    console.error('Failed to set executable permissions:', error.message);
  }
}

console.log('Python backend built and copied successfully!');
