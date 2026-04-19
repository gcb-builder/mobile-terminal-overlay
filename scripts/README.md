# scripts/

## deploy.sh

End-to-end "make my edits live" script. Runs:

1. Python import check (cheap pre-flight; catches syntax errors)
2. Frontend build via `npm run build` (sync-version + esbuild)
3. `systemctl --user restart mto`
4. Smoke test: `GET /health` and `GET /api/ws-debug` both return 200

Fails fast — a broken import or build aborts before the restart, so the
service keeps serving the previous bundle until you fix the error.

### Usage

```bash
bash scripts/deploy.sh                # build + restart + smoke test
bash scripts/deploy.sh --no-restart   # build only (e.g. testing build locally)
bash scripts/deploy.sh --skip-build   # restart + smoke test only (e.g. .py-only edits)
```

### Why this exists

`systemctl --user restart mto` alone re-execs the Python process but
does **not** rebuild the frontend bundle. If you forgot to
`npm run build` first, your JS edits sit invisible in `dist/` from a
prior build. Same trap secondbrain's `scripts/deploy-web.sh` solves —
this script copies that pattern.

Doesn't touch `tmux-claude.service` — that holds your Claude session
panes. Restart it manually only if you've changed PTY-handling code.

### Tunables

If you change the install layout, edit the constants near the top:
`SERVICE_NAME`, `HEALTH_URL`, `DEBUG_URL`, `PYTHON_VENV`.

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
