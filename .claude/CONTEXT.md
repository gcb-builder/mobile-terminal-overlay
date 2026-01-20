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

### Current Limitation

`get_current_repo_path()` in `server.py` falls back to `Path.cwd()` which is the **server's** working directory, not the tmux session's. This means if you start the server from one directory but want to work on a different project, features like log viewing, git ops, and file search won't find the right files.

### Solution: Query tmux for pane's cwd

```python
def get_current_repo_path() -> Optional[Path]:
    # ... existing checks ...

    # Get pane's actual working directory from tmux
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", session_name, "#{pane_current_path}"],
        capture_output=True, text=True, timeout=2
    )
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())

    return Path.cwd()  # Last resort fallback
```

### Alternative: Config-based mapping

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
| Log file | Works* | Uses `~/.claude/projects/{project-id}` - works if Claude Code is running there |
| Git ops | Needs fix | Uses `get_current_repo_path()` |
| File search | Needs fix | Uses `get_current_repo_path()` |
| Uploads | Needs fix | Goes to `.claude/uploads/` relative to repo path |
| Pipe-pane | Works* | Logs to repo's `.claude/` if path is correct |

**TODO:** Implement tmux pane cwd detection to make overlay fully portable.

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
