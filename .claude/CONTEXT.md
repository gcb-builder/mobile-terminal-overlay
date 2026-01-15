# Mobile Terminal Overlay - Session Context

## Current State

- **Branch:** master
- **Stage:** Production-ready with PWA support
- **Last Updated:** 2026-01-15

## Objective

Build a mobile-optimized terminal overlay for accessing tmux sessions from phones/tablets.

## Completed

- [x] Project structure and pyproject.toml
- [x] FastAPI server with WebSocket tmux relay
- [x] Auth disabled by default (Tailscale-friendly), opt-in via --require-token
- [x] Static files (HTML, CSS, JS)
- [x] xterm.js integration
- [x] View/Control toggle with lock indicator
- [x] Control keys bar (^B, ^C, ^D, ^Z, ^L, ^A, ^E, ^W, ^U, ^K, ^R, Tab, Esc)
- [x] Quick bar (Select, Copy, arrows, numbers, y/n/enter, slash)
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

## Recent Changes (2026-01-15)

- Added image upload feature in compose modal
- Auth disabled by default (use --require-token to enable)
- Removed --no-auth flag (now the default behavior)

## Key Files

| File | Purpose |
|------|---------|
| `mobile_terminal/server.py` | FastAPI + WebSocket endpoint |
| `mobile_terminal/config.py` | Config dataclass + YAML loading |
| `mobile_terminal/cli.py` | CLI entrypoint |
| `mobile_terminal/static/terminal.js` | xterm.js + WebSocket client |
| `mobile_terminal/static/sw.js` | Service worker for PWA |
| `mobile-terminal.service` | systemd unit file |

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
