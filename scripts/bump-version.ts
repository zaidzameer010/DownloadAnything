// Sets one version in every place the repo declares it: the manifests plus the
// Cargo.lock/uv.lock entries mirroring the workspace members, which the
// resolvers only rewrite on their next run.
//
//   bun scripts/bump-version.ts 0.8.0
//   bun scripts/bump-version.ts minor
//   bun scripts/bump-version.ts 0.8.0 --dry-run

import { readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(dirname(fileURLToPath(import.meta.url)))
const SEMVER = /^(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/
// tauri.conf.json is what the updater compares against, so it seeds `current`.
const SOURCE = join(root, 'src-tauri', 'tauri.conf.json')

type Edit = (source: string, version: string, file: string) => string

function nextVersion(current: string, input: string): string {
  const explicit = input.replace(/^v/, '')
  if (SEMVER.test(explicit)) return explicit
  const match = current.match(SEMVER)
  if (!match) throw new Error(`${SOURCE} version "${current}" is not semver`)
  const [major, minor, patch] = [Number(match[1]), Number(match[2]), Number(match[3])]
  if (input === 'major') return `${major + 1}.0.0`
  if (input === 'minor') return `${major}.${minor + 1}.0`
  if (input === 'patch') return `${major}.${minor}.${patch + 1}`
  throw new Error(`"${input}" is not a version or one of patch|minor|major`)
}

function swap(source: string, pattern: RegExp, version: string, file: string): string {
  if (!pattern.test(source)) throw new Error(`${file}: version line not found`)
  return source.replace(pattern, (_match: string, head: string, tail: string) => `${head}${version}${tail}`)
}

const jsonVersion: Edit = (source, version, file) =>
  swap(source, /^(\s*"version"\s*:\s*")[^"]*(")/m, version, file)

// Scoped to the table so dependency `version` keys are never touched.
const tomlVersion = (table: string): Edit => (source, version, file) => {
  const lines = source.split('\n')
  let inTable = false
  for (let i = 0; i < lines.length; i++) {
    const header = lines[i].match(/^\s*\[([^\[\]]+)\]\s*$/)
    if (header) {
      inTable = header[1] === table
      continue
    }
    const line = inTable ? lines[i].match(/^(\s*version\s*=\s*")[^"]*(")/) : null
    if (!line) continue
    lines[i] = `${line[1]}${version}${line[2]}`
    return lines.join('\n')
  }
  throw new Error(`${file}: no version in [${table}]`)
}

const lockVersion = (names: ReadonlyArray<string>): Edit => (source, version, file) => {
  let out = source
  for (const name of names) {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const pattern = new RegExp(`(\\[\\[package\\]\\]\\r?\\nname = "${escaped}"\\r?\\nversion = ")[^"]*(")`)
    out = swap(out, pattern, version, file)
  }
  return out
}

const TARGETS: ReadonlyArray<readonly [string, Edit]> = [
  ['src-tauri/tauri.conf.json', jsonVersion],
  ['ui/package.json', jsonVersion],
  ['ext/manifest.json', jsonVersion],
  ['src-rs/Cargo.toml', tomlVersion('package')],
  ['src-tauri/Cargo.toml', tomlVersion('package')],
  ['src-py/pyproject.toml', tomlVersion('project')],
  ['Cargo.lock', lockVersion(['app', 'downloadanything_rs'])],
  ['src-py/uv.lock', lockVersion(['downloadanything-py'])],
]

const args = process.argv.slice(2)
const dryRun = args.includes('--dry-run')
const input = args.find((arg) => !arg.startsWith('-'))

if (!input) {
  console.error('usage: bun scripts/bump-version.ts <version|patch|minor|major> [--dry-run]')
  process.exit(1)
}

try {
  const { version: current } = JSON.parse(readFileSync(SOURCE, 'utf8'))
  if (typeof current !== 'string') throw new Error(`${SOURCE} has no version`)
  const next = nextVersion(current, input)
  let changed = 0
  for (const [path, edit] of TARGETS) {
    const full = join(root, path)
    const before = readFileSync(full, 'utf8')
    const after = edit(before, next, path)
    if (after === before) continue
    if (!dryRun) writeFileSync(full, after)
    changed++
    console.log(`${dryRun ? 'would update' : 'updated'} ${path}`)
  }
  console.log(changed ? `${current} -> ${next} (${changed} files${dryRun ? ', dry run' : ''})` : `already at ${next}`)
} catch (error) {
  console.error(`error: ${error instanceof Error ? error.message : String(error)}`)
  process.exit(1)
}
