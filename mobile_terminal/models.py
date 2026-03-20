"""Data classes and queue persistence for Mobile Terminal Overlay."""

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Directory for persistent command queue (survives server restart)
QUEUE_DIR = Path.home() / ".cache" / "mobile-overlay" / "queue"
# Directory for persistent backlog (project-scoped deferred work items)
BACKLOG_DIR = Path.home() / ".cache" / "mobile-overlay" / "backlog"


def get_queue_file(session: str, pane_id: Optional[str] = None) -> Path:
    """Get the queue file path for a session (optionally scoped by pane)."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    # Sanitize session name for filename
    safe_name = session.replace("/", "_").replace(":", "_")
    if pane_id:
        safe_pane = pane_id.replace("/", "_").replace(":", "_").replace(".", "_")
        return QUEUE_DIR / f"{safe_name}_{safe_pane}.jsonl"
    return QUEUE_DIR / f"{safe_name}.jsonl"


def load_queue_from_disk(session: str, pane_id: Optional[str] = None) -> list:
    """Load queue items from JSONL file."""
    queue_file = get_queue_file(session, pane_id)
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


def save_queue_to_disk(session: str, items: list, pane_id: Optional[str] = None):
    """Save queue items to JSONL file."""
    queue_file = get_queue_file(session, pane_id)
    try:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        with open(queue_file, "w") as f:
            for item in items:
                f.write(json.dumps(item) + "\n")
    except Exception as e:
        logger.error(f"Error saving queue for {session}: {e}")


# ── Backlog persistence ────────────────────────────────────────────────


@dataclass
class BacklogItem:
    """A unit of deferred/possible work, project-scoped."""
    id: str
    summary: str                         # what user sees
    prompt: str                          # what gets queued (instruction text)
    status: str                          # pending | queued | done | dismissed
    source: str                          # human | agent
    created_at: float
    updated_at: float
    project: str                         # repo path
    queue_item_id: Optional[str] = None  # linked QueueItem.id when queued
    origin: str = "manual"               # manual | jsonl_candidate | api_report


@dataclass
class BacklogCandidate:
    """An inferred work item from agent output. In-memory only, not persisted.

    Candidates are disposable suggestions extracted from JSONL tool_use blocks.
    They become durable BacklogItems only when the user explicitly keeps them.
    """
    id: str                 # uuid
    summary: str            # short display text (from subject)
    prompt: str             # full instruction (from description or subject)
    source_tool: str        # "TodoWrite" | "TaskCreate"
    detected_at: float      # time.time()
    session: str            # tmux session name
    pane_id: str            # pane that produced it
    content_hash: str       # md5 of normalized summary for dedup


def _sanitize_project(project: str) -> str:
    """Turn an absolute path into a safe filename stem."""
    return project.replace("/", "_").replace(":", "_").lstrip("_") or "default"


def get_backlog_file(project: str) -> Path:
    """Get JSONL path for a project's backlog."""
    BACKLOG_DIR.mkdir(parents=True, exist_ok=True)
    return BACKLOG_DIR / f"{_sanitize_project(project)}.jsonl"


def load_backlog_from_disk(project: str) -> list:
    """Load backlog items from JSONL. Returns raw dicts."""
    path = get_backlog_file(project)
    items = []
    if path.exists():
        try:
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        items.append(json.loads(line))
                    except Exception as e:
                        logger.warning(f"Skipping invalid backlog line: {e}")
        except Exception as e:
            logger.warning(f"Error reading backlog for {project}: {e}")
    return items


def save_backlog_to_disk(project: str, items: list) -> None:
    """Write backlog items to JSONL (full rewrite)."""
    path = get_backlog_file(project)
    try:
        BACKLOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            for item in items:
                f.write(json.dumps(item) + "\n")
    except Exception as e:
        logger.error(f"Error saving backlog for {project}: {e}")


class BacklogStore:
    """
    Project-scoped backlog with JSONL persistence.

    Lazy-loads items per project on first access. All access is on the
    event loop (no locking needed).
    """

    VALID_STATUSES = {"pending", "queued", "done", "dismissed"}

    def __init__(self):
        self._items: Dict[str, List[BacklogItem]] = {}
        self._loaded: set = set()
        self._app = None

    def set_app(self, app) -> None:
        self._app = app

    def _get_items(self, project: str) -> List[BacklogItem]:
        if project not in self._loaded:
            self._load(project)
            self._loaded.add(project)
        return self._items.setdefault(project, [])

    def _load(self, project: str) -> None:
        raw = load_backlog_from_disk(project)
        items = []
        for data in raw:
            try:
                items.append(BacklogItem(
                    id=data["id"],
                    summary=data["summary"],
                    prompt=data["prompt"],
                    status=data.get("status", "pending"),
                    source=data.get("source", "human"),
                    created_at=data.get("created_at", time.time()),
                    updated_at=data.get("updated_at", time.time()),
                    project=data.get("project", project),
                    queue_item_id=data.get("queue_item_id"),
                    origin=data.get("origin", "manual"),
                ))
            except Exception as e:
                logger.warning(f"Skipping invalid backlog item: {e}")
        self._items[project] = items
        if items:
            logger.info(f"Loaded {len(items)} backlog items for {project}")

    def _save(self, project: str) -> None:
        items = self._items.get(project, [])
        save_backlog_to_disk(project, [asdict(i) for i in items])

    def add(self, project: str, summary: str, prompt: str,
            source: str = "human", origin: str = "manual") -> BacklogItem:
        items = self._get_items(project)
        now = time.time()
        item = BacklogItem(
            id=str(uuid.uuid4()),
            summary=summary,
            prompt=prompt,
            status="pending",
            source=source,
            created_at=now,
            updated_at=now,
            project=project,
            origin=origin,
        )
        items.append(item)
        self._save(project)
        return item

    def list_items(self, project: str) -> List[BacklogItem]:
        return self._get_items(project).copy()

    def update_status(self, project: str, item_id: str,
                      status: str,
                      queue_item_id: Optional[str] = None) -> Optional[BacklogItem]:
        if status not in self.VALID_STATUSES:
            return None
        items = self._get_items(project)
        for item in items:
            if item.id == item_id:
                if item.status == status and queue_item_id is None:
                    return item  # redundant transition, no-op
                item.status = status
                item.updated_at = time.time()
                if queue_item_id is not None:
                    item.queue_item_id = queue_item_id
                self._save(project)
                return item
        return None

    def remove(self, project: str, item_id: str) -> bool:
        items = self._get_items(project)
        before = len(items)
        self._items[project] = [i for i in items if i.id != item_id]
        if len(self._items[project]) < before:
            self._save(project)
            return True
        return False


class CandidateStore:
    """In-memory store for inferred backlog candidates. Not persisted.

    Candidates are disposable suggestions from JSONL interception.
    Dismissed hashes are remembered to prevent re-detection within session.
    """

    def __init__(self):
        self._candidates: Dict[str, List[BacklogCandidate]] = {}  # project → list
        self._dismissed_hashes: set = set()

    def add(self, project: str, candidate: BacklogCandidate) -> Optional[BacklogCandidate]:
        """Add candidate if hash not already present or dismissed. Returns added candidate or None."""
        if candidate.content_hash in self._dismissed_hashes:
            return None
        items = self._candidates.setdefault(project, [])
        if any(c.content_hash == candidate.content_hash for c in items):
            return None
        items.append(candidate)
        return candidate

    def list_candidates(self, project: str) -> List[BacklogCandidate]:
        return list(self._candidates.get(project, []))

    def remove(self, project: str, candidate_id: str) -> Optional[BacklogCandidate]:
        """Remove and return candidate by id."""
        items = self._candidates.get(project, [])
        for i, c in enumerate(items):
            if c.id == candidate_id:
                return items.pop(i)
        return None

    def dismiss(self, project: str, candidate_id: str) -> bool:
        """Remove candidate and remember hash to prevent re-detection."""
        candidate = self.remove(project, candidate_id)
        if candidate:
            self._dismissed_hashes.add(candidate.content_hash)
            return True
        return False

    def is_seen(self, content_hash: str) -> bool:
        """Check if hash is in any project's candidates or dismissed."""
        if content_hash in self._dismissed_hashes:
            return True
        for items in self._candidates.values():
            if any(c.content_hash == content_hash for c in items):
                return True
        return False


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
    """Simple flag to prevent concurrent git operations (race conditions from double-taps).

    Auto-releases after MAX_HOLD_SECONDS to prevent permanent stuck locks.
    Uses a plain boolean — no asyncio.Lock needed since all access is
    single-threaded on the event loop.
    """

    MAX_HOLD_SECONDS = 120.0

    def __init__(self):
        self._held = False
        self._current_op: Optional[str] = None
        self._acquired_at: float = 0.0

    async def acquire(self, operation: str) -> bool:
        """Try to acquire lock for an operation. Returns False if already held."""
        if self._held:
            # Auto-release stale locks
            held = time.time() - self._acquired_at
            if held > self.MAX_HOLD_SECONDS:
                logger.warning("GitOpLock stale (%s held %.0fs), force-releasing",
                               self._current_op, held)
                self._held = False
            else:
                return False

        self._held = True
        self._current_op = operation
        self._acquired_at = time.time()
        return True

    def release(self):
        """Release the lock."""
        self._held = False
        self._current_op = None
        self._acquired_at = 0.0

    @property
    def is_locked(self) -> bool:
        return self._held

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

    def __init__(self, runtime=None):
        self._runtime = runtime
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

    async def send(self, msg_id: str, data: bytes, websocket) -> bool:
        """
        Queue a send request and wait for ACK.
        Returns True if send was acknowledged, False on timeout.
        """
        event = asyncio.Event()
        self._pending_acks[msg_id] = event

        await self._queue.put((msg_id, data, websocket))

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
                msg_id, data, websocket = await asyncio.wait_for(
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

                    # Write to PTY via runtime
                    self._runtime.pty_write(data)
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
    backlog_id: Optional[str] = None


class CommandQueue:
    """
    Per-session command queue with ready-gate, policy enforcement, and pause/resume.

    Commands are held until the terminal is ready (quiet + prompt visible).
    Safe commands auto-send; unsafe commands require manual confirmation.
    """

    QUIET_MS = 400           # Wait for output quiet before sending
    COOLDOWN_MS = 250        # Between sends
    CHECK_INTERVAL_MS = 100  # How often to check ready state

    # Patterns indicating terminal is ready for input (shell prompts only).
    # Excludes interactive prompts like [y/n] and [1-9] — those indicate
    # the agent is waiting for user input, not ready for queued commands.
    PROMPT_PATTERNS = [
        r'❯\s*$',            # Claude Code prompt
        r'\$\s*$',           # Bash prompt
        r'#\s*$',            # Root prompt
        r'>>>\s*$',          # Python REPL
    ]

    # Patterns indicating agent is waiting for user input — NOT ready for queue
    BUSY_PATTERNS = [
        r'\[y/n\]',                          # Yes/no prompt
        r'\[Y/n\]',                          # Default yes prompt
        r'\[y/N\]',                          # Default no prompt
        r'Do you want to proceed\?',         # Confirmation question
        r'Allow\s',                          # Permission prompt
        r'\? \(\d+ options?\)',              # Multi-choice question
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

    @staticmethod
    def _queue_key(session: str, pane_id: Optional[str] = None) -> str:
        """Build internal dict key: 'session:pane_id' when pane_id given, else 'session'."""
        if pane_id:
            return f"{session}:{pane_id}"
        return session

    def _get_queue(self, session: str, pane_id: Optional[str] = None) -> List[QueueItem]:
        """Get or create queue for session+pane, loading from disk if needed."""
        key = self._queue_key(session, pane_id)
        if key not in self._queues:
            self._queues[key] = []
            # Load from disk on first access
            if key not in self._loaded_sessions:
                self._load_from_disk(session, pane_id)
                self._loaded_sessions.add(key)
        return self._queues[key]

    def _load_from_disk(self, session: str, pane_id: Optional[str] = None):
        """Load queue from disk for a session+pane."""
        key = self._queue_key(session, pane_id)
        items_data = load_queue_from_disk(session, pane_id)
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
            self._queues[key] = items
            logger.info(f"Loaded {len(items)} queued items for {key}")

    def _save_to_disk(self, session: str, pane_id: Optional[str] = None):
        """Save queue to disk for a session+pane."""
        key = self._queue_key(session, pane_id)
        queue = self._queues.get(key, [])
        items_data = [asdict(item) for item in queue]
        save_queue_to_disk(session, items_data, pane_id)

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

    def enqueue(self, session: str, text: str, policy: str = "auto", item_id: Optional[str] = None, pane_id: Optional[str] = None, backlog_id: Optional[str] = None) -> tuple:
        """
        Add a command to the queue.

        Returns (item, is_new) tuple. If item_id already exists, returns existing item
        with is_new=False (idempotency).
        """
        queue = self._get_queue(session, pane_id)

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
            backlog_id=backlog_id,
        )
        queue.append(item)
        self._save_to_disk(session, pane_id)
        return (item, True)

    def dequeue(self, session: str, item_id: str, pane_id: Optional[str] = None) -> bool:
        """Remove an item from the queue."""
        queue = self._get_queue(session, pane_id)
        for i, item in enumerate(queue):
            if item.id == item_id:
                queue.pop(i)
                self._save_to_disk(session, pane_id)
                return True
        return False

    def reorder(self, session: str, item_id: str, new_index: int, pane_id: Optional[str] = None) -> bool:
        """Move an item to a new position."""
        queue = self._get_queue(session, pane_id)
        for i, item in enumerate(queue):
            if item.id == item_id:
                queue.pop(i)
                new_index = max(0, min(new_index, len(queue)))
                queue.insert(new_index, item)
                self._save_to_disk(session, pane_id)
                return True
        return False

    def list_items(self, session: str, pane_id: Optional[str] = None) -> List[QueueItem]:
        """Get all items in the queue."""
        return self._get_queue(session, pane_id).copy()

    def pause(self, session: str, pane_id: Optional[str] = None) -> None:
        """Pause queue processing for a session+pane."""
        key = self._queue_key(session, pane_id)
        self._paused[key] = True

    def resume(self, session: str, pane_id: Optional[str] = None) -> None:
        """Resume queue processing for a session+pane."""
        key = self._queue_key(session, pane_id)
        self._paused[key] = False

    def is_paused(self, session: str, pane_id: Optional[str] = None) -> bool:
        """Check if queue is paused."""
        key = self._queue_key(session, pane_id)
        return self._paused.get(key, False)

    def flush(self, session: str, pane_id: Optional[str] = None) -> int:
        """Clear all queued items. Returns count cleared."""
        queue = self._get_queue(session, pane_id)
        count = len(queue)
        queue.clear()
        self._save_to_disk(session, pane_id)
        return count

    def get_next_unsafe(self, session: str, pane_id: Optional[str] = None) -> Optional[QueueItem]:
        """Get the next unsafe item waiting for manual send."""
        queue = self._get_queue(session, pane_id)
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
        from mobile_terminal.helpers import run_subprocess

        if not self._app:
            return False

        # Check quiet period
        input_queue = self._app.state.input_queue
        since_output = (time.time() - input_queue._last_output_ts) * 1000
        if since_output < self.QUIET_MS:
            return False

        # Check for prompt via tmux capture-pane
        try:
            result = await run_subprocess(
                ["tmux", "capture-pane", "-t", session, "-p", "-S", "-5"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode != 0:
                return False

            content = result.stdout

            # Check if agent is waiting for user input — NOT ready
            for pattern in self.BUSY_PATTERNS:
                if re.search(pattern, content, re.MULTILINE):
                    return False

            # Check if any prompt pattern matches
            for pattern in self.PROMPT_PATTERNS:
                if re.search(pattern, content, re.MULTILINE):
                    return True

            # Check pane title for Claude Code waiting state
            title_result = await run_subprocess(
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

    async def _send_item(self, session: str, item: QueueItem, pane_id: Optional[str] = None) -> bool:
        """Send a single item to the terminal."""
        if not self._app:
            return False

        item.status = "pending"

        try:
            runtime = self._app.state.runtime
            websocket = self._app.state.active_client

            if not runtime.has_fd:
                item.status = "failed"
                item.error = "No PTY available"
                return False

            # Send via InputQueue for proper serialization
            success = await self._app.state.input_queue.send(
                msg_id=item.id,
                data=(item.text + '\r').encode('utf-8'),
                websocket=websocket,
            )

            if success:
                item.status = "sent"
                item.sent_at = time.time()
                self._save_to_disk(session, pane_id)  # Persist sent status

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
                self._save_to_disk(session, pane_id)  # Persist failed status
                return False

        except Exception as e:
            item.status = "failed"
            item.error = str(e)
            self._save_to_disk(session, pane_id)  # Persist failed status
            return False

    async def send_next_unsafe(self, session: str, item_id: Optional[str] = None, pane_id: Optional[str] = None) -> Optional[QueueItem]:
        """Manually send the next unsafe item (or specific item)."""
        queue = self._get_queue(session, pane_id)

        # Find the item
        item = None
        if item_id:
            for i in queue:
                if i.id == item_id and i.status == "queued":
                    item = i
                    break
        else:
            item = self.get_next_unsafe(session, pane_id)

        if not item:
            return None

        # Wait for ready and send
        for _ in range(20):  # Try for 2 seconds
            if await self._check_ready(session):
                await self._send_item(session, item, pane_id)
                return item
            await asyncio.sleep(0.1)

        # Timeout waiting for ready, send anyway
        await self._send_item(session, item, pane_id)
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

            # Use active_target as pane_id for per-pane queue scoping
            pane_id = getattr(self._app.state, 'active_target', None)

            # Process only current session+pane
            if self.is_paused(current_session, pane_id):
                continue

            queue = self._get_queue(current_session, pane_id)
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
            await self._send_item(current_session, item, pane_id)

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
