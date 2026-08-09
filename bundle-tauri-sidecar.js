import { chmodSync, copyFileSync, cpSync, existsSync, mkdirSync, readdirSync, rmSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const projectRoot = dirname(fileURLToPath(import.meta.url));
const isWindows = process.platform === 'win32';

const targetResult = spawnSync('rustc', ['--print', 'host-tuple'], {
	encoding: 'utf8',
});
const targetTriple = process.env.TAURI_TARGET_TRIPLE || targetResult.stdout.trim();
if (targetResult.error || !targetTriple) {
	throw new Error('Could not determine the Rust target triple. Set TAURI_TARGET_TRIPLE or install rustc.');
}

// PyInstaller onedir output is a directory named after the executable.
const executableName = `downloadanything-backend${isWindows ? '.exe' : ''}`;
const backendSourceDir = join(projectRoot, 'build', 'backend', executableName.replace(/\.exe$/, ''));
const backendExecutable = join(backendSourceDir, executableName);

// Incremental build check: get modification times of python source files, spec, and requirements
const getPythonFiles = (dir) => {
	let results = [];
	if (!existsSync(dir)) return results;
	const list = readdirSync(dir, { withFileTypes: true });
	for (const entry of list) {
		const fullPath = join(dir, entry.name);
		if (entry.isDirectory()) {
			if (
				entry.name !== 'node_modules' &&
				entry.name !== '.git' &&
				entry.name !== 'dist' &&
				entry.name !== 'build' &&
				entry.name !== 'src-tauri'
			) {
				results = results.concat(getPythonFiles(fullPath));
			}
		} else if (entry.name.endsWith('.py')) {
			results.push(fullPath);
		}
	}
	return results;
};

let skipBuild = false;
if (existsSync(backendExecutable)) {
	let maxMtime = 0;
	const sources = [
		...getPythonFiles(join(projectRoot, 'backend')),
		join(projectRoot, 'requirements.txt'),
		join(projectRoot, 'downloadanything-backend.spec'),
	];
	for (const src of sources) {
		if (existsSync(src)) {
			const stat = statSync(src);
			if (stat.mtimeMs > maxMtime) {
				maxMtime = stat.mtimeMs;
			}
		}
	}

	const destStat = statSync(backendExecutable);
	if (destStat.mtimeMs > maxMtime) {
		console.log('Python backend is up to date (incremental check of python files). Skipping PyInstaller build.');
		skipBuild = true;
	}
}

if (!skipBuild) {
	console.log('Running PyInstaller build...');
	const pyinstallerFlags = [
		'--name',
		'downloadanything-backend',
		'--paths',
		'.',
		'--paths',
		'backend',
		'--collect-all',
		'yt_dlp',
		'--exclude-module',
		'backend.tests',
		'--exclude-module',
		'pytest',
		'--onedir',
		'--console',
		'--distpath',
		'build/backend',
		'--workpath',
		'build/backend/work',
		'--noconfirm',
		'--osx-entitlements-file',
		'src-tauri/Entitlements.plist',
		'backend/server.py',
	];
	const pyRes = spawnSync('python', ['-m', 'PyInstaller', ...pyinstallerFlags], { stdio: 'inherit' });

	if (pyRes.status !== 0) {
		throw new Error(`PyInstaller build failed with exit code ${pyRes.status}`);
	}
}

if (!existsSync(backendSourceDir)) {
	throw new Error(`PyInstaller output not found: ${backendSourceDir}.`);
}


const resourceDirectory = join(projectRoot, 'src-tauri', 'resources');
const backendResourceDir = join(resourceDirectory, executableName.replace(/\.exe$/, ''));
rmSync(backendResourceDir, { recursive: true, force: true });
cpSync(backendSourceDir, backendResourceDir, { recursive: true });

const backendResourceExecutable = join(backendResourceDir, executableName);
if (!isWindows) chmodSync(backendResourceExecutable, 0o755);
console.log(`Bundled backend resource: ${backendResourceDir}`);

const binariesDirectory = join(projectRoot, 'src-tauri', 'binaries');
mkdirSync(binariesDirectory, { recursive: true });

const aria2Candidates = readdirSync(projectRoot, { withFileTypes: true })
	.filter((dirent) => dirent.isFile())
	.map((dirent) => dirent.name)
	.filter((name) => {
		const lower = name.toLowerCase();
		if (!lower.startsWith('aria2-next-')) return false;
		return isWindows ? lower.endsWith('.exe') : !lower.endsWith('.exe');
	})
	.map((name) => join(projectRoot, name));

if (aria2Candidates.length === 0) {
	throw new Error(`aria2-next binary not found in ${projectRoot}. Download it first.`);
}

if (aria2Candidates.length > 1) {
	console.warn(`Multiple aria2-next binaries found; using the first: ${aria2Candidates[0]}`);
}

const aria2Source = aria2Candidates[0];
const aria2Destination = join(binariesDirectory, `aria2-next-${targetTriple}${isWindows ? '.exe' : ''}`);
copyFileSync(aria2Source, aria2Destination);
if (!isWindows) chmodSync(aria2Destination, 0o755);
console.log(`Bundled aria2-next sidecar: ${aria2Destination}`);
