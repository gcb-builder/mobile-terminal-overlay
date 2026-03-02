# Plan: Spawn Team from Mobile UI

## Context
Users can't spawn agent teams from the mobile terminal overlay — they have to manually run `spawn-team.sh` from a terminal. Adding a "Spawn Team" button and API endpoint lets users spin up a full team (leader + agents with git branches) from their phone.

## Changes

### 1. Backend: `POST /api/team/spawn` (server.py, after `/api/team/dispatch` ~line 5439)

Replicates `spawn-team.sh` logic in Python:
- **Params** (JSON body): `repo_path`, `agent_names` (comma-separated, default "eval, back"), `base_branch` (optional)
- **Flow:**
  1. Auth check (standard pattern)
  2. Validate repo_path exists, is a git repo
  3. Parse + sanitize agent names (alphanumeric + `-_`, max 8 agents)
  4. Check no team windows already exist (409 if leader/a-* found)
  5. Determine base branch: use current, or create `feature/team-{timestamp}` if on main/master
  6. Create tmux windows: "leader" + "a-{name}" for each agent via `_create_tmux_window()`
  7. Git checkout per-agent branches via `tmux send-keys`
  8. Start `claude` via `_send_startup_command()` with 1s delay
  9. Audit log + return `{success, base_branch, windows: [...]}`

### 2. HTML: Spawn Team Modal (index.html, after newWindowModal ~line 391)

Reuses all `new-window-*` CSS classes (zero new CSS). Fields:
- Repository `<select>` (same optgroup pattern as New Window)
- Agent names `<input>` (default "eval, back")
- Base branch `<input>` (optional, placeholder explains auto-detect)
- Cancel / Spawn buttons

### 3. Frontend JS (terminal.js)

- **DOM refs** for modal elements (near existing modal refs ~line 604)
- **`showSpawnTeamModal()`** — populate repo select via `loadRepos()`, show modal
- **`submitSpawnTeam()`** — POST to `/api/team/spawn`, toast result, `setTimeout` 2s then `updateTeamState()` + switch to team view
- **`setupSpawnTeamModal()`** — wire close/cancel/submit/backdrop-click/Enter handlers
- **`updateActionBar()` modification** — when `!hasTeam` and on log/terminal view, prepend a "Spawn Team" button before the standard buttons

### 4. Cache bust
- Bump `terminal.js?v=263` → `v=264` in index.html

## Files
- `mobile_terminal/server.py` — new endpoint (~70 lines)
- `mobile_terminal/static/index.html` — modal HTML (~25 lines)
- `mobile_terminal/static/terminal.js` — modal logic + action bar change (~100 lines)

## Verification
1. Start server, open mobile UI
2. Confirm "Spawn Team" button appears in action bar (log/terminal view, no team)
3. Tap Spawn Team → modal opens with repo selector and agent names
4. Submit → verify leader + a-eval + a-back windows appear in tmux
5. Verify each window has its own git branch and claude starts
6. Verify UI auto-switches to team view after spawn
7. Verify button disappears once team exists
