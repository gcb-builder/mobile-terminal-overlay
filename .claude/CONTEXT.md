# Mobile Terminal Overlay - Session Context

## Current State

- **Branch:** master
- **Stage:** Initial scaffold complete
- **Last Updated:** 2026-01-10

## Objective

Build a mobile-optimized terminal overlay for accessing tmux sessions from phones/tablets.

## Completed

- [x] Project structure and pyproject.toml
- [x] FastAPI server with WebSocket tmux relay
- [x] Token-based authentication
- [x] Static files (HTML, CSS, JS)
- [x] xterm.js integration
- [x] View/Control toggle
- [x] Control keys bar
- [x] Role prefixes from config
- [x] Quick commands from config
- [x] Auto-discovery from CLAUDE.md
- [x] README documentation

## In Progress

- [ ] Initial git commit

## Next Steps

1. Test installation with `pip install -e .`
2. Test with actual tmux session
3. Add unit tests
4. Test auto-discovery from geo-cv CLAUDE.md

## Key Files

| File | Purpose |
|------|---------|
| `mobile_terminal/server.py` | FastAPI + WebSocket endpoint |
| `mobile_terminal/config.py` | Config dataclass + YAML loading |
| `mobile_terminal/discovery.py` | Auto-discovery from CLAUDE.md |
| `mobile_terminal/cli.py` | CLI entrypoint |
| `mobile_terminal/static/terminal.js` | xterm.js + WebSocket client |

## Notes

- Designed for cross-repo reusability
- Auto-discovers role prefixes from CLAUDE.md
- Uses URL-based token auth for simplicity
