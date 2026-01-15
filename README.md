# Mobile Terminal Overlay

Mobile-optimized terminal UI for accessing tmux sessions from a phone or tablet.

## Features

- **View/Control Toggle**: Safe view-only mode by default; unlock to send input
- **Control Keys Bar**: Quick access to Ctrl+C, Ctrl+D, Tab, arrows, Esc
- **Role Prefixes**: Auto-discovered from CLAUDE.md (Planner:, Implementer:, etc.)
- **Quick Commands**: Configurable one-tap commands
- **Token Auth**: URL-based token authentication
- **Auto-Discovery**: Finds project context from CLAUDE.md or .mobile-terminal.yaml

## Installation

```bash
pip install mobile-terminal-overlay
```

Or install from source:

```bash
git clone https://github.com/yourusername/mobile-terminal-overlay.git
cd mobile-terminal-overlay
pip install -e .
```

## Usage

### Basic Usage

```bash
# Auto-discover project context from current directory
mobile-terminal

# Explicit session name and port
mobile-terminal --session claude --port 9000

# Print resolved configuration
mobile-terminal --print-config
```

### Access from Phone

1. Start the server: `mobile-terminal`
2. Note the URL printed (includes auth token)
3. Open URL on your phone (must be on same network)

For remote access, use a tunnel like Cloudflare Tunnel or ngrok.

## tmux Configuration

For best scrollback history in the Transcript view, increase tmux's history limit:

```bash
# Add to ~/.tmux.conf
set -g history-limit 50000
```

Then reload tmux config: `tmux source-file ~/.tmux.conf`

## Configuration

Create `.mobile-terminal.yaml` in your project root:

```yaml
session_name: claude
port: 8765

quick_commands:
  - label: "git status"
    command: "git status\n"
  - label: "pytest"
    command: "pytest -v\n"

role_prefixes:
  - label: "Planner:"
    insert: "Planner: "
  - label: "Implementer:"
    insert: "Implementer: "

context_buttons:
  - label: "CONTEXT"
    command: "cat .claude/CONTEXT.md\n"
```

### Auto-Discovery

If no config file is found, the tool walks up the directory tree looking for:

1. `.mobile-terminal.yaml` - Explicit configuration
2. `CLAUDE.md` - Extracts role prefixes (Planner Agent, Implementer Agent, etc.)
3. `.git/` - Project root marker

## Development

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install in development mode
pip install -e .

# Run
mobile-terminal --verbose
```

## Architecture

```
mobile-terminal-overlay/
├── pyproject.toml           # Package configuration
├── mobile_terminal/
│   ├── __init__.py
│   ├── cli.py               # CLI entrypoint
│   ├── config.py            # Configuration dataclass + YAML loading
│   ├── discovery.py         # Auto-discovery from CLAUDE.md
│   ├── server.py            # FastAPI server + WebSocket
│   └── static/
│       ├── index.html       # Main UI
│       ├── styles.css       # Mobile-first CSS
│       └── terminal.js      # xterm.js + WebSocket client
```

## License

MIT
