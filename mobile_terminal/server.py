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
import re
import secrets
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, List, Optional

from mobile_terminal.challenge import run_challenge, get_available_models, DEFAULT_MODEL


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


class InputQueue:
    """
    Async queue for serializing PTY writes with quiet-wait and ACKs.

    Features:
    - Single writer per session (serialized writes)
    - Waits for quiet period before sending (reduces tmux races)
    - Tracks message IDs for ACKs
    - Cooldown between sends
    """

    QUIET_MS = 350  # Wait for output to settle
    COOLDOWN_MS = 200  # Min time between sends
    QUIET_TIMEOUT_MS = 2000  # Max wait for quiet

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._last_output_ts: float = 0
        self._last_send_ts: float = 0
        self._pending_acks: dict = {}  # msg_id -> asyncio.Event
        self._lock = asyncio.Lock()
        self._running = False
        self._writer_task = None

    def update_output_ts(self):
        """Called when PTY output is received."""
        self._last_output_ts = time.time()

    async def send(self, msg_id: str, data: bytes, master_fd: int, websocket) -> bool:
        """
        Queue a send request and wait for ACK.
        Returns True if send was acknowledged, False on timeout.
        """
        event = asyncio.Event()
        self._pending_acks[msg_id] = event

        await self._queue.put((msg_id, data, master_fd, websocket))

        try:
            # Wait for ACK with timeout
            await asyncio.wait_for(event.wait(), timeout=2.0)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._pending_acks.pop(msg_id, None)

    def ack(self, msg_id: str):
        """Acknowledge a message was processed."""
        if msg_id in self._pending_acks:
            self._pending_acks[msg_id].set()

    async def _wait_for_quiet(self):
        """Wait for output to settle before sending."""
        deadline = time.time() + (self.QUIET_TIMEOUT_MS / 1000)

        while time.time() < deadline:
            since_output = (time.time() - self._last_output_ts) * 1000
            if since_output >= self.QUIET_MS:
                return True

            # Wait a bit more
            wait_time = min(
                (self.QUIET_MS - since_output) / 1000,
                deadline - time.time()
            )
            if wait_time > 0:
                await asyncio.sleep(wait_time)

        # Timeout reached, send anyway
        return False

    async def _writer_loop(self):
        """Process queued sends one at a time."""
        self._running = True

        while self._running:
            try:
                msg_id, data, master_fd, websocket = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            try:
                async with self._lock:
                    # Wait for quiet period
                    await self._wait_for_quiet()

                    # Enforce cooldown
                    since_send = (time.time() - self._last_send_ts) * 1000
                    if since_send < self.COOLDOWN_MS:
                        await asyncio.sleep((self.COOLDOWN_MS - since_send) / 1000)

                    # Write to PTY
                    os.write(master_fd, data)
                    self._last_send_ts = time.time()

                    # Send ACK to client
                    if websocket:
                        try:
                            await websocket.send_json({
                                "type": "ack",
                                "id": msg_id
                            })
                        except Exception:
                            pass

                    # Mark as acknowledged internally
                    self.ack(msg_id)

            except Exception as e:
                logger.error(f"InputQueue write error: {e}")

            self._queue.task_done()

    def start(self):
        """Start the writer loop."""
        if self._writer_task is None:
            self._writer_task = asyncio.create_task(self._writer_loop())

    def stop(self):
        """Stop the writer loop."""
        self._running = False
        if self._writer_task:
            self._writer_task.cancel()
            self._writer_task = None


@dataclass
class QueueItem:
    """A command waiting to be sent to the terminal."""
    id: str
    text: str
    policy: str  # "safe" | "unsafe"
    status: str  # "queued" | "pending" | "sent" | "failed"
    created_at: float
    sent_at: Optional[float] = None
    error: Optional[str] = None


class CommandQueue:
    """
    Per-session command queue with ready-gate, policy enforcement, and pause/resume.

    Commands are held until the terminal is ready (quiet + prompt visible).
    Safe commands auto-send; unsafe commands require manual confirmation.
    """

    QUIET_MS = 400           # Wait for output quiet before sending
    COOLDOWN_MS = 250        # Between sends
    CHECK_INTERVAL_MS = 100  # How often to check ready state

    # Patterns indicating terminal is ready for input
    PROMPT_PATTERNS = [
        r'❯\s*$',            # Claude Code prompt
        r'\$\s*$',           # Bash prompt
        r'#\s*$',            # Root prompt
        r'>>>\s*$',          # Python REPL
        r'>\s*$',            # Node REPL
        r'\[y/n\]',          # Yes/no prompt
        r'\[[1-9]\]',        # Numbered options
    ]

    # Patterns for commands that are safe to auto-send
    SAFE_PATTERNS = [
        r'^[1-9]$',          # Single digit
        r'^[yn]$',           # y/n
        r'^(yes|no)$',       # yes/no
        r'^$',               # Empty (just Enter)
    ]

    # Patterns for unsafe commands (require confirmation)
    UNSAFE_PATTERNS = [
        r'[|;&><]',          # Shell metacharacters
        r'^\s*sudo\s',       # sudo commands
        r'^\s*rm\s',         # rm commands
        r'^\s*git\s+push',   # git push
    ]

    def __init__(self):
        self._queues: Dict[str, List[QueueItem]] = {}
        self._paused: Dict[str, bool] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._processor_task = None
        self._app = None  # Set during startup

    def set_app(self, app):
        """Set the FastAPI app reference for accessing state."""
        self._app = app

    def _get_queue(self, session: str) -> List[QueueItem]:
        """Get or create queue for session."""
        if session not in self._queues:
            self._queues[session] = []
        return self._queues[session]

    def _classify_policy(self, text: str) -> str:
        """Determine if command is safe or unsafe."""
        text = text.strip()

        # Check unsafe patterns first
        for pattern in self.UNSAFE_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return "unsafe"

        # Check safe patterns
        for pattern in self.SAFE_PATTERNS:
            if re.match(pattern, text, re.IGNORECASE):
                return "safe"

        # Long commands are unsafe
        if len(text) > 50:
            return "unsafe"

        # Multi-word commands are unsafe
        if len(text.split()) > 3:
            return "unsafe"

        # Default to safe for short, simple commands
        return "safe"

    def enqueue(self, session: str, text: str, policy: str = "auto") -> QueueItem:
        """Add a command to the queue."""
        queue = self._get_queue(session)

        # Determine policy
        if policy == "auto":
            policy = self._classify_policy(text)

        item = QueueItem(
            id=str(uuid.uuid4()),
            text=text,
            policy=policy,
            status="queued",
            created_at=time.time(),
        )
        queue.append(item)
        return item

    def dequeue(self, session: str, item_id: str) -> bool:
        """Remove an item from the queue."""
        queue = self._get_queue(session)
        for i, item in enumerate(queue):
            if item.id == item_id:
                queue.pop(i)
                return True
        return False

    def reorder(self, session: str, item_id: str, new_index: int) -> bool:
        """Move an item to a new position."""
        queue = self._get_queue(session)
        for i, item in enumerate(queue):
            if item.id == item_id:
                queue.pop(i)
                new_index = max(0, min(new_index, len(queue)))
                queue.insert(new_index, item)
                return True
        return False

    def list_items(self, session: str) -> List[QueueItem]:
        """Get all items in the queue."""
        return self._get_queue(session).copy()

    def pause(self, session: str) -> None:
        """Pause queue processing for a session."""
        self._paused[session] = True

    def resume(self, session: str) -> None:
        """Resume queue processing for a session."""
        self._paused[session] = False

    def is_paused(self, session: str) -> bool:
        """Check if queue is paused."""
        return self._paused.get(session, False)

    def flush(self, session: str) -> int:
        """Clear all queued items. Returns count cleared."""
        queue = self._get_queue(session)
        count = len(queue)
        queue.clear()
        return count

    def get_next_unsafe(self, session: str) -> Optional[QueueItem]:
        """Get the next unsafe item waiting for manual send."""
        queue = self._get_queue(session)
        for item in queue:
            if item.status == "queued" and item.policy == "unsafe":
                return item
        return None

    async def _check_ready(self, session: str) -> bool:
        """
        Check if terminal is ready to receive input:
        1. Output has been quiet for QUIET_MS
        2. Prompt is visible
        """
        if not self._app:
            return False

        # Check quiet period
        input_queue = self._app.state.input_queue
        since_output = (time.time() - input_queue._last_output_ts) * 1000
        if since_output < self.QUIET_MS:
            return False

        # Check for prompt via tmux capture-pane
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", session, "-p", "-S", "-5"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode != 0:
                return False

            content = result.stdout
            # Check if any prompt pattern matches
            for pattern in self.PROMPT_PATTERNS:
                if re.search(pattern, content, re.MULTILINE):
                    return True

            # Check pane title for Claude Code waiting state
            title_result = subprocess.run(
                ["tmux", "display-message", "-p", "-t", session, "#{pane_title}"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if "Signal Detection Pending" in title_result.stdout:
                return True

        except Exception as e:
            logger.warning(f"Ready check failed: {e}")

        return False

    async def _send_item(self, session: str, item: QueueItem) -> bool:
        """Send a single item to the terminal."""
        if not self._app:
            return False

        item.status = "pending"

        try:
            master_fd = self._app.state.master_fd
            websocket = self._app.state.active_websocket

            if not master_fd:
                item.status = "failed"
                item.error = "No PTY available"
                return False

            # Send via InputQueue for proper serialization
            success = await self._app.state.input_queue.send(
                msg_id=item.id,
                data=(item.text + '\r').encode('utf-8'),
                master_fd=master_fd,
                websocket=websocket,
            )

            if success:
                item.status = "sent"
                item.sent_at = time.time()

                # Notify client
                if websocket:
                    try:
                        await websocket.send_json({
                            "type": "queue_sent",
                            "id": item.id,
                            "sent_at": item.sent_at,
                        })
                    except Exception:
                        pass

                return True
            else:
                item.status = "failed"
                item.error = "Send timeout"
                return False

        except Exception as e:
            item.status = "failed"
            item.error = str(e)
            return False

    async def send_next_unsafe(self, session: str, item_id: Optional[str] = None) -> Optional[QueueItem]:
        """Manually send the next unsafe item (or specific item)."""
        queue = self._get_queue(session)

        # Find the item
        item = None
        if item_id:
            for i in queue:
                if i.id == item_id and i.status == "queued":
                    item = i
                    break
        else:
            item = self.get_next_unsafe(session)

        if not item:
            return None

        # Wait for ready and send
        for _ in range(20):  # Try for 2 seconds
            if await self._check_ready(session):
                await self._send_item(session, item)
                return item
            await asyncio.sleep(0.1)

        # Timeout waiting for ready, send anyway
        await self._send_item(session, item)
        return item

    async def _process_loop(self) -> None:
        """Main processing loop - runs continuously."""
        self._running = True

        while self._running:
            await asyncio.sleep(self.CHECK_INTERVAL_MS / 1000)

            if not self._app:
                continue

            current_session = self._app.state.current_session
            if not current_session:
                continue

            # Process only current session
            if self.is_paused(current_session):
                continue

            queue = self._get_queue(current_session)
            if not queue:
                continue

            # Get first queued safe item
            item = None
            for i in queue:
                if i.status == "queued" and i.policy == "safe":
                    item = i
                    break

            if not item:
                continue

            # Check ready gate
            if not await self._check_ready(current_session):
                continue

            # Send the item
            await self._send_item(current_session, item)

            # Cooldown
            await asyncio.sleep(self.COOLDOWN_MS / 1000)

    def start(self):
        """Start the processor loop."""
        if self._processor_task is None:
            self._processor_task = asyncio.create_task(self._process_loop())

    def stop(self):
        """Stop the processor loop."""
        self._running = False
        if self._processor_task:
            self._processor_task.cancel()
            self._processor_task = None


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
    app.state.input_queue = InputQueue()  # Serialized input queue with ACKs
    app.state.command_queue = CommandQueue()  # Deferred-send command queue
    app.state.command_queue.set_app(app)

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
                headers={
                    "Service-Worker-Allowed": "/",
                    "Cache-Control": "no-cache, no-store, must-revalidate",
                },
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
        return FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

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

    def get_current_repo_path() -> Optional[Path]:
        """Get the path of the current repo based on session name."""
        session_name = app.state.current_session
        # Check if session matches a configured repo
        for repo in config.repos:
            if repo.session == session_name:
                return Path(repo.path)
        # Fall back to project_root if set
        if config.project_root:
            return config.project_root
        # Fall back to current working directory
        return Path.cwd()

    @app.get("/api/context")
    async def get_context(token: Optional[str] = Query(None)):
        """
        Get the .claude/CONTEXT.md file from the current repo.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        context_file = repo_path / ".claude" / "CONTEXT.md"

        if not context_file.exists():
            return {
                "exists": False,
                "content": "",
                "path": str(context_file),
                "session": app.state.current_session,
            }

        try:
            content = context_file.read_text(errors="replace")
            return {
                "exists": True,
                "content": content,
                "path": str(context_file),
                "session": app.state.current_session,
                "modified": context_file.stat().st_mtime,
            }
        except Exception as e:
            logger.error(f"Error reading context file: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/touch")
    async def get_touch(token: Optional[str] = Query(None)):
        """
        Get the .claude/touch-summary.md file from the current repo.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        touch_file = repo_path / ".claude" / "touch-summary.md"

        if not touch_file.exists():
            return {
                "exists": False,
                "content": "",
                "path": str(touch_file),
                "session": app.state.current_session,
            }

        try:
            content = touch_file.read_text(errors="replace")
            return {
                "exists": True,
                "content": content,
                "path": str(touch_file),
                "session": app.state.current_session,
                "modified": touch_file.stat().st_mtime,
            }
        except Exception as e:
            logger.error(f"Error reading touch file: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/log")
    async def get_log(token: Optional[str] = Query(None), limit: int = Query(200)):
        """
        Get the Claude conversation log from ~/.claude/projects/.
        Finds the most recently modified .jsonl file for the current repo.
        Parses JSONL and returns readable conversation text.
        """
        import json
        import re

        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        if not repo_path:
            return {"exists": False, "content": "", "error": "No repo path found"}

        # Convert repo path to Claude's project identifier format
        # e.g., /home/user/dev/myproject -> -home-user-dev-myproject
        project_id = str(repo_path.resolve()).replace("/", "-")
        claude_projects_dir = Path.home() / ".claude" / "projects" / project_id

        if not claude_projects_dir.exists():
            return {
                "exists": False,
                "content": "",
                "path": str(claude_projects_dir),
                "session": app.state.current_session,
            }

        # Find the most recently modified .jsonl file
        jsonl_files = list(claude_projects_dir.glob("*.jsonl"))
        if not jsonl_files:
            return {
                "exists": False,
                "content": "",
                "path": str(claude_projects_dir),
                "session": app.state.current_session,
            }

        # Sort by modification time, most recent first
        jsonl_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        log_file = jsonl_files[0]

        try:
            raw_content = log_file.read_text(errors="replace")
            lines = raw_content.strip().split('\n')

            # Parse JSONL and extract conversation
            conversation = []
            for line in lines:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    msg_type = entry.get('type')
                    message = entry.get('message', {})

                    if msg_type == 'user':
                        content = message.get('content', '')
                        if isinstance(content, str) and content.strip():
                            conversation.append(f"$ {content}")

                    elif msg_type == 'assistant':
                        content = message.get('content', [])
                        if isinstance(content, str):
                            conversation.append(content)
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict):
                                    if block.get('type') == 'text':
                                        text = block.get('text', '')
                                        if text.strip():
                                            conversation.append(text)
                                    elif block.get('type') == 'tool_use':
                                        tool_name = block.get('name', 'tool')
                                        tool_input = block.get('input', {})
                                        # Format tool call nicely
                                        if tool_name == 'Bash':
                                            cmd = tool_input.get('command', '')
                                            conversation.append(f"• Bash: {cmd[:200]}")
                                        elif tool_name in ('Read', 'Edit', 'Write', 'Glob', 'Grep'):
                                            path = tool_input.get('file_path') or tool_input.get('path') or tool_input.get('pattern', '')
                                            conversation.append(f"• {tool_name}: {path[:100]}")
                                        elif tool_name == 'AskUserQuestion':
                                            # Show questions with options for user to respond
                                            questions = tool_input.get('questions', [])
                                            for q in questions:
                                                qtext = q.get('question', '')
                                                opts = q.get('options', [])
                                                conversation.append(f"❓ {qtext}")
                                                for i, opt in enumerate(opts, 1):
                                                    label = opt.get('label', '')
                                                    desc = opt.get('description', '')
                                                    conversation.append(f"  {i}. {label}" + (f" - {desc}" if desc else ""))
                                        else:
                                            conversation.append(f"• {tool_name}")
                except json.JSONDecodeError:
                    continue

            # Limit to last N messages
            if len(conversation) > limit:
                conversation = conversation[-limit:]
                truncated = True
            else:
                truncated = False

            content = '\n\n'.join(conversation)

            # Redact potential secrets
            content = re.sub(r'(sk-[a-zA-Z0-9]{20,})', '[REDACTED_API_KEY]', content)
            content = re.sub(r'(ghp_[a-zA-Z0-9]{36,})', '[REDACTED_GITHUB_TOKEN]', content)

            return {
                "exists": True,
                "content": content,
                "path": str(log_file),
                "session": app.state.current_session,
                "modified": log_file.stat().st_mtime,
                "truncated": truncated,
            }
        except Exception as e:
            logger.error(f"Error reading log file: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/terminal/capture")
    async def capture_terminal(
        token: Optional[str] = Query(None),
        lines: int = Query(50),
    ):
        """
        Capture recent terminal output from tmux pane.

        Uses tmux capture-pane to get scrollback buffer.
        Returns last N lines of terminal content.
        Also returns pane_title which Claude Code uses to signal its state.
        """
        import subprocess

        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session = app.state.current_session
        if not session:
            return {"content": "", "error": "No session"}

        try:
            # Capture last N lines from tmux pane
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{lines}"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            # Get pane title - Claude Code sets this to indicate state
            # e.g., "✳ Signal Detection Pending" when waiting for input
            pane_title = ""
            try:
                title_result = subprocess.run(
                    ["tmux", "display-message", "-p", "-t", session, "#{pane_title}"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if title_result.returncode == 0:
                    pane_title = title_result.stdout.strip()
            except Exception:
                pass

            if result.returncode == 0:
                return {
                    "content": result.stdout,
                    "lines": lines,
                    "pane_title": pane_title,
                }
            else:
                return {"content": "", "error": result.stderr, "pane_title": pane_title}
        except Exception as e:
            logger.error(f"Failed to capture terminal: {e}")
            return {"content": "", "error": str(e)}

    @app.get("/api/challenge/models")
    async def get_challenge_models(token: Optional[str] = Query(None)):
        """
        Get list of available AI models for challenge function.

        Returns only models whose provider has a valid API key configured.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        models = get_available_models()
        return {"models": models, "default": DEFAULT_MODEL}

    @app.post("/api/challenge")
    async def challenge_code(
        token: Optional[str] = Query(None),
        model: str = Query(DEFAULT_MODEL),
        problem: str = Query(""),
        include_terminal: bool = Query(True),
        terminal_lines: int = Query(50),
        include_diff: bool = Query(True),
    ):
        """
        Run problem-focused code review using AI models.

        Supports multiple providers: Together.ai, OpenAI, Anthropic.
        User selects model, system routes to appropriate provider.

        Context bundle is built from:
        - User's problem description (required for focused review)
        - Terminal output (optional, captures current state)
        - Git diff (optional, shows recent changes)
        - Minimal project context

        Returns AI's analysis focused on the specific problem.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        if not repo_path:
            return JSONResponse(
                {"error": "No repo path found"},
                status_code=400,
            )

        # Build problem-focused bundle
        bundle_parts = []

        # 1. Problem statement (user-provided)
        if problem.strip():
            bundle_parts.append(f"## Problem Statement\n{problem.strip()}")
        else:
            bundle_parts.append("## Problem Statement\nGeneral code review requested (no specific problem described)")

        # 2. Terminal content (captures current debugging state)
        if include_terminal:
            session = app.state.current_session
            if session:
                try:
                    result = subprocess.run(
                        ["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{terminal_lines}"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        bundle_parts.append(f"## Current Terminal State (last {terminal_lines} lines)\n```\n{result.stdout}\n```")
                except Exception as e:
                    logger.warning(f"Failed to capture terminal: {e}")

        # 3. Git diff (shows recent changes)
        if include_diff:
            try:
                # Get diff stat first
                diff_stat = subprocess.run(
                    ["git", "diff", "--stat"],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                # Get actual diff (limited)
                diff_content = subprocess.run(
                    ["git", "diff"],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if diff_stat.returncode == 0 and diff_stat.stdout.strip():
                    diff_text = diff_content.stdout[:8000] if diff_content.stdout else ""
                    if len(diff_content.stdout) > 8000:
                        diff_text += "\n... [diff truncated]"
                    bundle_parts.append(f"## Uncommitted Changes\n```\n{diff_stat.stdout}\n```\n\n### Diff Detail\n```diff\n{diff_text}\n```")
            except Exception as e:
                logger.warning(f"Failed to get git diff: {e}")

        # 4. Minimal project context (git status + branch)
        try:
            status = subprocess.run(
                ["git", "status", "-sb"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if status.returncode == 0:
                bundle_parts.append(f"## Git Status\n```\n{status.stdout}\n```")
        except Exception:
            pass

        bundle = "\n\n".join(bundle_parts)

        # Call API with problem-focused system prompt
        from .challenge import call_api, MODELS
        if model not in MODELS:
            return JSONResponse({"error": f"Unknown model: {model}"}, status_code=400)

        # Custom system prompt for problem-focused review
        system_prompt = """You are a code reviewer focusing on a SPECIFIC problem described by the user.

Focus your review ONLY on the problem described in the "Problem Statement" section.
Use the terminal output and git diff to understand the current state.

Do not give generic project feedback unrelated to the problem.
Do not suggest running commands.
Be concise and actionable.

Output format:
1. Problem Analysis: [Your understanding of the issue]
2. Potential Causes: [Based on the context provided]
3. Suggested Fix: [Specific actionable suggestions]
4. Risks/Edge Cases: [Things to watch out for]"""

        # Build request manually to use custom system prompt
        model_info = MODELS[model]
        provider_key = model_info["provider"]
        model_id = model_info["model_id"]

        from .challenge import (
            PROVIDERS, get_api_key, validate_api_key,
            build_openai_payload, build_anthropic_payload,
            build_openai_headers, build_anthropic_headers,
            parse_openai_response, parse_anthropic_response,
        )
        import httpx

        provider = PROVIDERS[provider_key]
        api_key = get_api_key(provider_key)
        is_valid, error_msg = validate_api_key(api_key, provider_key)
        if not is_valid:
            return JSONResponse({"error": error_msg}, status_code=400)

        # Build payload with custom system prompt
        fmt = provider["format"]
        if fmt == "openai":
            headers = build_openai_headers(api_key)
            payload = {
                "model": model_id,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": bundle},
                ],
                "temperature": 0.2,
                "max_tokens": 1000,
            }
            parse_response = parse_openai_response
        elif fmt == "anthropic":
            headers = build_anthropic_headers(api_key)
            payload = {
                "model": model_id,
                "system": system_prompt,
                "messages": [
                    {"role": "user", "content": bundle},
                ],
                "temperature": 0.2,
                "max_tokens": 1000,
            }
            parse_response = parse_anthropic_response
        else:
            return JSONResponse({"error": f"Unknown format: {fmt}"}, status_code=400)

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    provider["url"],
                    headers=headers,
                    json=payload,
                )
                if response.status_code != 200:
                    return JSONResponse(
                        {"error": f"API error {response.status_code}: {response.text}"},
                        status_code=500,
                    )
                data = response.json()
                content = parse_response(data)
                result = {
                    "success": True,
                    "content": content,
                    "model": model,
                    "model_name": model_info["name"],
                    "provider": provider_key,
                    "bundle_chars": len(bundle),
                }
        except httpx.TimeoutException:
            return JSONResponse({"error": "Request timed out (120s)"}, status_code=500)
        except Exception as e:
            logger.error(f"Challenge API error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

        if result.get("success"):
            return {
                "success": True,
                "content": result["content"],
                "model": result.get("model"),
                "model_name": result.get("model_name"),
                "provider": result.get("provider"),
                "bundle_chars": result.get("bundle_chars"),
                "usage": result.get("usage", {}),
            }
        else:
            return JSONResponse(
                {"error": result.get("error", "Unknown error")},
                status_code=500,
            )

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

    # ========== Command Queue API Endpoints ==========

    @app.post("/api/queue/enqueue")
    async def queue_enqueue(
        text: str = Query(...),
        session: str = Query(...),
        policy: str = Query("auto"),  # "auto" | "safe" | "unsafe"
        token: Optional[str] = Query(None),
    ):
        """
        Add command to the deferred queue.

        Policy:
        - "auto": server determines safe/unsafe based on text
        - "safe": force auto-send when ready
        - "unsafe": always require manual confirmation
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        item = app.state.command_queue.enqueue(session, text, policy)

        # Notify connected clients
        if app.state.active_websocket:
            try:
                await app.state.active_websocket.send_json({
                    "type": "queue_update",
                    "action": "add",
                    "item": asdict(item),
                })
            except Exception:
                pass

        return {"status": "ok", "item": asdict(item)}

    @app.get("/api/queue/list")
    async def queue_list(
        session: str = Query(...),
        token: Optional[str] = Query(None),
    ):
        """List all queued items for a session."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        items = app.state.command_queue.list_items(session)
        paused = app.state.command_queue.is_paused(session)
        return {
            "items": [asdict(i) for i in items],
            "paused": paused,
            "session": session,
        }

    @app.post("/api/queue/remove")
    async def queue_remove(
        session: str = Query(...),
        item_id: str = Query(...),
        token: Optional[str] = Query(None),
    ):
        """Remove an item from the queue."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        success = app.state.command_queue.dequeue(session, item_id)

        if success and app.state.active_websocket:
            try:
                await app.state.active_websocket.send_json({
                    "type": "queue_update",
                    "action": "remove",
                    "item": {"id": item_id},
                })
            except Exception:
                pass

        return {"status": "ok" if success else "not_found"}

    @app.post("/api/queue/reorder")
    async def queue_reorder(
        session: str = Query(...),
        item_id: str = Query(...),
        new_index: int = Query(...),
        token: Optional[str] = Query(None),
    ):
        """Reorder an item in the queue."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        success = app.state.command_queue.reorder(session, item_id, new_index)
        return {"status": "ok" if success else "not_found"}

    @app.post("/api/queue/pause")
    async def queue_pause(
        session: str = Query(...),
        token: Optional[str] = Query(None),
    ):
        """Pause queue processing for a session."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        app.state.command_queue.pause(session)

        if app.state.active_websocket:
            try:
                await app.state.active_websocket.send_json({
                    "type": "queue_state",
                    "paused": True,
                    "count": len(app.state.command_queue.list_items(session)),
                })
            except Exception:
                pass

        return {"status": "ok", "paused": True}

    @app.post("/api/queue/resume")
    async def queue_resume(
        session: str = Query(...),
        token: Optional[str] = Query(None),
    ):
        """Resume queue processing for a session."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        app.state.command_queue.resume(session)

        if app.state.active_websocket:
            try:
                await app.state.active_websocket.send_json({
                    "type": "queue_state",
                    "paused": False,
                    "count": len(app.state.command_queue.list_items(session)),
                })
            except Exception:
                pass

        return {"status": "ok", "paused": False}

    @app.post("/api/queue/flush")
    async def queue_flush(
        session: str = Query(...),
        confirm: bool = Query(False),
        token: Optional[str] = Query(None),
    ):
        """Clear all queued items. Requires confirm=true."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        if not confirm:
            items = app.state.command_queue.list_items(session)
            return {"status": "confirm_required", "count": len(items)}

        count = app.state.command_queue.flush(session)

        if app.state.active_websocket:
            try:
                await app.state.active_websocket.send_json({
                    "type": "queue_state",
                    "paused": False,
                    "count": 0,
                })
            except Exception:
                pass

        return {"status": "ok", "cleared": count}

    @app.post("/api/queue/send-next")
    async def queue_send_next(
        session: str = Query(...),
        item_id: Optional[str] = Query(None),
        token: Optional[str] = Query(None),
    ):
        """
        Manually send the next unsafe item (or specific item).
        Bypasses policy check for one item.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        item = await app.state.command_queue.send_next_unsafe(session, item_id)

        if item:
            return {"status": "ok", "item": asdict(item)}
        else:
            return {"status": "not_found", "message": "No unsafe items in queue"}

    # ========== End Command Queue API ==========

    @app.post("/api/send")
    async def send_line(
        text: str = Query(...),
        session: str = Query(...),
        msg_id: str = Query(...),
        token: Optional[str] = Query(None),
    ):
        """
        Send a line of text to the terminal with Enter.
        Uses the InputQueue for serialized, atomic writes with ACKs.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Validate session matches current
        if session != app.state.current_session:
            return JSONResponse({
                "error": "Session mismatch",
                "expected": app.state.current_session,
                "got": session
            }, status_code=400)

        if app.state.master_fd is None:
            return JSONResponse({"error": "No active terminal"}, status_code=400)

        # Atomic write: text + carriage return
        data = (text + "\r").encode("utf-8")

        # Queue the send (will wait for quiet period and send ACK)
        success = await app.state.input_queue.send(
            msg_id,
            data,
            app.state.master_fd,
            app.state.active_websocket
        )

        if success:
            return {"status": "ok", "id": msg_id}
        else:
            return JSONResponse({"error": "Send timeout", "id": msg_id}, status_code=504)

    @app.post("/api/sendkey")
    async def send_key(
        key: str = Query(...),
        session: str = Query(...),
        msg_id: str = Query(...),
        token: Optional[str] = Query(None),
    ):
        """
        Send a control key using tmux send-keys.
        Supports: C-c, C-d, C-z, C-l, Tab, Escape, Enter, Up, Down, Left, Right, etc.
        """
        import subprocess

        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Validate session matches current
        if session != app.state.current_session:
            return JSONResponse({
                "error": "Session mismatch",
                "expected": app.state.current_session,
                "got": session
            }, status_code=400)

        # Map common key names to tmux send-keys format
        key_map = {
            "ctrl-c": "C-c",
            "ctrl-d": "C-d",
            "ctrl-z": "C-z",
            "ctrl-l": "C-l",
            "ctrl-a": "C-a",
            "ctrl-e": "C-e",
            "ctrl-w": "C-w",
            "ctrl-u": "C-u",
            "ctrl-k": "C-k",
            "ctrl-r": "C-r",
            "ctrl-o": "C-o",
            "ctrl-b": "C-b",
            "tab": "Tab",
            "escape": "Escape",
            "esc": "Escape",
            "enter": "Enter",
            "up": "Up",
            "down": "Down",
            "left": "Left",
            "right": "Right",
            "pageup": "PageUp",
            "pagedown": "PageDown",
            "home": "Home",
            "end": "End",
        }

        tmux_key = key_map.get(key.lower(), key)
        target = f"{session}:0.0"

        try:
            result = subprocess.run(
                ["tmux", "send-keys", "-t", target, tmux_key],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return JSONResponse({
                    "error": f"tmux send-keys failed: {result.stderr}",
                    "id": msg_id
                }, status_code=500)

            # Send ACK via WebSocket if connected
            if app.state.active_websocket:
                try:
                    await app.state.active_websocket.send_json({
                        "type": "ack",
                        "id": msg_id
                    })
                except Exception:
                    pass

            return {"status": "ok", "id": msg_id, "key": tmux_key}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "tmux command timeout", "id": msg_id}, status_code=504)
        except Exception as e:
            return JSONResponse({"error": str(e), "id": msg_id}, status_code=500)

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

        # Send history snapshot using tmux capture-pane with escape sequences
        try:
            import subprocess
            session_name = app.state.current_session
            # Use -e to preserve escape sequences for proper rendering
            result = subprocess.run(
                ["tmux", "capture-pane", "-p", "-e", "-S", "-5000", "-t", session_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout:
                history_text = result.stdout
                logger.info(f"Sending {len(history_text)} chars of capture-pane history")
                # Clear screen and move cursor home before sending history
                await websocket.send_text("\x1b[2J\x1b[H" + history_text)
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

                    # Update input queue timestamp (for quiet-wait logic)
                    app.state.input_queue.update_output_ts()

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
                                elif data.get("type") == "ping":
                                    # Respond to heartbeat ping with pong
                                    await websocket.send_json({"type": "pong"})
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
        """Start input queue and command queue on startup."""
        app.state.input_queue.start()
        app.state.command_queue.start()

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
        app.state.input_queue.stop()
        app.state.command_queue.stop()

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
