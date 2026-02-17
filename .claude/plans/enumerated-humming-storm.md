# Implementation Checklist: Startup Automation, Session Recovery, Layout Hints

## Feature 2: Per-Repo Startup Automation (First)

### config.py
- [ ] Add `startup_command: Optional[str] = None` to Repo dataclass
- [ ] Add `startup_delay_ms: int = 300` to Repo dataclass
- [ ] Update config loading to parse new fields with clamping (0..5000)
- [ ] Update `to_dict()` serialization

### server.py
- [ ] Update `/api/repos` response to include `startup_command`, `startup_delay_ms`
- [ ] In `/api/window/new`: use `repo.startup_command or "claude"` when auto_start enabled
- [ ] Validate startup_command: reject if contains newline or length > 200
- [ ] Use `tmux send-keys -l` (literal mode) + separate Enter
- [ ] Audit log the startup command execution

---

## Feature 3: Session Recovery (Second)

### server.py
- [ ] Add `GET /api/health/claude?pane_id=...`
  - Get pane_pid via `tmux display-message`
  - Scan process tree for "claude-code" in cmdline (not just pgrep -x claude)
  - Return `{pane_alive, shell_pid, claude_running, claude_pid, pane_title}`
- [ ] Add `POST /api/claude/start?pane_id=...`
  - Check health first (409 if already running)
  - Get repo's startup_command from config
  - Send via tmux send-keys -l

### terminal.js
- [ ] Add health polling state: `claudeHealthInterval`, `lastClaudeHealth`, `claudeStartedAt`
- [ ] Add `checkClaudeHealth()` - poll `/api/health/claude` every 5s
- [ ] Only poll when `document.visibilityState === 'visible'` and `activeTarget` set
- [ ] Add `updateClaudeCrashBanner()` - show if was running, now not, debounce 3s
- [ ] Add `respawnClaude()` - call `/api/claude/start`
- [ ] Track dismissed panes in memory (per-pane dismiss)
- [ ] Wire up visibility change listener to start/stop polling

### index.html
- [ ] Add crash banner HTML after multiProjectBanner

### styles.css
- [ ] Add `.claude-crash-banner` styles (red warning, 44px touch targets)

---

## Feature 1: Layout Convention Hints (Last)

### server.py
- [ ] In `/api/window/new`: default window name to `Path(path).name` if not provided

### terminal.js
- [ ] In target dropdown rendering: check if window_name roughly matches project dir
- [ ] Show subtle hint badge if mismatch (normalize + substring)

### styles.css
- [ ] Add `.target-name-hint` style (muted, small)

---

## Verification

1. Config: Add `startup_command: "echo test"` to a repo, verify `/api/repos` returns it
2. Window: Create window with auto_start, verify custom command runs
3. Health: Call `/api/health/claude`, verify claude_running detection
4. Crash: Kill claude manually, verify banner appears after debounce
5. Respawn: Click respawn, verify claude starts
6. Naming: Create window without name, verify uses directory basename
7. Hint: Rename window to mismatch, verify hint shows in dropdown
