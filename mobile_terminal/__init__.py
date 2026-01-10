"""
Mobile Terminal Overlay - A mobile-optimized terminal UI for tmux sessions.

Features:
- Native touch scrolling through terminal history
- Dedicated control key buttons (Ctrl+C, Tab, arrows)
- Stable text input that doesn't fight with the terminal
- Read-only by default with explicit "Take Control" toggle
- Config-driven quick commands and role prefixes
- Auto-discovery of project context from CLAUDE.md

Usage:
    # Run in any project directory
    mobile-terminal

    # With explicit options
    mobile-terminal --session my-session --port 9000

    # Generate config template
    mobile-terminal --print-config > .mobile-terminal.yaml
"""

__version__ = "0.1.0"

from .config import Config, load_config
from .discovery import discover_project_config

__all__ = ["Config", "load_config", "discover_project_config", "__version__"]
