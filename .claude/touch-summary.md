# Touch Summary Log

Append-only log of implementation batches.

---

## 2026-01-10: Initial Scaffold

**Goal:** Create initial project structure for mobile-terminal-overlay

**Files Created:**
- `pyproject.toml` - pip package configuration
- `mobile_terminal/__init__.py` - package init
- `mobile_terminal/config.py` - Config dataclass with YAML loading
- `mobile_terminal/discovery.py` - Auto-discovery from CLAUDE.md
- `mobile_terminal/server.py` - FastAPI + WebSocket tmux relay
- `mobile_terminal/cli.py` - CLI entrypoint
- `mobile_terminal/static/index.html` - Main UI with xterm.js
- `mobile_terminal/static/styles.css` - Mobile-first CSS
- `mobile_terminal/static/terminal.js` - WebSocket + xterm.js client
- `README.md` - Documentation
- `.gitignore` - Git exclusions
- `CLAUDE.md` - Claude Code guidelines
- `.claude/CONTEXT.md` - Session context
- `.claude/touch-summary.md` - This file

**Risks/Follow-ups:**
- Needs testing with actual tmux session
- Needs unit tests
- Role prefix regex may need tuning for different CLAUDE.md formats

---

## 2026-01-15: Copy/Select Fix + PWA Standalone

**Goal:** Fix copy functionality breaking terminal input, add PWA standalone support

**Files Changed:**
- `mobile_terminal/server.py` - Robust message handling, continue on errors instead of disconnecting
- `mobile_terminal/static/terminal.js` - Focus restoration after copy, isComposing reset, rewritten copy handler
- `mobile_terminal/static/index.html` - Added ^B button, service worker registration
- `mobile_terminal/static/manifest.json` - Added scope and display_override for standalone
- `mobile_terminal/static/styles.css` - Various UX improvements

**Files Created:**
- `mobile_terminal/static/sw.js` - Service worker for PWA installation
- `mobile-terminal.service` - systemd unit file for auto-start

**Commits:**
- `da6e940` Fix copy/select bug and add PWA standalone support
- `3d25636` Add Ctrl+B (tmux prefix) to control bar

**Risks/Follow-ups:**
- PWA needs reinstall after service worker changes
- systemd service needs manual setup (sudo required)
