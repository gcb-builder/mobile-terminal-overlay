# Mobile Terminal Overlay - Session Context

## Current State

- **Branch:** master
- **Stage:** Production-ready with PWA support + V2 features
- **Last Updated:** 2026-01-20

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
