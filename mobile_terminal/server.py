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


# ANSI escape sequence pattern for stripping terminal formatting
# Covers: CSI sequences, OSC sequences, character set selection, and other escapes
_ANSI_ESCAPE_RE = re.compile(
    r'\x1b\[[0-9;]*[A-Za-z]'  # CSI sequences (colors, cursor, etc.)
    r'|\x1b\][^\x07]*\x07'     # OSC sequences (title, etc.)
    r'|\x1b[PX^_][^\x1b]*\x1b\\\\'  # DCS/SOS/PM/APC sequences
    r'|\x1b[\(\)][A-Z0-9]'     # Character set selection (e.g., \x1b(B)
    r'|\x1b[=>]'               # Keypad modes
    r'|\x1b[78]'               # Save/restore cursor
    r'|\x1b[DME]'              # Line operations
)


def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from text for plain tail output."""
    return _ANSI_ESCAPE_RE.sub('', text)


def find_utf8_boundary(data: bytes, max_len: int) -> int:
    """Find the last valid UTF-8 character boundary at or before max_len.

    Avoids splitting multi-byte UTF-8 characters which causes garbled output.
    Returns the safe cut position (may be less than max_len).
    """
    if max_len >= len(data):
        return len(data)

    # Start at max_len and scan backwards for a valid boundary
    pos = max_len

    # UTF-8 continuation bytes have pattern 10xxxxxx (0x80-0xBF)
    # We need to find a byte that is NOT a continuation byte
    while pos > 0 and pos > max_len - 4:  # UTF-8 chars are at most 4 bytes
        byte = data[pos]
        # Check if this is a continuation byte (10xxxxxx)
        if (byte & 0xC0) != 0x80:
            # This is either ASCII (0xxxxxxx) or a start byte (11xxxxxx)
            # Safe to cut here
            return pos
        pos -= 1

    # Fallback: couldn't find boundary, use max_len (rare edge case)
    return max_len


async def get_bounded_snapshot(session: str, active_target: str = None, max_bytes: int = 16000) -> str:
    """Get bounded tmux capture-pane snapshot for mode switch catchup.

    Returns screen content with ANSI (-e) for accurate rendering.
    Auto-reduces line count if output exceeds max_bytes.
    """
    import subprocess

    target = get_tmux_target(session, active_target) if active_target else session

    # Start with 50 lines, reduce if too large
    for lines in [50, 30, 20, 10]:
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-p", "-e", "-S", f"-{lines}", "-t", target],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                content = result.stdout or ""
                if len(content) <= max_bytes:
                    return content
                # Too large, try fewer lines
                continue
            else:
                return ""
        except Exception:
            return ""

    # Fallback: return whatever we got, truncated
    return content[:max_bytes] if content else ""


class RingBuffer:
    """Thread-safe ring buffer for storing PTY output."""

    def __init__(self, max_size: int = 1024 * 1024):  # 1MB default
        self._max_size = max_size
        self._buffer = bytearray()
        self._lock = threading.Lock()

    def write(self, data: bytes) -> None:
        """Append data to buffer, discarding oldest bytes if over capacity."""
        with self._lock:
            self._buffer.extend(data)
            if len(self._buffer) > self._max_size:
                excess = len(self._buffer) - self._max_size
                del self._buffer[:excess]

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
        """Return snapshot summaries including new fields."""
        with self._lock:
            if session not in self._snapshots:
                return []
            items = list(self._snapshots[session].values())[-limit:]
            return [{
                "id": s["id"],
                "timestamp": s["timestamp"],
                "label": s["label"],
                "pinned": s.get("pinned", False),
                "pane_id": s.get("pane_id", ""),
                "note": s.get("note", ""),
                "image_path": s.get("image_path"),
                "git_head": s.get("git_head", ""),
            } for s in reversed(items)]

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


from fastapi import FastAPI, File, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Config, DeviceConfig, Repo

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


# Log API cache - caches parsed log content to avoid re-parsing on every request
# Key: (project_id, pane_id), Value: (timestamp, file_mtime, result)
_log_cache: dict = {}
LOG_CACHE_TTL = 2.0  # 2 second TTL - log content changes slowly


def get_cached_log(project_id: str, pane_id: Optional[str], file_mtime: float) -> Optional[dict]:
    """Get cached log result if still valid and file hasn't changed."""
    import time
    key = (project_id, pane_id or "")
    if key in _log_cache:
        ts, cached_mtime, result = _log_cache[key]
        # Valid if within TTL AND file hasn't been modified
        if time.time() - ts < LOG_CACHE_TTL and cached_mtime == file_mtime:
            return result
    return None


def set_cached_log(project_id: str, pane_id: Optional[str], file_mtime: float, result: dict):
    """Cache log result."""
    import time
    key = (project_id, pane_id or "")
    _log_cache[key] = (time.time(), file_mtime, result)
    # Clean old entries
    now = time.time()
    stale = [k for k, (ts, _, _) in _log_cache.items() if now - ts > LOG_CACHE_TTL * 10]
    for k in stale:
        del _log_cache[k]


def get_tmux_target(session_name: str, active_target: str) -> str:
    """
    Convert active_target to tmux target format.

    active_target is stored as "window:pane" (e.g., "2:0")
    tmux expects "session:window.pane" (e.g., "claude:2.0")

    Returns session_name if active_target is None or invalid.
    """
    if not active_target:
        return session_name
    parts = active_target.split(":")
    if len(parts) == 2:
        return f"{session_name}:{parts[0]}.{parts[1]}"
    return session_name


class PermissionDetector:
    """Detects when Claude Code is waiting for tool approval via JSONL log entries."""

    def __init__(self):
        self.last_log_size = 0
        self.last_sent_id = None
        self.log_file = None

    def set_log_file(self, path: Path):
        """Called when we know which JSONL file to watch."""
        self.log_file = Path(path) if path else None
        try:
            self.last_log_size = self.log_file.stat().st_size if self.log_file and self.log_file.exists() else 0
        except Exception:
            self.last_log_size = 0

    def check_sync(self, session: str, target: str) -> Optional[dict]:
        """Check for pending permission request. Returns payload or None."""
        if not self.log_file or not self.log_file.exists():
            return None

        # 1. Check if file grew (new entries)
        try:
            current_size = self.log_file.stat().st_size
        except Exception:
            return None
        if current_size <= self.last_log_size:
            return None

        # 2. Read only new bytes
        new_text = self._read_new_entries(current_size)
        self.last_log_size = current_size

        if not new_text:
            return None

        # 3. Find last tool_use in new entries
        tool_info = self._extract_last_tool_use(new_text)
        if not tool_info:
            return None

        # 4. Confirm Claude is actually waiting (pane_title check)
        try:
            tmux_target = get_tmux_target(session, target)
            title_result = subprocess.run(
                ["tmux", "display-message", "-p", "-t", tmux_target, "#{pane_title}"],
                capture_output=True, text=True, timeout=2,
            )
            if "Signal Detection Pending" not in (title_result.stdout or ""):
                return None
        except Exception:
            return None

        # 5. Build payload (dedupe by id)
        payload_id = f"{tool_info['name']}:{hash(str(tool_info.get('target', '')))}"
        if payload_id == self.last_sent_id:
            return None
        self.last_sent_id = payload_id

        return {
            "tool": tool_info["name"],
            "target": tool_info.get("target", ""),
            "context": tool_info.get("context", ""),
            "id": payload_id,
        }

    def _read_new_entries(self, current_size):
        """Read new JSONL lines since last check."""
        try:
            with open(self.log_file, 'r') as f:
                f.seek(self.last_log_size)
                return f.read()
        except Exception:
            return ""

    def _extract_last_tool_use(self, new_text):
        """Parse JSONL text for the most recent tool_use block."""
        last_tool = None
        for line in new_text.strip().split('\n'):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get('type') != 'assistant':
                    continue
                content = entry.get('message', {}).get('content', [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'tool_use':
                        name = block.get('name', '')
                        inp = block.get('input', {})
                        target = inp.get('command') or inp.get('file_path') or inp.get('pattern') or ''
                        last_tool = {"name": name, "target": str(target)[:200], "context": ""}
            except json.JSONDecodeError:
                continue
        return last_tool

    def clear(self):
        """Clear state (e.g., after user responds)."""
        self.last_sent_id = None


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


def _tmux_session_exists(session: str) -> bool:
    """Check if a tmux session exists."""
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _list_session_windows(session: str) -> list:
    """List windows in a tmux session with their pane info.

    Returns list of dicts with window_index, window_name, pane_id, cwd.
    Only returns the first pane per window.
    """
    try:
        result = subprocess.run(
            [
                "tmux", "list-panes", "-s", "-t", session,
                "-F", "#{window_index}|#{window_name}|#{pane_id}|#{pane_current_path}"
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []

        windows = []
        seen_indices = set()
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.strip().split("|", 3)
            if len(parts) < 4:
                continue
            win_idx = parts[0]
            # Only take first pane per window
            if win_idx in seen_indices:
                continue
            seen_indices.add(win_idx)
            windows.append({
                "window_index": win_idx,
                "window_name": parts[1],
                "pane_id": parts[2],
                "cwd": parts[3],
            })
        return windows
    except Exception as e:
        logger.error(f"auto_setup: error listing session windows: {e}")
        return []


def _match_repo_to_window(repo, windows: list) -> Optional[dict]:
    """Match a repo to an existing tmux window using three-pass strategy.

    1. Exact name match (case-insensitive)
    2. Prefix match (handles suffixed names like 'geo-cv-a3f2')
    3. cwd match (resolved paths)
    """
    repo_label_lower = repo.label.lower()

    # Pass 1: exact name match
    for w in windows:
        if w["window_name"].lower() == repo_label_lower:
            return w

    # Pass 2: prefix match (window name starts with repo label)
    for w in windows:
        if w["window_name"].lower().startswith(repo_label_lower):
            return w

    # Pass 3: cwd match
    try:
        repo_resolved = str(Path(repo.path).resolve())
    except Exception:
        return None
    for w in windows:
        try:
            if str(Path(w["cwd"]).resolve()) == repo_resolved:
                return w
        except Exception:
            continue

    return None


def _get_pane_command(pane_id: str) -> Optional[str]:
    """Get the foreground command running in a tmux pane."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_current_command}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
        return None
    except Exception:
        return None


def _create_tmux_window(session: str, window_name: str, path: str) -> dict:
    """Create a new tmux window in a session.

    Returns dict with target_id and pane_id.
    Raises RuntimeError on failure.
    """
    result = subprocess.run(
        [
            "tmux", "new-window",
            "-t", f"{session}:",
            "-n", window_name,
            "-c", path,
            "-P", "-F", "#{window_index}:#{pane_index}|#{pane_id}"
        ],
        capture_output=True, text=True, timeout=10,
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Failed to create window")

    output = result.stdout.strip()
    if "|" not in output:
        raise RuntimeError(f"Unexpected tmux output format: '{output}'")

    parts = output.split("|")
    return {
        "target_id": parts[0],
        "pane_id": parts[1] if len(parts) > 1 else None,
    }


async def _send_startup_command(pane_id: str, command: str, delay_seconds: float = 0.3):
    """Send a startup command to a tmux pane after a delay."""
    await asyncio.sleep(delay_seconds)
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, "-l", command],
            capture_output=True, timeout=5,
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, "Enter"],
            capture_output=True, timeout=5,
        )
        logger.info(f"auto_setup: sent startup command '{command}' to pane {pane_id}")
    except Exception as e:
        logger.error(f"auto_setup: failed to send startup command to {pane_id}: {e}")


async def ensure_tmux_setup(config) -> dict:
    """Create or adopt a tmux session, ensuring all configured repos have windows.

    Returns a summary dict with session status, adopted/created windows, and any errors.
    """
    session = config.session_name
    result = {
        "session": session,
        "created_session": False,
        "adopted_windows": [],
        "created_windows": [],
        "skipped_commands": [],
        "errors": [],
    }

    # Filter repos belonging to this session
    repos = [r for r in config.repos if r.session == session]
    if not repos:
        logger.info(f"auto_setup: no repos configured for session '{session}', skipping")
        return result

    handled_repos = set()

    # Check if session exists, create if not
    if not _tmux_session_exists(session):
        first_repo = repos[0]
        first_path = str(Path(first_repo.path).resolve())

        if not Path(first_repo.path).exists():
            msg = f"auto_setup: first repo path does not exist: {first_repo.path}"
            logger.error(msg)
            result["errors"].append(msg)
            return result

        try:
            # Sanitize window name
            win_name = re.sub(r'[^a-zA-Z0-9_.-]', '', first_repo.label)[:50] or "window"
            proc = subprocess.run(
                ["tmux", "new-session", "-d", "-s", session, "-n", win_name, "-c", first_path],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode != 0:
                msg = f"auto_setup: failed to create session: {proc.stderr.strip()}"
                logger.error(msg)
                result["errors"].append(msg)
                return result

            result["created_session"] = True
            handled_repos.add(first_repo.label)
            result["created_windows"].append(first_repo.label)
            logger.info(f"auto_setup: created session '{session}' with window '{win_name}'")

            # Send startup command for the newly created first window
            if first_repo.startup_command:
                # Get pane ID of the first window
                windows = _list_session_windows(session)
                if windows:
                    pane_id = windows[0]["pane_id"]
                    delay = first_repo.startup_delay_ms / 1000.0
                    asyncio.create_task(
                        _send_startup_command(pane_id, first_repo.startup_command, delay)
                    )
                    logger.info(f"auto_setup: queued startup command for '{first_repo.label}'")

        except Exception as e:
            msg = f"auto_setup: exception creating session: {e}"
            logger.error(msg)
            result["errors"].append(msg)
            return result
    else:
        logger.info(f"auto_setup: session '{session}' already exists, adopting")

    # List existing windows and match remaining repos
    windows = _list_session_windows(session)

    for repo in repos:
        if repo.label in handled_repos:
            continue

        matched = _match_repo_to_window(repo, windows)
        if matched:
            result["adopted_windows"].append(repo.label)
            logger.info(
                f"auto_setup: adopted window '{matched['window_name']}' "
                f"(index {matched['window_index']}) for repo '{repo.label}'"
            )
            # Never send startup commands to adopted windows
            if repo.startup_command:
                result["skipped_commands"].append(repo.label)
        else:
            # Create a new window for this repo
            repo_path = Path(repo.path)
            if not repo_path.exists():
                msg = f"auto_setup: repo path does not exist: {repo.path}"
                logger.warning(msg)
                result["errors"].append(msg)
                continue

            try:
                win_name = re.sub(r'[^a-zA-Z0-9_.-]', '', repo.label)[:50] or "window"
                win_info = _create_tmux_window(session, win_name, str(repo_path.resolve()))
                result["created_windows"].append(repo.label)
                logger.info(f"auto_setup: created window '{win_name}' for repo '{repo.label}'")

                # Send startup command only for newly created windows with a shell
                if repo.startup_command and win_info.get("pane_id"):
                    pane_cmd = _get_pane_command(win_info["pane_id"])
                    shell_names = {"bash", "zsh", "sh", "fish", "dash", "ksh", "tcsh", "csh"}
                    if pane_cmd and pane_cmd.lower() in shell_names:
                        delay = repo.startup_delay_ms / 1000.0
                        asyncio.create_task(
                            _send_startup_command(win_info["pane_id"], repo.startup_command, delay)
                        )
                        logger.info(f"auto_setup: queued startup command for '{repo.label}'")
                    else:
                        result["skipped_commands"].append(repo.label)
                        logger.info(
                            f"auto_setup: skipped startup command for '{repo.label}' "
                            f"(pane running '{pane_cmd}')"
                        )
            except RuntimeError as e:
                msg = f"auto_setup: failed to create window for '{repo.label}': {e}"
                logger.error(msg)
                result["errors"].append(msg)

    summary = (
        f"auto_setup: session='{session}' created={result['created_session']} "
        f"adopted={len(result['adopted_windows'])} created={len(result['created_windows'])} "
        f"errors={len(result['errors'])}"
    )
    logger.info(summary)

    return result


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


def _resolve_device(request: Request, devices: dict) -> Optional[DeviceConfig]:
    """Resolve client IP to a Tailscale hostname and return matching DeviceConfig."""
    if not devices:
        return None
    client_ip = request.client.host if request.client else None
    if not client_ip:
        return None
    try:
        result = subprocess.run(
            ["tailscale", "whois", "--json", client_ip],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            info = json.loads(result.stdout)
            hostname = info.get("Node", {}).get("ComputedName", "")
            if hostname in devices:
                return devices[hostname]
    except Exception:
        pass
    return None


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
    app.state.target_log_mapping = {}  # Maps pane_id -> {"path": str, "pinned": bool}
    app.state.last_restart_time = 0.0  # Timestamp of last server restart request
    app.state.target_epoch = 0  # Incremented on each target switch for cache invalidation
    app.state.setup_result = None  # Result from ensure_tmux_setup()
    app.state.last_ws_input_time = 0  # Last time mobile client sent input (for desktop activity detection)
    app.state.permission_detector = PermissionDetector()  # JSONL-based permission prompt detector

    async def send_typed(ws, msg_type: str, payload: dict, level: str = "info"):
        """Send a v2 typed message over WebSocket."""
        try:
            await ws.send_json({
                "v": 2,
                "id": str(uuid.uuid4()),
                "type": msg_type,
                "level": level,
                "session": app.state.current_session,
                "target": app.state.active_target,
                "ts": time.time(),
                "payload": payload,
            })
        except Exception:
            pass  # Connection may be closed

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
    async def get_config(request: Request, token: Optional[str] = Query(None)):
        """Return client configuration as JSON, with per-device overrides."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        result = app.state.config.to_dict()
        device = _resolve_device(request, app.state.config.devices)
        if device:
            if device.font_size is not None:
                result["font_size"] = device.font_size
            result["physical_kb"] = device.physical_kb
        return result

    @app.get("/health")
    async def health():
        """Health check endpoint."""
        return {"status": "ok", "version": "0.2.0"}

    @app.get("/api/setup-status")
    async def setup_status(token: Optional[str] = Query(None)):
        """Return tmux auto-setup status and result."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return {
            "auto_setup": config.auto_setup,
            "result": app.state.setup_result,
        }

    @app.post("/restart")
    async def restart_server(token: Optional[str] = Query(None)):
        """Restart the server by sending SIGTERM. Systemd will auto-restart it."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
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
        import time as _time
        _start_total = _time.time()
        logger.info(f"[TIMING] /api/target/select START target_id={target_id}")

        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session = app.state.current_session

        # Verify target exists
        try:
            _t1 = _time.time()
            result = subprocess.run(
                ["tmux", "list-panes", "-s", "-t", session,
                 "-F", "#{window_index}:#{pane_index}"],
                capture_output=True, text=True, timeout=5
            )
            logger.info(f"[TIMING] list-panes took {_time.time()-_t1:.3f}s")
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

        # Clear old target's log mapping if not pinned (force re-detection)
        old_target = app.state.active_target
        if old_target and old_target in app.state.target_log_mapping:
            old_mapping = app.state.target_log_mapping[old_target]
            if not (isinstance(old_mapping, dict) and old_mapping.get("pinned")):
                del app.state.target_log_mapping[old_target]

        app.state.active_target = target_id
        app.state.audit_log.log("target_select", {"target": target_id})

        # Actually switch tmux to the selected pane so the PTY shows it
        switch_verified = False
        try:
            # Parse target_id (format: "window:pane" like "0:1")
            parts = target_id.split(":")
            if len(parts) == 2:
                window_idx, pane_idx = parts
                # Switch to the window first
                _t2 = _time.time()
                subprocess.run(
                    ["tmux", "select-window", "-t", f"{session}:{window_idx}"],
                    capture_output=True, timeout=2
                )
                logger.info(f"[TIMING] select-window took {_time.time()-_t2:.3f}s")
                # Then select the pane within that window (format: session:window.pane)
                _t3 = _time.time()
                subprocess.run(
                    ["tmux", "select-pane", "-t", f"{session}:{window_idx}.{pane_idx}"],
                    capture_output=True, timeout=2
                )
                logger.info(f"[TIMING] select-pane took {_time.time()-_t3:.3f}s")
                logger.info(f"Switched tmux to pane {target_id}")

                # Verify switch completed (max 1s total, not per-iteration)
                _t4 = _time.time()
                _verify_iterations = 0
                _verify_deadline = _t4 + 1.0  # Hard cap at 1 second total
                while _time.time() < _verify_deadline:
                    _verify_iterations += 1
                    try:
                        verify_result = subprocess.run(
                            ["tmux", "display-message", "-t", session, "-p", "#{window_index}:#{pane_index}"],
                            capture_output=True, text=True, timeout=0.5  # Short timeout per call
                        )
                        if verify_result.returncode == 0 and verify_result.stdout.strip() == target_id:
                            switch_verified = True
                            break
                    except subprocess.TimeoutExpired:
                        pass  # Continue loop, will exit via deadline
                    await asyncio.sleep(0.05)
                logger.info(f"[TIMING] verify loop took {_time.time()-_t4:.3f}s ({_verify_iterations} iterations)")

                if not switch_verified:
                    logger.warning(f"Target switch verification failed for {target_id}")
        except Exception as e:
            logger.warning(f"Failed to switch tmux pane: {e}")

        # Increment epoch and clear output buffer on verified switch
        app.state.target_epoch += 1
        app.state.output_buffer.clear()
        logger.info(f"Target switch epoch={app.state.target_epoch}, buffer cleared")

        # Close existing PTY so next WebSocket connection respawns with new target
        # This ensures the PTY attaches to the newly active pane
        _t5 = _time.time()
        if app.state.master_fd is not None:
            try:
                os.close(app.state.master_fd)
                logger.info("Closed PTY for target switch")
            except Exception as e:
                logger.warning(f"Error closing PTY: {e}")
            app.state.master_fd = None

        # Kill child process
        if app.state.child_pid is not None:
            try:
                os.kill(app.state.child_pid, signal.SIGTERM)
            except Exception:
                pass
            app.state.child_pid = None
        logger.info(f"[TIMING] PTY/child cleanup took {_time.time()-_t5:.3f}s")

        # Close active WebSocket to force client reconnect
        _t6 = _time.time()
        if app.state.active_websocket is not None:
            try:
                await app.state.active_websocket.close(code=4003, reason="Target switched")
                logger.info("Closed WebSocket for target switch")
            except Exception:
                pass
            app.state.active_websocket = None
        logger.info(f"[TIMING] WebSocket close took {_time.time()-_t6:.3f}s")

        # Start background file monitor to detect which log file this target uses
        asyncio.create_task(monitor_log_file_for_target(target_id))

        logger.info(f"[TIMING] /api/target/select TOTAL took {_time.time()-_start_total:.3f}s")
        return {
            "success": True,
            "active": target_id,
            "pane_id": target_id,
            "epoch": app.state.target_epoch,
            "verified": switch_verified
        }

    @app.post("/api/window/new")
    async def create_new_window(
        request: Request,
        token: Optional[str] = Query(None)
    ):
        """
        Create a new tmux window in a repo's configured session.

        JSON body:
          - repo_label: Label of repo from config (use this OR path)
          - path: Absolute path to directory under a workspace_dir (use this OR repo_label)
          - window_name: (optional) Name for the new window
          - auto_start_claude: (optional, default false) Start Claude after creating window
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Parse JSON body
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        repo_label = body.get("repo_label")
        dir_path = body.get("path")
        window_name = body.get("window_name", "")
        auto_start_claude = body.get("auto_start_claude", False)

        repo = None  # Set when using repo_label flow

        if repo_label:
            # --- Existing repo-based flow ---
            repo = next((r for r in config.repos if r.label == repo_label), None)
            if not repo:
                return JSONResponse({
                    "error": f"Unknown repo: {repo_label}",
                    "available_repos": [r.label for r in config.repos]
                }, status_code=404)

            repo_path = Path(repo.path)
            if not repo_path.exists():
                return JSONResponse({
                    "error": f"Repo path does not exist: {repo.path}"
                }, status_code=400)

            session = repo.session
            resolved_path = str(repo_path.resolve())

        elif dir_path:
            # --- Workspace directory flow ---
            # Validate path is under one of the configured workspace_dirs
            target = Path(dir_path).resolve()
            allowed = False
            for ws_dir in config.workspace_dirs:
                ws_resolved = Path(ws_dir).expanduser().resolve()
                try:
                    target.relative_to(ws_resolved)
                    allowed = True
                    break
                except ValueError:
                    continue

            if not allowed:
                return JSONResponse({
                    "error": "Path is not under any configured workspace_dir"
                }, status_code=403)

            if not target.is_dir():
                return JSONResponse({
                    "error": f"Path does not exist or is not a directory: {dir_path}"
                }, status_code=400)

            session = app.state.current_session
            resolved_path = str(target)

        else:
            return JSONResponse({
                "error": "Either repo_label or path is required"
            }, status_code=400)

        # Sanitize window name: only allow [a-zA-Z0-9_.-], max 50 chars
        if window_name:
            sanitized_name = re.sub(r'[^a-zA-Z0-9_.-]', '', window_name)[:50]
        else:
            sanitized_name = ""

        # If sanitized name is empty, use directory basename
        if not sanitized_name:
            dir_basename = Path(resolved_path).name
            sanitized_name = re.sub(r'[^a-zA-Z0-9_.-]', '', dir_basename)[:50]
            if not sanitized_name and repo_label:
                sanitized_name = re.sub(r'[^a-zA-Z0-9_.-]', '', repo_label)[:50]
            if not sanitized_name:
                sanitized_name = "window"

        # Add random suffix to handle name collisions
        final_name = f"{sanitized_name}-{secrets.token_hex(2)}"

        try:
            win_info = _create_tmux_window(session, final_name, resolved_path)
            target_id = win_info["target_id"]
            pane_id = win_info.get("pane_id")

            # Audit log the action
            app.state.audit_log.log("window_create", {
                "repo_label": repo_label or Path(resolved_path).name,
                "session": session,
                "window_name": final_name,
                "target_id": target_id,
                "pane_id": pane_id,
                "path": resolved_path,
                "auto_start_claude": auto_start_claude
            })

            logger.info(f"Created window '{final_name}' in session '{session}' at {resolved_path}")

            # If auto_start_claude, send startup command after configured delay
            if auto_start_claude and pane_id:
                # Get startup command from repo config (if repo flow), default to "claude"
                startup_cmd = (repo.startup_command if repo else None) or "claude"

                # Validate startup command
                if "\n" in startup_cmd or "\r" in startup_cmd:
                    return JSONResponse({
                        "error": "startup_command cannot contain newlines"
                    }, status_code=400)
                if len(startup_cmd) > 200:
                    return JSONResponse({
                        "error": "startup_command exceeds 200 character limit"
                    }, status_code=400)

                startup_delay = (repo.startup_delay_ms if repo else 300) / 1000.0
                audit_label = repo_label or Path(resolved_path).name

                async def _send_and_audit():
                    await _send_startup_command(pane_id, startup_cmd, startup_delay)
                    app.state.audit_log.log("startup_command_exec", {
                        "pane_id": pane_id,
                        "command": startup_cmd,
                        "repo_label": audit_label
                    })

                asyncio.create_task(_send_and_audit())

            return {
                "success": True,
                "target_id": target_id,
                "pane_id": pane_id,
                "window_name": final_name,
                "session": session,
                "repo_label": repo_label,
                "path": resolved_path,
                "auto_start_claude": auto_start_claude
            }

        except RuntimeError as e:
            error_msg = str(e)
            if "can't find" in error_msg.lower() or "no such session" in error_msg.lower():
                return JSONResponse({
                    "error": f"Session '{session}' not found. Create it first with: tmux new -s {session}"
                }, status_code=400)
            return JSONResponse({"error": error_msg}, status_code=500)
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Timeout creating window"}, status_code=504)
        except Exception as e:
            logger.error(f"Error creating window: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/pane/kill")
    async def kill_pane(
        request: Request,
        token: Optional[str] = Query(None)
    ):
        """
        Kill a tmux pane. Cannot kill the currently active pane.

        JSON body:
          - target_id: Pane target in "window:pane" format (e.g. "2:0")
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        target_id = body.get("target_id")
        if not target_id or not isinstance(target_id, str):
            return JSONResponse({"error": "target_id is required"}, status_code=400)

        # Validate format: "window:pane"
        parts = target_id.split(":")
        if len(parts) != 2 or not all(p.strip() for p in parts):
            return JSONResponse({"error": "Invalid target_id format, expected 'window:pane'"}, status_code=400)

        # Cannot kill the active pane
        if target_id == app.state.active_target:
            return JSONResponse({"error": "Cannot kill the active pane"}, status_code=400)

        session = app.state.current_session
        tmux_target = get_tmux_target(session, target_id)

        # Verify the pane exists
        try:
            check = subprocess.run(
                ["tmux", "list-panes", "-t", tmux_target],
                capture_output=True, text=True, timeout=5
            )
            if check.returncode != 0:
                return JSONResponse({"error": f"Pane {target_id} not found"}, status_code=404)
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Timeout checking pane"}, status_code=504)

        # Kill the pane
        try:
            result = subprocess.run(
                ["tmux", "kill-pane", "-t", tmux_target],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                return JSONResponse({"error": f"Failed to kill pane: {result.stderr.strip()}"}, status_code=500)
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Timeout killing pane"}, status_code=504)

        app.state.audit_log.log("pane_kill", {
            "target_id": target_id,
            "session": session,
        })

        logger.info(f"Killed pane {target_id} in session '{session}'")
        return {"success": True, "killed": target_id}

    @app.get("/api/workspace/dirs")
    async def list_workspace_dirs(token: Optional[str] = Query(None)):
        """
        List directories under configured workspace_dirs for new window creation.
        Excludes hidden dirs and dirs already in config.repos.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Build set of resolved repo paths for exclusion
        repo_paths = set()
        for repo in config.repos:
            try:
                repo_paths.add(str(Path(repo.path).expanduser().resolve()))
            except Exception:
                pass

        dirs = []
        for ws_dir in config.workspace_dirs:
            ws_path = Path(ws_dir).expanduser()
            if not ws_path.is_dir():
                continue
            parent_display = ws_dir  # Keep original form (e.g. "~/dev")
            try:
                entries = sorted(os.scandir(ws_path), key=lambda e: e.name.lower())
            except OSError:
                continue
            for entry in entries:
                if not entry.is_dir(follow_symlinks=True):
                    continue
                if entry.name.startswith('.'):
                    continue
                resolved = str(Path(entry.path).resolve())
                if resolved in repo_paths:
                    continue
                dirs.append({
                    "name": entry.name,
                    "path": resolved,
                    "parent": parent_display,
                })
                if len(dirs) >= 200:
                    break
            if len(dirs) >= 200:
                break

        return {"dirs": dirs}

    @app.get("/api/repos")
    async def list_repos(token: Optional[str] = Query(None)):
        """
        List configured repos available for new window creation.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repos_list = []
        for repo in config.repos:
            repo_path = Path(repo.path)
            repos_list.append({
                "label": repo.label,
                "path": repo.path,
                "session": repo.session,
                "exists": repo_path.exists(),
                "startup_command": repo.startup_command,
                "startup_delay_ms": repo.startup_delay_ms,
            })

        return {
            "repos": repos_list,
            "current_session": app.state.current_session
        }

    @app.get("/api/files/tree")
    async def list_files_tree(token: Optional[str] = Query(None)):
        """
        List all files in repo as a flat list grouped by directory.
        Uses git ls-files to respect .gitignore.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        if not repo_path:
            return {"files": [], "directories": [], "root": None}

        try:
            # Get list of tracked files using git ls-files
            result = subprocess.run(
                ["git", "ls-files"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=repo_path,
            )

            if result.returncode != 0:
                # Fallback: list files excluding .git
                result = subprocess.run(
                    ["find", ".", "-type", "f", "-not", "-path", "./.git/*"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    cwd=repo_path,
                )
                files = sorted([f.lstrip("./") for f in result.stdout.strip().split("\n") if f])
            else:
                files = sorted(result.stdout.strip().split("\n"))

            # Build directory structure
            directories = set()
            for f in files:
                parts = f.split("/")
                for i in range(1, len(parts)):
                    directories.add("/".join(parts[:i]))

            return {
                "files": files,
                "directories": sorted(directories),
                "root": str(repo_path),
                "root_name": repo_path.name
            }

        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Listing timeout"}, status_code=504)
        except Exception as e:
            logger.error(f"File tree error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

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

            repo_path = get_current_repo_path()
            cwd = str(repo_path) if repo_path else None

            # Get list of tracked files using git ls-files
            result = subprocess.run(
                ["git", "ls-files"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=cwd,
            )

            if result.returncode != 0:
                # Fallback to find if not a git repo
                result = subprocess.run(
                    ["find", ".", "-type", "f", "-name", f"*{q}*", "-not", "-path", "./.git/*"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=cwd,
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

    @app.get("/api/file")
    async def read_file(path: str = Query(...), token: Optional[str] = Query(None)):
        """
        Read a file from the current repo.
        Path is relative to repo root.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo path"}, status_code=400)

        # Sanitize path - no parent traversal
        if ".." in path or path.startswith("/"):
            return JSONResponse({"error": "Invalid path"}, status_code=400)

        file_path = (repo_path / path).resolve()
        # Ensure resolved path is still within repo (catches symlink escapes)
        if not str(file_path).startswith(str(repo_path.resolve())):
            return JSONResponse({"error": "Invalid path"}, status_code=400)

        if not file_path.exists():
            return JSONResponse({"error": "File not found"}, status_code=404)

        if not file_path.is_file():
            return JSONResponse({"error": "Not a file"}, status_code=400)

        # Limit file size (1MB)
        if file_path.stat().st_size > 1024 * 1024:
            return JSONResponse({"error": "File too large"}, status_code=413)

        try:
            content = file_path.read_text(errors="replace")
            return {"path": path, "content": content}
        except Exception as e:
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
    async def refresh_terminal(
        token: Optional[str] = Query(None),
        cols: Optional[int] = Query(None),
        rows: Optional[int] = Query(None),
    ):
        """
        Get current terminal snapshot for refresh.
        If cols/rows provided, resizes tmux pane first to fix garbled output.
        Uses capture-pane with visible content only.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        try:
            import subprocess
            session_name = app.state.current_session

            # Use active target pane if set, otherwise fall back to session
            target = get_tmux_target(session_name, app.state.active_target)

            # Resize tmux pane if dimensions provided (fixes garbled output)
            resized = False
            if cols and rows:
                resize_result = subprocess.run(
                    ["tmux", "resize-pane", "-t", target, "-x", str(cols), "-y", str(rows)],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if resize_result.returncode != 0:
                    logger.warning(f"tmux resize-pane failed: {resize_result.stderr}")
                else:
                    logger.info(f"Resized tmux pane {target} to {cols}x{rows}")
                    resized = True

                    # Send Ctrl+L to force screen redraw after resize
                    subprocess.run(
                        ["tmux", "send-keys", "-t", target, "C-l"],
                        capture_output=True,
                        timeout=1,
                    )
                    # Small delay for redraw to complete
                    await asyncio.sleep(0.15)

            # Capture visible area only (not scrollback) to avoid stale wrapped content
            # Use -S - to start from visible area, or omit -S for default
            result = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", target],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return JSONResponse(
                    {"error": f"tmux capture-pane failed: {result.stderr}"},
                    status_code=500,
                )
            return {"content": result.stdout, "session": session_name, "target": target}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Refresh timeout"}, status_code=504)
        except Exception as e:
            logger.error(f"Refresh error: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/restart")
    async def restart_server(request: Request, token: Optional[str] = Query(None)):
        """
        Trigger a safe server restart without affecting tmux/Claude sessions.

        - Debounced: 429 if restarted within last 30 seconds
        - Tries systemd first, falls back to execv
        - Returns 202 immediately, restart happens after response flushes
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Debounce: prevent restart loops
        RESTART_COOLDOWN = 30  # seconds
        now = time.time()
        time_since_last = now - app.state.last_restart_time
        if time_since_last < RESTART_COOLDOWN:
            retry_after = int(RESTART_COOLDOWN - time_since_last) + 1
            logger.warning(f"Restart request rejected: cooldown ({retry_after}s remaining)")
            return JSONResponse(
                {"error": "Restart too soon", "retry_after": retry_after},
                status_code=429,
            )

        # Get client info for logging
        client_ip = "unknown"
        if request.client:
            client_ip = request.client.host

        app.state.last_restart_time = now
        logger.info(f"Restart requested by {client_ip}")

        async def do_restart():
            """Perform restart after response flushes."""
            await asyncio.sleep(0.3)  # Let response flush

            # Try systemd first
            try:
                result = subprocess.run(
                    ["systemctl", "--user", "is-active", "mobile-terminal.service"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                )
                if result.returncode == 0:
                    logger.info(f"Restarting via systemd (requested by {client_ip})")
                    subprocess.Popen(
                        ["systemctl", "--user", "restart", "mobile-terminal.service"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

            # Fallback: execv (replaces process in-place)
            # Note: Not compatible with uvicorn --reload or multiple workers
            logger.info(f"Restarting via execv (requested by {client_ip})")
            os.execv(sys.executable, [sys.executable] + sys.argv)

        # Schedule restart in background
        asyncio.create_task(do_restart())

        return JSONResponse({"status": "restarting"}, status_code=202)

    @app.post("/api/reload-env")
    async def reload_env(token: Optional[str] = Query(None)):
        """
        Reload environment variables from .env file.
        Useful for updating API keys without full server restart.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        try:
            from dotenv import load_dotenv
            # override=True replaces existing env vars with .env values
            loaded = load_dotenv(override=True)
            if loaded:
                logger.info("Reloaded .env file")
                return {"status": "reloaded", "message": "Environment variables refreshed from .env"}
            else:
                return {"status": "no_file", "message": "No .env file found (env unchanged)"}
        except ImportError:
            return JSONResponse(
                {"error": "python-dotenv not installed"},
                status_code=500,
            )
        except Exception as e:
            logger.error(f"Failed to reload .env: {e}")
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
                target = get_tmux_target(session_name, app.state.active_target)
                result = subprocess.run(
                    ["tmux", "display-message", "-p", "-t", target,
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
        if not repo_path:
            return {"exists": False, "content": "", "session": app.state.current_session}
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
        if not repo_path:
            return {"exists": False, "content": "", "session": app.state.current_session}
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
                if len(content.split('\n')) > 10:
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
                if len(content.split('\n')) > 15:
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
                        # File was modified - associate with this target (not pinned)
                        app.state.target_log_mapping[target_id] = {"path": str(f), "pinned": False}
                        app.state.permission_detector.set_log_file(f)
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

        # Check if we have a PINNED mapping for this target (user explicitly selected)
        if target_id:
            cached = app.state.target_log_mapping.get(target_id)
            if cached:
                is_pinned = cached.get("pinned", False) if isinstance(cached, dict) else False
                if is_pinned:
                    cached_path = Path(cached["path"]) if isinstance(cached, dict) else Path(cached)
                    if cached_path.exists():
                        logger.debug(f"Using pinned log file for target {target_id}: {cached_path.name}")
                        return cached_path

        # For non-pinned: ALWAYS use the most recently modified file
        # This is simpler and more reliable than complex process detection
        newest_file = max(jsonl_files, key=lambda f: f.stat().st_mtime)
        logger.info(f"Using newest log file: {newest_file.name} (mtime-based)")
        return newest_file

    # NOTE: Legacy process-based detection code removed in favor of simple mtime approach.
    # The complex detection (matching Claude process to log file via timestamps) was unreliable,
    # especially after plan mode transitions. Simple mtime-based selection is more robust.
    @app.get("/api/log")
    async def get_log(
        request: Request,
        token: Optional[str] = Query(None),
        limit: int = Query(200),
        session_id: Optional[str] = Query(None, description="Specific session UUID to view (for Docs browser)"),
        pane_id: Optional[str] = Query(None, description="Pane ID (window:pane) to get log for, avoids race with global state"),
    ):
        """
        Get the Claude conversation log from ~/.claude/projects/.
        Finds the most recently modified .jsonl file for the current repo.
        Parses JSONL and returns readable conversation text.
        Falls back to cached log if source is cleared (e.g., after /clear).

        If session_id is provided, loads that specific session log (read-only view).
        If pane_id is provided, uses that pane's cwd for repo path (avoids multi-tab race condition).
        """
        import json
        import re

        # Log client ID for debugging duplicate requests
        client_id = request.headers.get('X-Client-ID', 'unknown')[:8]
        logger.debug(f"[{client_id}] GET /api/log pane_id={pane_id}")

        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Use pane_id if provided (avoids race condition with multi-tab)
        # Otherwise fall back to global active_target
        target_id = pane_id or app.state.active_target

        # Get repo path - either from explicit pane cwd or global state
        if pane_id:
            # Get cwd directly from tmux for this pane
            try:
                parts = pane_id.split(":")
                if len(parts) == 2:
                    result = subprocess.run(
                        ["tmux", "display-message", "-t", f"{app.state.current_session}:{parts[0]}.{parts[1]}", "-p", "#{pane_current_path}"],
                        capture_output=True, text=True, timeout=2
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        repo_path = Path(result.stdout.strip())
                    else:
                        repo_path = get_current_repo_path()
                else:
                    repo_path = get_current_repo_path()
            except Exception:
                repo_path = get_current_repo_path()
        else:
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

        # If specific session_id provided, load that directly (for Docs browser)
        if session_id:
            log_file = claude_projects_dir / f"{session_id}.jsonl"
            if not log_file.exists():
                return {"exists": False, "content": "", "error": f"Session not found: {session_id}"}
        else:
            # Detect which log file belongs to this target
            log_file = detect_target_log_file(target_id, app.state.current_session, claude_projects_dir)
            if not log_file:
                return return_cached()
            # Update permission detector with discovered log file
            if not app.state.permission_detector.log_file:
                app.state.permission_detector.set_log_file(log_file)

        # Check mtime-based cache - avoid re-parsing if file unchanged
        try:
            file_stat = log_file.stat()
            file_mtime = file_stat.st_mtime
            cached_result = get_cached_log(project_id, target_id, file_mtime)
            if cached_result:
                logger.debug(f"Log cache hit for {log_file.name}")
                return cached_result
        except Exception as e:
            logger.debug(f"Log cache check failed: {e}")
            file_mtime = 0

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

            # If log was cleared (empty), fall back to cache
            if not conversation:
                return return_cached()

            # Redact potential secrets from each message
            def redact_secrets(text):
                text = re.sub(r'(sk-[a-zA-Z0-9]{20,})', '[REDACTED_API_KEY]', text)
                text = re.sub(r'(ghp_[a-zA-Z0-9]{36,})', '[REDACTED_GITHUB_TOKEN]', text)
                return text

            messages = [redact_secrets(msg) for msg in conversation]
            content = '\n\n'.join(messages)

            # Cache the content for persistence across /clear
            write_log_cache(project_id, content)

            result = {
                "exists": True,
                "content": content,  # For backward compatibility
                "messages": messages,  # New: array of messages (preserves code blocks)
                "path": str(log_file),
                "session": app.state.current_session,
                "modified": file_mtime,  # Use cached mtime to avoid extra stat call
                "truncated": truncated,
            }

            # Cache parsed result for fast subsequent requests
            if file_mtime > 0:
                set_cached_log(project_id, target_id, file_mtime, result)
                logger.debug(f"Log cache set for {log_file.name}")

            return result
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

    @app.get("/api/log/sessions")
    async def list_log_sessions(token: Optional[str] = Query(None)):
        """
        List available log files for the current project directory.
        Returns metadata for each session log to allow manual selection.
        """
        import json
        from datetime import datetime

        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        if not repo_path:
            return {"sessions": [], "error": "No repo path found"}

        project_id = str(repo_path.resolve()).replace("~", "-").replace("/", "-")
        claude_projects_dir = Path.home() / ".claude" / "projects" / project_id

        if not claude_projects_dir.exists():
            return {"sessions": [], "current": None, "detection_method": None}

        jsonl_files = list(claude_projects_dir.glob("*.jsonl"))
        if not jsonl_files:
            return {"sessions": [], "current": None, "detection_method": None}

        # Get the currently detected/pinned log file
        target_id = app.state.active_target
        current_log = detect_target_log_file(target_id, app.state.current_session, claude_projects_dir)
        current_id = current_log.stem if current_log else None

        # Check if current is pinned
        cached = app.state.target_log_mapping.get(target_id) if target_id else None
        is_pinned = cached.get("pinned", False) if isinstance(cached, dict) else False
        detection_method = "pinned" if is_pinned else "auto"

        sessions = []
        for log_file in jsonl_files:
            try:
                stat = log_file.stat()
                session_id = log_file.stem

                # Get first user message as preview and session start time
                preview = ""
                started = None
                try:
                    with open(log_file, 'r') as f:
                        for line in f:
                            if not line.strip():
                                continue
                            entry = json.loads(line)
                            # Get timestamp from first entry
                            if started is None:
                                ts_str = entry.get('timestamp', '')
                                if not ts_str and 'snapshot' in entry:
                                    ts_str = entry['snapshot'].get('timestamp', '')
                                if ts_str:
                                    started = ts_str
                            # Get first user message as preview
                            if entry.get('type') == 'user':
                                msg = entry.get('message', {})
                                content = msg.get('content', '')
                                if isinstance(content, str) and content.strip():
                                    preview = content.strip()[:100]
                                    break
                except Exception:
                    pass

                sessions.append({
                    "id": session_id,
                    "started": started,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat() + "Z",
                    "size": stat.st_size,
                    "preview": preview,
                    "is_current": session_id == current_id,
                    "is_pinned": is_pinned and session_id == current_id,
                })
            except Exception as e:
                logger.debug(f"Error reading log file {log_file}: {e}")
                continue

        # Sort by modification time (most recent first)
        sessions.sort(key=lambda s: s.get("modified", ""), reverse=True)

        return {
            "sessions": sessions,
            "current": current_id,
            "detection_method": detection_method,
        }

    @app.post("/api/log/select")
    async def select_log_session(
        token: Optional[str] = Query(None),
        session_id: str = Query(..., description="Session UUID to pin"),
    ):
        """
        Pin a specific log file to the current target.
        This overrides auto-detection until unpinned.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        if not repo_path:
            return JSONResponse({"error": "No repo path found"}, status_code=400)

        project_id = str(repo_path.resolve()).replace("~", "-").replace("/", "-")
        claude_projects_dir = Path.home() / ".claude" / "projects" / project_id

        log_file = claude_projects_dir / f"{session_id}.jsonl"
        if not log_file.exists():
            return JSONResponse({"error": f"Log file not found: {session_id}"}, status_code=404)

        target_id = app.state.active_target
        if not target_id:
            return JSONResponse({"error": "No active target selected"}, status_code=400)

        # Pin the log file to this target
        app.state.target_log_mapping[target_id] = {"path": str(log_file), "pinned": True}
        app.state.permission_detector.set_log_file(log_file)
        logger.info(f"Pinned log file for target {target_id}: {session_id}")

        return {
            "success": True,
            "target": target_id,
            "session_id": session_id,
            "pinned": True,
        }

    @app.post("/api/log/unpin")
    async def unpin_log_session(token: Optional[str] = Query(None)):
        """
        Unpin the current target's log file, reverting to auto-detection.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        target_id = app.state.active_target
        if not target_id:
            return JSONResponse({"error": "No active target selected"}, status_code=400)

        cached = app.state.target_log_mapping.get(target_id)
        if not cached:
            return {"success": True, "message": "No mapping to unpin"}

        # Remove the mapping so auto-detection runs again
        del app.state.target_log_mapping[target_id]
        logger.info(f"Unpinned log file for target {target_id}")

        return {
            "success": True,
            "target": target_id,
            "message": "Reverted to auto-detection",
        }

    @app.get("/api/docs/context")
    async def get_context_doc(token: Optional[str] = Query(None)):
        """
        Read .claude/CONTEXT.md from the current target's repo.
        Returns the raw markdown content for display in Docs modal.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        if not repo_path:
            return {"exists": False, "content": "", "error": "No repo path found"}

        context_file = repo_path / ".claude" / "CONTEXT.md"
        if not context_file.exists():
            return {
                "exists": False,
                "content": "",
                "path": str(context_file),
            }

        try:
            content = context_file.read_text(errors="replace")
            return {
                "exists": True,
                "content": content,
                "path": str(context_file),
                "modified": context_file.stat().st_mtime,
            }
        except Exception as e:
            logger.error(f"Error reading CONTEXT.md: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/docs/touch")
    async def get_touch_summary(token: Optional[str] = Query(None)):
        """
        Read .claude/touch-summary.md from the current target's repo.
        Returns the raw markdown content for display in Docs modal.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        repo_path = get_current_repo_path()
        if not repo_path:
            return {"exists": False, "content": "", "error": "No repo path found"}

        touch_file = repo_path / ".claude" / "touch-summary.md"
        if not touch_file.exists():
            return {
                "exists": False,
                "content": "",
                "path": str(touch_file),
            }

        try:
            content = touch_file.read_text(errors="replace")
            return {
                "exists": True,
                "content": content,
                "path": str(touch_file),
                "modified": touch_file.stat().st_mtime,
            }
        except Exception as e:
            logger.error(f"Error reading touch-summary.md: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/terminal/capture")
    async def capture_terminal(
        request: Request,
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

        # Log client ID for debugging duplicate requests
        client_id = request.headers.get('X-Client-ID', 'unknown')[:8]
        logger.debug(f"[{client_id}] GET /api/terminal/capture lines={lines}")

        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Use provided session or fall back to current
        target_session = session or app.state.current_session
        if not target_session:
            return {"content": "", "error": "No session"}

        # Use active_target if no explicit pane provided, otherwise use params
        if app.state.active_target and pane == 0 and session is None:
            # Use active target - convert "window:pane" to "session:window.pane"
            target = get_tmux_target(target_session, app.state.active_target)
            pane_id = app.state.active_target
        else:
            # Use explicit params
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

    @app.get("/api/terminal/snapshot")
    async def terminal_snapshot(
        token: Optional[str] = Query(None),
        target: Optional[str] = Query(None),
    ):
        """
        Get terminal snapshot for resync after queue overflow.

        Returns tmux capture-pane with ANSI escape sequences (-e) for
        accurate screen reproduction. Limited to 80 lines max.
        Used by client when terminal render queue overflows.
        """
        import subprocess

        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session_name = app.state.current_session
        if not session_name:
            return {"content": "", "error": "No session"}

        # Use provided target or active target
        if target:
            tmux_target = get_tmux_target(session_name, target)
        elif app.state.active_target:
            tmux_target = get_tmux_target(session_name, app.state.active_target)
        else:
            tmux_target = session_name

        try:
            # Capture with ANSI escape sequences for accurate screen state
            # Limit to 80 lines to keep payload reasonable
            result = subprocess.run(
                ["tmux", "capture-pane", "-p", "-e", "-S", "-80", "-t", tmux_target],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                content = result.stdout or ""
                # If still too large, reduce further
                if len(content) > 50000:  # 50KB max
                    result = subprocess.run(
                        ["tmux", "capture-pane", "-p", "-e", "-S", "-40", "-t", tmux_target],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    content = result.stdout or "" if result.returncode == 0 else ""
                logger.info(f"Terminal snapshot: {len(content)} chars")
                return {"content": content, "target": tmux_target}
            else:
                logger.warning(f"Snapshot failed: {result.stderr}")
                return {"content": "", "error": result.stderr}
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Snapshot timeout"}, status_code=504)
        except Exception as e:
            logger.error(f"Snapshot error: {e}")
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
                # Use active target pane if set, otherwise fall back to session default
                target = get_tmux_target(session, app.state.active_target)
                try:
                    result = subprocess.run(
                        ["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{terminal_lines}"],
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
                if not re.match(r'^[\w\-\.]+\.md$', plan_filename):
                    logger.warning(f"Invalid plan filename rejected: {plan_filename}")
                    plan_filename = None
            except Exception:
                plan_filename = None
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
            # Use active target pane if set, otherwise fall back to session default
            target = get_tmux_target(session, app.state.active_target)
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", target, "-p", "-S", "-50"],
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
        pane_id: Optional[str] = Query(None),
        token: Optional[str] = Query(None),
    ):
        """List available snapshots, optionally filtered by pane_id."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session = app.state.current_session
        target = pane_id or app.state.active_target
        buf = app.state.snapshot_buffer

        # Try target-scoped key first, then fall back to session-only
        snap_key = f"{session}:{target}" if target else session
        snapshots = buf.list_snapshots(snap_key, limit)
        if not snapshots and target:
            # Fall back to session-only snapshots (legacy)
            snapshots = buf.list_snapshots(session, limit)

        logger.info(f"List snapshots: key={snap_key}, count={len(snapshots)}")
        return {"session": session, "pane_id": target, "snapshots": snapshots}

    @app.get("/api/rollback/preview/{snap_id}")
    async def get_preview(
        snap_id: str,
        token: Optional[str] = Query(None),
    ):
        """Get full snapshot data. Populates heavy fields on demand."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session = app.state.current_session
        target = app.state.active_target
        buf = app.state.snapshot_buffer

        # Search in target-scoped key first, then session
        snap_key = f"{session}:{target}" if target else session
        snapshot = buf.get_snapshot(snap_key, snap_id)
        if not snapshot and target:
            snapshot = buf.get_snapshot(session, snap_id)

        if not snapshot:
            return JSONResponse({"error": "Snapshot not found"}, status_code=404)

        # Populate heavy fields on demand if they're empty (lazy loading)
        if not snapshot.get("terminal_text") and snapshot.get("pane_id"):
            try:
                tmux_t = get_tmux_target(session, snapshot["pane_id"])
                cap = subprocess.run(
                    ["tmux", "capture-pane", "-p", "-S", "-100", "-t", tmux_t],
                    capture_output=True, text=True, timeout=3,
                )
                if cap.returncode == 0:
                    snapshot["terminal_text"] = cap.stdout or ""
            except Exception:
                pass

        if not snapshot.get("log_entries") and snapshot.get("log_path") and snapshot.get("log_offset"):
            try:
                lp = Path(snapshot["log_path"])
                if lp.exists():
                    # Read last 4KB before the offset for context
                    offset = snapshot["log_offset"]
                    read_start = max(0, offset - 4096)
                    with open(lp, 'rb') as f:
                        f.seek(read_start)
                        data = f.read(offset - read_start)
                    snapshot["log_entries"] = data.decode('utf-8', errors='replace')
            except Exception:
                pass

        return snapshot

    @app.post("/api/rollback/preview/{snap_id}/annotate")
    async def annotate_snapshot(
        snap_id: str,
        request: Request,
        token: Optional[str] = Query(None),
    ):
        """Add a note or image_path to a snapshot."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        body = await request.json()
        note = body.get("note", "")
        image_path = body.get("image_path")

        # Cap note at 500 chars
        if note and len(note) > 500:
            note = note[:500]

        session = app.state.current_session
        target = app.state.active_target
        buf = app.state.snapshot_buffer

        # Search in target-scoped key first, then session
        snap_key = f"{session}:{target}" if target else session
        snapshot = None
        with buf._lock:
            snaps = buf._snapshots.get(snap_key, {})
            if snap_id in snaps:
                snapshot = snaps[snap_id]
            elif target:
                snaps = buf._snapshots.get(session, {})
                if snap_id in snaps:
                    snapshot = snaps[snap_id]

        if not snapshot:
            return JSONResponse({"error": "Snapshot not found"}, status_code=404)

        if note is not None:
            snapshot["note"] = note
        if image_path is not None:
            snapshot["image_path"] = image_path

        app.state.audit_log.log("snapshot_annotate", {"snap_id": snap_id, "note": note[:50] if note else ""})
        return {"success": True, "snap_id": snap_id}

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

    @app.get("/api/health/claude")
    async def check_claude_health(
        pane_id: str = Query(...),
        token: Optional[str] = Query(None),
    ):
        """
        Check if Claude is running in a specific pane.

        Returns:
          - pane_alive: Whether the pane exists
          - shell_pid: PID of the shell in the pane
          - claude_running: Whether claude-code process is found (in pane)
          - claude_pid: PID of claude-code if running
          - pane_title: Current pane title
          - activity_detected: Whether log file was recently modified (within 30s)
          - message: Human-readable status message
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        result = {
            "pane_alive": False,
            "shell_pid": None,
            "claude_running": False,
            "claude_pid": None,
            "pane_title": None,
            "activity_detected": False,
            "message": "Claude not running",
        }

        try:
            # Get pane info: PID and title
            pane_info = subprocess.run(
                ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_pid}|#{pane_title}"],
                capture_output=True, text=True, timeout=5
            )

            if pane_info.returncode != 0:
                return result  # Pane doesn't exist

            output = pane_info.stdout.strip()
            if "|" not in output:
                return result

            parts = output.split("|", 1)
            shell_pid = parts[0]
            pane_title = parts[1] if len(parts) > 1 else ""

            result["pane_alive"] = True
            result["shell_pid"] = int(shell_pid) if shell_pid.isdigit() else None
            result["pane_title"] = pane_title

            # Scan process tree for claude-code in cmdline
            # Use pgrep to find processes with "claude" in command line under the shell's tree
            if result["shell_pid"]:
                # Get all descendant processes of shell_pid
                try:
                    ps_result = subprocess.run(
                        ["ps", "-o", "pid,comm,args", "--ppid", str(result["shell_pid"]), "--no-headers"],
                        capture_output=True, text=True, timeout=5
                    )

                    # Also check direct children and their children
                    pgrep_result = subprocess.run(
                        ["pgrep", "-P", str(result["shell_pid"]), "-a"],
                        capture_output=True, text=True, timeout=5
                    )

                    # Look for claude-code in process output
                    combined_output = ps_result.stdout + "\n" + pgrep_result.stdout
                    for line in combined_output.split("\n"):
                        line_lower = line.lower()
                        if "claude" in line_lower and ("node" in line_lower or "claude" in line.split()[1:2] if len(line.split()) > 1 else False):
                            # Found claude process
                            parts = line.split()
                            if parts and parts[0].isdigit():
                                result["claude_running"] = True
                                result["claude_pid"] = int(parts[0])
                                break

                    # Alternative: check if any child process has "claude" in its cmdline
                    if not result["claude_running"]:
                        proc_check = subprocess.run(
                            ["pgrep", "-f", "claude", "-P", str(result["shell_pid"])],
                            capture_output=True, text=True, timeout=5
                        )
                        if proc_check.returncode == 0 and proc_check.stdout.strip():
                            pids = proc_check.stdout.strip().split("\n")
                            if pids and pids[0].isdigit():
                                result["claude_running"] = True
                                result["claude_pid"] = int(pids[0])

                except Exception as e:
                    logger.warning(f"Error scanning for claude process: {e}")

        except subprocess.TimeoutExpired:
            logger.warning(f"Timeout checking claude health for pane {pane_id}")
        except Exception as e:
            logger.error(f"Error checking claude health: {e}")

        # Check for recent log activity (even if Claude not in pane)
        try:
            repo_path = get_current_repo_path()
            if repo_path:
                project_id = str(repo_path.resolve()).replace("~", "-").replace("/", "-").lstrip("-")
                claude_projects_dir = Path.home() / ".claude" / "projects" / project_id
                if claude_projects_dir.exists():
                    jsonl_files = list(claude_projects_dir.glob("*.jsonl"))
                    if jsonl_files:
                        # Check most recent file modification time
                        most_recent = max(jsonl_files, key=lambda f: f.stat().st_mtime)
                        mtime = most_recent.stat().st_mtime
                        age_seconds = time.time() - mtime
                        if age_seconds < 30:
                            result["activity_detected"] = True
        except Exception as e:
            logger.debug(f"Error checking log activity: {e}")

        # Set human-readable message
        if result["claude_running"]:
            result["message"] = "Claude running in pane"
        elif result["activity_detected"]:
            result["message"] = "Claude not in pane (activity in logs)"
        else:
            result["message"] = "Claude not running"

        return result

    # ===== Phase Detection (Status Strip) =====
    _phase_cache: dict = {"log_path": "", "mtime": 0.0, "size": 0, "result": None}
    _phase_last_activity: dict = {"time": 0.0, "was_active": False}
    _git_head_cache: dict = {"value": "", "ts": 0.0}

    def _get_git_head() -> str:
        """Get short git HEAD hash, cached for 10s."""
        now = time.time()
        if now - _git_head_cache["ts"] < 10 and _git_head_cache["value"]:
            return _git_head_cache["value"]
        try:
            repo_path = get_current_repo_path()
            if not repo_path:
                return ""
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=2,
                cwd=str(repo_path),
            )
            if result.returncode == 0:
                val = result.stdout.strip()
                _git_head_cache.update({"value": val, "ts": now})
                return val
        except Exception:
            pass
        return ""

    def _try_auto_snapshot(session: str, target: str, phase_result: dict):
        """Auto-capture a minimal snapshot from push_monitor (rate-limited by caller)."""
        tool = phase_result.get("tool", "")
        phase = phase_result.get("phase", "")

        # Determine label from tool
        label_map = {
            "Edit": "edit", "Write": "edit", "NotebookEdit": "edit",
            "Bash": "bash",
            "EnterPlanMode": "plan_transition", "ExitPlanMode": "plan_transition",
            "Task": "task",
            "AskUserQuestion": "tool_call",
        }
        label = label_map.get(tool, "tool_call")

        # Find current log file info
        repo_path = get_current_repo_path()
        if not repo_path:
            return
        project_id = str(repo_path.resolve()).replace("~", "-").replace("/", "-")
        cpd = Path.home() / ".claude" / "projects" / project_id
        if not cpd.exists():
            return
        jf = list(cpd.glob("*.jsonl"))
        if not jf:
            return
        lf = max(jf, key=lambda f: f.stat().st_mtime)

        ts = int(time.time() * 1000)
        snap_id = f"snap_{ts}"
        snapshot = {
            "id": snap_id,
            "timestamp": ts,
            "session": session,
            "pane_id": target or "",
            "label": label,
            "log_offset": lf.stat().st_size,
            "log_path": str(lf),
            "git_head": _get_git_head(),
            "terminal_text": "",  # Empty by default, load on demand
            "log_entries": "",    # Empty by default, load on demand
            "note": "",
            "image_path": None,
            "pinned": False,
        }

        # Use existing SnapshotBuffer (keyed by session:pane_id)
        snap_key = f"{session}:{target}" if target else session
        buf = app.state.snapshot_buffer
        with buf._lock:
            if snap_key not in buf._snapshots:
                buf._snapshots[snap_key] = OrderedDict()
            buf._snapshots[snap_key][snap_id] = snapshot
            while len(buf._snapshots[snap_key]) > buf.MAX_SNAPSHOTS:
                evicted = False
                for key in list(buf._snapshots[snap_key].keys()):
                    if not buf._snapshots[snap_key][key].get("pinned"):
                        del buf._snapshots[snap_key][key]
                        evicted = True
                        break
                if not evicted:
                    break

    def _detect_phase(session_name: str, target: str, claude_running: bool) -> dict:
        """
        Detect Claude's current phase by parsing the tail of the JSONL log.
        Uses (log_path, mtime, size) cache key for <5ms cached returns.
        """
        result = {
            "phase": "idle",
            "detail": "",
            "tool": "",
            "session": session_name,
            "pane_id": target or "",
        }

        if not claude_running:
            return result

        # Find the log file
        repo_path = get_current_repo_path()
        if not repo_path:
            return result
        project_id = str(repo_path.resolve()).replace("~", "-").replace("/", "-")
        claude_projects_dir = Path.home() / ".claude" / "projects" / project_id
        if not claude_projects_dir.exists():
            return result
        jsonl_files = list(claude_projects_dir.glob("*.jsonl"))
        if not jsonl_files:
            return result
        log_file = max(jsonl_files, key=lambda f: f.stat().st_mtime)

        # Check cache
        try:
            st = log_file.stat()
            if (_phase_cache["log_path"] == str(log_file)
                    and _phase_cache["mtime"] == st.st_mtime
                    and _phase_cache["size"] == st.st_size
                    and _phase_cache["result"] is not None):
                return _phase_cache["result"]
        except Exception:
            return result

        # Cache miss - parse last 8KB of JSONL
        try:
            file_size = st.st_size
            read_size = min(file_size, 8192)
            with open(log_file, 'rb') as f:
                if file_size > read_size:
                    f.seek(file_size - read_size)
                tail_bytes = f.read(read_size)
            tail_text = tail_bytes.decode('utf-8', errors='replace')

            # Find complete JSON lines (skip partial first line if we seeked)
            lines = tail_text.split('\n')
            if file_size > read_size:
                lines = lines[1:]  # Skip partial first line

            # Parse entries from tail
            entries = []
            for line in reversed(lines):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if len(entries) >= 30:  # Only need recent entries
                    break

            # Check pane_title for signal detection
            if target:
                try:
                    tmux_target = get_tmux_target(session_name, target)
                    title_result = subprocess.run(
                        ["tmux", "display-message", "-t", tmux_target, "-p", "#{pane_title}"],
                        capture_output=True, text=True, timeout=2
                    )
                    if title_result.returncode == 0:
                        pane_title = title_result.stdout.strip()
                        if "Signal Detection Pending" in pane_title:
                            result["phase"] = "waiting"
                            result["detail"] = "Signal Detection Pending"
                            _phase_cache.update({"log_path": str(log_file), "mtime": st.st_mtime,
                                                  "size": st.st_size, "result": result})
                            return result
                except Exception:
                    pass

            # Scan entries (already in reverse chronological order)
            plan_mode = False
            last_tool = None
            last_tool_detail = ""
            active_form = ""

            for entry in entries:
                msg = entry.get("message", {})
                msg_type = entry.get("type", "")

                if msg_type != "assistant":
                    continue

                content = msg.get("content", [])
                if isinstance(content, str):
                    continue
                if not isinstance(content, list):
                    continue

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_use":
                        continue

                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})

                    if tool_name == "AskUserQuestion":
                        result["phase"] = "waiting"
                        questions = tool_input.get("questions", [])
                        if questions:
                            result["detail"] = questions[0].get("question", "Needs input")[:80]
                        else:
                            result["detail"] = "Needs input"
                        result["tool"] = tool_name
                        _phase_cache.update({"log_path": str(log_file), "mtime": st.st_mtime,
                                              "size": st.st_size, "result": result})
                        return result

                    if tool_name == "EnterPlanMode" and not last_tool:
                        plan_mode = True

                    if tool_name == "ExitPlanMode":
                        plan_mode = False

                    if tool_name == "TodoWrite":
                        todos = tool_input.get("todos", [])
                        in_progress = [t for t in todos if t.get("status") == "in_progress"]
                        if in_progress:
                            active_form = in_progress[0].get("activeForm", "")

                    if not last_tool and tool_name:
                        last_tool = tool_name
                        # Extract detail from tool input
                        if tool_name == "Bash":
                            cmd = tool_input.get("command", "")
                            last_tool_detail = cmd[:60]
                        elif tool_name in ("Read", "Edit", "Write", "Glob", "Grep"):
                            path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("pattern", "")
                            last_tool_detail = path.split("/")[-1][:60] if "/" in str(path) else str(path)[:60]
                        elif tool_name == "Task":
                            last_tool_detail = tool_input.get("description", "")[:60]
                        elif tool_name == "EnterPlanMode":
                            last_tool_detail = "Planning..."
                        elif tool_name == "ExitPlanMode":
                            last_tool_detail = "Plan ready"

            # Determine phase from collected data
            if plan_mode:
                result["phase"] = "planning"
                result["detail"] = active_form or "Planning..."
                result["tool"] = "EnterPlanMode"
            elif last_tool == "Task":
                result["phase"] = "running_task"
                result["detail"] = active_form or last_tool_detail or "Running agent..."
                result["tool"] = last_tool
            elif last_tool in ("Edit", "Write", "Bash", "Read", "Glob", "Grep", "TodoWrite",
                               "NotebookEdit", "TaskCreate", "TaskUpdate"):
                result["phase"] = "working"
                result["detail"] = active_form or f"{last_tool}: {last_tool_detail}" if last_tool_detail else active_form or "Working..."
                result["tool"] = last_tool
            elif last_tool:
                result["phase"] = "working"
                result["detail"] = active_form or last_tool_detail or f"Using {last_tool}"
                result["tool"] = last_tool
            else:
                result["phase"] = "idle"
                result["detail"] = ""

        except Exception as e:
            logger.debug(f"Phase detection error: {e}")

        # Update cache
        try:
            _phase_cache.update({"log_path": str(log_file), "mtime": st.st_mtime,
                                  "size": st.st_size, "result": result})
        except Exception:
            pass

        return result

    @app.get("/api/status/phase")
    async def get_status_phase(
        pane_id: str = Query(None),
        token: Optional[str] = Query(None),
    ):
        """Get Claude's current phase for the status strip."""
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        session = app.state.current_session
        target = pane_id or app.state.active_target

        # Check if Claude is running (reuse health check logic)
        claude_running = False
        if target:
            try:
                tmux_target = get_tmux_target(session, target)
                pane_info = subprocess.run(
                    ["tmux", "display-message", "-t", tmux_target, "-p", "#{pane_pid}"],
                    capture_output=True, text=True, timeout=2
                )
                if pane_info.returncode == 0:
                    shell_pid = pane_info.stdout.strip()
                    if shell_pid.isdigit():
                        proc_check = subprocess.run(
                            ["pgrep", "-f", "claude", "-P", shell_pid],
                            capture_output=True, text=True, timeout=2
                        )
                        claude_running = proc_check.returncode == 0 and proc_check.stdout.strip() != ""
            except Exception:
                pass

        result = _detect_phase(session, target, claude_running)
        result["claude_running"] = claude_running
        return result

    @app.post("/api/claude/start")
    async def start_claude_in_pane(
        request: Request,
        pane_id: str = Query(...),
        token: Optional[str] = Query(None),
    ):
        """
        Start Claude in a pane if not already running.

        Returns 409 if Claude is already running.
        Uses the repo's startup_command from config if available.
        """
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        # Check health first
        try:
            pane_info = subprocess.run(
                ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_pid}"],
                capture_output=True, text=True, timeout=5
            )
            if pane_info.returncode != 0:
                return JSONResponse({"error": "Pane not found"}, status_code=404)

            shell_pid = pane_info.stdout.strip()

            # Check if claude is already running
            if shell_pid.isdigit():
                proc_check = subprocess.run(
                    ["pgrep", "-f", "claude", "-P", shell_pid],
                    capture_output=True, text=True, timeout=5
                )
                if proc_check.returncode == 0 and proc_check.stdout.strip():
                    return JSONResponse({
                        "error": "Claude is already running in this pane",
                        "claude_pid": int(proc_check.stdout.strip().split("\n")[0])
                    }, status_code=409)

        except Exception as e:
            logger.error(f"Error checking claude status: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

        # Get startup command from request body or find matching repo
        startup_cmd = "claude"  # Default
        repo_label = None

        try:
            body = await request.json()
            if body.get("startup_command"):
                startup_cmd = body["startup_command"]
            repo_label = body.get("repo_label")
        except Exception:
            pass  # No body or invalid JSON, use default

        # If repo_label provided, look up its startup_command
        if repo_label:
            repo = next((r for r in config.repos if r.label == repo_label), None)
            if repo and repo.startup_command:
                startup_cmd = repo.startup_command

        # Validate startup command
        if "\n" in startup_cmd or "\r" in startup_cmd:
            return JSONResponse({"error": "startup_command cannot contain newlines"}, status_code=400)
        if len(startup_cmd) > 200:
            return JSONResponse({"error": "startup_command exceeds 200 character limit"}, status_code=400)

        try:
            # Send command using literal mode + separate Enter
            subprocess.run(
                ["tmux", "send-keys", "-t", pane_id, "-l", startup_cmd],
                capture_output=True, timeout=5
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", pane_id, "Enter"],
                capture_output=True, timeout=5
            )

            logger.info(f"Started '{startup_cmd}' in pane {pane_id}")
            app.state.audit_log.log("claude_start", {
                "pane_id": pane_id,
                "command": startup_cmd,
                "repo_label": repo_label,
            })

            return {
                "success": True,
                "pane_id": pane_id,
                "command": startup_cmd,
            }

        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "Timeout starting Claude"}, status_code=504)
        except Exception as e:
            logger.error(f"Error starting Claude: {e}")
            return JSONResponse({"error": str(e)}, status_code=500)

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

        # Get snapshots (try target-scoped first, then session)
        target = app.state.active_target
        snap_key = f"{session}:{target}" if target else session
        snapshots = app.state.snapshot_buffer.list_snapshots(snap_key, limit)
        if not snapshots and target:
            snapshots = app.state.snapshot_buffer.list_snapshots(session, limit)
        for snap in snapshots:
            items.append({
                "type": "snapshot",
                "id": snap["id"],
                "label": snap["label"],
                "timestamp": snap["timestamp"],
                "pinned": snap.get("pinned", False),
                "pane_id": snap.get("pane_id", ""),
                "note": snap.get("note", ""),
                "image_path": snap.get("image_path"),
                "git_head": snap.get("git_head", ""),
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
                subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_path,
                               capture_output=True, timeout=10)
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
            subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_path,
                           capture_output=True, timeout=10)

            app.state.audit_log.log("revert_dry_run", {"commit": commit_hash})

            return {
                "success": True,
                "commit": commit_hash,
                "changes": diff.stdout,
                "message": f"Revert \"{commit_hash[:7]}\" would succeed"
            }
        except subprocess.TimeoutExpired:
            # Clean up on timeout
            subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_path,
                           capture_output=True, timeout=10)
            return JSONResponse({"error": "Git command timed out"}, status_code=500)
        except Exception as e:
            subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_path,
                           capture_output=True, timeout=10)
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
        # Use active target pane if set, otherwise default to 0.0
        target = get_tmux_target(session, app.state.active_target)

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
        import time as _time
        _ws_start = _time.time()
        logger.info(f"[TIMING] WebSocket /ws/terminal START")

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
        logger.info(f"[TIMING] WebSocket lock+accept took {_time.time()-_ws_start:.3f}s")

        # Spawn tmux if not already running
        _spawn_start = _time.time()
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
        logger.info(f"[TIMING] spawn_tmux took {_time.time()-_spawn_start:.3f}s")
        master_fd = app.state.master_fd
        output_buffer = app.state.output_buffer

        # Send hello handshake FIRST - client expects this within 2s
        # Must be sent before capture-pane which can be slow
        _hello_start = _time.time()
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
        logger.info(f"[TIMING] hello handshake took {_time.time()-_hello_start:.3f}s")

        # Don't send capture-pane history on initial connect
        # Default mode is "tail" which uses lightweight JSON updates
        # History will be sent as catchup when client switches to "full" mode
        # Just send clear screen to trigger client overlay hide
        await websocket.send_text("\x1b[2J\x1b[H")
        logger.info(f"[TIMING] WebSocket setup TOTAL took {_time.time()-_ws_start:.3f}s")

        # Track PTY death for proper close code
        pty_died = False
        # Track connection closed to prevent send-after-close errors
        connection_closed = False

        # Client output mode: "tail" (default) or "full"
        # In tail mode: don't forward raw PTY bytes, send periodic tail snapshots
        # In full mode: forward raw PTY bytes for full terminal rendering
        client_mode = "tail"
        # Ring buffer for recent output (for tail extraction and mode switch catchup)
        recent_buffer = bytearray()
        RECENT_BUFFER_MAX = 64 * 1024  # 64KB of recent output
        # Tail state
        tail_seq = 0
        TAIL_INTERVAL = 0.2  # Send tail updates every 200ms
        # Shared PTY output batch (cleared on mode switch to prevent stale data)
        pty_batch = bytearray()
        pty_batch_flush_time = time.time()

        # Create tasks for bidirectional I/O
        async def read_from_terminal():
            """Read from terminal and send to WebSocket with batching.

            CRITICAL: PTY is ALWAYS drained regardless of client_mode.
            - In 'full' mode: forward raw bytes to WebSocket (coalesced)
            - In 'tail' mode: skip WebSocket send (tail_sender handles updates)

            Coalescing: accumulate PTY bytes and flush every 25ms or 16KB
            to reduce WS message frequency and client pressure.
            """
            nonlocal pty_died, connection_closed, recent_buffer, pty_batch, pty_batch_flush_time
            loop = asyncio.get_event_loop()
            # Coalescing parameters - balance latency vs throughput
            # Aggressive rate limiting for mobile debugging
            FLUSH_INTERVAL = 0.200  # 200ms = 5 FPS max
            FLUSH_MAX_BYTES = 2048   # 2KB max per message (10KB/s total)

            while app.state.active_websocket == websocket and not connection_closed:
                try:
                    # Non-blocking read with select-like behavior
                    # ALWAYS read - never pause PTY drain
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

                    # Store in recent buffer for tail extraction and mode switch catchup
                    recent_buffer.extend(data)
                    if len(recent_buffer) > RECENT_BUFFER_MAX:
                        # Trim to last RECENT_BUFFER_MAX bytes
                        recent_buffer = recent_buffer[-RECENT_BUFFER_MAX:]

                    # Only forward to WebSocket in 'full' mode
                    if client_mode == "full":
                        # Add to batch (cap size to prevent memory issues)
                        pty_batch.extend(data)
                        if len(pty_batch) > FLUSH_MAX_BYTES * 4:
                            # Drop old data if accumulating too fast
                            # Keep from a UTF-8 safe boundary
                            keep_from = len(pty_batch) - FLUSH_MAX_BYTES
                            # Find start of a valid UTF-8 character
                            while keep_from < len(pty_batch) and (pty_batch[keep_from] & 0xC0) == 0x80:
                                keep_from += 1
                            pty_batch = pty_batch[keep_from:]

                        # ONLY flush on time interval (enforces rate limit for mobile)
                        # This prevents flooding the client even if PTY is very active
                        now = time.time()
                        if (now - pty_batch_flush_time) >= FLUSH_INTERVAL:
                            if app.state.active_websocket == websocket and pty_batch and not connection_closed:
                                # Send at most FLUSH_MAX_BYTES per interval
                                # Use UTF-8 safe boundary to avoid splitting multi-byte chars
                                cut_pos = find_utf8_boundary(pty_batch, FLUSH_MAX_BYTES)
                                send_data = bytes(pty_batch[:cut_pos])
                                pty_batch = pty_batch[cut_pos:]
                                await websocket.send_bytes(send_data)
                                pty_batch_flush_time = now

                except Exception as e:
                    # Ignore send-after-close errors (expected during disconnect)
                    if connection_closed or "after sending" in str(e) or "websocket.close" in str(e):
                        break
                    if app.state.active_websocket == websocket:
                        logger.error(f"Error reading from terminal: {e}")
                    break

            # Flush remaining data (only in full mode)
            if client_mode == "full" and pty_batch and app.state.active_websocket == websocket and not connection_closed:
                try:
                    await websocket.send_bytes(bytes(pty_batch))
                except Exception:
                    pass

        async def write_to_terminal():
            """Read from WebSocket and write to terminal."""
            nonlocal client_mode, pty_batch, pty_batch_flush_time
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
                        app.state.last_ws_input_time = time.time()
                    elif "text" in message:
                        text = message["text"]
                        logger.info(f"Received text message: {text[:100]}")
                        # Handle JSON messages (resize, input)
                        try:
                            data = json.loads(text)
                            if isinstance(data, dict):
                                msg_type = data.get("type")
                                if msg_type == "resize":
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
                                elif msg_type == "input":
                                    input_data = data.get("data")
                                    if input_data:
                                        os.write(master_fd, input_data.encode())
                                        app.state.last_ws_input_time = time.time()
                                elif msg_type == "ping":
                                    # Respond to heartbeat ping with pong
                                    if not connection_closed:
                                        await websocket.send_json({"type": "pong"})
                                elif msg_type == "pong":
                                    # Client responding to server_ping - connection is alive
                                    pass  # No action needed, connection confirmed alive
                                elif msg_type == "text":
                                    # Atomic text send via tmux send-keys (not PTY write)
                                    # This avoids interleaving with PTY output stream
                                    text_data = data.get("text", "")
                                    send_enter = data.get("enter", False)
                                    loop = asyncio.get_event_loop()
                                    session = app.state.current_session
                                    target = app.state.active_target
                                    tmux_t = get_tmux_target(session, target)
                                    if text_data:
                                        try:
                                            await loop.run_in_executor(
                                                None,
                                                lambda: subprocess.run(
                                                    ["tmux", "send-keys", "-t", tmux_t, "-l", text_data],
                                                    timeout=3, check=True,
                                                ),
                                            )
                                        except Exception as e:
                                            logger.warning(f"tmux send-keys failed: {e}")
                                    if send_enter:
                                        try:
                                            await loop.run_in_executor(
                                                None,
                                                lambda: subprocess.run(
                                                    ["tmux", "send-keys", "-t", tmux_t, "Enter"],
                                                    timeout=3, check=True,
                                                ),
                                            )
                                        except Exception as e:
                                            logger.warning(f"tmux send-keys Enter failed: {e}")
                                    app.state.last_ws_input_time = time.time()
                                elif msg_type == "set_mode":
                                    # Client requests output mode change
                                    new_mode = data.get("mode", "tail")
                                    if new_mode in ("tail", "full"):
                                        old_mode = client_mode
                                        client_mode = new_mode
                                        logger.info(f"[MODE] {old_mode} -> {client_mode}")
                                        # When switching to full mode:
                                        # Send capture-pane snapshot as immediate catchup,
                                        # then SIGWINCH for live forwarding.
                                        # The snapshot fixes the race where resize SIGWINCH
                                        # fires while still in tail mode (data lost).
                                        if new_mode == "full" and not connection_closed:
                                            pty_batch.clear()
                                            pty_batch_flush_time = 0
                                            # Send capture-pane snapshot so client has
                                            # current screen content immediately
                                            try:
                                                session = app.state.current_session
                                                target = app.state.active_target
                                                snapshot = await asyncio.get_event_loop().run_in_executor(
                                                    None,
                                                    lambda: subprocess.run(
                                                        ["tmux", "capture-pane", "-p", "-e", "-t",
                                                         get_tmux_target(session, target)],
                                                        capture_output=True, text=True, timeout=2,
                                                    ).stdout or ""
                                                )
                                                if snapshot and not connection_closed:
                                                    # Clear screen + send snapshot for clean render
                                                    await websocket.send_text("\x1b[2J\x1b[H" + snapshot)
                                                    logger.info(f"[MODE] Sent capture-pane snapshot ({len(snapshot)} bytes)")
                                            except Exception as e:
                                                logger.warning(f"[MODE] capture-pane catchup failed: {e}")
                                            # Also send SIGWINCH so live PTY forwarding
                                            # picks up from the correct state
                                            if app.state.child_pid:
                                                try:
                                                    os.kill(app.state.child_pid, signal.SIGWINCH)
                                                except ProcessLookupError:
                                                    pass
                                            logger.info("[MODE] Full mode activated with snapshot catchup")
                                elif msg_type == "term_subscribe":
                                    # Legacy: treat as set_mode full
                                    client_mode = "full"
                                    logger.info("Client subscribed to terminal view (mode=full)")
                                    if not connection_closed:
                                        pty_batch.clear()
                                        pty_batch_flush_time = time.time()
                                elif msg_type == "term_unsubscribe":
                                    # Legacy: treat as set_mode tail
                                    client_mode = "tail"
                                    logger.info("Client unsubscribed from terminal view (mode=tail)")
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

        async def tail_sender():
            """Send periodic tail updates when in tail mode.

            Extracts last ~50 lines from recent_buffer, strips ANSI,
            and sends as JSON for lightweight Log view rendering.
            Also checks for pending permission requests (v2 messages).
            """
            nonlocal tail_seq
            perm_check_counter = 0
            while app.state.active_websocket == websocket and not connection_closed:
                try:
                    await asyncio.sleep(TAIL_INTERVAL)
                    if client_mode == "tail" and recent_buffer and not connection_closed:
                        # Extract last portion and decode
                        try:
                            text = bytes(recent_buffer[-8192:]).decode('utf-8', errors='replace')
                            # Strip ANSI and get last 50 lines
                            plain = strip_ansi(text)
                            lines = plain.split('\n')[-50:]
                            tail_text = '\n'.join(lines)
                            tail_seq += 1
                            await websocket.send_json({
                                "type": "tail",
                                "text": tail_text,
                                "seq": tail_seq
                            })
                        except Exception as e:
                            logger.debug(f"Tail extraction error: {e}")

                    # Check for permission requests every ~1s (5 ticks at 200ms)
                    perm_check_counter += 1
                    if perm_check_counter >= 5 and not connection_closed:
                        perm_check_counter = 0
                        try:
                            detector = app.state.permission_detector
                            session = app.state.current_session
                            target = app.state.active_target
                            if session and detector.log_file:
                                perm = await asyncio.get_event_loop().run_in_executor(
                                    None, detector.check_sync, session, target
                                )
                                if perm:
                                    await send_typed(websocket, "permission_request", perm, level="urgent")
                        except Exception as e:
                            logger.debug(f"Permission check error: {e}")
                except Exception:
                    break

        async def desktop_activity_monitor():
            """Detect desktop keyboard activity in tmux session (1.5s polling)."""
            last_hash = 0
            desktop_active = False
            desktop_since = 0
            while app.state.active_websocket == websocket and not connection_closed:
                await asyncio.sleep(1.5)
                try:
                    session = app.state.current_session
                    target = app.state.active_target
                    if not session:
                        continue
                    tmux_target = get_tmux_target(session, target)
                    def _capture_desktop():
                        r = subprocess.run(
                            ["tmux", "capture-pane", "-t", tmux_target, "-p", "-S", "-5"],
                            capture_output=True, text=True, timeout=1
                        )
                        return r.stdout
                    stdout = await asyncio.get_event_loop().run_in_executor(
                        None, _capture_desktop
                    )
                    current_hash = hash(stdout)
                    if current_hash != last_hash:
                        last_hash = current_hash
                        time_since_ws = time.time() - app.state.last_ws_input_time
                        if time_since_ws > 1.5 and not desktop_active:
                            desktop_active = True
                            desktop_since = time.time()
                            await send_typed(websocket, "device_state",
                                             {"desktop_active": True}, level="info")
                        elif time_since_ws <= 1.5 and desktop_active:
                            desktop_active = False
                            await send_typed(websocket, "device_state",
                                             {"desktop_active": False}, level="info")
                    if desktop_active and (time.time() - desktop_since) > 10:
                        desktop_active = False
                        await send_typed(websocket, "device_state",
                                         {"desktop_active": False}, level="info")
                except Exception:
                    pass

        # Run all tasks concurrently
        read_task = asyncio.create_task(read_from_terminal())
        app.state.read_task = read_task
        write_task = asyncio.create_task(write_to_terminal())
        keepalive_task = asyncio.create_task(server_keepalive())
        tail_task = asyncio.create_task(tail_sender())
        desktop_task = asyncio.create_task(desktop_activity_monitor())

        try:
            await asyncio.gather(read_task, write_task, keepalive_task, tail_task, desktop_task)
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
            tail_task.cancel()
            desktop_task.cancel()
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

    # ===== Push Notifications =====
    PUSH_DIR = Path.home() / ".mobile-terminal"
    PUSH_SUBS_FILE = PUSH_DIR / "push_subs.json"

    def load_push_subscriptions() -> list:
        if PUSH_SUBS_FILE.exists():
            try:
                return json.loads(PUSH_SUBS_FILE.read_text())
            except Exception:
                return []
        return []

    def save_push_subscriptions(subs: list):
        PUSH_DIR.mkdir(parents=True, exist_ok=True)
        PUSH_SUBS_FILE.write_text(json.dumps(subs, indent=2))

    _push_cooldowns: dict = {}

    async def maybe_send_push(title: str, body: str, push_type: str = "info", extra_data: dict = None):
        """Send push only if no active client and cooldown expired."""
        if not config.push_enabled:
            return
        if app.state.active_websocket is not None:
            return
        cooldowns = {"permission": 30, "completed": 300, "crashed": 60}
        min_interval = cooldowns.get(push_type, 30)
        now = time.time()
        if now - _push_cooldowns.get(push_type, 0) < min_interval:
            return
        subs = load_push_subscriptions()
        if not subs:
            return
        vapid_key_path = getattr(app.state, 'vapid_key_path', None)
        if not vapid_key_path:
            return
        try:
            from pywebpush import webpush, WebPushException
        except ImportError:
            return
        payload = {"title": title, "body": body, "type": push_type}
        if extra_data:
            payload.update(extra_data)
        stale = []
        for sub in subs:
            try:
                webpush(sub, json.dumps(payload),
                        vapid_private_key=str(vapid_key_path),
                        vapid_claims={"sub": "mailto:noreply@localhost"})
            except WebPushException as e:
                if "410" in str(e) or "404" in str(e):
                    stale.append(sub.get('endpoint', ''))
            except Exception:
                pass
        if stale:
            subs = [s for s in subs if s.get('endpoint', '') not in stale]
            save_push_subscriptions(subs)
        _push_cooldowns[push_type] = now

    @app.get("/api/push/vapid-key")
    async def get_vapid_key(token: str = Query(None)):
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        pub_key = getattr(app.state, 'vapid_public_key', None)
        if not pub_key:
            return JSONResponse({"error": "Push not configured"}, status_code=503)
        return {"key": pub_key}

    @app.post("/api/push/subscribe")
    async def push_subscribe(request: Request, token: str = Query(None)):
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        sub = await request.json()
        subs = load_push_subscriptions()
        subs = [s for s in subs if s.get('endpoint') != sub.get('endpoint')]
        subs.append(sub)
        save_push_subscriptions(subs)
        return {"ok": True}

    @app.delete("/api/push/subscribe")
    async def push_unsubscribe(request: Request, token: str = Query(None)):
        if not app.state.no_auth and token != app.state.token:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        sub = await request.json()
        subs = load_push_subscriptions()
        subs = [s for s in subs if s.get('endpoint') != sub.get('endpoint')]
        save_push_subscriptions(subs)
        return {"ok": True}

    @app.on_event("startup")
    async def startup():
        """Start input queue and command queue on startup, run auto-setup if enabled."""
        app.state.input_queue.start()
        app.state.command_queue.start()

        # Generate VAPID keys for push notifications
        if config.push_enabled:
            try:
                key_dir = Path.home() / ".mobile-terminal"
                key_dir.mkdir(parents=True, exist_ok=True)
                key_path = key_dir / "vapid_private.pem"
                if not key_path.exists():
                    from py_vapid import Vapid
                    vapid = Vapid()
                    vapid.generate_keys()
                    vapid.save_key(str(key_path))
                    vapid.save_public_key(str(key_dir / "vapid_public.pem"))
                    logger.info("Generated new VAPID keys for push notifications")
                from py_vapid import Vapid
                vapid = Vapid.from_file(str(key_path))
                app.state.vapid_key_path = key_path
                import base64 as _b64
                from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
                raw_pub = vapid.public_key.public_bytes(
                    encoding=Encoding.X962,
                    format=PublicFormat.UncompressedPoint
                )
                app.state.vapid_public_key = _b64.urlsafe_b64encode(raw_pub).rstrip(b'=').decode('ascii')
                logger.info("VAPID keys loaded for push notifications")
            except ImportError:
                logger.info("pywebpush/py_vapid not installed, push notifications disabled")
                app.state.vapid_key_path = None
                app.state.vapid_public_key = None
            except Exception as e:
                logger.warning(f"VAPID key setup failed: {e}")
                app.state.vapid_key_path = None
                app.state.vapid_public_key = None

        # Auto-setup: create/adopt tmux session with configured repo windows
        if config.auto_setup:
            try:
                setup_result = await ensure_tmux_setup(config)
                app.state.setup_result = setup_result
                if setup_result["created_session"]:
                    logger.info(f"auto_setup: created new session '{config.session_name}'")
                if setup_result["errors"]:
                    for err in setup_result["errors"]:
                        logger.warning(f"auto_setup: {err}")
            except Exception as e:
                logger.error(f"auto_setup: failed: {e}")
                app.state.setup_result = {"error": str(e)}

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

        # Start background push monitor
        if config.push_enabled and getattr(app.state, 'vapid_key_path', None):
            async def push_monitor():
                """Check for permission prompts, idle transitions, and crashes."""
                _perm_pending_since = 0
                _last_activity_time = time.time()
                _was_active_phase = False
                _was_claude_running = False
                _crash_candidate_since = 0
                _last_log_mtime = 0.0
                _last_log_size = 0
                _last_snap_time = 0.0  # Rate-limit auto snapshots

                while True:
                    await asyncio.sleep(5)
                    try:
                        session = app.state.current_session
                        target = app.state.active_target
                        if not session:
                            continue

                        pane_target = f"{session}:{target}" if target else session
                        extra = {"session": session, "pane_id": target or ""}

                        # Track log activity
                        try:
                            repo_path = get_current_repo_path()
                            if repo_path:
                                pid = str(repo_path.resolve()).replace("~", "-").replace("/", "-")
                                cpd = Path.home() / ".claude" / "projects" / pid
                                if cpd.exists():
                                    jf = list(cpd.glob("*.jsonl"))
                                    if jf:
                                        lf = max(jf, key=lambda f: f.stat().st_mtime)
                                        st = lf.stat()
                                        if st.st_mtime != _last_log_mtime or st.st_size != _last_log_size:
                                            _last_activity_time = time.time()
                                            _last_log_mtime = st.st_mtime
                                            _last_log_size = st.st_size
                        except Exception:
                            pass

                        # Check if Claude is running
                        claude_running = False
                        if target:
                            try:
                                tmux_t = get_tmux_target(session, target)
                                pi = subprocess.run(
                                    ["tmux", "display-message", "-t", tmux_t, "-p", "#{pane_pid}"],
                                    capture_output=True, text=True, timeout=2
                                )
                                if pi.returncode == 0:
                                    spid = pi.stdout.strip()
                                    if spid.isdigit():
                                        pc = subprocess.run(
                                            ["pgrep", "-f", "claude", "-P", spid],
                                            capture_output=True, text=True, timeout=2
                                        )
                                        claude_running = pc.returncode == 0 and pc.stdout.strip() != ""
                            except Exception:
                                pass

                        # Get current phase (reuse cached result)
                        phase_result = _detect_phase(session, target, claude_running)
                        current_phase = phase_result.get("phase", "idle")
                        is_active = current_phase not in ("idle",)

                        # === Permission push (existing) ===
                        if app.state.active_websocket is None:
                            detector = app.state.permission_detector
                            if detector.log_file:
                                perm = detector.check_sync(session, target)
                                if perm:
                                    if _perm_pending_since == 0:
                                        _perm_pending_since = time.time()
                                    elif time.time() - _perm_pending_since > 10:
                                        await maybe_send_push(
                                            "Claude needs approval",
                                            f"Allow {perm['tool']}: {perm['target'][:80]}?",
                                            "permission",
                                            extra_data=extra,
                                        )
                                else:
                                    _perm_pending_since = 0

                        # === Completed push (idle transition) ===
                        if _was_active_phase and not is_active:
                            idle_duration = time.time() - _last_activity_time
                            if idle_duration > 20:
                                await maybe_send_push(
                                    "Claude finished",
                                    f"Turn complete in {pane_target}. Tap to review.",
                                    "completed",
                                    extra_data=extra,
                                )

                        # === Crashed push (process-tree check with debounce) ===
                        if _was_claude_running and not claude_running:
                            if _crash_candidate_since == 0:
                                _crash_candidate_since = time.time()
                            elif time.time() - _crash_candidate_since > 10:
                                # Confirm no output for 10s
                                if time.time() - _last_activity_time > 10:
                                    await maybe_send_push(
                                        "Claude crashed",
                                        f"Claude stopped in {pane_target}. Tap to respawn.",
                                        "crashed",
                                        extra_data=extra,
                                    )
                                    _crash_candidate_since = 0
                        else:
                            _crash_candidate_since = 0

                        # === Auto-capture snapshots (event-driven, rate-limited) ===
                        now = time.time()
                        if (is_active and _last_log_mtime > 0
                                and now - _last_snap_time > 30):
                            try:
                                _try_auto_snapshot(session, target, phase_result)
                                _last_snap_time = now
                            except Exception:
                                pass

                        _was_active_phase = is_active
                        _was_claude_running = claude_running

                    except Exception as e:
                        logger.debug(f"push_monitor error: {e}")
            app.state.push_monitor_task = asyncio.create_task(push_monitor())

    @app.on_event("shutdown")
    async def shutdown():
        """Cleanup on shutdown."""
        app.state.input_queue.stop()
        app.state.command_queue.stop()

        push_task = getattr(app.state, 'push_monitor_task', None)
        if push_task and not push_task.done():
            push_task.cancel()

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
