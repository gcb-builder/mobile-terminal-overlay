"""
CLI entrypoint for Mobile Terminal Overlay.

Usage:
    mobile-terminal                              # Auto-discover project context
    mobile-terminal --session claude --port 9000 # Explicit session
    mobile-terminal --print-config               # Print resolved config
"""

import argparse
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from . import __version__
from .config import Config, load_config
from .discovery import discover_project_config
from .server import create_app


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="mobile-terminal",
        description="Mobile-optimized terminal overlay for tmux sessions",
    )

    parser.add_argument(
        "--config", "-c",
        type=Path,
        help="Path to config file (default: auto-discover)",
    )
    parser.add_argument(
        "--session", "-s",
        help="tmux session name (default: from config or 'mobile-term')",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        help="Server port (default: 8765)",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Server host (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--token", "-t",
        help="Auth token (implies --require-token, auto-generated if not set)",
    )
    parser.add_argument(
        "--require-token",
        action="store_true",
        help="Enable token authentication (disabled by default for Tailscale use)",
    )
    parser.add_argument(
        "--no-discovery",
        action="store_true",
        help="Disable auto-discovery of project context",
    )
    parser.add_argument(
        "--no-auto-setup",
        action="store_true",
        help="Disable automatic tmux session creation/adoption on startup",
    )
    parser.add_argument(
        "--agent-type",
        help="Agent driver to use: claude, codex, generic (default: claude)",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print resolved config as YAML and exit",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose logging",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"mobile-terminal {__version__}",
    )

    return parser.parse_args()


def main() -> int:
    """Main entrypoint."""
    # Load environment variables from .env file
    load_dotenv()

    args = parse_args()

    # Load config
    if args.config:
        # Explicit config file
        config = load_config(args.config)
    elif args.no_discovery:
        # Use defaults only
        config = Config()
    else:
        # Auto-discover
        config = discover_project_config()

    # Apply CLI overrides
    if args.session:
        config.session_name = args.session
    if args.port:
        config.port = args.port
    if args.host:
        config.host = args.host
    if args.token:
        config.token = args.token
        config.no_auth = False  # --token implies auth required
    if args.require_token:
        config.no_auth = False
    if args.no_auto_setup:
        config.auto_setup = False
    if args.agent_type:
        config.agent_type = args.agent_type

    # Print config and exit if requested
    if args.print_config:
        print(config.to_yaml())
        return 0

    # Set up logging
    log_level = "debug" if args.verbose else "info"
    # Also configure the application logger (not just uvicorn)
    import logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s:%(name)s:%(message)s"
    )

    # Create and run app
    app = create_app(config)

    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level=log_level,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
