# Claude Code Guidelines for mobile-terminal-overlay

## Project Overview

Mobile-optimized terminal overlay for accessing tmux sessions from phones/tablets. Provides a safe view-only mode with unlock-to-control functionality.

## Allowed Edit Paths

- `mobile_terminal/**/*.py` - Python source code
- `mobile_terminal/static/**` - HTML, CSS, JS static files
- `tests/**/*.py` - Test files
- `pyproject.toml` - Package configuration
- `README.md` - Documentation
- `.gitignore` - Git exclusions
- `.claude/*.md` - Context management files
- `CLAUDE.md` - These guidelines

## Forbidden Paths (Never Commit)

- `.git/` - Git internals
- `venv/`, `.venv/`, `__pycache__/`, `*.pyc` - Virtual envs, bytecode
- `.env`, `.env.*` - Secrets
- `*.log` - Log files

## Tech Stack

- **FastAPI** - Web server and WebSocket handling
- **xterm.js** - Terminal emulation in browser
- **tmux** - Terminal multiplexer backend
- **PyYAML** - Configuration parsing
- **uvicorn** - ASGI server

## Command Cheatsheet

```bash
# Install in development mode
pip install -e .

# Run server
mobile-terminal
mobile-terminal --session claude --port 9000 --verbose

# Print config
mobile-terminal --print-config

# Run tests
pytest tests/
```

## Context Management

Claude sessions are ephemeral. Context must be file-backed.

### Required Files

| File | Purpose |
|------|---------|
| `.claude/CONTEXT.md` | Persistent project state (objective, branch, stage) |
| `.claude/touch-summary.md` | Append-only log of implementation batches |

### Mandatory Behaviors

1. **Session Start:** Claude MUST read `.claude/CONTEXT.md` before acting
2. **State Changes:** Claude MUST update `CONTEXT.md` when goals or stage change
3. **After Implementation:** Claude MUST append to `touch-summary.md`

## Operating Rules

### Explicit Planning Before Action

Before any code changes, produce a clear plan:
- Goal / problem statement
- Files to be touched
- Step-by-step implementation plan

### Touch Summary (Required After Implementation)

After every implementation batch, output:
- Files changed (exact paths)
- New files created
- Risks / follow-ups

### Incremental, Reviewable Changes

- One logical change per commit
- No refactors mixed with features
- No speculative or unused code

## Notes for AI Assistant

- **Prefer editing existing files** over creating new ones
- **Include file:line references** when discussing code
- **No emojis** unless explicitly requested
- Keep mobile UX in mind - touch targets should be >= 44px
