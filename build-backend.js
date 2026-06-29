const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

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
const destName = platform === 'win32' ? `main-${targetTriple}.exe` : `main-${targetTriple}`;
const destPath = path.join(binariesDir, destName);
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

if (fs.existsSync(destPath)) {
  const pyFiles = getPythonFiles(__dirname);
  pyFiles.forEach((file) => {
    const stat = fs.statSync(file);
    if (stat.mtimeMs > maxMtime) {
      maxMtime = stat.mtimeMs;
    }
  });

  const destStat = fs.statSync(destPath);
  if (destStat.mtimeMs > maxMtime) {
    console.log('Python backend sidecar is up to date (incremental check of all python files). Skipping PyInstaller build.');
    process.exit(0);
  }
}

console.log('Changes detected or sidecar missing. Compiling Python backend...');

try {
  // Run PyInstaller
  execSync('pyinstaller --onefile --clean --noupx main.py', { stdio: 'inherit' });
} catch (error) {
  console.error('Failed to run PyInstaller:', error.message);
  process.exit(1);
}

if (!fs.existsSync(binariesDir)) {
  fs.mkdirSync(binariesDir, { recursive: true });
}

const srcName = platform === 'win32' ? 'main.exe' : 'main';
const srcPath = path.join(__dirname, 'dist', srcName);

console.log(`Copying sidecar: ${srcPath} -> ${destPath}`);
try {
  fs.copyFileSync(srcPath, destPath);
} catch (error) {
  console.error('Failed to copy sidecar binary:', error.message);
  process.exit(1);
}

// Make sure it is executable on Unix
if (platform !== 'win32') {
  try {
    fs.chmodSync(destPath, '755');
  } catch (error) {
    console.error('Failed to set executable permissions:', error.message);
  }
}

console.log('Python backend sidecar built and copied successfully!');
