# Mobile Terminal Overlay - Session Context

## Current State

- **Branch:** master
- **Stage:** Agent Teams: Team View with Card-Based Overview
- **Last Updated:** 2026-02-24
- **Server Version:** v254 (terminal.js), v115 (sw.js cache), v154 (styles.css)
- **Server Start:** `./venv/bin/mobile-terminal --session claude --verbose > /tmp/mto-server.log 2>&1 &`

## Active Work: Team View - Consolidated Agent Cards (2026-02-24)

### Feature: Team View with Card-Based Agent Overview
Swipeable tab (between Log and Terminal) showing card-based overview of all team agents with status, branch, tail text, and action buttons.

#### Server Endpoints
- **`GET /api/team/capture`** - Batch capture last N lines from each team pane; reuses team pane discovery + capture cache (300ms TTL)
- **`POST /api/team/send`** - Send input to a specific team pane without switching activeTarget; validates target is a team pane

#### Dynamic Tab System
- `getTabOrder()` returns `['log', 'team', 'terminal']` when team present, `['log', 'terminal']` otherwise
- `updateTabIndicator()` dynamically creates/destroys dots before `#collapseToggle`
- Static dot HTML removed from index.html; dots created at runtime
- Team presence transitions: dot appears/disappears on team state change; auto-switch to log if team disappears

#### Team Card UI
- `.team-cards-grid` - 1-column mobile, 2-column at 600px+
- `.team-card` - header (status dot + name + phase + branch pill), body (pre with last 5 lines, tap to switch), footer (Switch + Allow/Deny buttons)
- Allow/Deny sends y/n to correct pane via `/api/team/send` without switching activeTarget
- Auto-refresh every 5s when team view visible
- Team view hides viewBar + controlBars (no Select/Compose needed)

### Files Changed
- `mobile_terminal/server.py` - GET /api/team/capture, POST /api/team/send
- `mobile_terminal/static/index.html` - #teamView div, removed static tab dots, version bumps (v154/v254)
- `mobile_terminal/static/styles.css` - .team-view, .team-cards-grid, .team-card-*, responsive 2-column grid
- `mobile_terminal/static/terminal.js` - getTabOrder(), dynamic updateTabIndicator(), switchToTeamView(), card rendering, auto-refresh, team presence transitions

---

## Previous: Agent Teams - Discovery, Batch State, UI Grouping (2026-02-23)

### Feature: Team Discovery + Batch State + UI Grouping
Team-aware views for Claude agent teams using tmux window naming conventions: `leader` = team leader, `a-*` = agents.

#### Step 1: Team Discovery in /api/targets
- `team_role` ("leader" | "agent" | null) and `agent_name` fields on each target
- `has_team` boolean on response

#### Step 2: GET /api/team/state Batch Endpoint
- **`_detect_phase_for_cwd()`** - Per-pane phase detection using explicit cwd, no pgrep
- **Activity:** Log file mtime recency (< 30s) as informational `active` flag, NOT a gate
- **Always parses:** JSONL + pane_title checked regardless of activity (catches long waits)
- **`waiting_reason`:** "permission" (Signal Detection Pending) | "question" (AskUserQuestion) | null
- **`permission_tool`/`permission_target`:** Extracted from last tool_use when waiting for permission
- **`_get_git_info_cached()`:** Branch, worktree detection (.git is file), is_main warning; 10s cache per cwd
- **Cache:** Composite key `session:target:cwd:log_path`, mtime+size invalidation, 50 entry cap

#### Step 3: UI Grouping in Nav Dropdown
- **Team section** before "Current Session" with status dots, branch labels, permission badges
- **Status dots:** Color per phase (green=working, blue=planning, orange=waiting, purple=agent, grey=idle)
- **Branch labels:** Truncated at 20 chars, red for main/master, worktree tooltip
- **Permission badge:** Orange `!` circle only for permission waits (not questions)
- **Non-team panes** filtered out of Team section, shown in normal "Current Session"
- **Polling:** `updateTeamState()` in health poll `Promise.all`, re-renders dropdown if visible

#### spawn-team.sh
Shell script for safe team creation with guardrails:
- Session validation, main-branch protection (auto-creates feature branch)
- Per-agent branches (`<base>-leader`, `<base>-a-eval`, etc.)
- Duplicate window detection, `--kill` cleanup mode
- Starts `claude --worktree` in each window

### Files Changed
- `mobile_terminal/server.py` - team_role/agent_name in /api/targets, _detect_phase_for_cwd(), _get_git_info_cached(), GET /api/team/state
- `mobile_terminal/static/terminal.js` - teamState variable, updateTeamState(), Team section in populateRepoDropdown()
- `mobile_terminal/static/styles.css` - .team-dot, .team-phase, .team-branch, .team-perm-badge, .team-agent layout
- `mobile_terminal/static/index.html` - Version bumps: styles.css?v=153, terminal.js?v=253
- `spawn-team.sh` - Team creation script

---

## Previous: Agent-Native Features (2026-02-18)

### Status Strip
- `GET /api/status/phase` with `(log_path, mtime, size)` cache key
- Phase detection: last 8KB JSONL + pane_title + tool_use scan
- Action buttons: "Approve" (waiting), "History" (idle transition)

### Push Completed/Crashed
- Idle transition detection (20s), crash detection (10s debounce)
- Per-type push notification actions

### Artifacts & Replay
- Event-driven auto-capture (1 per 30s), minimal payload, lazy heavy fields
- Annotation, per-target scoping, timeline UI

---

## Previous: Workspace Directory Picker (2026-02-17)

### Feature
The "New Window in Repo" modal now also shows directories under configurable `workspace_dirs` (e.g. `~/dev/`), so you can open a tmux window in any project without pre-configuring each one in YAML.

### Changes
- `config.py`: Added `workspace_dirs: List[str]` field, parsing, serialization, merge
- `server.py`: Added `GET /api/workspace/dirs` endpoint (scans workspace dirs, excludes hidden + already-configured repos, limit 200)
- `server.py`: Modified `POST /api/window/new` to accept `path` as alternative to `repo_label` (validates path is under a workspace_dir)
- `terminal.js`: Updated `showNewWindowModal()` to fetch workspace dirs and render `<optgroup>` sections
- `terminal.js`: Updated `createNewWindow()` to parse `repo:` vs `dir:` value prefixes
- `terminal.js`: Updated `hasRepos` checks to also consider `workspace_dirs`

### Config
```yaml
workspace_dirs:
  - "~/dev"
```

---

## Previous: Terminal Responsiveness + Garbled Output (2026-02-04)

### Problem
1. Terminal takes ~30-90s to become responsive on mobile
2. Terminal view shows garbled output at start (tail/log view is OK)

### NEW FIX (v245): ANSI-Safe Boundary Detection

**Root cause found:** Client-side `enqueueSplit()` was splitting incoming binary data
at arbitrary 2KB boundaries, which could split ANSI escape sequences (like `\x1b[38;2;255;128;64m`)
in the middle. This causes xterm.js to receive incomplete sequences, resulting in garbled output.

**Fix implemented:**
1. Added `findSafeBoundary()` function that scans backwards from cut position to find safe split points
2. Detects incomplete ANSI sequences by looking for ESC (0x1B) without terminator
3. Also avoids splitting UTF-8 continuation bytes (0x80-0xBF)
4. Increased CHUNK_SIZE from 2KB to 8KB to reduce splitting frequency

**Diagnostic logging added:**
- `=== TERMINAL.JS v245 EPOCH SYSTEM LOADED ===` at script start
- `[v245] WebSocket connected (mode=..., epoch=...)` on socket open
- `[MODE] v245 Switching ... -> ... (epoch=...)` on mode changes
- `[TERMINAL] v245 Writing ... bytes (epoch=..., first chunk)` on first terminal write

### Files Modified (v245)
- `mobile_terminal/static/terminal.js`:
  - Added `findSafeBoundary()` function for ANSI-safe chunking
  - Modified `enqueueSplit()` to use safe boundaries
  - Increased CHUNK_SIZE to 8192 (8KB)
  - Added distinctive v245 diagnostic logging
- `mobile_terminal/static/index.html` - v245
- `mobile_terminal/static/sw.js` - v108

### Verification Steps for User
1. Open browser dev tools (Console tab)
2. Reload the page
3. Should see: `=== TERMINAL.JS v245 EPOCH SYSTEM LOADED ===`
4. Switch to Terminal view
5. Should see: `[MODE] v245 Switching tail -> full (epoch=1)`
6. If you DON'T see these logs, clear site data and unregister Service Worker

### Previous Fixes (v244) - Still Active
- Mode epoch cancellation system
- Mode-gated writes (all data gated behind `outputMode === 'full'`)
- Bytes-only pipeline (no string splitting)

---

## Recent: Target Switch Fixes and Loading Indicators (2026-01-31)

### Root Cause Fix
- **Bug:** tmux target format was wrong - used `session:window:pane` instead of `session:window.pane`
- **Fix:** Added `get_tmux_target()` helper to convert `window:pane` (e.g., "2:0") to tmux format `session:window.pane` (e.g., "claude:2.0")
- Fixed in: select-pane, capture-pane, WebSocket handler, /api/refresh, /api/terminal/capture

### Target Switch Improvements
- `/api/target/select` now blocking with verification (polls up to 500ms)
- Closes PTY and kills child process on target switch
- Closes WebSocket with code 4003 to force client reconnect
- Added `target_epoch` counter for cache invalidation
- Clears output buffer on switch

### Loading Indicators
- "Switching to target..." - shown immediately when user taps target
- "Connected, loading..." - shown when WebSocket connects
- "Loading terminal..." - shown after hello handshake
- Overlay hides when terminal data arrives

### Nav Label Fix
- **Bug:** Label always showed first matching repo (by session), ignoring which pane was selected
- **Fix:** Now matches repos by target's `cwd` path, falls back to directory name if no repo matches

---

## Recent: Startup Automation, Session Recovery, Layout Hints (2026-01-26)

### Per-Repo Startup Automation
- Added `startup_command` and `startup_delay_ms` fields to Repo config
- `/api/repos` now returns startup settings
- `/api/window/new` uses repo's startup_command when auto_start enabled
- Validation: no newlines, max 200 chars
- Uses `tmux send-keys -l` (literal mode) + separate Enter for safety

### Session Recovery (Claude Health Monitoring)
- `GET /api/health/claude?pane_id=...` - Check if Claude is running
  - Returns: `{pane_alive, shell_pid, claude_running, claude_pid, pane_title}`
  - Scans process tree for claude-code process
- `POST /api/claude/start?pane_id=...` - Start Claude in pane
  - Returns 409 if already running
  - Uses repo's startup_command if available
- Client-side health polling every 5s (when visible)
- Crash banner with respawn button after 3s debounce
- Per-pane dismiss tracking

### Layout Convention Hints
- New windows default to directory basename (not repo label)
- Target dropdown shows "?" hint badge when window name doesn't match directory
- Helps identify mismatched layouts

---

## Recent: New Window in Repo Feature (2026-01-25)

### Feature
Create new tmux windows in configured repos directly from the mobile overlay, with optional auto-start Claude.

### New Endpoints
- `POST /api/window/new` - Create new tmux window in repo's session
  - Body: `{repo_label, window_name?, auto_start_claude?}`
  - Returns: `{success, target_id, pane_id, window_name, session}`
- `GET /api/repos` - List configured repos with path existence status

### Security
- Only repos defined in `.mobile-terminal.yaml` are allowed
- Server-side window name sanitization: `[a-zA-Z0-9_.-]`, max 50 chars
- Random suffix added to prevent collisions
- All subprocess calls use list args (no shell=True)
- Actions audit logged

### Client UI
- "+ New Window in Repo..." option in target dropdown
- Modal with repo selector, window name input, auto-start Claude checkbox
- Auto-select new target after creation (with retry logic)

---

## Recent: Server Auto-Restart on Resume (2026-01-25)

### Problem
When PWA resumes from background, server code changes aren't picked up until manual restart. This breaks the mobile workflow.

### Solution
Safe server restart endpoint that PWA calls automatically on failed reconnect.

### Endpoint: POST /api/restart
- **Auth:** Same token mechanism as other APIs
- **Response:** 202 `{"status": "restarting"}` on success
- **Debounce:** 429 `{"error": "Restart too soon", "retry_after": N}` if within 30s
- **Logging:** Logs client IP and restart mechanism used

### Restart Mechanism Priority
1. **systemd (preferred):** `systemctl --user restart mobile-terminal.service`
   - Checks if service is active first
   - Clean restart, maintains socket activation benefits
2. **execv (fallback):** `os.execv(sys.executable, [sys.executable] + sys.argv)`
   - Used when systemd unavailable
   - Not compatible with uvicorn --reload or multiple workers

### Client Behavior (terminal.js)
1. On `visibilitychange` → visible: attempt normal reconnect
2. If still disconnected after 2.5s: call POST /api/restart
3. Client-side cooldown: 60s between restart attempts
4. On 202 response: wait 1.5s, then reconnect

### Safety Features
- Server-side 30s debounce prevents restart loops
- Client-side 60s cooldown provides additional protection
- tmux/Claude sessions completely unaffected (only web server restarts)
- Restart happens in background task after response flushes

## Active Work: Session-to-Log Mapping + Manual Selection (Implemented)

### Problem
When multiple Claude Code instances run in the same directory (different tmux sessions), the `/api/log` endpoint picks the most recently modified `.jsonl` file, which may not match the session being viewed.

### Solution Implemented
Target-to-log mapping using `pane_id` as the key with pinning support:

```python
app.state.target_log_mapping = {}  # Maps pane_id -> {"path": str, "pinned": bool}
```

**Auto-detection strategy** in `detect_target_log_file()`:
1. Check cached/pinned mapping first (pinned = user manually selected)
2. Find Claude process in target pane via `pgrep -P {pane_pid} -x claude`
3. Get process start time from `/proc/{pid}/stat`
4. **Strategy A: Debug file correlation** (most reliable)
   - Check `~/.claude/debug/*.txt` files
   - Match debug file first-line timestamp to Claude process start time (within 5s)
   - Debug file UUID = log file UUID
5. Strategy B: Match against first entry timestamp in each `.jsonl` file (within 60s)
6. Fallback: Most recently modified file not assigned elsewhere
7. Cache the mapping for future requests (pinned=False)

**Manual session selection** (2026-01-25):
- `GET /api/log/sessions` - List all log files with metadata (preview, timestamps, size)
- `POST /api/log/select?session_id=UUID` - Pin a specific log to current target
- `POST /api/log/unpin` - Revert to auto-detection

**Key fix (2026-01-25):**
- Fixed tmux target format: `session:window.pane` (e.g., `claude:0.0`) not `session:window:pane`

**Cache invalidation:**
- On target selection change (`/api/target/select`) - only if not pinned
- On session switch (`/api/switch`)
- On explicit unpin (`/api/log/unpin`)

### Files Modified
- `mobile_terminal/server.py`: Added `detect_target_log_file()`, `target_log_mapping` state, session selector endpoints, docs endpoints

## Docs Modal (2026-01-25)

Unified document viewer accessible via "Docs" button in header. Replaces the old Plan button.

### Tabs
| Tab | Content | Features |
|-----|---------|----------|
| **Plans** | ~/.claude/plans/*.md | Dropdown selector to pick plan |
| **Context** | .claude/CONTEXT.md | Read-only view |
| **Touch** | .claude/touch-summary.md | Read-only view |
| **Sessions** | Other session logs | Read-only viewer with Back button |

### New Endpoints
- `GET /api/docs/context` - Read CONTEXT.md from target repo
- `GET /api/docs/touch` - Read touch-summary.md from target repo
- `GET /api/log?session_id=xxx` - Load specific session log (for Sessions tab)

### Files Modified
- `mobile_terminal/server.py` - Added `/api/docs/context`, `/api/docs/touch`, `session_id` param to `/api/log`
- `mobile_terminal/static/index.html` - Changed planBtn to docsBtn, planModal to docsModal with tabs
- `mobile_terminal/static/styles.css` - Added docs-modal and docs-tab styles
- `mobile_terminal/static/terminal.js` - Replaced setupPlanButton with setupDocsButton, tab switching logic

## Objective

Build a mobile-optimized terminal overlay for accessing tmux sessions from phones/tablets.

## Completed

- [x] Project structure and pyproject.toml
- [x] FastAPI server with WebSocket tmux relay
- [x] Auth disabled by default (Tailscale-friendly), opt-in via --require-token
- [x] Static files (HTML, CSS, JS)
- [x] xterm.js integration
- [x] Control bars with collapse toggle
- [x] Control keys bar (^B, ^C, ^D, ^Z, ^L, ^A, ^E, ^W, ^U, ^K, ^R, ^O, Tab, Esc)
- [x] Quick bar (arrows, numbers, y/n/enter, slash)
- [x] Role prefixes from config
- [x] Compose modal for predictive text / speech-to-text
- [x] Image upload in compose modal (saves to .claude/uploads/)
- [x] Repo switching dropdown
- [x] File search modal
- [x] Select mode with tap-to-select
- [x] Copy to clipboard functionality
- [x] PWA support (service worker, manifest, standalone mode)
- [x] systemd service file for auto-start
- [x] WebSocket resilience (handles malformed messages)
- [x] Transcript view with syntax highlighting
- [x] tmux capture-pane history on connect
- [x] Terminal block UI with active prompt display
- [x] Input box sync with tmux (Up/Down/Tab sync via refresh button)
- [x] Client-side key debounce (150ms)
- [x] Always-on controls (lock removed, collapse toggle in tab bar)
- [x] Unified collapse (view bar + control bars collapse together)
- [x] Streamlined header (refresh in header, no working indicator)
- [x] V2: Git PR-aware status + safer revert UX
- [x] V2: Process management (terminate/respawn/status)
- [x] V2: Preview filters + turn-based auto-snapshots
- [x] V2: Runner with allowlisted quick commands
- [x] V2: Connection resilience (hello handshake, watchdog, PTY death detection)
- [x] Target Selector: Explicit pane selection for multi-project workflows
- [x] Docs Modal: Unified viewer for Plans, Context, Touch, and Sessions
- [x] New Window in Repo: Create tmux windows from mobile with auto-start Claude option

## Recent Changes (2026-01-23) - Git Revert Dirty Handling

### Safe Revert with Dirty Directory
- **Dirty choice modal** - When repo is dirty, shows choice instead of blocking:
  - "Stash changes and continue" (safe, preserves work)
  - "Discard all changes" (requires 2-step confirmation)
- **Post-revert stash management** - After revert with stash, shows Apply/Drop options
- **Untracked files checkbox** - Opt-in removal of untracked files on discard
- **Safer stash handling** - Uses `git stash apply` not `pop` (preserves stash on conflict)

### New Endpoints
- `POST /api/git/stash/push` - Auto-stash with timestamp
- `GET /api/git/stash/list` - List stashes
- `POST /api/git/stash/apply` - Apply stash (non-destructive)
- `POST /api/git/stash/drop` - Drop stash
- `POST /api/git/discard` - Reset hard + optional clean

---

## Recent Changes (2026-01-23) - Dev Preview & UX Polish

### Dev Preview Tab (Replit-like)
- **New "Dev" tab** in drawer for previewing running services
- Configuration via `preview.config.json` per repo
- Health status indicators (green/red dots)
- Start/Stop/Restart controls send commands to PTY
- Open/Copy buttons for Tailscale URLs
- Iframe preview with sandbox

### Challenge Improvements
- **Plan selector dropdown** - pick any plan from ~/.claude/plans
- Replaces "Include active plan" checkbox
- Preview updates when plan selection changes

### UI Reorganization
- **Plan button moved to top bar** (visible when plan exists)
- **Removed "Terminal" header** from terminal view
- **Unified refresh button** - refreshes log or terminal based on active view
- Refresh shows toast feedback and reconnects WebSocket if needed

### Queue Persistence
- **Client-side queue persistence** via localStorage
- Queue survives page reload and reconnects
- Reconciliation on reconnect (dedup, reorder)
- Grace period for reconnect overlay (reduces flicker)

### Log View Fixes
- **Fixed log stuck after plan mode** - force render on refresh
- **Content hash comparison** for change detection
- Plan mode tool logging (EnterPlanMode, ExitPlanMode, Task, TodoWrite)

### WebSocket Stability
- **Send-after-close fix** - prevents errors during disconnect
- Connection closed flag checked before all sends

## Recent Changes (2026-01-21) - Safety & UX

### Target Safety Checks (v0.2.0)
- **Server-side validation:** All action endpoints now validate session+pane_id
- **Client sends params:** Every action API call includes `getTargetParams()`
- **409 Conflict:** Server returns expected vs received if mismatch
- **Lock toggle:** Target locked by default (prevents auto-follow of tmux focus)

### Log Scroll Fix
- **Problem:** Scroll jumped to random positions when new content arrived while reading
- **Solution:** Don't re-render while user is scrolling - store pending content
- **Indicator:** "↓ New content" shows when updates are pending
- **Render on demand:** Content renders when user scrolls to bottom or clicks indicator

### Drawer Backdrop
- Tap outside drawer to close (semi-transparent overlay)
- Backdrop appears when drawer opens, hides when closed

## Recent Changes (2026-01-20) - Target Selector

### Multi-Project Target Selector
- **Problem solved:** Working with multiple projects in different tmux windows now works correctly
- `/api/targets` - Lists all panes/windows in session with their working directories
- `/api/target/select` - Set the active target pane for all operations
- `get_current_repo_path()` updated to prioritize explicitly selected target
- Header dropdown shows project name with pane ID, path in dropdown options
- Selecting a target refreshes log view and context to show that project's data

### How It Works
1. Open tmux session with multiple windows (e.g., `ops:geo-cv`, `ops:cuisina`, `ops:studie`)
2. Each window can be in a different project directory
3. Use target dropdown to select which window's project to work with
4. All operations (git, log, context) use the selected target's directory

## Recent Changes (2026-01-20) - V2 Features

### Git v2
- PR-aware status banner: Shows associated PR number/link when branch has an open PR (uses `gh pr view`)
- Safer revert UX: Revert button disabled until dry-run passes; dry-run validates each commit separately

### Process v2
- `/api/process/terminate` - SIGTERM first, SIGKILL fallback (force=true)
- `/api/process/respawn` - Recreate PTY session
- `/api/process/status` - Check process health
- Process tab in drawer with visual status banner and action buttons

### Preview v2
- Filter buttons: All | User | Tool | Done | Error
- Turn detection: Auto-captures snapshots when log changes with `tool_call`, `claude_done`, or `error` labels
- Friendly labels: Display names like "User", "Tool", "Done" instead of raw codes

### Runner v2
- Allowlisted commands: Build, Test, Lint, Format, Typecheck, Dev Server
- Quick command buttons: Grid layout with icons in Runner tab
- Custom command input: Run arbitrary commands with basic safety checks
- `/api/runner/commands`, `/api/runner/execute`, `/api/runner/custom`

### Connection Resilience
- Server hello handshake on WebSocket connect (client expects within 2s)
- PTY death detection with close code 4500
- Connection watchdog for stuck states
- Hard refresh button after 3 failed reconnects

---

## Portability: Using with Other Projects

### Solution: Target Selector (Implemented)

The overlay now supports explicit target selection via the header dropdown:

1. **Resolution priority** in `get_current_repo_path()`:
   - Explicit target selection (`app.state.active_target`)
   - Configured repos (from config.yaml)
   - project_root config option
   - Active tmux pane cwd (fallback)
   - Server cwd (last resort)

2. **Target dropdown** in header shows all panes with their project directories

3. **Selecting a target** refreshes all context-dependent views (log, git, context)

### Config-based mapping (still supported)

Add to `~/.config/mobile-terminal/config.yaml`:
```yaml
repos:
  - session: claude
    path: /home/gbons/dev/mobile-terminal-overlay
  - session: myproject
    path: /home/gbons/dev/myproject
```

### Feature Portability Status

| Feature | Status | Notes |
|---------|--------|-------|
| Terminal I/O | Works | Pure relay, no path dependency |
| Log file | Works | Uses selected target's project directory |
| Git ops | Works | Uses `get_current_repo_path()` with target selection |
| File search | Works | Uses `get_current_repo_path()` with target selection |
| Uploads | Works | Goes to `.claude/uploads/` relative to selected target |
| Context/Touch | Works | Reads from selected target's `.claude/` directory |

---

## Architecture: Session vs Target

```
┌─────────────────────────────────────────────────────────────┐
│  SESSION: "ops"  (tmux session name, set via --session)     │
│                                                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│  │ Window 0    │  │ Window 1    │  │ Window 2    │         │
│  │ name: geo   │  │ name: cuisi │  │ name: studie│         │
│  │             │  │             │  │             │         │
│  │ Pane 0:0    │  │ Pane 1:0    │  │ Pane 2:0    │         │
│  │ cwd: geo-cv │  │ cwd: cuisina│  │ cwd: studie │         │
│  │   ← TARGET  │  │             │  │             │         │
│  └─────────────┘  └─────────────┘  └─────────────┘         │
└─────────────────────────────────────────────────────────────┘
                           ↓
              get_current_repo_path() returns
              /home/gbons/dev/geo-cv
                           ↓
         Git ops, log view, context.md all use this path
```

### Session
- **What:** tmux session to connect terminal I/O to
- **Set:** At server start via `mobile-terminal --session ops`
- **Scope:** All windows/panes in that session are available as targets
- **Terminal typing:** Goes to the **active pane** in tmux (wherever cursor is)

### Target
- **What:** Which pane's working directory to use for file operations
- **Set:** Via header dropdown in the UI
- **Scope:** Determines `get_current_repo_path()` result
- **Used by:** Git status, log file, CONTEXT.md, file search

### Key Distinction

| Operation | Follows | Behavior |
|-----------|---------|----------|
| Terminal I/O | tmux active pane | Follows focus (typing goes where cursor is) |
| File operations | Selected target | Stays locked (git/log/context use explicit selection) |

This prevents the "cd elsewhere mid-task" footgun: even if you switch tmux focus or cd to another directory, git/log/context still use your explicitly selected target.

### Safety Features
- **Target strip:** Shows current working directory below header
- **Fallback warning:** Yellow highlight when using auto-detected path
- **localStorage:** Target persists across page reloads
- **409 on missing:** Server returns error if selected pane no longer exists

---

## Recent Changes (2026-01-18)

### Header Simplification
- Removed "Log" title from log view
- Moved refresh button (↻) to header - refreshes log AND syncs input box
- Removed sync button from input box (refresh handles both)
- Removed "working..." indicator (redundant with activity timestamp)
- Header now: `[repo] ... [activity] [refresh] [search] [connection dot]`

### Unified Collapse Behavior
- View bar (Select/Stop/Challenge/Compose) now collapses with control bars
- Single collapse toggle controls all bottom bars

### UI Simplification
- Removed lock button from header (controls always enabled)
- Moved collapse toggle to tab indicator section (next to dots)
- Empty input box Enter sends `'\r'` (confirms prompts)
- Control bar Enter kept as "stateless confirm" (ignores input box state)
- Fixed aggressive filter that hid Claude's interactive options

### Input Box Sync with tmux Terminal
- Atomic send: `command + '\r'` as single write (no race conditions)
- Up/Down arrows sync terminal history to input box
- Tab completion syncs completed text back to input
- Client-side 150ms debounce on key sends (Ctrl+C bypasses for immediate interrupt)
- Multi-prompt pattern detection: Claude `❯`, bash `$`, zsh, Python `>>>`, Node `>`

### Terminal Block UI
- Active prompt display shows last 20 lines of terminal
- Clean terminal output in log view
- Auto-suggest commands from terminal prompt

---

## Changes (2026-01-15)

### UI Simplification
- Unified action bar (viewBar) always visible: Select | Copy | Scroll | Refresh | Compose
- Removed duplicate Select/Copy buttons from inputBar
- Compose button now auto-unlocks control mode
- Removed bottomBar (merged into viewBar)

### Transcript View
- Added Term/Log toggle in header to switch views
- Transcript fetches clean history via `tmux capture-pane -p -J -S -10000`
- Syntax highlighting: prompts (green), paths (blue), flags (yellow), strings (green)
- Visual separation: command lines have blue border, output is indented/muted
- Search with match highlighting
- Proper flexbox scrolling (min-height: 0 fix)

### History & Scrolling
- WebSocket connect now uses `tmux capture-pane` for clean history (not raw buffer)
- Added `/api/transcript` endpoint for full transcript fetch
- Added `/api/refresh` endpoint for manual terminal refresh
- Fixed auto-scroll hijacking (only scroll to bottom if user was already there)
- Fixed touchend preventDefault blocking scroll on transcript

### Endpoints
| Endpoint | Purpose |
|----------|---------|
| `GET /api/transcript` | Returns 10000 lines of tmux history |
| `GET /api/refresh` | Returns 5000 lines for terminal refresh |

## Known Issues / In Testing

- Terminal native scroll doesn't work with tmux (use Scroll button for tmux copy mode)
- Transcript view is the recommended way to read history

## Key Files

| File | Purpose |
|------|---------|
| `mobile_terminal/server.py` | FastAPI + WebSocket endpoint + capture-pane APIs |
| `mobile_terminal/config.py` | Config dataclass + YAML loading |
| `mobile_terminal/cli.py` | CLI entrypoint |
| `mobile_terminal/static/terminal.js` | xterm.js + WebSocket + transcript view |
| `mobile_terminal/static/styles.css` | Mobile-first CSS with transcript styling |
| `mobile_terminal/static/sw.js` | Service worker for PWA |
| `mobile-terminal.service` | systemd unit file |

## tmux Configuration

For best scrollback in Transcript view:
```bash
# Add to ~/.tmux.conf
set -g history-limit 50000
```

## Deployment

```bash
# Manual start (auth disabled by default for Tailscale)
mobile-terminal --session claude --port 8765

# With token auth (for non-Tailscale networks)
mobile-terminal --session claude --require-token

# systemd (auto-start on boot)
sudo cp mobile-terminal.service /etc/systemd/system/
sudo systemctl enable --now mobile-terminal
```

## Access

- HTTP: `http://<hostname>:8765/`
- PWA: Install from Chrome menu for standalone mode
