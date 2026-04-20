#!/usr/bin/env node
/**
 * sync-version.js — single source of truth for static-asset cache-bust.
 *
 * Reads scripts/version.txt (a monotonically-increasing integer) and writes
 * it into every place the frontend cares about:
 *
 *   1. mobile_terminal/static/index.html          → terminal.js?v=N, styles.css?v=N
 *   2. mobile_terminal/static/sw.js               → CACHE_NAME = 'terminal-vN'
 *   3. mobile_terminal/static/terminal.js         → /sw.js?v=N (SW register URL)
 *
 * Run automatically by `npm run build` (and `npm run watch`) before esbuild,
 * so the bundled output and HTML always carry the same version. To bump,
 * edit scripts/version.txt and run `npm run build`. No manual edits to
 * the four target lines.
 *
 * Exits non-zero if any expected pattern is missing — that means a target
 * file moved out from under us and the sync was silent. Better to fail loud.
 */

'use strict';

const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const VERSION_FILE = path.join(__dirname, 'version.txt');
const STATIC_DIR = path.join(ROOT, 'mobile_terminal', 'static');

const TARGETS = [
    {
        file: path.join(STATIC_DIR, 'index.html'),
        replacements: [
            // <link rel="stylesheet" href="/static/styles.css?v=N">
            { pattern: /(styles\.css\?v=)\d+/g, replace: (v) => `$1${v}` },
            // <script defer src="/static/terminal.js?v=N">
            { pattern: /(terminal\.js\?v=)\d+/g, replace: (v) => `$1${v}` },
        ],
    },
    {
        file: path.join(STATIC_DIR, 'sw.js'),
        replacements: [
            // const CACHE_NAME = 'terminal-vN';
            { pattern: /(CACHE_NAME\s*=\s*['"]terminal-v)\d+(['"])/g, replace: (v) => `$1${v}$2` },
        ],
    },
    {
        file: path.join(STATIC_DIR, 'terminal.js'),
        replacements: [
            // navigator.serviceWorker.register(_bp + '/sw.js?v=N', ...)
            { pattern: /(\/sw\.js\?v=)\d+/g, replace: (v) => `$1${v}` },
            // console.log('=== TERMINAL.JS vN ===') — diagnostic version
            { pattern: /(=== TERMINAL\.JS v)\d+( ===)/g, replace: (v) => `$1${v}$2` },
        ],
    },
];

function readVersion() {
    const raw = fs.readFileSync(VERSION_FILE, 'utf8').trim();
    if (!/^\d+$/.test(raw)) {
        throw new Error(`scripts/version.txt must be a positive integer, got: ${JSON.stringify(raw)}`);
    }
    return raw;
}

function syncFile(target, version) {
    const original = fs.readFileSync(target.file, 'utf8');
    let updated = original;
    let totalHits = 0;
    for (const { pattern, replace } of target.replacements) {
        const before = updated;
        updated = updated.replace(pattern, replace(version));
        // Count hits by counting the difference in matches between before and after
        const hits = (before.match(pattern) || []).length;
        if (hits === 0) {
            throw new Error(
                `sync-version: no matches for ${pattern} in ${path.relative(ROOT, target.file)}. ` +
                `Did the file structure change?`
            );
        }
        totalHits += hits;
    }
    if (updated !== original) {
        fs.writeFileSync(target.file, updated);
        return { changed: true, hits: totalHits };
    }
    return { changed: false, hits: totalHits };
}

function main() {
    const version = readVersion();
    let changedFiles = 0;
    for (const target of TARGETS) {
        const rel = path.relative(ROOT, target.file);
        const { changed, hits } = syncFile(target, version);
        const status = changed ? 'updated' : 'already in sync';
        console.log(`  ${rel}: ${hits} ref${hits === 1 ? '' : 's'} → v=${version} (${status})`);
        if (changed) changedFiles++;
    }
    console.log(`sync-version: v=${version} written; ${changedFiles} file${changedFiles === 1 ? '' : 's'} touched.`);
}

try {
    main();
} catch (err) {
    console.error(`sync-version: ${err.message}`);
    process.exit(1);
}
