"""
FastAPI server for Mobile Terminal Overlay.

Provides:
- Static file serving for the web UI
- WebSocket endpoint for terminal I/O
- Token-based authentication
"""

import asyncio
import fcntl
import json
import logging
import os
import pty
import secrets
import struct
import termios
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import Config

logger = logging.getLogger(__name__)

# Directory containing static files
STATIC_DIR = Path(__file__).parent / "static"


def create_app(config: Config) -> FastAPI:
    """
    Create FastAPI application with configured routes.

    Args:
        config: Configuration instance.

    Returns:
        Configured FastAPI app.
    """
    app = FastAPI(
        title="Mobile Terminal Overlay",
        description="Mobile-optimized terminal UI for tmux sessions",
        version="0.1.0",
    )

    # Store config and state on app
    app.state.config = config
    app.state.no_auth = config.no_auth
    app.state.token = None if config.no_auth else (config.token or secrets.token_urlsafe(16))
    app.state.master_fd = None
    app.state.child_pid = None
    app.state.active_websocket = None
    app.state.read_task = None

    # Mount static files
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index(token: Optional[str] = Query(None)):
        """Serve the main HTML page."""
        if not app.state.no_auth and token != app.state.token:
            return HTMLResponse(
                content="<h1>401 Unauthorized</h1><p>Invalid or missing token.</p>",
                status_code=401,
            )
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/config")
    async def get_config(token: Optional[str] = Query(None)):
        """Return client configuration as JSON."""
        if not app.state.no_auth and token != app.state.token:
            return {"error": "Unauthorized"}, 401
        return app.state.config.to_dict()

    @app.get("/health")
    async def health():
        """Health check endpoint."""
        return {"status": "ok", "version": "0.1.0"}

    @app.websocket("/ws/terminal")
    async def terminal_websocket(websocket: WebSocket, token: Optional[str] = Query(None)):
        """WebSocket endpoint for terminal I/O."""
        # Validate token (skip if no_auth)
        if not app.state.no_auth and token != app.state.token:
            await websocket.close(code=4001)
            return

        await websocket.accept()
        logger.info("WebSocket connection accepted")

        # Close any existing connection (single client mode)
        if app.state.active_websocket is not None:
            try:
                await app.state.active_websocket.close(code=4002)
                logger.info("Closed previous WebSocket connection")
            except Exception:
                pass
        if app.state.read_task is not None:
            app.state.read_task.cancel()
            app.state.read_task = None

        app.state.active_websocket = websocket

        # Spawn tmux if not already running
        if app.state.master_fd is None:
            try:
                master_fd, child_pid = spawn_tmux(config.session_name)
                app.state.master_fd = master_fd
                app.state.child_pid = child_pid
                logger.info(f"Spawned tmux session: {config.session_name}")
            except Exception as e:
                logger.error(f"Failed to spawn tmux: {e}")
                await websocket.send_json({"type": "error", "message": str(e)})
                await websocket.close()
                return

        master_fd = app.state.master_fd

        # Create tasks for bidirectional I/O
        async def read_from_terminal():
            """Read from terminal and send to WebSocket."""
            loop = asyncio.get_event_loop()
            while app.state.active_websocket == websocket:
                try:
                    data = await loop.run_in_executor(
                        None, lambda: os.read(master_fd, 4096)
                    )
                    if not data:
                        break
                    if app.state.active_websocket == websocket:
                        await websocket.send_bytes(data)
                except Exception as e:
                    if app.state.active_websocket == websocket:
                        logger.error(f"Error reading from terminal: {e}")
                    break

        async def write_to_terminal():
            """Read from WebSocket and write to terminal."""
            while app.state.active_websocket == websocket:
                try:
                    message = await websocket.receive()

                    # Check message type safely
                    if not isinstance(message, dict):
                        continue

                    msg_type = message.get("type", "")
                    if msg_type == "websocket.disconnect":
                        break

                    if "bytes" in message:
                        os.write(master_fd, message["bytes"])
                    elif "text" in message:
                        text = message["text"]
                        # Handle JSON messages (resize, input)
                        try:
                            data = json.loads(text)
                            if isinstance(data, dict):
                                if data.get("type") == "resize":
                                    set_terminal_size(
                                        master_fd,
                                        data.get("cols", 80),
                                        data.get("rows", 24),
                                    )
                                elif data.get("type") == "input":
                                    os.write(master_fd, data["data"].encode())
                            else:
                                # JSON but not dict, treat as plain text
                                os.write(master_fd, text.encode())
                        except (json.JSONDecodeError, TypeError):
                            # Plain text input
                            os.write(master_fd, text.encode())

                except WebSocketDisconnect:
                    break
                except Exception as e:
                    if app.state.active_websocket == websocket:
                        logger.error(f"Error writing to terminal: {e}")
                    break

        # Run both tasks concurrently
        read_task = asyncio.create_task(read_from_terminal())
        app.state.read_task = read_task
        write_task = asyncio.create_task(write_to_terminal())

        try:
            await asyncio.gather(read_task, write_task)
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        finally:
            read_task.cancel()
            write_task.cancel()
            if app.state.active_websocket == websocket:
                app.state.active_websocket = None
                app.state.read_task = None
            logger.info("WebSocket connection closed")

    @app.on_event("startup")
    async def startup():
        """Print access URL on startup."""
        if app.state.no_auth:
            url = f"http://localhost:{config.port}/"
            print(f"\n{'=' * 60}")
            print(f"Mobile Terminal Overlay v0.1.0")
            print(f"{'=' * 60}")
            print(f"Session: {config.session_name}")
            print(f"Auth:    DISABLED (--no-auth)")
            print(f"URL:     {url}")
            print(f"{'=' * 60}\n")
        else:
            url = f"http://localhost:{config.port}/?token={app.state.token}"
            print(f"\n{'=' * 60}")
            print(f"Mobile Terminal Overlay v0.1.0")
            print(f"{'=' * 60}")
            print(f"Session: {config.session_name}")
            print(f"Token:   {app.state.token}")
            print(f"URL:     {url}")
            print(f"{'=' * 60}\n")

    @app.on_event("shutdown")
    async def shutdown():
        """Cleanup on shutdown."""
        if app.state.master_fd is not None:
            try:
                os.close(app.state.master_fd)
            except Exception:
                pass

    return app


def spawn_tmux(session_name: str) -> tuple:
    """
    Spawn a tmux session with a pty.

    Uses `tmux new -A -s <session>` which:
    - Creates the session if it doesn't exist
    - Attaches to it if it does exist

    Args:
        session_name: Name of the tmux session.

    Returns:
        Tuple of (master_fd, child_pid).
    """
    master_fd, slave_fd = pty.openpty()

    pid = os.fork()
    if pid == 0:
        # Child process
        os.setsid()
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        os.close(master_fd)
        os.close(slave_fd)

        # Execute tmux
        os.execvp("tmux", ["tmux", "new", "-A", "-s", session_name])
    else:
        # Parent process
        os.close(slave_fd)
        return master_fd, pid


def set_terminal_size(fd: int, cols: int, rows: int) -> None:
    """
    Set terminal size using TIOCSWINSZ ioctl.

    Args:
        fd: File descriptor of the pty master.
        cols: Number of columns.
        rows: Number of rows.
    """
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
