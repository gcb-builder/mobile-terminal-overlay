"""
FastAPI server for Mobile Terminal Overlay.

Provides:
- Static file serving for the web UI
- WebSocket endpoint for terminal I/O
- Token-based authentication
"""

import asyncio
import atexit
import fcntl
import json
import logging
import os
import pty
import secrets
import signal
import struct
import termios
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional


class RingBuffer:
    """Thread-safe ring buffer for storing PTY output."""

    def __init__(self, max_size: int = 1024 * 1024):  # 1MB default
        self._buffer = deque(maxlen=max_size)
        self._lock = threading.Lock()

    def write(self, data: bytes) -> None:
        """Append data to buffer."""
        with self._lock:
            self._buffer.extend(data)

    def read_all(self) -> bytes:
        """Read all buffered data without clearing."""
        with self._lock:
            return bytes(self._buffer)

    def clear(self) -> None:
        """Clear the buffer."""
        with self._lock:
            self._buffer.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)

from fastapi import FastAPI, File, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Config, Repo

logger = logging.getLogger(__name__)

# Directory containing static files
STATIC_DIR = Path(__file__).parent / "static"

# Directory for transcript logs (pipe-pane output)
TRANSCRIPT_DIR = Path.home() / ".cache" / "mobile-overlay" / "transcripts"


def get_transcript_log_path(session_name: str, window: int = 0, pane: int = 0) -> Path:
    """Get the transcript log file path for a session/window/pane."""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    return TRANSCRIPT_DIR / f"{session_name}_w{window}_p{pane}.log"


def enable_pipe_pane(session_name: str, window: int = 0, pane: int = 0) -> Optional[Path]:
    """
    Enable tmux pipe-pane for a session to capture output to a log file.
    Returns the log file path if successful, None otherwise.
    """
    import subprocess

    log_path = get_transcript_log_path(session_name, window, pane)
    target = f"{session_name}:{window}.{pane}"

    try:
        # -o = don't double-pipe if already enabled
        result = subprocess.run(
            ["tmux", "pipe-pane", "-o", "-t", target, f"cat >> {log_path}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            logger.info(f"Enabled pipe-pane for {target} -> {log_path}")
            return log_path
        else:
            logger.warning(f"pipe-pane failed for {target}: {result.stderr}")
            return None
    except Exception as e:
        logger.error(f"Error enabling pipe-pane: {e}")
        return None


def list_tmux_sessions(prefix: str = "") -> list:
    """List tmux sessions, optionally filtered by prefix."""
    import subprocess

    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return []

        sessions = [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]
        if prefix:
            sessions = [s for s in sessions if s.startswith(prefix)]
        return sessions
    except Exception as e:
        logger.error(f"Error listing tmux sessions: {e}")
        return []


def _sigchld_handler(signum, frame):
    """Reap zombie child processes."""
    try:
        while True:
            pid, status = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
            logger.debug(f"Reaped child process {pid} with status {status}")
    except ChildProcessError:
        pass  # No child processes


# Install SIGCHLD handler to prevent zombie processes
signal.signal(signal.SIGCHLD, _sigchld_handler)


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
    app.state.current_session = config.session_name  # Track current session
    app.state.last_ws_connect = 0  # Timestamp of last WebSocket connection
    app.state.ws_connect_lock = asyncio.Lock()  # Prevent concurrent connection handling
    app.state.output_buffer = RingBuffer(max_size=2 * 1024 * 1024)  # 2MB scrollback buffer

    # Mount static files
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/sw.js")
    async def service_worker():
        """Serve service worker from root with proper headers."""
        sw_path = STATIC_DIR / "sw.js"
        if sw_path.exists():
            return FileResponse(
                sw_path,
                media_type="application/javascript",
                headers={"Service-Worker-Allowed": "/"},
            )
        return HTMLResponse(status_code=404)

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

    @app.get("/api/tmux/sessions")
    async def get_tmux_sessions(
        token: Optional[str] = Query(None),
        prefix: str = Query(""),
    ):
        """
        List available tmux sessions.
        Optionally filter by prefix (e.g., 'claude-' for Claude sessions).
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        sessions = list_tmux_sessions(prefix)
        return {
            "sessions": sessions,
            "current": app.state.current_session,
            "prefix": prefix,
        }

    @app.get("/api/files/search")
    async def search_files(q: str = Query(""), token: Optional[str] = Query(None), limit: int = Query(20)):
        """
        Search files in the current repo.
        Uses git ls-files to respect .gitignore.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        if not q or len(q) < 1:
            return {"files": []}

        try:
            import subprocess

            # Get list of tracked files using git ls-files
            result = subprocess.run(
                ["git", "ls-files"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            if result.returncode != 0:
                # Fallback to find if not a git repo
                result = subprocess.run(
                    ["find", ".", "-type", "f", "-name", f"*{q}*", "-not", "-path", "./.git/*"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                files = [f.lstrip("./") for f in result.stdout.strip().split("\n") if f][:limit]
            else:
                # Filter files by query (case-insensitive)
                all_files = result.stdout.strip().split("\n")
                q_lower = q.lower()
                files = [f for f in all_files if q_lower in f.lower()][:limit]

            return {"files": files, "query": q}

        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Search timeout"}, status_code=504)
        except Exception as e:
            logger.error(f"File search error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/transcript")
    async def get_transcript(
        token: Optional[str] = Query(None),
        lines: int = Query(10000),
        source: str = Query("auto"),  # "auto", "log", or "capture"
    ):
        """
        Get terminal transcript.

        Sources:
        - "log": Read from pipe-pane log file (cleanest, if available)
        - "capture": Use tmux capture-pane (fallback)
        - "auto": Try log first, fall back to capture-pane
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session_name = app.state.current_session
        log_path = get_transcript_log_path(session_name)

        # Try reading from log file first (if source is auto or log)
        if source in ("auto", "log") and log_path.exists():
            try:
                with open(log_path, "r", errors="replace") as f:
                    # Read last N lines efficiently
                    content = f.read()
                    all_lines = content.split("\n")
                    if len(all_lines) > lines:
                        all_lines = all_lines[-lines:]
                    text = "\n".join(all_lines)
                return {
                    "text": text,
                    "session": session_name,
                    "source": "log",
                    "log_path": str(log_path),
                }
            except Exception as e:
                logger.warning(f"Error reading log file: {e}")
                if source == "log":
                    return JSONResponse({"error": f"Log file error: {e}"}, status_code=500)
                # Fall through to capture-pane

        # Fallback to tmux capture-pane
        try:
            import subprocess
            result = subprocess.run(
                ["tmux", "capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", session_name],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return JSONResponse(
                    {"error": f"tmux capture-pane failed: {result.stderr}"},
                    status_code=500,
                )
            return {
                "text": result.stdout,
                "session": session_name,
                "source": "capture",
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Capture timeout"}, status_code=504)
        except Exception as e:
            logger.error(f"Transcript error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/refresh")
    async def refresh_terminal(token: Optional[str] = Query(None)):
        """
        Get current terminal snapshot for refresh (without full history).
        Uses capture-pane with visible content only.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        try:
            import subprocess
            session_name = app.state.current_session
            result = subprocess.run(
                ["tmux", "capture-pane", "-p", "-J", "-S", "-5000", "-t", session_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return JSONResponse(
                    {"error": f"tmux capture-pane failed: {result.stderr}"},
                    status_code=500,
                )
            return {"text": result.stdout, "session": session_name}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Refresh timeout"}, status_code=504)
        except Exception as e:
            logger.error(f"Refresh error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/upload")
    async def upload_image(
        file: UploadFile = File(...),
        token: Optional[str] = Query(None),
    ):
        """
        Upload an image file for use in terminal prompts.

        Saves to .claude/uploads/ directory (git-ignored).
        Returns the relative path for insertion into terminal.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Validate content type
        allowed_types = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
        if file.content_type not in allowed_types:
            return JSONResponse(
                {"error": f"Invalid file type: {file.content_type}. Allowed: png, jpeg, webp, gif"},
                status_code=400,
            )

        # Read file content and check size (max 5MB)
        max_size = 5 * 1024 * 1024  # 5MB
        content = await file.read()
        if len(content) > max_size:
            return JSONResponse(
                {"error": f"File too large: {len(content)} bytes. Max: {max_size} bytes"},
                status_code=400,
            )

        # Create uploads directory
        uploads_dir = Path(".claude/uploads")
        uploads_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename with timestamp
        ext = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "png"
        timestamp = int(time.time() * 1000)
        filename = f"img-{timestamp}.{ext}"
        filepath = uploads_dir / filename

        # Write file
        try:
            with open(filepath, "wb") as f:
                f.write(content)
            logger.info(f"Uploaded image: {filepath}")
            return {"path": str(filepath), "filename": filename, "size": len(content)}
        except Exception as e:
            logger.error(f"Failed to save upload: {e}")
            return JSONResponse({"error": "Failed to save file"}, status_code=500)

    @app.get("/current-session")
    async def get_current_session(token: Optional[str] = Query(None)):
        """Return current session name."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return {"session": app.state.current_session}

    @app.post("/switch-repo")
    async def switch_repo(session: str = Query(...), token: Optional[str] = Query(None)):
        """
        Switch to a different tmux session (repo).

        This closes the current pty and prepares for a new connection.
        The client should reconnect the WebSocket after this call.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Validate session is in configured repos (or is the default session)
        valid_sessions = [r.session for r in config.repos] + [config.session_name]
        if session not in valid_sessions:
            return JSONResponse({"error": f"Unknown session: {session}"}, status_code=400)

        # Close current WebSocket connection
        if app.state.active_websocket is not None:
            try:
                await app.state.active_websocket.close(code=4003)  # 4003 = switching repos
            except Exception:
                pass
            app.state.active_websocket = None

        # Cancel read task
        if app.state.read_task is not None:
            app.state.read_task.cancel()
            app.state.read_task = None

        # Kill child process and close pty
        if app.state.child_pid is not None:
            try:
                os.kill(app.state.child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass  # Process already dead
        if app.state.master_fd is not None:
            try:
                os.close(app.state.master_fd)
            except Exception:
                pass
        app.state.master_fd = None
        app.state.child_pid = None

        # Clear output buffer (don't replay old session's content)
        app.state.output_buffer.clear()

        # Update current session
        app.state.current_session = session
        logger.info(f"Switched to session: {session}")

        return {"status": "ok", "session": session}

    @app.websocket("/ws/terminal")
    async def terminal_websocket(websocket: WebSocket, token: Optional[str] = Query(None)):
        """WebSocket endpoint for terminal I/O."""
        # Validate token (skip if no_auth)
        if not app.state.no_auth and token != app.state.token:
            await websocket.close(code=4001)
            return

        # Use lock to prevent concurrent connection setup
        async with app.state.ws_connect_lock:
            # Rate limit connections - minimum 500ms between accepts
            now = time.time()
            elapsed = now - app.state.last_ws_connect
            if elapsed < 0.5:
                logger.info(f"Rate limiting WebSocket connection ({elapsed:.2f}s since last)")
                await websocket.close(code=4004)  # 4004 = rate limited
                return
            app.state.last_ws_connect = now

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
                session_name = app.state.current_session
                master_fd, child_pid = spawn_tmux(session_name)
                app.state.master_fd = master_fd
                app.state.child_pid = child_pid
                logger.info(f"Spawned tmux session: {session_name}")

                # Enable pipe-pane for transcript logging (after short delay for tmux to be ready)
                await asyncio.sleep(0.5)
                log_path = enable_pipe_pane(session_name)
                if log_path:
                    logger.info(f"Transcript logging enabled: {log_path}")
            except Exception as e:
                logger.error(f"Failed to spawn tmux: {e}")
                await websocket.send_json({"type": "error", "message": str(e)})
                await websocket.close()
                return
        else:
            # Existing session - ensure pipe-pane is enabled
            session_name = app.state.current_session
            enable_pipe_pane(session_name)

        master_fd = app.state.master_fd
        output_buffer = app.state.output_buffer

        # Send history snapshot using tmux capture-pane (cleaner than raw buffer replay)
        try:
            import subprocess
            session_name = app.state.current_session
            result = subprocess.run(
                ["tmux", "capture-pane", "-p", "-J", "-S", "-5000", "-t", session_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout:
                history_text = result.stdout
                logger.info(f"Sending {len(history_text)} chars of capture-pane history")
                await websocket.send_text(history_text)
        except subprocess.TimeoutExpired:
            logger.warning("tmux capture-pane timed out")
        except Exception as e:
            logger.error(f"Error getting capture-pane history: {e}")

        # Create tasks for bidirectional I/O
        async def read_from_terminal():
            """Read from terminal and send to WebSocket with batching."""
            loop = asyncio.get_event_loop()
            batch = bytearray()
            last_flush = time.time()
            flush_interval = 0.03  # 30ms batching window

            while app.state.active_websocket == websocket:
                try:
                    # Non-blocking read with select-like behavior
                    data = await loop.run_in_executor(
                        None, lambda: os.read(master_fd, 4096)
                    )
                    if not data:
                        break

                    # Store in ring buffer for future reconnects
                    output_buffer.write(data)

                    # Add to batch
                    batch.extend(data)

                    # Flush if batch is large or enough time has passed
                    now = time.time()
                    if len(batch) >= 8192 or (now - last_flush) >= flush_interval:
                        if app.state.active_websocket == websocket and batch:
                            await websocket.send_bytes(bytes(batch))
                            batch.clear()
                            last_flush = now

                except Exception as e:
                    if app.state.active_websocket == websocket:
                        logger.error(f"Error reading from terminal: {e}")
                    break

            # Flush remaining data
            if batch and app.state.active_websocket == websocket:
                try:
                    await websocket.send_bytes(bytes(batch))
                except Exception:
                    pass

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
                        logger.info(f"Received text message: {text[:100]}")
                        # Handle JSON messages (resize, input)
                        try:
                            data = json.loads(text)
                            if isinstance(data, dict):
                                if data.get("type") == "resize":
                                    cols = data.get("cols", 80)
                                    rows = data.get("rows", 24)
                                    logger.info(f"Resize request: {cols}x{rows}, fd={master_fd}, pid={app.state.child_pid}")
                                    set_terminal_size(
                                        master_fd,
                                        cols,
                                        rows,
                                        app.state.child_pid,
                                    )
                                    logger.info(f"Terminal resized to {cols}x{rows}")
                                elif data.get("type") == "input":
                                    input_data = data.get("data")
                                    if input_data:
                                        os.write(master_fd, input_data.encode())
                            else:
                                # JSON but not dict, treat as plain text
                                os.write(master_fd, text.encode())
                        except (json.JSONDecodeError, TypeError, KeyError):
                            # Plain text input
                            os.write(master_fd, text.encode())

                except WebSocketDisconnect:
                    break
                except (OSError, IOError) as e:
                    # Terminal write errors are fatal (terminal closed, etc.)
                    if app.state.active_websocket == websocket:
                        logger.error(f"Error writing to terminal: {e}")
                    break
                except Exception as e:
                    # Log but continue on other errors (malformed messages, etc.)
                    if app.state.active_websocket == websocket:
                        logger.warning(f"Ignoring malformed message: {e}")
                    continue

        # Run both tasks concurrently
        read_task = asyncio.create_task(read_from_terminal())
        app.state.read_task = read_task
        write_task = asyncio.create_task(write_to_terminal())

        try:
            await asyncio.gather(read_task, write_task)
        except asyncio.CancelledError:
            # Normal termination when connection is replaced or closed
            pass
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


def set_terminal_size(fd: int, cols: int, rows: int, child_pid: int = None) -> None:
    """
    Set terminal size using TIOCSWINSZ ioctl.

    Args:
        fd: File descriptor of the pty master.
        cols: Number of columns.
        rows: Number of rows.
        child_pid: Optional child process ID to send SIGWINCH for redraw.
    """
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)

    # Send SIGWINCH to trigger tmux redraw
    if child_pid:
        try:
            os.kill(child_pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass
