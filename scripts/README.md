# scripts/

## sync-version.js

Single source of truth for the static-asset cache-bust version. The integer
in `version.txt` gets propagated to:

- `mobile_terminal/static/index.html` — `terminal.js?v=N` and `styles.css?v=N`
- `mobile_terminal/static/sw.js` — `CACHE_NAME = 'terminal-vN'`
- `mobile_terminal/static/terminal.js` — `/sw.js?v=N` in the SW register call

Runs automatically before `esbuild` via `npm run build` and `npm run watch`.
Idempotent — re-running with no source change is a no-op.

### To bump the version

1. Edit `scripts/version.txt` — bump the integer by 1 (or whatever).
2. `npm run build`.
3. Commit `scripts/version.txt`, the four updated files, and the rebuilt
   bundle in one commit.

### Why an integer, not semver

Cache-bust is a "did the assets change" signal, not a release version. Semver
in `package.json` describes the Python package release. Two different concerns
intentionally kept separate.

### When sync-version fails

The script exits non-zero if any of the regex patterns find zero matches.
That means a target file moved or its structure changed and the sync would
have been silent. Fix the regex in `sync-version.js` (the patterns are
intentionally narrow so a missing match is a loud signal, not a quiet drop).
