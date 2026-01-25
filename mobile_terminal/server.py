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
import hashlib
from collections import deque, OrderedDict
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


class SnapshotBuffer:
    """Ring buffer for session snapshots with hash deduplication."""

    MAX_SNAPSHOTS = 200

    def __init__(self):
        self._snapshots: Dict[str, OrderedDict] = {}  # session -> {id: snapshot}
        self._lock = threading.Lock()
        self._last_hash: Dict[str, str] = {}  # session -> last log_hash

    def capture(self, session: str, label: str, log_content: str,
                terminal_text: str, queue_state: list) -> Optional[dict]:
        """Capture snapshot if content changed."""
        log_hash = hashlib.md5(log_content.encode()).hexdigest()[:12]

        with self._lock:
            # Skip if unchanged
            if self._last_hash.get(session) == log_hash:
                return None

            # Create snapshot
            ts = int(time.time() * 1000)
            snap_id = f"snap_{ts}"
            snapshot = {
                "id": snap_id,
                "timestamp": ts,
                "session": session,
                "label": label,
                "log_entries": log_content,
                "log_hash": log_hash,
                "terminal_text": terminal_text,
                "queue_state": queue_state,
                "pinned": False,
            }

            # Initialize session buffer if needed
            if session not in self._snapshots:
                self._snapshots[session] = OrderedDict()

            # Add snapshot, evict oldest non-pinned if over limit
            self._snapshots[session][snap_id] = snapshot
            while len(self._snapshots[session]) > self.MAX_SNAPSHOTS:
                # Find first non-pinned snapshot to evict
                evicted = False
                for key in list(self._snapshots[session].keys()):
                    if not self._snapshots[session][key].get("pinned"):
                        del self._snapshots[session][key]
                        evicted = True
                        break
                if not evicted:
                    break  # All pinned, can't evict more

            self._last_hash[session] = log_hash
            return snapshot

    def list_snapshots(self, session: str, limit: int = 50) -> list:
        """Return snapshot summaries (id, timestamp, label, pinned)."""
        with self._lock:
            if session not in self._snapshots:
                return []
            items = list(self._snapshots[session].values())[-limit:]
            return [{"id": s["id"], "timestamp": s["timestamp"], "label": s["label"],
                     "pinned": s.get("pinned", False)}
                    for s in reversed(items)]

    def get_snapshot(self, session: str, snap_id: str) -> Optional[dict]:
        """Get full snapshot by ID."""
        with self._lock:
            return self._snapshots.get(session, {}).get(snap_id)

    def clear(self, session: str) -> int:
        """Clear all snapshots for a session. Returns count cleared."""
        with self._lock:
            if session not in self._snapshots:
                return 0
            count = len(self._snapshots[session])
            self._snapshots[session] = OrderedDict()
            self._last_hash[session] = ""
            return count

    def pin_snapshot(self, session: str, snap_id: str, pinned: bool = True) -> bool:
        """Pin or unpin a snapshot to prevent eviction."""
        with self._lock:
            if session in self._snapshots and snap_id in self._snapshots[session]:
                self._snapshots[session][snap_id]["pinned"] = pinned
                return True
            return False


class AuditLog:
    """In-memory audit log with size limit for tracking rollback operations."""

    MAX_ENTRIES = 500

    def __init__(self):
        self._entries: List[dict] = []
        self._lock = threading.Lock()

    def log(self, action: str, details: dict = None):
        """Log an action with timestamp."""
        with self._lock:
            self._entries.append({
                "timestamp": int(time.time() * 1000),
                "action": action,
                "details": details or {}
            })
            if len(self._entries) > self.MAX_ENTRIES:
                self._entries = self._entries[-self.MAX_ENTRIES:]

    def get_entries(self, limit: int = 100) -> list:
        """Return recent entries, newest first."""
        with self._lock:
            return list(reversed(self._entries[-limit:]))


class GitOpLock:
    """Async lock to prevent concurrent git operations (race conditions from double-taps)."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._current_op: Optional[str] = None

    async def acquire(self, operation: str) -> bool:
        """Try to acquire lock for an operation. Returns False if already locked."""
        if self._lock.locked():
            return False
        await self._lock.acquire()
        self._current_op = operation
        return True

    def release(self):
        """Release the lock."""
        self._current_op = None
        if self._lock.locked():
            self._lock.release()

    @property
    def is_locked(self) -> bool:
        return self._lock.locked()

    @property
    def current_operation(self) -> Optional[str]:
        return self._current_op


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
        self._loaded_sessions: set = set()  # Track which sessions loaded from disk

    def set_app(self, app):
        """Set the FastAPI app reference for accessing state."""
        self._app = app

    def _get_queue(self, session: str) -> List[QueueItem]:
        """Get or create queue for session, loading from disk if needed."""
        if session not in self._queues:
            self._queues[session] = []
            # Load from disk on first access
            if session not in self._loaded_sessions:
                self._load_from_disk(session)
                self._loaded_sessions.add(session)
        return self._queues[session]

    def _load_from_disk(self, session: str):
        """Load queue from disk for a session."""
        items_data = load_queue_from_disk(session)
        items = []
        for data in items_data:
            try:
                item = QueueItem(
                    id=data["id"],
                    text=data["text"],
                    policy=data.get("policy", "safe"),
                    status=data.get("status", "queued"),
                    created_at=data.get("created_at", time.time()),
                    sent_at=data.get("sent_at"),
                    error=data.get("error"),
                )
                # Only load items that are still queued (not sent/failed)
                if item.status in ("queued", "pending"):
                    items.append(item)
            except Exception as e:
                logger.warning(f"Skipping invalid queue item: {e}")
        if items:
            self._queues[session] = items
            logger.info(f"Loaded {len(items)} queued items for session {session}")

    def _save_to_disk(self, session: str):
        """Save queue to disk for a session."""
        queue = self._queues.get(session, [])
        items_data = [asdict(item) for item in queue]
        save_queue_to_disk(session, items_data)

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

    def enqueue(self, session: str, text: str, policy: str = "auto", item_id: Optional[str] = None) -> tuple:
        """
        Add a command to the queue.

        Returns (item, is_new) tuple. If item_id already exists, returns existing item
        with is_new=False (idempotency).
        """
        queue = self._get_queue(session)

        # Idempotency check: if ID provided and exists, return existing
        if item_id:
            for existing in queue:
                if existing.id == item_id:
                    return (existing, False)  # Already exists

        # Determine policy
        if policy == "auto":
            policy = self._classify_policy(text)

        item = QueueItem(
            id=item_id or str(uuid.uuid4()),
            text=text,
            policy=policy,
            status="queued",
            created_at=time.time(),
        )
        queue.append(item)
        self._save_to_disk(session)
        return (item, True)

    def dequeue(self, session: str, item_id: str) -> bool:
        """Remove an item from the queue."""
        queue = self._get_queue(session)
        for i, item in enumerate(queue):
            if item.id == item_id:
                queue.pop(i)
                self._save_to_disk(session)
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
                self._save_to_disk(session)
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
        self._save_to_disk(session)
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
                self._save_to_disk(session)  # Persist sent status

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
                self._save_to_disk(session)  # Persist failed status
                return False

        except Exception as e:
            item.status = "failed"
            item.error = str(e)
            self._save_to_disk(session)  # Persist failed status
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

# Directory for cached Claude conversation logs (persists across /clear)
LOG_CACHE_DIR = Path.home() / ".cache" / "mobile-overlay" / "logs"

# Directory for persistent command queue (survives server restart)
QUEUE_DIR = Path.home() / ".cache" / "mobile-overlay" / "queue"

# Plan links file (maps plan filenames to repos)
PLAN_LINKS_FILE = Path.home() / ".claude" / "plan-links.json"


def get_queue_file(session: str) -> Path:
    """Get the queue file path for a session."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    # Sanitize session name for filename
    safe_name = session.replace("/", "_").replace(":", "_")
    return QUEUE_DIR / f"{safe_name}.jsonl"


def load_queue_from_disk(session: str) -> list:
    """Load queue items from JSONL file."""
    import json
    queue_file = get_queue_file(session)
    items = []
    if queue_file.exists():
        try:
            with open(queue_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        items.append(json.loads(line))
        except Exception as e:
            logger.warning(f"Error loading queue for {session}: {e}")
    return items


def save_queue_to_disk(session: str, items: list):
    """Save queue items to JSONL file."""
    import json
    queue_file = get_queue_file(session)
    try:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        with open(queue_file, "w") as f:
            for item in items:
                f.write(json.dumps(item) + "\n")
    except Exception as e:
        logger.error(f"Error saving queue for {session}: {e}")

# Capture-pane cache to prevent DoS from rapid polling
# Key: (session, pane_id, lines), Value: (timestamp, result)
_capture_cache: dict = {}
CAPTURE_CACHE_TTL = 0.3  # 300ms TTL


def get_cached_capture(session: str, pane_id: str, lines: int) -> Optional[dict]:
    """Get cached capture-pane result if still valid."""
    import time
    key = (session, pane_id, lines)
    if key in _capture_cache:
        ts, result = _capture_cache[key]
        if time.time() - ts < CAPTURE_CACHE_TTL:
            return result
    return None


def set_cached_capture(session: str, pane_id: str, lines: int, result: dict):
    """Cache capture-pane result."""
    import time
    key = (session, pane_id, lines)
    _capture_cache[key] = (time.time(), result)
    # Clean old entries (keep cache small)
    now = time.time()
    stale = [k for k, (ts, _) in _capture_cache.items() if now - ts > CAPTURE_CACHE_TTL * 10]
    for k in stale:
        del _capture_cache[k]


def get_plan_links() -> dict:
    """Read plan-links.json, return empty dict if not found."""
    if PLAN_LINKS_FILE.exists():
        try:
            import json
            return json.loads(PLAN_LINKS_FILE.read_text())
        except Exception:
            pass
    return {}


def save_plan_links(links: dict):
    """Write plan-links.json."""
    import json
    PLAN_LINKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PLAN_LINKS_FILE.write_text(json.dumps(links, indent=2))


def score_plan_for_repo(plan_path: Path, repo_path: Path) -> int:
    """
    Score how well a plan matches a repo based on content.
    Higher score = better match.
    """
    try:
        text = plan_path.read_text(errors="replace")
    except Exception:
        return 0

    repo_str = str(repo_path)
    repo_name = repo_path.name
    parent_str = str(repo_path.parent)

    score = 0
    if repo_str in text:
        score += 3  # Full path match
    if repo_name in text:
        score += 2  # Repo name match
    if parent_str in text:
        score += 1  # Parent path match

    return score


def get_plans_for_repo(repo_path: Path) -> list:
    """
    Get plans matching a repo, sorted by score then modification time.
    Returns list of (plan_path, score) tuples.
    """
    plans_dir = Path.home() / ".claude" / "plans"
    if not plans_dir.exists():
        return []

    scored = []
    for plan in plans_dir.glob("*.md"):
        score = score_plan_for_repo(plan, repo_path)
        if score > 0:
            scored.append((plan, score))

    # Sort by score desc, then mtime desc
    scored.sort(key=lambda x: (x[1], x[0].stat().st_mtime), reverse=True)
    return scored


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
    app.state.snapshot_buffer = SnapshotBuffer()  # Preview snapshots ring buffer
    app.state.audit_log = AuditLog()  # Audit log for rollback operations
    app.state.git_op_lock = GitOpLock()  # Lock for git write operations
    app.state.active_target = None  # Explicit target pane (window:pane like "0:0")
    app.state.target_log_mapping = {}  # Maps pane_id -> log_file_path

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

    @app.post("/restart")
    async def restart_server():
        """Restart the server by sending SIGTERM. Systemd will auto-restart it."""
        import os
        import signal
        os.kill(os.getpid(), signal.SIGTERM)

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

    @app.get("/api/targets")
    async def list_targets(token: Optional[str] = Query(None)):
        """
        List all panes/windows in the current session with their working directories.
        Used for explicit target selection when working with multiple projects.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session = app.state.current_session
        targets = []

        try:
            # tmux list-panes -s lists all panes in session
            # Format: window_index:pane_index|pane_current_path|window_name|pane_id|pane_title
            result = subprocess.run(
                ["tmux", "list-panes", "-s", "-t", session,
                 "-F", "#{window_index}:#{pane_index}|#{pane_current_path}|#{window_name}|#{pane_id}|#{pane_title}"],
                capture_output=True, text=True, timeout=5
            )

            if result.returncode == 0:
                seen_cwds = {}  # Track duplicate cwds
                for line in result.stdout.strip().split("\n"):
                    if "|" in line:
                        parts = line.split("|")
                        if len(parts) >= 5:
                            cwd = Path(parts[1])
                            cwd_str = str(cwd)
                            target_id = parts[0]  # "0:0" (window:pane)
                            pane_title = parts[4] if parts[4] else None

                            # Track duplicates
                            if cwd_str in seen_cwds:
                                seen_cwds[cwd_str].append(target_id)
                            else:
                                seen_cwds[cwd_str] = [target_id]

                            targets.append({
                                "id": target_id,
                                "pane_id": parts[3],  # "%0"
                                "cwd": cwd_str,
                                "window_name": parts[2],
                                "window_index": parts[0].split(":")[0],
                                "pane_title": pane_title,
                                "project": cwd.name,  # Last component of path
                                "is_active": target_id == app.state.active_target
                            })

                # Mark targets with duplicate cwds
                for target in targets:
                    target["has_duplicate_cwd"] = len(seen_cwds.get(target["cwd"], [])) > 1

        except subprocess.TimeoutExpired:
            logger.error("Timeout listing tmux panes")
        except Exception as e:
            logger.error(f"Error listing targets: {e}")

        # Check if active target still exists
        active_exists = any(t["id"] == app.state.active_target for t in targets)

        # Get current path resolution info
        path_info = get_repo_path_info()

        # Check if session has multiple distinct projects
        unique_cwds = set(t["cwd"] for t in targets)
        multi_project = len(unique_cwds) > 1

        return {
            "targets": targets,
            "active": app.state.active_target,
            "active_exists": active_exists,
            "session": session,
            "multi_project": multi_project,
            "unique_projects": len(unique_cwds),
            "resolution": {
                "path": str(path_info["path"]) if path_info["path"] else None,
                "source": path_info["source"],
                "is_fallback": path_info["is_fallback"],
                "warning": path_info["warning"]
            }
        }

    @app.post("/api/target/select")
    async def select_target(
        target_id: str = Query(...),
        token: Optional[str] = Query(None)
    ):
        """Set the active target pane for repo operations."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session = app.state.current_session

        # Verify target exists
        try:
            result = subprocess.run(
                ["tmux", "list-panes", "-s", "-t", session,
                 "-F", "#{window_index}:#{pane_index}"],
                capture_output=True, text=True, timeout=5
            )
            valid_targets = result.stdout.strip().split("\n") if result.returncode == 0 else []

            if target_id not in valid_targets:
                return JSONResponse({
                    "error": "Target pane not found",
                    "target_id": target_id,
                    "valid_targets": valid_targets
                }, status_code=409)

        except Exception as e:
            logger.error(f"Error verifying target: {e}")
            # Allow selection even if verification fails
            pass

        # Clear old target's log mapping to force re-detection
        old_target = app.state.active_target
        if old_target and old_target in app.state.target_log_mapping:
            del app.state.target_log_mapping[old_target]

        app.state.active_target = target_id
        app.state.audit_log.log("target_select", {"target": target_id})

        # Start background file monitor to detect which log file this target uses
        asyncio.create_task(monitor_log_file_for_target(target_id))

        return {"success": True, "active": target_id}

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
        lines: int = Query(500),
        session: Optional[str] = Query(None),
        pane: int = Query(0),
    ):
        """
        Get terminal transcript using tmux capture-pane.
        Returns last N lines of terminal history.

        Params:
            session: tmux session name (defaults to current_session)
            pane: pane index within session (default 0)
            lines: number of lines to capture (default 500)

        Uses same 300ms cache as /api/terminal/capture.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        target_session = session or app.state.current_session
        if not target_session:
            return JSONResponse({"error": "No session"}, status_code=400)

        pane_id = str(pane)
        target = f"{target_session}:{0}.{pane}"

        # Check cache first
        cached = get_cached_capture(target_session, pane_id, lines)
        if cached and "text" in cached:
            return cached

        try:
            import subprocess
            result = subprocess.run(
                ["tmux", "capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", target],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                if "can't find" in result.stderr.lower() or "no such" in result.stderr.lower():
                    return JSONResponse(
                        {"error": f"Target not found: {target}", "session": target_session, "pane": pane},
                        status_code=409,
                    )
                return JSONResponse(
                    {"error": f"tmux capture-pane failed: {result.stderr}"},
                    status_code=500,
                )
            response = {
                "text": result.stdout,
                "session": target_session,
                "pane": pane,
            }
            set_cached_capture(target_session, pane_id, lines, response)
            return response
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

    def get_repo_path_info() -> dict:
        """
        Get repo path with resolution details.
        Returns: {path, source, target_id, is_fallback, warning}
        """
        session_name = app.state.current_session
        result_info = {
            "path": None,
            "source": None,
            "target_id": app.state.active_target,
            "is_fallback": False,
            "warning": None
        }

        # Priority 1: Explicit target selection
        if app.state.active_target:
            try:
                result = subprocess.run(
                    ["tmux", "display-message", "-p", "-t",
                     f"{session_name}:{app.state.active_target}",
                     "#{pane_current_path}"],
                    capture_output=True, text=True, timeout=2
                )
                if result.returncode == 0 and result.stdout.strip():
                    target_path = Path(result.stdout.strip())
                    if target_path.exists():
                        result_info["path"] = target_path
                        result_info["source"] = "explicit_target"
                        return result_info
                    else:
                        result_info["warning"] = f"Target path does not exist: {target_path}"
                else:
                    result_info["warning"] = f"Target pane not found: {app.state.active_target}"
            except Exception as e:
                result_info["warning"] = f"Error resolving target: {e}"

            # Target was set but failed - this is a fallback situation
            result_info["is_fallback"] = True

        # Priority 2: Check if session matches a configured repo
        for repo in config.repos:
            if repo.session == session_name:
                result_info["path"] = Path(repo.path)
                result_info["source"] = "configured_repo"
                if not app.state.active_target:
                    result_info["is_fallback"] = True
                return result_info

        # Priority 3: Fall back to project_root if set
        if config.project_root:
            result_info["path"] = config.project_root
            result_info["source"] = "project_root"
            result_info["is_fallback"] = True
            return result_info

        # Priority 4: Query tmux for active pane's working directory
        try:
            result = subprocess.run(
                ["tmux", "display-message", "-p", "-t", session_name, "#{pane_current_path}"],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode == 0 and result.stdout.strip():
                pane_path = Path(result.stdout.strip())
                if pane_path.exists():
                    result_info["path"] = pane_path
                    result_info["source"] = "active_pane_cwd"
                    result_info["is_fallback"] = True
                    return result_info
        except Exception:
            pass

        # Last resort: server's working directory
        result_info["path"] = Path.cwd()
        result_info["source"] = "server_cwd"
        result_info["is_fallback"] = True
        result_info["warning"] = "Using server working directory (no target selected)"
        return result_info

    def get_current_repo_path() -> Optional[Path]:
        """Get the path of the current repo based on session name and target."""
        return get_repo_path_info()["path"]

    def validate_target(session: Optional[str], pane_id: Optional[str]) -> dict:
        """
        Validate that the client's session and pane_id match server state.
        Returns dict with 'valid' bool and 'error' message if invalid.

        Used to prevent state-changing operations on wrong target.
        """
        result = {"valid": True, "error": None, "expected": {}, "received": {}}

        expected_session = app.state.current_session
        expected_pane = app.state.active_target

        result["expected"] = {"session": expected_session, "pane_id": expected_pane}
        result["received"] = {"session": session, "pane_id": pane_id}

        # Validate session
        if session and session != expected_session:
            result["valid"] = False
            result["error"] = f"Session mismatch: expected '{expected_session}', got '{session}'"
            return result

        # Validate pane_id (only if server has an active target set)
        if expected_pane and pane_id and pane_id != expected_pane:
            result["valid"] = False
            result["error"] = f"Target mismatch: expected '{expected_pane}', got '{pane_id}'"
            return result

        # If pane_id provided, verify it exists in current session
        if pane_id:
            try:
                check = subprocess.run(
                    ["tmux", "list-panes", "-s", "-t", expected_session,
                     "-F", "#{window_index}:#{pane_index}"],
                    capture_output=True, text=True, timeout=2
                )
                valid_panes = check.stdout.strip().split("\n") if check.returncode == 0 else []
                if pane_id not in valid_panes:
                    result["valid"] = False
                    result["error"] = f"Pane '{pane_id}' not found in session '{expected_session}'"
                    return result
            except Exception as e:
                logger.warning(f"Could not verify pane: {e}")

        return result

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

    @app.get("/api/plan")
    async def get_plan(
        token: Optional[str] = Query(None),
        filename: str = Query(..., description="Plan filename"),
        preview: bool = Query(True, description="Return only first 10 lines"),
    ):
        """
        Get a plan file from ~/.claude/plans/.
        Returns preview (first 10 lines) by default, or full content.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Sanitize filename - only allow alphanumeric, dash, underscore, dot
        import re
        if not re.match(r'^[\w\-\.]+\.md$', filename):
            return JSONResponse({"error": "Invalid filename"}, status_code=400)

        plan_file = Path.home() / ".claude" / "plans" / filename

        if not plan_file.exists():
            return {
                "exists": False,
                "content": "",
                "filename": filename,
            }

        try:
            content = plan_file.read_text(errors="replace")
            if preview:
                # Return first 10 lines for preview
                lines = content.split('\n')[:10]
                content = '\n'.join(lines)
                if len(plan_file.read_text().split('\n')) > 10:
                    content += '\n...'

            return {
                "exists": True,
                "content": content,
                "filename": filename,
                "modified": plan_file.stat().st_mtime,
            }
        except Exception as e:
            logger.error(f"Error reading plan file: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/plan/active")
    async def get_active_plan(
        token: Optional[str] = Query(None),
        preview: bool = Query(True, description="Return only first 15 lines"),
    ):
        """
        Get the active plan for the current repo.
        Priority: 1) Explicit link, 2) Content grep with scoring.
        Returns status: "found" | "ambiguous" | "none"
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        plans_dir = Path.home() / ".claude" / "plans"
        if not plans_dir.exists():
            return {"status": "none", "exists": False, "error": "No plans directory"}

        repo_path = get_current_repo_path()
        repo_str = str(repo_path) if repo_path else None

        def read_plan_content(plan_path: Path, preview_mode: bool) -> str:
            content = plan_path.read_text(errors="replace")
            if preview_mode:
                lines = content.split('\n')[:15]
                content = '\n'.join(lines)
                if len(plan_path.read_text().split('\n')) > 15:
                    content += '\n...'
            return content

        def plan_response(plan_path: Path, status: str, linked: bool = False):
            return {
                "status": status,
                "exists": True,
                "content": read_plan_content(plan_path, preview),
                "filename": plan_path.name,
                "modified": plan_path.stat().st_mtime,
                "linked": linked,
                "repo": repo_str,
            }

        # Priority 1: Check explicit link
        if repo_str:
            links = get_plan_links()
            for filename, link_data in links.items():
                if link_data.get("repo") == repo_str:
                    plan_path = plans_dir / filename
                    if plan_path.exists():
                        return plan_response(plan_path, "found", linked=True)

        # Priority 2: Content grep with scoring
        if repo_path:
            matches = get_plans_for_repo(repo_path)
            if len(matches) == 1:
                return plan_response(matches[0][0], "found")
            elif len(matches) > 1:
                # Check if scores are close (ambiguous)
                top_score = matches[0][1]
                close_matches = [(p, s) for p, s in matches if s >= top_score - 1]
                if len(close_matches) > 1:
                    # Ambiguous - return candidates
                    candidates = []
                    for plan_path, score in close_matches[:5]:  # Max 5 candidates
                        candidates.append({
                            "filename": plan_path.name,
                            "score": score,
                            "modified": plan_path.stat().st_mtime,
                            "preview": read_plan_content(plan_path, True)[:200],
                        })
                    return {
                        "status": "ambiguous",
                        "exists": True,
                        "candidates": candidates,
                        "repo": repo_str,
                    }
                else:
                    # Clear winner
                    return plan_response(matches[0][0], "found")

        # Fallback: most recent plan (global)
        plan_files = list(plans_dir.glob("*.md"))
        if not plan_files:
            return {"status": "none", "exists": False, "error": "No plan files"}

        plan_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return {
            "status": "none",
            "exists": True,
            "fallback": True,
            "content": read_plan_content(plan_files[0], preview),
            "filename": plan_files[0].name,
            "modified": plan_files[0].stat().st_mtime,
            "repo": repo_str,
        }

    @app.get("/api/plans")
    async def list_all_plans(token: Optional[str] = Query(None)):
        """List all plan files for manual browsing/selection."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        plans_dir = Path.home() / ".claude" / "plans"
        if not plans_dir.exists():
            return {"plans": []}

        plan_files = list(plans_dir.glob("*.md"))
        plan_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        plans = []
        for p in plan_files:
            try:
                content = p.read_text(errors="replace")
                # First line as title (strip # prefix)
                title = content.split('\n')[0].lstrip('#').strip() or p.name
                preview = content[:200]
                plans.append({
                    "filename": p.name,
                    "title": title,
                    "preview": preview,
                    "modified": p.stat().st_mtime,
                })
            except Exception:
                plans.append({
                    "filename": p.name,
                    "title": p.name,
                    "preview": "",
                    "modified": p.stat().st_mtime,
                })

        return {"plans": plans}

    @app.post("/api/plan/link")
    async def link_plan(
        filename: str = Query(..., description="Plan filename to link"),
        token: Optional[str] = Query(None),
    ):
        """
        Link a plan to the current repo.
        Creates an explicit mapping in plan-links.json.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo path found"}, status_code=400)

        plans_dir = Path.home() / ".claude" / "plans"
        plan_path = plans_dir / filename
        if not plan_path.exists():
            return JSONResponse({"error": f"Plan file not found: {filename}"}, status_code=404)

        repo_str = str(repo_path)
        links = get_plan_links()

        # Remove any existing link for this repo (one plan per repo)
        links = {k: v for k, v in links.items() if v.get("repo") != repo_str}

        # Add new link
        from datetime import datetime
        links[filename] = {
            "repo": repo_str,
            "linked_at": datetime.utcnow().isoformat() + "Z",
        }
        save_plan_links(links)

        logger.info(f"Linked plan {filename} to repo {repo_str}")
        return {"success": True, "filename": filename, "repo": repo_str}

    @app.delete("/api/plan/link")
    async def unlink_plan(
        token: Optional[str] = Query(None),
    ):
        """
        Unlink the plan for the current repo.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo path found"}, status_code=400)

        repo_str = str(repo_path)
        links = get_plan_links()

        # Find and remove link for this repo
        removed = None
        for filename, link_data in list(links.items()):
            if link_data.get("repo") == repo_str:
                removed = filename
                del links[filename]
                break

        if removed:
            save_plan_links(links)
            logger.info(f"Unlinked plan {removed} from repo {repo_str}")
            return {"success": True, "removed": removed, "repo": repo_str}
        else:
            return {"success": False, "error": "No plan linked to this repo"}

    def get_log_cache_path(project_id: str) -> Path:
        """Get the cache file path for a project's log."""
        LOG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return LOG_CACHE_DIR / f"{project_id}.log"

    def read_cached_log(project_id: str) -> Optional[str]:
        """Read cached log content if it exists."""
        cache_path = get_log_cache_path(project_id)
        if cache_path.exists():
            try:
                return cache_path.read_text(errors="replace")
            except Exception as e:
                logger.warning(f"Error reading log cache: {e}")
        return None

    def write_log_cache(project_id: str, content: str):
        """Write log content to cache."""
        cache_path = get_log_cache_path(project_id)
        try:
            cache_path.write_text(content)
        except Exception as e:
            logger.warning(f"Error writing log cache: {e}")

    async def monitor_log_file_for_target(target_id: str):
        """
        Background task that monitors log file modifications to detect which
        .jsonl file belongs to the selected target.

        Watches for 10 seconds and associates any modified file with the target.
        """
        import time

        # Get project directory for the target
        repo_path = get_current_repo_path()
        if not repo_path:
            return

        project_id = str(repo_path.resolve()).replace("~", "-").replace("/", "-")
        claude_projects_dir = Path.home() / ".claude" / "projects" / project_id

        if not claude_projects_dir.exists():
            return

        jsonl_files = list(claude_projects_dir.glob("*.jsonl"))
        if not jsonl_files:
            return

        # Skip if already cached
        if target_id in app.state.target_log_mapping:
            return

        # Record initial mtimes
        initial_mtimes = {str(f): f.stat().st_mtime for f in jsonl_files}
        logger.debug(f"Starting log file monitor for target {target_id}")

        # Monitor for 10 seconds
        for _ in range(20):
            await asyncio.sleep(0.5)

            # Check if target changed while we were monitoring
            if app.state.active_target != target_id:
                logger.debug(f"Target changed, stopping monitor for {target_id}")
                return

            # Check if already cached (by another code path)
            if target_id in app.state.target_log_mapping:
                return

            # Check for mtime changes
            for f in jsonl_files:
                try:
                    current_mtime = f.stat().st_mtime
                    if current_mtime > initial_mtimes.get(str(f), 0):
                        # File was modified - associate with this target
                        app.state.target_log_mapping[target_id] = str(f)
                        logger.info(f"Monitor detected log file for target {target_id}: {f.name}")
                        return
                except Exception:
                    pass

        logger.debug(f"Monitor timeout for target {target_id}, no file changes detected")

    def detect_target_log_file(target_id: Optional[str], session_name: str, claude_projects_dir: Path) -> Optional[Path]:
        """
        Detect which .jsonl log file belongs to a specific target pane.

        Strategy:
        1. Check cached mapping
        2. Find Claude process in the target pane
        3. Match Claude process start time to log file creation/first-entry time
        4. Fall back to most recently modified if detection fails
        """
        import subprocess
        import os
        import json

        jsonl_files = list(claude_projects_dir.glob("*.jsonl"))
        if not jsonl_files:
            return None

        # Check if we have a cached mapping for this target
        if target_id:
            cached = app.state.target_log_mapping.get(target_id)
            if cached:
                cached_path = Path(cached)
                if cached_path.exists():
                    logger.debug(f"Using cached log file for target {target_id}: {cached_path.name}")
                    return cached_path

        # Determine which pane to check
        # target_id format is "window:pane" (e.g., "0:0")
        # tmux target format is "session:window.pane" (e.g., "claude:0.0")
        if target_id:
            parts = target_id.split(':')
            if len(parts) == 2:
                pane_target = f"{session_name}:{parts[0]}.{parts[1]}"
            else:
                pane_target = f"{session_name}:{target_id}"
        else:
            pane_target = session_name

        def fallback(reason="unknown"):
            """Return most recently modified file."""
            jsonl_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
            logger.debug(f"Detection fallback ({reason}): using {jsonl_files[0].name}")
            return jsonl_files[0]

        try:
            # Get PID of the shell in the target pane
            result = subprocess.run(
                ["tmux", "list-panes", "-t", pane_target, "-F", "#{pane_pid}"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                logger.debug(f"Could not get pane PID for {pane_target}")
                return fallback("no pane PID")

            pane_pid = result.stdout.strip().split('\n')[0]
            if not pane_pid:
                return fallback("empty pane PID")

            # Find Claude process (direct child named 'claude')
            result = subprocess.run(
                ["pgrep", "-P", pane_pid, "-x", "claude"],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode != 0 or not result.stdout.strip():
                logger.debug(f"No claude process found under pane PID {pane_pid}")
                return fallback("no claude process")

            claude_pid = result.stdout.strip().split('\n')[0]

            # Get Claude process start time (in seconds since boot)
            stat_path = Path(f"/proc/{claude_pid}/stat")
            if not stat_path.exists():
                return fallback()

            stat_content = stat_path.read_text()
            # Field 22 is starttime (in clock ticks since boot)
            # Format: pid (comm) state ... field22 ...
            # Need to handle comm field which may contain spaces/parens
            # Find the last ')' and parse from there
            last_paren = stat_content.rfind(')')
            if last_paren == -1:
                return fallback()
            fields_after_comm = stat_content[last_paren + 2:].split()
            if len(fields_after_comm) < 20:
                return fallback()
            starttime_ticks = int(fields_after_comm[19])  # 22nd field, 0-indexed after comm

            # Get system boot time and clock ticks per second
            with open('/proc/stat') as f:
                for line in f:
                    if line.startswith('btime '):
                        boot_time = int(line.split()[1])
                        break
                else:
                    return fallback()

            clock_ticks = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
            process_start_unix = boot_time + (starttime_ticks / clock_ticks)

            logger.debug(f"Detection: Claude PID {claude_pid} for target {target_id}, process_start={process_start_unix}")

            # Strategy A: Check debug files to find the session UUID
            # Debug files in ~/.claude/debug/ have the same UUID as log files
            # The first line timestamp should match the process start time
            debug_dir = Path.home() / ".claude" / "debug"
            if debug_dir.exists():
                from datetime import datetime
                debug_files = [f for f in debug_dir.glob("*.txt") if f.name != "latest"]
                for debug_file in debug_files:
                    try:
                        # Read first line to get debug session start time
                        with open(debug_file, 'r') as f:
                            first_line = f.readline().strip()
                            if not first_line:
                                continue
                            # Format: "2026-01-24T19:35:38.660Z [DEBUG] ..."
                            ts_str = first_line.split(' ')[0]
                            ts_str = ts_str.replace('Z', '+00:00')
                            dt = datetime.fromisoformat(ts_str)
                            debug_start_unix = dt.timestamp()

                            # Check if debug file start matches process start (within 5 seconds)
                            diff = abs(debug_start_unix - process_start_unix)
                            if diff < 5:
                                session_uuid = debug_file.stem
                                matching_log = claude_projects_dir / f"{session_uuid}.jsonl"
                                if matching_log.exists():
                                    if target_id:
                                        app.state.target_log_mapping[target_id] = str(matching_log)
                                    logger.info(f"Matched log file via debug for target {target_id}: {matching_log.name} (diff={diff:.1f}s)")
                                    return matching_log
                    except Exception as e:
                        logger.debug(f"Error checking debug file {debug_file.name}: {e}")
                        continue

            # Strategy B: Find log file whose first entry timestamp is closest to process start
            best_match = None
            best_diff = float('inf')

            for log_file in jsonl_files:
                try:
                    # Read first line to get session start timestamp
                    with open(log_file, 'r') as f:
                        first_line = f.readline().strip()
                        if not first_line:
                            continue
                        entry = json.loads(first_line)
                        # Timestamp may be nested in snapshot object
                        ts_str = entry.get('timestamp', '')
                        if not ts_str and 'snapshot' in entry:
                            ts_str = entry['snapshot'].get('timestamp', '')
                        if not ts_str:
                            continue
                        # Parse ISO timestamp (format: "2026-01-24T10:54:56.123Z")
                        from datetime import datetime
                        ts_str = ts_str.replace('Z', '+00:00')
                        dt = datetime.fromisoformat(ts_str)
                        log_start_unix = dt.timestamp()

                        diff = abs(log_start_unix - process_start_unix)
                        logger.debug(f"Log {log_file.name}: start={log_start_unix}, diff={diff}s")

                        if diff < best_diff:
                            best_diff = diff
                            best_match = log_file
                except Exception as e:
                    logger.debug(f"Error parsing log file {log_file.name}: {e}")
                    continue

            # Accept match if within 60 seconds of process start
            if best_match and best_diff < 60:
                if target_id:
                    app.state.target_log_mapping[target_id] = str(best_match)
                logger.info(f"Matched log file for target {target_id}: {best_match.name} (diff={best_diff:.1f}s)")
                return best_match

            logger.debug(f"No close timestamp match found (best diff: {best_diff}s)")

            # Fallback strategy: Find file that's been modified recently
            # If the target's Claude process is active, its log should have recent modifications
            import time
            now = time.time()
            recent_threshold = 300  # 5 minutes

            # Sort by modification time
            jsonl_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

            for log_file in jsonl_files:
                mtime = log_file.stat().st_mtime
                age = now - mtime
                if age < recent_threshold:
                    # This file was modified recently - likely belongs to an active session
                    # If we haven't assigned this file to another target, use it
                    already_assigned = any(
                        v == str(log_file) for k, v in app.state.target_log_mapping.items()
                        if k != target_id
                    )
                    if not already_assigned:
                        if target_id:
                            app.state.target_log_mapping[target_id] = str(log_file)
                        logger.info(f"Assigned recent log file to target {target_id}: {log_file.name} (age={age:.0f}s)")
                        return log_file

        except Exception as e:
            logger.debug(f"Target log detection failed: {e}")

        return fallback()

    @app.get("/api/log")
    async def get_log(token: Optional[str] = Query(None), limit: int = Query(200)):
        """
        Get the Claude conversation log from ~/.claude/projects/.
        Finds the most recently modified .jsonl file for the current repo.
        Parses JSONL and returns readable conversation text.
        Falls back to cached log if source is cleared (e.g., after /clear).
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
        # Note: Claude Code strips ~ from paths before converting
        project_id = str(repo_path.resolve()).replace("~", "-").replace("/", "-")
        claude_projects_dir = Path.home() / ".claude" / "projects" / project_id

        # Helper to return cached content
        def return_cached():
            cached = read_cached_log(project_id)
            if cached:
                return {
                    "exists": True,
                    "content": cached,
                    "path": str(claude_projects_dir),
                    "session": app.state.current_session,
                    "cached": True,
                }
            return {
                "exists": False,
                "content": "",
                "path": str(claude_projects_dir),
                "session": app.state.current_session,
            }

        if not claude_projects_dir.exists():
            return return_cached()

        # Detect which log file belongs to this target
        log_file = detect_target_log_file(app.state.active_target, app.state.current_session, claude_projects_dir)
        if not log_file:
            return return_cached()

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
                                        elif tool_name == 'EnterPlanMode':
                                            conversation.append("📋 Entering plan mode...")
                                        elif tool_name == 'ExitPlanMode':
                                            conversation.append("✅ Exiting plan mode")
                                        elif tool_name == 'Task':
                                            # Show agent spawning with description
                                            desc = tool_input.get('description', '')
                                            agent_type = tool_input.get('subagent_type', '')
                                            if desc:
                                                conversation.append(f"🤖 Task ({agent_type}): {desc[:80]}")
                                            else:
                                                conversation.append(f"🤖 Task: {agent_type}")
                                        elif tool_name == 'TodoWrite':
                                            # Show todo updates
                                            todos = tool_input.get('todos', [])
                                            in_progress = [t for t in todos if t.get('status') == 'in_progress']
                                            if in_progress:
                                                conversation.append(f"📝 {in_progress[0].get('activeForm', 'Working...')}")
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

            # If log was cleared (empty), fall back to cache
            if not content.strip():
                return return_cached()

            # Redact potential secrets
            content = re.sub(r'(sk-[a-zA-Z0-9]{20,})', '[REDACTED_API_KEY]', content)
            content = re.sub(r'(ghp_[a-zA-Z0-9]{36,})', '[REDACTED_GITHUB_TOKEN]', content)

            # Cache the content for persistence across /clear
            write_log_cache(project_id, content)

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

    @app.delete("/api/log/cache")
    async def clear_log_cache(token: Optional[str] = Query(None)):
        """Clear the cached log for the current project."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        if not repo_path:
            return {"cleared": False, "error": "No repo path found"}

        project_id = str(repo_path.resolve()).replace("~", "-").replace("/", "-")
        cache_path = get_log_cache_path(project_id)

        if cache_path.exists():
            try:
                cache_path.unlink()
                logger.info(f"Cleared log cache: {cache_path}")
                return {"cleared": True, "path": str(cache_path)}
            except Exception as e:
                logger.error(f"Error clearing log cache: {e}")
                return JSONResponse({"error": str(e)}, status_code=500)

        return {"cleared": False, "error": "No cache file exists"}

    @app.get("/api/terminal/capture")
    async def capture_terminal(
        token: Optional[str] = Query(None),
        lines: int = Query(50),
        session: Optional[str] = Query(None),
        pane: int = Query(0),
    ):
        """
        Capture recent terminal output from tmux pane.

        Uses tmux capture-pane to get scrollback buffer.
        Returns last N lines of terminal content.
        Also returns pane_title which Claude Code uses to signal its state.

        Params:
            session: tmux session name (defaults to current_session)
            pane: pane index within session (default 0)
            lines: number of lines to capture (default 50)

        Includes 300ms cache to prevent DoS from rapid polling.
        """
        import subprocess

        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Use provided session or fall back to current
        target_session = session or app.state.current_session
        if not target_session:
            return {"content": "", "error": "No session"}

        pane_id = str(pane)
        target = f"{target_session}:{0}.{pane}"

        # Check cache first
        cached = get_cached_capture(target_session, pane_id, lines)
        if cached:
            return cached

        try:
            # Capture last N lines from tmux pane
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}"],
                capture_output=True,
                text=True,
                timeout=5,
            )

            # Get pane title - Claude Code sets this to indicate state
            # e.g., "✳ Signal Detection Pending" when waiting for input
            pane_title = ""
            try:
                title_result = subprocess.run(
                    ["tmux", "display-message", "-p", "-t", target, "#{pane_title}"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if title_result.returncode == 0:
                    pane_title = title_result.stdout.strip()
            except Exception:
                pass

            if result.returncode == 0:
                response = {
                    "content": result.stdout,
                    "lines": lines,
                    "pane_title": pane_title,
                    "session": target_session,
                    "pane": pane,
                }
                set_cached_capture(target_session, pane_id, lines, response)
                return response
            else:
                # Target missing or invalid
                if "can't find" in result.stderr.lower() or "no such" in result.stderr.lower():
                    return JSONResponse(
                        {"error": f"Target not found: {target}", "session": target_session, "pane": pane},
                        status_code=409,
                    )
                return {"content": "", "error": result.stderr, "pane_title": pane_title}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Capture timeout"}, status_code=504)
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
        plan_filename: Optional[str] = Query(None, description="Specific plan filename to include"),
    ):
        """
        Run problem-focused code review using AI models.

        Supports multiple providers: Together.ai, OpenAI, Anthropic.
        User selects model, system routes to appropriate provider.

        Context bundle is built from:
        - User's problem description (required for focused review)
        - Terminal output (optional, captures current state)
        - Git diff (optional, shows recent changes)
        - Active plan (optional, shows what Claude is working on)
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

        # 4. Plan (if specified by filename)
        if plan_filename:
            try:
                plans_dir = Path.home() / ".claude" / "plans"
                plan_path = plans_dir / plan_filename
                if plan_path.exists():
                    plan_content = plan_path.read_text(errors="replace")
                    # Extract title from first line
                    plan_title = plan_content.split('\n')[0].lstrip('#').strip() or plan_filename
                    if len(plan_content) > 4000:
                        plan_content = plan_content[:4000] + "\n... [plan truncated]"
                    bundle_parts.append(f"## Plan: {plan_title}\n```markdown\n{plan_content}\n```")
            except Exception as e:
                logger.warning(f"Failed to read plan {plan_filename}: {e}")

        # 5. Minimal project context (git status + branch)
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

        # Adversarial challenge prompt - stress-tests implementation against stated problem
        system_prompt = """## Role

You are an adversarial code reviewer and design challenger. Your job is to stress-test the current implementation and plan against the stated problem.

You are not a collaborator and not a planner.
Assume the implementation may be wrong.

---

## Scope & Focus Rules (Strict)

Focus only on the issue described in Problem Statement.

Use only the provided inputs:
- Terminal output
- Git diff / status
- Plan (if present)

Do not:
- Give generic project feedback
- Suggest running commands
- Re-explain the code unless needed to justify a concern
- Propose large refactors unless the current approach is fundamentally flawed

---

## Evaluation Mandate

Answer four questions:

1. Does the current code actually solve the stated problem?
2. If it appears to work, what assumptions could break it?
3. If it doesn't work, what is the minimal correction?
4. What failure mode is most likely to show up in production first?

---

## Output Format (Enforced)

**Problem Analysis**
Concise restatement of the problem in your own words.
If the problem statement is ambiguous or underspecified, say so explicitly.

**Evidence from Current State**
Concrete observations from:
- terminal output (line-level if relevant)
- git diff (file + behavior level)

**Potential Failure Points**
List specific, testable risks, not hypotheticals.
Example: race condition, stale state, incorrect boundary, missing guard.

**Minimal Corrective Action**
One of:
- "No change required"
- "Small fix" (≤ 3 focused changes)
- "Design mismatch" (current plan does not meet requirements)

**Risks / Edge Cases**
Only the top 1–3 risks worth caring about.

---

## Tone & Constraints

- Be direct, not polite
- Prefer negative certainty ("this will fail if…") over hedging
- If everything looks correct, say so—but still identify one thing to watch"""

        # Build request manually to use custom system prompt
        model_info = MODELS[model]
        provider_key = model_info["provider"]
        model_id = model_info["model_id"]

        from .challenge import (
            PROVIDERS, get_api_key, validate_api_key,
            build_openai_payload, build_anthropic_payload,
            build_openai_headers, build_anthropic_headers,
            parse_openai_response, parse_anthropic_response,
            parse_openai_responses_response,
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
        elif fmt == "openai_responses":
            # GPT-5.2+ uses Responses API
            headers = build_openai_headers(api_key)
            payload = {
                "model": model_id,
                "input": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": bundle},
                ],
                "temperature": 0.2,
                "max_output_tokens": 1000,
            }
            parse_response = parse_openai_responses_response
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

        # Clear target selection and log mappings (pane IDs are session-specific)
        app.state.active_target = None
        app.state.target_log_mapping.clear()

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
        id: Optional[str] = Query(None),  # Client-provided ID for idempotency
        token: Optional[str] = Query(None),
    ):
        """
        Add command to the deferred queue.

        Policy:
        - "auto": server determines safe/unsafe based on text
        - "safe": force auto-send when ready
        - "unsafe": always require manual confirmation

        If `id` is provided and already exists, returns the existing item
        without creating a duplicate (idempotency).
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        item, is_new = app.state.command_queue.enqueue(session, text, policy, item_id=id)

        # Notify connected clients only for new items
        if is_new and app.state.active_websocket:
            try:
                await app.state.active_websocket.send_json({
                    "type": "queue_update",
                    "action": "add",
                    "item": asdict(item),
                })
            except Exception:
                pass

        return {"status": "ok", "item": asdict(item), "is_new": is_new}

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

    # ========== Preview/Snapshot API ==========

    @app.post("/api/rollback/preview/capture")
    async def capture_preview(
        label: str = Query("manual"),
        token: Optional[str] = Query(None),
    ):
        """Capture a snapshot of current session state."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session = app.state.current_session
        if not session:
            return JSONResponse({"error": "No active session"}, status_code=400)

        # Get log content (reuse /api/log logic)
        try:
            repo_path = get_current_repo_path()
            if repo_path:
                project_id = str(repo_path.resolve()).replace("~", "-").replace("/", "-").lstrip("-")
                claude_dir = Path.home() / ".claude" / "projects" / project_id
                jsonl_files = sorted(claude_dir.glob("*.jsonl"),
                                   key=lambda f: f.stat().st_mtime, reverse=True)
                log_content = jsonl_files[0].read_text(errors="replace") if jsonl_files else ""
            else:
                log_content = ""
        except Exception as e:
            log_content = f"[Error reading log: {e}]"

        # Capture terminal (last 50 lines)
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", session, "-p", "-S", "-50"],
                capture_output=True, text=True, timeout=5
            )
            terminal_text = result.stdout if result.returncode == 0 else ""
        except Exception:
            terminal_text = ""

        # Get queue state
        queue_state = [asdict(item) for item in app.state.command_queue.list_items(session)]

        # Capture snapshot
        logger.info(f"Capturing snapshot: session={session}, label={label}, log_len={len(log_content)}, term_len={len(terminal_text)}")
        snapshot = app.state.snapshot_buffer.capture(
            session, label, log_content, terminal_text, queue_state
        )

        if snapshot:
            logger.info(f"Snapshot created: {snapshot['id']}")
            app.state.audit_log.log("snapshot_capture", {"snap_id": snapshot["id"], "label": label})
            return {"success": True, "snapshot": {"id": snapshot["id"], "label": label}}
        logger.info("Snapshot skipped: content unchanged")
        return {"success": True, "snapshot": None, "reason": "unchanged"}

    @app.get("/api/rollback/previews")
    async def list_previews(
        limit: int = Query(50),
        token: Optional[str] = Query(None),
    ):
        """List available snapshots."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session = app.state.current_session
        snapshots = app.state.snapshot_buffer.list_snapshots(session, limit)
        logger.info(f"List snapshots: session={session}, count={len(snapshots)}")
        return {"session": session, "snapshots": snapshots}

    @app.get("/api/rollback/preview/{snap_id}")
    async def get_preview(
        snap_id: str,
        token: Optional[str] = Query(None),
    ):
        """Get full snapshot data."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session = app.state.current_session
        snapshot = app.state.snapshot_buffer.get_snapshot(session, snap_id)

        if not snapshot:
            return JSONResponse({"error": "Snapshot not found"}, status_code=404)
        return snapshot

    @app.post("/api/rollback/preview/select")
    async def select_preview(
        snap_id: Optional[str] = Query(None),
        token: Optional[str] = Query(None),
    ):
        """Enter or exit preview mode (snap_id=null exits)."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session = app.state.current_session

        if snap_id:
            # Verify snapshot exists
            snapshot = app.state.snapshot_buffer.get_snapshot(session, snap_id)
            if not snapshot:
                return JSONResponse({"error": "Snapshot not found"}, status_code=404)
            app.state.audit_log.log("preview_enter", {"snap_id": snap_id})
        else:
            app.state.audit_log.log("preview_exit", {})

        return {"success": True, "preview_mode": snap_id is not None, "snap_id": snap_id}

    @app.post("/api/rollback/preview/{snap_id}/pin")
    async def pin_snapshot(
        snap_id: str,
        pinned: bool = Query(True),
        token: Optional[str] = Query(None),
    ):
        """Pin or unpin a snapshot to prevent eviction."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session = app.state.current_session
        success = app.state.snapshot_buffer.pin_snapshot(session, snap_id, pinned)

        if not success:
            return JSONResponse({"error": "Snapshot not found"}, status_code=404)

        app.state.audit_log.log("snapshot_pin", {"snap_id": snap_id, "pinned": pinned})
        return {"success": True, "snap_id": snap_id, "pinned": pinned}

    @app.get("/api/rollback/preview/{snap_id}/export")
    async def export_snapshot(
        snap_id: str,
        token: Optional[str] = Query(None),
    ):
        """Export snapshot as JSON file."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session = app.state.current_session
        snapshot = app.state.snapshot_buffer.get_snapshot(session, snap_id)

        if not snapshot:
            return JSONResponse({"error": "Snapshot not found"}, status_code=404)

        app.state.audit_log.log("snapshot_export", {"snap_id": snap_id})

        from starlette.responses import Response
        return Response(
            json.dumps(snapshot, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={snap_id}.json"}
        )

    @app.get("/api/rollback/preview/diff")
    async def diff_snapshots(
        snap_a: str = Query(...),
        snap_b: str = Query(...),
        token: Optional[str] = Query(None),
    ):
        """Compare two snapshots."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session = app.state.current_session
        a = app.state.snapshot_buffer.get_snapshot(session, snap_a)
        b = app.state.snapshot_buffer.get_snapshot(session, snap_b)

        if not a:
            return JSONResponse({"error": f"Snapshot {snap_a} not found"}, status_code=404)
        if not b:
            return JSONResponse({"error": f"Snapshot {snap_b} not found"}, status_code=404)

        return {
            "a": {"id": snap_a, "timestamp": a["timestamp"], "label": a["label"]},
            "b": {"id": snap_b, "timestamp": b["timestamp"], "label": b["label"]},
            "log_changed": a["log_hash"] != b["log_hash"],
            "terminal_changed": a["terminal_text"] != b["terminal_text"],
            "queue_changed": a["queue_state"] != b["queue_state"],
        }

    @app.post("/api/rollback/preview/clear")
    async def clear_previews(
        token: Optional[str] = Query(None),
    ):
        """Clear all snapshots for current session."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session = app.state.current_session
        count = app.state.snapshot_buffer.clear(session)
        app.state.audit_log.log("snapshots_cleared", {"count": count})
        logger.info(f"Cleared {count} snapshots for session {session}")
        return {"success": True, "cleared": count}

    @app.get("/api/rollback/audit")
    async def get_audit_log(
        limit: int = Query(100),
        token: Optional[str] = Query(None),
    ):
        """Get recent audit log entries."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        entries = app.state.audit_log.get_entries(limit)
        return {"entries": entries}

    @app.post("/api/rollback/audit/log")
    async def log_audit_action(
        action: str = Query(...),
        details: Optional[str] = Query(None),
        token: Optional[str] = Query(None),
    ):
        """Log an audit action from client."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        detail_dict = {}
        if details:
            try:
                import json
                detail_dict = json.loads(details)
            except:
                detail_dict = {"raw": details}

        app.state.audit_log.log(action, detail_dict)
        return {"success": True}

    # ========== End Preview/Snapshot API ==========

    # ========== Process Management API ==========

    @app.post("/api/process/terminate")
    async def terminate_process(
        token: Optional[str] = Query(None),
        force: bool = Query(False),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """
        Terminate the PTY process.

        First tries SIGTERM, then SIGKILL if force=True or if SIGTERM fails.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Validate target before destructive operation
        target_check = validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        child_pid = app.state.child_pid
        if not child_pid:
            return JSONResponse({"error": "No process running"}, status_code=400)

        try:
            # First try SIGTERM (graceful)
            os.kill(child_pid, signal.SIGTERM)
            app.state.audit_log.log("process_terminate", {"pid": child_pid, "signal": "SIGTERM"})

            # Wait briefly for process to terminate
            await asyncio.sleep(0.5)

            # Check if still running
            try:
                os.kill(child_pid, 0)  # Signal 0 just checks if process exists
                still_running = True
            except OSError:
                still_running = False

            if still_running and force:
                # SIGKILL as fallback
                os.kill(child_pid, signal.SIGKILL)
                app.state.audit_log.log("process_terminate", {"pid": child_pid, "signal": "SIGKILL"})
                await asyncio.sleep(0.2)

            # Clean up PTY state
            if app.state.master_fd is not None:
                try:
                    os.close(app.state.master_fd)
                except Exception:
                    pass
            app.state.master_fd = None
            app.state.child_pid = None

            return {
                "success": True,
                "pid": child_pid,
                "method": "SIGKILL" if (still_running and force) else "SIGTERM"
            }

        except ProcessLookupError:
            # Process already dead
            app.state.master_fd = None
            app.state.child_pid = None
            return {"success": True, "pid": child_pid, "method": "already_dead"}
        except Exception as e:
            logger.error(f"Failed to terminate process: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/process/respawn")
    async def respawn_process(
        token: Optional[str] = Query(None),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """
        Respawn the PTY process.

        Terminates existing process (if any) and creates a new one.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Validate target before destructive operation
        target_check = validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        old_pid = app.state.child_pid

        # Terminate existing process if any
        if old_pid:
            try:
                os.kill(old_pid, signal.SIGTERM)
                await asyncio.sleep(0.3)
                try:
                    os.kill(old_pid, 0)
                    os.kill(old_pid, signal.SIGKILL)
                except OSError:
                    pass
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.warning(f"Error terminating old process: {e}")

        # Clean up old PTY
        if app.state.master_fd is not None:
            try:
                os.close(app.state.master_fd)
            except Exception:
                pass
        app.state.master_fd = None
        app.state.child_pid = None
        app.state.output_buffer.clear()

        # Spawn new PTY
        try:
            session_name = app.state.current_session
            master_fd, child_pid = spawn_tmux(session_name)
            app.state.master_fd = master_fd
            app.state.child_pid = child_pid

            app.state.audit_log.log("process_respawn", {
                "old_pid": old_pid,
                "new_pid": child_pid,
                "session": session_name
            })

            return {
                "success": True,
                "old_pid": old_pid,
                "new_pid": child_pid,
                "session": session_name
            }
        except Exception as e:
            logger.error(f"Failed to respawn process: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/process/status")
    async def process_status(
        token: Optional[str] = Query(None),
    ):
        """Get current process status."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        child_pid = app.state.child_pid
        is_running = False

        if child_pid:
            try:
                os.kill(child_pid, 0)
                is_running = True
            except OSError:
                is_running = False

        return {
            "pid": child_pid,
            "is_running": is_running,
            "has_pty": app.state.master_fd is not None,
            "session": app.state.current_session
        }

    # ========== End Process Management API ==========

    # ========== Runner API (Allowlisted Commands) ==========

    # Allowlisted runner commands - safe to execute via UI
    RUNNER_COMMANDS = {
        "build": {
            "label": "Build",
            "description": "Run build script",
            "commands": ["npm run build", "yarn build", "make build", "cargo build"],
            "icon": "🔨"
        },
        "test": {
            "label": "Test",
            "description": "Run tests",
            "commands": ["npm test", "yarn test", "pytest", "cargo test", "make test"],
            "icon": "✅"
        },
        "lint": {
            "label": "Lint",
            "description": "Run linter",
            "commands": ["npm run lint", "yarn lint", "ruff check .", "cargo clippy"],
            "icon": "🔍"
        },
        "format": {
            "label": "Format",
            "description": "Format code",
            "commands": ["npm run format", "ruff format .", "cargo fmt", "black ."],
            "icon": "📝"
        },
        "typecheck": {
            "label": "Typecheck",
            "description": "Run type checker",
            "commands": ["npm run typecheck", "tsc --noEmit", "mypy .", "pyright"],
            "icon": "📋"
        },
        "dev": {
            "label": "Dev Server",
            "description": "Start dev server",
            "commands": ["npm run dev", "yarn dev", "python -m http.server"],
            "icon": "🚀"
        },
    }

    @app.get("/api/runner/commands")
    async def list_runner_commands(
        token: Optional[str] = Query(None),
    ):
        """List available runner commands."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        return {"commands": RUNNER_COMMANDS}

    @app.post("/api/runner/execute")
    async def execute_runner_command(
        command_id: str = Query(...),
        variant: int = Query(0),  # Which command variant to use
        token: Optional[str] = Query(None),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """
        Execute an allowlisted runner command.

        Sends the command to the PTY (same as user typing it).
        Only commands in RUNNER_COMMANDS are allowed.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Validate target before executing
        target_check = validate_target(session, pane_id)
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
        token: Optional[str] = Query(None),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """
        Execute a custom command (with basic safety checks).

        This is more permissive than the queue but still has some safety rails.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Validate target before executing
        target_check = validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        # Basic safety checks
        dangerous_patterns = [
            r'^\s*rm\s+-rf\s+/',  # rm -rf /
            r'^\s*:(){',          # Fork bomb
            r'>\s*/dev/sd',       # Writing to disk devices
            r'mkfs\.',            # Formatting disks
        ]

        for pattern in dangerous_patterns:
            if re.search(pattern, command):
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

    # ========== End Runner API ==========

    # ========== Preview API ==========

    # Cache for preview config per repo
    _preview_config_cache: Dict[str, dict] = {}
    _preview_status_cache: Dict[str, dict] = {}
    _preview_status_cache_time: float = 0
    PREVIEW_STATUS_CACHE_TTL = 2.0  # seconds

    def load_preview_config(repo_path: Optional[Path]) -> Optional[dict]:
        """Load preview.config.json from repo, with caching."""
        if not repo_path:
            return None

        cache_key = str(repo_path)
        config_file = repo_path / "preview.config.json"

        # Check if file exists
        if not config_file.exists():
            _preview_config_cache.pop(cache_key, None)
            return None

        # Check cache freshness by mtime
        try:
            mtime = config_file.stat().st_mtime
            cached = _preview_config_cache.get(cache_key)
            if cached and cached.get("_mtime") == mtime:
                return cached
        except Exception:
            pass

        # Load and parse
        try:
            import json as json_mod
            content = config_file.read_text()
            config = json_mod.loads(content)
            config["_mtime"] = mtime
            _preview_config_cache[cache_key] = config
            return config
        except Exception as e:
            logger.warning(f"Failed to load preview config: {e}")
            return None

    async def check_service_health(port: int, path: str = "/", timeout: float = 1.5) -> dict:
        """Check if service is responding via TCP connect + optional HTTP probe."""
        import socket
        import httpx

        # First try TCP connect (fast baseline)
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            result = sock.connect_ex(('127.0.0.1', port))
            sock.close()
            if result != 0:
                return {"status": "stopped", "latency": None}
        except Exception:
            return {"status": "stopped", "latency": None}

        # TCP succeeded, try HTTP probe for more accurate status
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                start = time.time()
                resp = await client.get(f"http://127.0.0.1:{port}{path}")
                latency = int((time.time() - start) * 1000)
                return {
                    "status": "running" if resp.status_code < 500 else "error",
                    "latency": latency,
                    "statusCode": resp.status_code
                }
        except httpx.ConnectError:
            return {"status": "stopped", "latency": None}
        except Exception as e:
            # TCP worked but HTTP failed - still likely running
            return {"status": "running", "latency": None, "note": "TCP only"}

    @app.get("/api/preview/config")
    async def get_preview_config(
        token: Optional[str] = Query(None),
    ):
        """Load preview.config.json from current repo."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        config = load_preview_config(repo_path)

        if not config:
            return {"services": [], "exists": False}

        # Strip internal fields
        return {
            "exists": True,
            "name": config.get("name", ""),
            "services": config.get("services", []),
            "tailscaleServe": config.get("tailscaleServe"),
        }

    @app.get("/api/preview/status")
    async def get_preview_status(
        token: Optional[str] = Query(None),
    ):
        """Check health of all configured preview services."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        global _preview_status_cache_time

        # Throttle: return cached if fresh
        now = time.time()
        if now - _preview_status_cache_time < PREVIEW_STATUS_CACHE_TTL:
            return {"services": list(_preview_status_cache.values()), "cached": True}

        repo_path = get_current_repo_path()
        config = load_preview_config(repo_path)

        if not config or not config.get("services"):
            return {"services": []}

        # Check each service
        results = []
        for svc in config.get("services", []):
            port = svc.get("port")
            health_path = svc.get("healthPath", "/")
            if not port:
                continue

            status = await check_service_health(port, health_path)
            result = {
                "id": svc.get("id"),
                "status": status.get("status", "unknown"),
                "latency": status.get("latency"),
            }
            results.append(result)
            _preview_status_cache[svc.get("id")] = result

        _preview_status_cache_time = now
        return {"services": results, "cached": False}

    @app.post("/api/preview/start")
    async def start_preview_service(
        service_id: str = Query(...),
        token: Optional[str] = Query(None),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """Start a preview service by sending its startCommand to PTY."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Validate target (safety: must match current session/pane)
        target_check = validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        # Load config and find service
        repo_path = get_current_repo_path()
        config = load_preview_config(repo_path)
        if not config:
            return JSONResponse({"error": "No preview config found"}, status_code=404)

        service = None
        for svc in config.get("services", []):
            if svc.get("id") == service_id:
                service = svc
                break

        if not service:
            return JSONResponse({"error": f"Service '{service_id}' not found"}, status_code=404)

        start_command = service.get("startCommand")
        if not start_command:
            return JSONResponse({"error": f"No startCommand for '{service_id}'"}, status_code=400)

        # Check if PTY is available
        master_fd = app.state.master_fd
        if not master_fd:
            return JSONResponse({"error": "No PTY available"}, status_code=400)

        # Send command to PTY
        try:
            os.write(master_fd, (start_command + '\r').encode('utf-8'))
            app.state.audit_log.log("preview_start", {
                "service_id": service_id,
                "command": start_command
            })
            return {
                "success": True,
                "service_id": service_id,
                "command": start_command,
                "label": service.get("label", service_id)
            }
        except Exception as e:
            logger.error(f"Preview start failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/preview/stop")
    async def stop_preview_service(
        service_id: str = Query(...),
        token: Optional[str] = Query(None),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """Stop a preview service by sending Ctrl+C to PTY."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Validate target (safety: must match current session/pane)
        target_check = validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        # Check if PTY is available
        master_fd = app.state.master_fd
        if not master_fd:
            return JSONResponse({"error": "No PTY available"}, status_code=400)

        # Send Ctrl+C (0x03) to PTY
        try:
            os.write(master_fd, b'\x03')
            app.state.audit_log.log("preview_stop", {"service_id": service_id})
            return {"success": True, "service_id": service_id}
        except Exception as e:
            logger.error(f"Preview stop failed: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ========== End Preview API ==========

    # ========== Git Rollback API ==========

    @app.get("/api/rollback/git/status")
    async def git_status(
        token: Optional[str] = Query(None),
    ):
        """Get current git status: branch, dirty, ahead/behind, lock status."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        if not repo_path:
            return {
                "has_repo": False,
                "error": "No git repository found"
            }

        try:
            # Get current branch
            branch_result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            )
            branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "unknown"

            # Get dirty status
            status_result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            )
            status_lines = [l for l in status_result.stdout.strip().split('\n') if l]
            is_dirty = bool(status_lines)
            # Count untracked separately (lines starting with ??)
            untracked_files = sum(1 for l in status_lines if l.startswith('??'))
            dirty_files = len(status_lines) - untracked_files  # Modified/staged files

            # Get ahead/behind (may fail if no upstream)
            ahead = 0
            behind = 0
            has_upstream = False
            try:
                rev_result = subprocess.run(
                    ["git", "rev-list", "--left-right", "--count", f"{branch}@{{upstream}}...HEAD"],
                    cwd=repo_path, capture_output=True, text=True, timeout=5
                )
                if rev_result.returncode == 0:
                    parts = rev_result.stdout.strip().split()
                    if len(parts) == 2:
                        behind = int(parts[0])
                        ahead = int(parts[1])
                        has_upstream = True
            except Exception:
                pass

            # Get repo path (relative to home for display)
            display_path = str(repo_path)
            home = str(Path.home())
            if display_path.startswith(home):
                display_path = "~" + display_path[len(home):]

            # Check for associated PR (using gh CLI if available)
            pr_info = None
            try:
                pr_result = subprocess.run(
                    ["gh", "pr", "view", "--json", "number,title,url,state"],
                    cwd=repo_path, capture_output=True, text=True, timeout=5
                )
                if pr_result.returncode == 0:
                    import json as json_mod
                    pr_data = json_mod.loads(pr_result.stdout)
                    pr_info = {
                        "number": pr_data.get("number"),
                        "title": pr_data.get("title"),
                        "url": pr_data.get("url"),
                        "state": pr_data.get("state"),
                    }
            except Exception:
                pass  # gh not available or no PR

            return {
                "has_repo": True,
                "repo_path": display_path,
                "branch": branch,
                "is_dirty": is_dirty,
                "dirty_files": dirty_files,
                "untracked_files": untracked_files,
                "has_upstream": has_upstream,
                "ahead": ahead,
                "behind": behind,
                "op_locked": app.state.git_op_lock.is_locked,
                "current_op": app.state.git_op_lock.current_operation,
                "pr": pr_info,
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/rollback/git/commits")
    async def list_git_commits(
        limit: int = Query(20),
        token: Optional[str] = Query(None),
    ):
        """List recent git commits."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        try:
            result = subprocess.run(
                ["git", "log", f"--max-count={limit}", "--format=%H|%s|%an|%ad", "--date=short"],
                cwd=repo_path, capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return JSONResponse({"error": result.stderr}, status_code=500)

            commits = []
            for line in result.stdout.strip().split("\n"):
                if "|" in line:
                    parts = line.split("|", 3)
                    commits.append({
                        "hash": parts[0],
                        "subject": parts[1],
                        "author": parts[2] if len(parts) > 2 else "",
                        "date": parts[3] if len(parts) > 3 else ""
                    })

            return {"commits": commits}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/history")
    async def get_unified_history(
        limit: int = Query(30),
        token: Optional[str] = Query(None),
    ):
        """Get unified history: commits + snapshots merged chronologically."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        items = []
        session = app.state.current_session

        # Get commits with unix timestamps
        repo_path = get_current_repo_path()
        if repo_path:
            try:
                result = subprocess.run(
                    ["git", "log", f"--max-count={limit}", "--format=%H|%s|%an|%at"],
                    cwd=repo_path, capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().split("\n"):
                        if "|" in line:
                            parts = line.split("|", 3)
                            ts = int(parts[3]) * 1000 if len(parts) > 3 else 0
                            items.append({
                                "type": "commit",
                                "id": parts[0][:7],
                                "hash": parts[0],
                                "subject": parts[1],
                                "author": parts[2] if len(parts) > 2 else "",
                                "timestamp": ts,
                            })
            except Exception as e:
                logger.warning(f"Failed to get commits for history: {e}")

        # Get snapshots
        snapshots = app.state.snapshot_buffer.list_snapshots(session, limit)
        for snap in snapshots:
            items.append({
                "type": "snapshot",
                "id": snap["id"],
                "label": snap["label"],
                "timestamp": snap["timestamp"],
                "pinned": snap.get("pinned", False),
            })

        # Sort by timestamp descending (newest first)
        items.sort(key=lambda x: x["timestamp"], reverse=True)

        # Limit total
        items = items[:limit]

        return {"items": items, "session": session}

    @app.get("/api/rollback/git/commit/{commit_hash}")
    async def get_git_commit_detail(
        commit_hash: str,
        token: Optional[str] = Query(None),
    ):
        """Get commit details including changed files."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Validate hash format (security)
        if not re.match(r'^[a-f0-9]{7,40}$', commit_hash):
            return JSONResponse({"error": "Invalid hash format"}, status_code=400)

        repo_path = get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        try:
            result = subprocess.run(
                ["git", "show", "--stat", "--format=%H%n%s%n%b%n---AUTHOR---%n%an%n---DATE---%n%ad", commit_hash],
                cwd=repo_path, capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                return JSONResponse({"error": "Commit not found"}, status_code=404)

            output = result.stdout
            parts = output.split("\n---AUTHOR---\n")
            first_part = parts[0].split("\n", 2)
            hash_val = first_part[0] if len(first_part) > 0 else commit_hash
            subject = first_part[1] if len(first_part) > 1 else ""
            body = first_part[2] if len(first_part) > 2 else ""

            author_part = parts[1].split("\n---DATE---\n") if len(parts) > 1 else ["", ""]
            author = author_part[0] if len(author_part) > 0 else ""
            rest = author_part[1] if len(author_part) > 1 else ""
            date_and_stat = rest.split("\n", 1)
            date = date_and_stat[0] if len(date_and_stat) > 0 else ""
            stat = date_and_stat[1] if len(date_and_stat) > 1 else ""

            return {
                "hash": hash_val,
                "subject": subject,
                "body": body.strip(),
                "author": author,
                "date": date,
                "stat": stat.strip()
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/rollback/git/revert/dry-run")
    async def dry_run_revert(
        commit_hash: str = Query(...),
        token: Optional[str] = Query(None),
    ):
        """Preview revert without executing."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Validate hash format
        if not re.match(r'^[a-f0-9]{7,40}$', commit_hash):
            return JSONResponse({"error": "Invalid hash format"}, status_code=400)

        repo_path = get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        # Acquire git operation lock
        if not await app.state.git_op_lock.acquire("dry_run"):
            return JSONResponse({
                "error": "Another git operation in progress",
                "current_op": app.state.git_op_lock.current_operation
            }, status_code=409)

        try:
            # Check for uncommitted changes
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            )
            if status.stdout.strip():
                return JSONResponse({
                    "error": "Working directory not clean",
                    "details": "Commit or stash changes first"
                }, status_code=400)

            # Try revert with --no-commit to preview
            result = subprocess.run(
                ["git", "revert", "--no-commit", commit_hash],
                cwd=repo_path, capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                # Reset any partial changes
                subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_path, timeout=10)
                return {
                    "success": False,
                    "error": "Revert would fail",
                    "details": result.stderr
                }

            # Get what would change
            diff = subprocess.run(
                ["git", "diff", "--cached", "--stat"],
                cwd=repo_path, capture_output=True, text=True, timeout=10
            )

            # Reset the staged revert
            subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_path, timeout=10)

            app.state.audit_log.log("revert_dry_run", {"commit": commit_hash})

            return {
                "success": True,
                "commit": commit_hash,
                "changes": diff.stdout,
                "message": f"Revert \"{commit_hash[:7]}\" would succeed"
            }
        except subprocess.TimeoutExpired:
            # Clean up on timeout
            subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_path, timeout=10)
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_path, timeout=10)
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            app.state.git_op_lock.release()

    @app.post("/api/rollback/git/revert/execute")
    async def execute_revert(
        commit_hash: str = Query(...),
        token: Optional[str] = Query(None),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """Execute git revert."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Validate target before destructive operation
        target_check = validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        # Validate hash format
        if not re.match(r'^[a-f0-9]{7,40}$', commit_hash):
            return JSONResponse({"error": "Invalid hash format"}, status_code=400)

        repo_path = get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        # Acquire git operation lock
        if not await app.state.git_op_lock.acquire("execute_revert"):
            return JSONResponse({
                "error": "Another git operation in progress",
                "current_op": app.state.git_op_lock.current_operation
            }, status_code=409)

        try:
            # Check working directory is clean
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            )
            if status.stdout.strip():
                return JSONResponse({
                    "error": "Working directory not clean"
                }, status_code=400)

            # Get current HEAD for undo
            head_before = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            ).stdout.strip()

            # Execute revert
            result = subprocess.run(
                ["git", "revert", "--no-edit", commit_hash],
                cwd=repo_path, capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                return JSONResponse({
                    "success": False,
                    "error": result.stderr
                }, status_code=500)

            # Get new HEAD
            new_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            ).stdout.strip()

            app.state.audit_log.log("revert_execute", {
                "commit": commit_hash,
                "head_before": head_before,
                "head_after": new_head
            })

            return {
                "success": True,
                "reverted_commit": commit_hash,
                "new_commit": new_head,
                "undo_target": head_before
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            app.state.git_op_lock.release()

    @app.post("/api/rollback/git/revert/undo")
    async def undo_revert(
        revert_commit: str = Query(..., description="SHA of the revert commit to undo"),
        token: Optional[str] = Query(None),
    ):
        """Undo a revert by reverting the revert commit (non-destructive)."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Validate hash format
        if not re.match(r'^[a-f0-9]{7,40}$', revert_commit):
            return JSONResponse({"error": "Invalid hash format"}, status_code=400)

        repo_path = get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        # Acquire git operation lock
        if not await app.state.git_op_lock.acquire("undo_revert"):
            return JSONResponse({
                "error": "Another git operation in progress",
                "current_op": app.state.git_op_lock.current_operation
            }, status_code=409)

        try:
            # Check working directory is clean
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            )
            if status.stdout.strip():
                return JSONResponse({
                    "error": "Working directory not clean"
                }, status_code=400)

            # Revert the revert commit (non-destructive, creates new commit)
            result = subprocess.run(
                ["git", "revert", "--no-edit", revert_commit],
                cwd=repo_path, capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                return JSONResponse({
                    "success": False,
                    "error": "Undo failed - revert may have conflicts",
                    "details": result.stderr
                }, status_code=500)

            # Get new HEAD (the undo commit)
            new_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            ).stdout.strip()

            app.state.audit_log.log("revert_undo", {
                "revert_commit": revert_commit,
                "undo_commit": new_head
            })

            return {
                "success": True,
                "reverted_commit": revert_commit,
                "new_commit": new_head
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            app.state.git_op_lock.release()

    # ========== Git Stash API ==========

    @app.post("/api/git/stash/push")
    async def stash_push(
        token: Optional[str] = Query(None),
    ):
        """Create a stash with auto-generated message."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        # Acquire git operation lock
        if not await app.state.git_op_lock.acquire("stash_push"):
            return JSONResponse({
                "error": "Another git operation in progress",
                "current_op": app.state.git_op_lock.current_operation
            }, status_code=409)

        try:
            import time
            timestamp = int(time.time())
            message = f"mobile-overlay-auto-stash-{timestamp}"

            # Stash including untracked files
            result = subprocess.run(
                ["git", "stash", "push", "-u", "-m", message],
                cwd=repo_path, capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                return JSONResponse({
                    "error": "Stash failed",
                    "details": result.stderr
                }, status_code=500)

            # Check if anything was actually stashed
            if "No local changes to save" in result.stdout:
                return JSONResponse({
                    "error": "No changes to stash"
                }, status_code=400)

            app.state.audit_log.log("stash_push", {"message": message})

            return {
                "success": True,
                "stash_ref": "stash@{0}",
                "message": message
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            app.state.git_op_lock.release()

    @app.get("/api/git/stash/list")
    async def stash_list(
        token: Optional[str] = Query(None),
    ):
        """List all stashes."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        try:
            result = subprocess.run(
                ["git", "stash", "list", "--format=%gd|%s|%ar"],
                cwd=repo_path, capture_output=True, text=True, timeout=10
            )

            stashes = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 2)
                if len(parts) >= 2:
                    stashes.append({
                        "ref": parts[0],
                        "message": parts[1],
                        "date": parts[2] if len(parts) > 2 else ""
                    })

            return {"stashes": stashes}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/git/stash/apply")
    async def stash_apply(
        ref: str = Query("stash@{0}"),
        token: Optional[str] = Query(None),
    ):
        """Apply a stash without removing it."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Validate stash ref format
        if not re.match(r'^stash@\{\d+\}$', ref):
            return JSONResponse({"error": "Invalid stash ref format"}, status_code=400)

        repo_path = get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        # Acquire git operation lock
        if not await app.state.git_op_lock.acquire("stash_apply"):
            return JSONResponse({
                "error": "Another git operation in progress",
                "current_op": app.state.git_op_lock.current_operation
            }, status_code=409)

        try:
            result = subprocess.run(
                ["git", "stash", "apply", ref],
                cwd=repo_path, capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                # Check for conflicts
                if "CONFLICT" in result.stdout or "conflict" in result.stderr.lower():
                    return {
                        "success": False,
                        "conflict": True,
                        "details": result.stdout + result.stderr
                    }
                return JSONResponse({
                    "error": "Apply failed",
                    "details": result.stderr
                }, status_code=500)

            app.state.audit_log.log("stash_apply", {"ref": ref})

            return {
                "success": True,
                "ref": ref
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            app.state.git_op_lock.release()

    @app.post("/api/git/stash/drop")
    async def stash_drop(
        ref: str = Query("stash@{0}"),
        token: Optional[str] = Query(None),
    ):
        """Drop a stash."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Validate stash ref format
        if not re.match(r'^stash@\{\d+\}$', ref):
            return JSONResponse({"error": "Invalid stash ref format"}, status_code=400)

        repo_path = get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        # Acquire git operation lock
        if not await app.state.git_op_lock.acquire("stash_drop"):
            return JSONResponse({
                "error": "Another git operation in progress",
                "current_op": app.state.git_op_lock.current_operation
            }, status_code=409)

        try:
            result = subprocess.run(
                ["git", "stash", "drop", ref],
                cwd=repo_path, capture_output=True, text=True, timeout=10
            )

            if result.returncode != 0:
                return JSONResponse({
                    "error": "Drop failed",
                    "details": result.stderr
                }, status_code=500)

            app.state.audit_log.log("stash_drop", {"ref": ref})

            return {"success": True, "ref": ref}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            app.state.git_op_lock.release()

    # ========== Git Discard API ==========

    @app.post("/api/git/discard")
    async def git_discard(
        include_untracked: bool = Query(False),
        token: Optional[str] = Query(None),
        session: Optional[str] = Query(None),
        pane_id: Optional[str] = Query(None),
    ):
        """Discard all uncommitted changes. Optionally remove untracked files."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Validate target before destructive operation
        target_check = validate_target(session, pane_id)
        if not target_check["valid"]:
            return JSONResponse({
                "error": "Target mismatch",
                "message": target_check["error"],
                "expected": target_check["expected"],
                "received": target_check["received"]
            }, status_code=409)

        repo_path = get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo found"}, status_code=400)

        # Acquire git operation lock
        if not await app.state.git_op_lock.acquire("discard"):
            return JSONResponse({
                "error": "Another git operation in progress",
                "current_op": app.state.git_op_lock.current_operation
            }, status_code=409)

        try:
            # Get list of files that will be discarded (for logging)
            status_result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repo_path, capture_output=True, text=True, timeout=5
            )
            files_to_discard = [
                line[3:] for line in status_result.stdout.strip().split("\n")
                if line and not line.startswith("??")
            ]
            untracked_files = [
                line[3:] for line in status_result.stdout.strip().split("\n")
                if line and line.startswith("??")
            ]

            # Reset tracked files
            result = subprocess.run(
                ["git", "reset", "--hard", "HEAD"],
                cwd=repo_path, capture_output=True, text=True, timeout=30
            )

            if result.returncode != 0:
                return JSONResponse({
                    "error": "Reset failed",
                    "details": result.stderr
                }, status_code=500)

            # Optionally clean untracked files
            cleaned_files = []
            if include_untracked and untracked_files:
                clean_result = subprocess.run(
                    ["git", "clean", "-fd"],
                    cwd=repo_path, capture_output=True, text=True, timeout=30
                )
                if clean_result.returncode == 0:
                    cleaned_files = untracked_files

            app.state.audit_log.log("git_discard", {
                "files_reset": files_to_discard,
                "files_cleaned": cleaned_files,
                "include_untracked": include_untracked
            })

            return {
                "success": True,
                "files_discarded": files_to_discard,
                "files_cleaned": cleaned_files
            }
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        finally:
            app.state.git_op_lock.release()

    # ========== End Git Rollback API ==========

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
            except Exception as e:
                logger.error(f"Failed to spawn tmux: {e}")
                await websocket.send_json({"type": "error", "message": str(e)})
                await websocket.close()
                return
        master_fd = app.state.master_fd
        output_buffer = app.state.output_buffer

        # Send hello handshake FIRST - client expects this within 2s
        # Must be sent before capture-pane which can be slow
        try:
            hello_msg = {
                "type": "hello",
                "session": app.state.current_session,
                "pid": app.state.child_pid,
                "started_at": int(time.time()),
            }
            await websocket.send_json(hello_msg)
            logger.info(f"Sent hello handshake: {hello_msg}")
        except Exception as e:
            logger.error(f"Failed to send hello: {e}")
            await websocket.close(code=4500)
            return

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

        # Track PTY death for proper close code
        pty_died = False
        # Track connection closed to prevent send-after-close errors
        connection_closed = False

        # Create tasks for bidirectional I/O
        async def read_from_terminal():
            """Read from terminal and send to WebSocket with batching."""
            nonlocal pty_died, connection_closed
            loop = asyncio.get_event_loop()
            batch = bytearray()
            last_flush = time.time()
            flush_interval = 0.03  # 30ms batching window

            while app.state.active_websocket == websocket and not connection_closed:
                try:
                    # Non-blocking read with select-like behavior
                    data = await loop.run_in_executor(
                        None, lambda: os.read(master_fd, 4096)
                    )
                    if not data:
                        # PTY returned EOF - terminal died
                        logger.warning("PTY returned EOF - terminal process died")
                        pty_died = True
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
                        if app.state.active_websocket == websocket and batch and not connection_closed:
                            await websocket.send_bytes(bytes(batch))
                            batch.clear()
                            last_flush = now

                except Exception as e:
                    # Ignore send-after-close errors (expected during disconnect)
                    if connection_closed or "after sending" in str(e) or "websocket.close" in str(e):
                        break
                    if app.state.active_websocket == websocket:
                        logger.error(f"Error reading from terminal: {e}")
                    break

            # Flush remaining data
            if batch and app.state.active_websocket == websocket and not connection_closed:
                try:
                    await websocket.send_bytes(bytes(batch))
                except Exception:
                    pass

        async def write_to_terminal():
            """Read from WebSocket and write to terminal."""
            while app.state.active_websocket == websocket and not connection_closed:
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
                                    if not connection_closed:
                                        await websocket.send_json({"type": "pong"})
                                elif data.get("type") == "pong":
                                    # Client responding to server_ping - connection is alive
                                    pass  # No action needed, connection confirmed alive
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

        async def server_keepalive():
            """Send periodic pings from server to keep connection alive."""
            SERVER_PING_INTERVAL = 20  # Send ping every 20s from server
            while app.state.active_websocket == websocket and not connection_closed:
                try:
                    await asyncio.sleep(SERVER_PING_INTERVAL)
                    if app.state.active_websocket == websocket and not connection_closed:
                        # Send server-initiated ping (client will respond with pong)
                        await websocket.send_json({"type": "server_ping"})
                except Exception:
                    break

        # Run all tasks concurrently
        read_task = asyncio.create_task(read_from_terminal())
        app.state.read_task = read_task
        write_task = asyncio.create_task(write_to_terminal())
        keepalive_task = asyncio.create_task(server_keepalive())

        try:
            await asyncio.gather(read_task, write_task, keepalive_task)
        except asyncio.CancelledError:
            # Normal termination when connection is replaced or closed
            pass
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
        finally:
            # Signal tasks to stop sending before canceling
            connection_closed = True
            read_task.cancel()
            write_task.cancel()
            keepalive_task.cancel()
            if app.state.active_websocket == websocket:
                app.state.active_websocket = None
                app.state.read_task = None

            # Close with appropriate code
            if pty_died:
                logger.warning("Closing WebSocket with code 4500 (PTY died)")
                try:
                    await websocket.close(code=4500, reason="PTY died")
                except Exception:
                    pass
                # Clear PTY state so next connection recreates it
                app.state.master_fd = None
                app.state.child_pid = None

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

        # Set the slave PTY as the controlling terminal
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        except Exception:
            pass  # May fail on some systems, but dup2 should still work

        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        os.close(master_fd)
        os.close(slave_fd)

        # Set TERM for tmux
        os.environ["TERM"] = "xterm-256color"

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
