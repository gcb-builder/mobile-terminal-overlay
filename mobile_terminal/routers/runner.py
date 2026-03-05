"""Routes for runner commands (build, test, lint, etc.)."""
import logging
import os
import re
from typing import Optional

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

RUNNER_COMMANDS = {
    "build": {
        "label": "Build",
        "description": "Run build script",
        "commands": ["npm run build", "yarn build", "make build", "cargo build"],
        "icon": "\U0001f528"
    },
    "test": {
        "label": "Test",
        "description": "Run tests",
        "commands": ["npm test", "yarn test", "pytest", "cargo test", "make test"],
        "icon": "\u2705"
    },
    "lint": {
        "label": "Lint",
        "description": "Run linter",
        "commands": ["npm run lint", "yarn lint", "ruff check .", "cargo clippy"],
        "icon": "\U0001f50d"
    },
    "format": {
        "label": "Format",
        "description": "Format code",
        "commands": ["npm run format", "ruff format .", "cargo fmt", "black ."],
        "icon": "\U0001f4dd"
    },
    "typecheck": {
        "label": "Typecheck",
        "description": "Run type checker",
        "commands": ["npm run typecheck", "tsc --noEmit", "mypy .", "pyright"],
        "icon": "\U0001f4cb"
    },
    "dev": {
        "label": "Dev Server",
        "description": "Start dev server",
        "commands": ["npm run dev", "yarn dev", "python -m http.server"],
        "icon": "\U0001f680"
    },
}


def register(app: FastAPI, deps):
    """Register runner routes."""

    @app.get("/api/runner/commands")
    async def list_runner_commands(
        _auth=Depends(deps.verify_token),
    ):
        """List available runner commands."""

        return {"commands": RUNNER_COMMANDS}

    @app.post("/api/runner/execute")
    async def execute_runner_command(
        command_id: str = Query(...),
        variant: int = Query(0),  # Which command variant to use
        _auth=Depends(deps.verify_token),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """
        Execute an allowlisted runner command.

        Sends the command to the PTY (same as user typing it).
        Only commands in RUNNER_COMMANDS are allowed.
        """

        # Validate target before executing
        target_check = deps.validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        if command_id not in RUNNER_COMMANDS:
            return JSONResponse({"error": "Unknown command"}, status_code=400)

        cmd_config = RUNNER_COMMANDS[command_id]
        commands = cmd_config["commands"]

        if variant < 0 or variant >= len(commands):
            variant = 0

        command = commands[variant]

        # Check if PTY is available
        master_fd = app.state.master_fd
        if not master_fd:
            return JSONResponse({"error": "No PTY available"}, status_code=400)

        # Send command to PTY
        try:
            os.write(master_fd, (command + '\r').encode('utf-8'))
            app.state.audit_log.log("runner_execute", {
                "command_id": command_id,
                "command": command
            })
            return {
                "success": True,
                "command_id": command_id,
                "command": command,
                "label": cmd_config["label"]
            }
        except Exception as e:
            logger.error(f"Runner execute failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/runner/custom")
    async def execute_custom_command(
        command: str = Query(...),
        _auth=Depends(deps.verify_token),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """
        Execute a custom command (with basic safety checks).

        This is more permissive than the queue but still has some safety rails.
        """

        # Validate target before executing
        target_check = deps.validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        # Safety checks — block destructive patterns
        dangerous_patterns = [
            r'rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|--force\s+).*/',  # rm -rf / or rm -f /path
            r'^\s*rm\s+-rf\s+/',     # rm -rf /
            r'^\s*:(){',             # Fork bomb
            r'>\s*/dev/sd',          # Writing to disk devices
            r'mkfs\.',               # Formatting disks
            r'dd\s+.*of=\s*/dev/',   # dd to device
            r'chmod\s+(-R\s+)?777\s+/',  # chmod 777 /
            r'chown\s+-R\s+.*\s+/',  # chown -R on root paths
            r'>\s*/etc/',            # Overwriting system config
            r'curl\s.*\|\s*sh',      # Pipe curl to shell
            r'wget\s.*\|\s*sh',      # Pipe wget to shell
            r'shutdown\b',           # System shutdown
            r'reboot\b',            # System reboot
            r'init\s+[06]',          # System halt/reboot via init
        ]

        for pattern in dangerous_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                logger.warning(f"Blocked dangerous command: {command[:100]}")
                return JSONResponse({
                    "error": "Command blocked for safety",
                    "reason": "Potentially destructive operation"
                }, status_code=400)

        # Check if PTY is available
        master_fd = app.state.master_fd
        if not master_fd:
            return JSONResponse({"error": "No PTY available"}, status_code=400)

        # Send command to PTY
        try:
            os.write(master_fd, (command + '\r').encode('utf-8'))
            app.state.audit_log.log("runner_custom", {"command": command})
            return {"success": True, "command": command}
        except Exception as e:
            logger.error(f"Runner custom execute failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)
