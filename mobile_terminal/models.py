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


def _safe_session(session: str) -> str:
    return session.replace("/", "_").replace(":", "_")


def repo_key_for_cwd(session: str, cwd: str) -> str:
    """Derive a stable scope key for a session+repo. The path is
    sanitized the same way the backlog already does (every non-alnum
    becomes underscore), so the same repo always lands on the same
    file regardless of which pane index it was bound to at any moment.
    """
    return f"{_safe_session(session)}__{_sanitize_project(cwd)}"


def get_queue_file(session: str, pane_id: Optional[str] = None,
                   repo_key: Optional[str] = None) -> Path:
    """Path to the queue jsonl for a scope.

    Scope resolution order (most-specific first):
      1. ``repo_key`` if provided — the new stable per-repo keying
      2. ``pane_id`` — legacy ephemeral per-pane keying (kept for
         backwards-compat reads + lazy migration)
      3. session-only — last fallback for unscoped queues
    """
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    if repo_key:
        return QUEUE_DIR / f"{repo_key}.jsonl"
    safe_name = _safe_session(session)
    if pane_id:
        safe_pane = pane_id.replace("/", "_").replace(":", "_").replace(".", "_")
        return QUEUE_DIR / f"{safe_name}_{safe_pane}.jsonl"
    return QUEUE_DIR / f"{safe_name}.jsonl"


def load_queue_from_disk(session: str, pane_id: Optional[str] = None,
                         repo_key: Optional[str] = None) -> list:
    """Load queue items from JSONL file."""
    queue_file = get_queue_file(session, pane_id, repo_key)
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


def save_queue_to_disk(session: str, items: list, pane_id: Optional[str] = None,
                       repo_key: Optional[str] = None):
    """Save queue items to JSONL file."""
    queue_file = get_queue_file(session, pane_id, repo_key)
    try:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        with open(queue_file, "w") as f:
            for item in items:
                f.write(json.dumps(item) + "\n")
    except Exception as e:
        logger.error(f"Error saving queue for {session}: {e}")


def get_sent_ids_file(session: str, pane_id: Optional[str] = None,
                      repo_key: Optional[str] = None) -> Path:
    """Sidecar file holding recently-sent item ids for replay protection.

    Lives next to the queue JSONL so it shares the same mkdir/dir scheme,
    but in its own ``.sent.jsonl`` to keep the queue file untouched by
    sent-id bookkeeping (a corrupt sent-ids file must never block queue
    operations).
    """
    base = get_queue_file(session, pane_id, repo_key)
    return base.with_suffix(".sent.jsonl")


def load_sent_ids_from_disk(session: str, pane_id: Optional[str] = None,
                            repo_key: Optional[str] = None) -> list:
    """Load recently-sent id snapshots from disk.

    Returns a list of {id, sent_at, item: <full QueueItem dict>} entries.
    Caller is responsible for TTL filtering — this function reads as-is
    so the same file can be inspected for debugging.
    """
    f = get_sent_ids_file(session, pane_id, repo_key)
    out = []
    if f.exists():
        try:
            with open(f, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
        except Exception as e:
            logger.warning(f"Error loading sent-ids for {session}: {e}")
    return out


def save_sent_ids_to_disk(session: str, entries: list, pane_id: Optional[str] = None,
                          repo_key: Optional[str] = None):
    """Write recently-sent id snapshots to disk.

    Each entry is a dict {id, sent_at, item}. Caller passes an already-
    pruned list — this function writes verbatim.
    """
    f = get_sent_ids_file(session, pane_id, repo_key)
    try:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        with open(f, "w") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.error(f"Error saving sent-ids for {session}: {e}")


def get_tomb_file(session: str, pane_id: Optional[str] = None,
                  repo_key: Optional[str] = None) -> Path:
    """Sidecar file holding tombstoned (removed) item ids.

    Stops cross-device localStorage replay: when one client deletes
    item X, the server records the tombstone so a different client
    that still has X locally can't resurrect it via reconcileQueue.
    """
    base = get_queue_file(session, pane_id, repo_key)
    return base.with_suffix(".tomb.jsonl")


def load_tombs_from_disk(session: str, pane_id: Optional[str] = None,
                         repo_key: Optional[str] = None) -> dict:
    """Returns {id: removed_at} dict. Caller TTL-filters."""
    f = get_tomb_file(session, pane_id, repo_key)
    out = {}
    if f.exists():
        try:
            with open(f, "r") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        entry = json.loads(line)
                        out[entry["id"]] = entry["removed_at"]
        except Exception as e:
            logger.warning(f"Error loading tombstones for {session}: {e}")
    return out


def migrate_pane_to_repo(session: str, pane_id: str, repo_key: str) -> dict:
    """Atomically rename pane-keyed queue files to repo-keyed names.

    For each of the three sidecar files (.jsonl, .sent.jsonl,
    .tomb.jsonl): if the pane-keyed file exists AND the repo-keyed
    one doesn't, Path.replace() the old to new. Atomic per file on
    Linux (rename(2) is atomic across same filesystem). Idempotent —
    re-running after a successful migration is a no-op.

    Returns a dict {moved: [...], skipped: [...], errors: [...]}.
    """
    out = {"moved": [], "skipped": [], "errors": []}
    for suffix in ("jsonl", "sent.jsonl", "tomb.jsonl"):
        if suffix == "jsonl":
            old = get_queue_file(session, pane_id=pane_id)
            new = get_queue_file(session, repo_key=repo_key)
        elif suffix == "sent.jsonl":
            old = get_sent_ids_file(session, pane_id=pane_id)
            new = get_sent_ids_file(session, repo_key=repo_key)
        else:
            old = get_tomb_file(session, pane_id=pane_id)
            new = get_tomb_file(session, repo_key=repo_key)
        if not old.exists():
            out["skipped"].append(suffix)
            continue
        if new.exists():
            # Both exist — leave alone, the new file is canonical.
            # (Old file lingers as orphan; could be cleaned up later.)
            out["skipped"].append(suffix)
            continue
        try:
            old.replace(new)
            out["moved"].append(f"{old.name} -> {new.name}")
        except Exception as e:
            out["errors"].append(f"{suffix}: {e}")
    if out["moved"]:
        logger.info(f"queue migration {pane_id} -> {repo_key}: {out['moved']}")
    return out


def save_tombs_to_disk(session: str, tombs: dict, pane_id: Optional[str] = None,
                       repo_key: Optional[str] = None):
    """Write tombstones {id: removed_at} verbatim. Caller has already
    pruned past-TTL entries."""
    f = get_tomb_file(session, pane_id, repo_key)
    try:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        with open(f, "w") as fh:
            for tid, ts in tombs.items():
                fh.write(json.dumps({"id": tid, "removed_at": ts}) + "\n")
    except Exception as e:
        logger.error(f"Error saving tombstones for {session}: {e}")


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

    A per-project cap (MAX_PER_PROJECT) prevents runaway growth if the
    detector ever picks up a noisy tool — new adds are silently dropped
    once the cap is reached. The user must dismiss or keep existing
    candidates before fresh ones can appear.
    """

    MAX_PER_PROJECT = 30

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
        if len(items) >= self.MAX_PER_PROJECT:
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


# ── Permission policy data ─────────────────────────────────────────────


@dataclass
class PermissionRequest:
    """Normalized permission request for policy evaluation."""
    tool: str               # Bash, Edit, Write, Read, Glob, Grep, ...
    target: str             # raw target from detector (command or path)
    command: Optional[str]  # extracted command (Bash only)
    path: Optional[str]     # extracted path (file ops only)
    repo: str               # current repo path
    risk: str               # high | medium | low
    perm_id: str            # dedup id from detector


@dataclass
class PermissionRule:
    """A single permission policy rule."""
    id: str
    tool: str               # Bash | Edit | Write | Read | * (wildcard)
    matcher_type: str       # command | path | tool_only
    matcher: str            # command prefix, path glob, or empty
    scope: str              # global | repo | session
    scope_value: Optional[str]  # repo path (for repo scope)
    action: str             # allow | prompt | deny
    created_at: float
    created_from: str       # banner | menu | default
    note: Optional[str] = None


@dataclass
class PermissionDecision:
    """Structured result of policy evaluation."""
    action: str             # allow | prompt | deny
    reason: str             # default_rule | repo_rule | session_rule | hard_guard | mode_manual | no_match
    rule_id: Optional[str]  # matched rule id (None for hard guard / mode)
    risk: str               # from PermissionRequest


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
    # Opt-in: only items the user has explicitly marked are eligible
    # for the processor's auto-drain. Default False so accidental
    # enqueues never fire on their own. Manual Send / Run buttons
    # ignore this flag — those are explicit user actions.
    auto_eligible: bool = False


class CommandQueue:
    """
    Per-session command queue with ready-gate, policy enforcement, and pause/resume.

    Commands are held until the terminal is ready (quiet + prompt visible).
    Safe commands auto-send; unsafe commands require manual confirmation.
    """

    QUIET_MS = 400           # Wait for output quiet before sending
    COOLDOWN_MS = 250        # Between sends
    CHECK_INTERVAL_MS = 100  # How often to check ready state

    # Patterns indicating terminal is ready for input (shell/REPL prompts).
    # Excludes interactive prompts like [y/n] and [1-9] — those indicate
    # the agent is waiting for user input, not ready for queued commands.
    #
    # Goal: match the broadest reasonable set of "your turn now" indicators
    # so the queue drains for users on zsh/fish/node/etc., not just bash +
    # Claude Code. ``BUSY_PATTERNS`` below is the safety override.
    PROMPT_PATTERNS = [
        r'❯\s*$',            # Claude Code, starship, fish
        r'\$\s*$',           # Bash, zsh, sh
        r'#\s*$',            # Root
        r'>>>\s*$',          # Python REPL
        r'\.\.\.\s*$',       # Python continuation
        r'>\s*$',            # Node, fish, generic single-char prompt
        r'▶\s*$',            # alt prompt char
        r'»\s*$',            # alt prompt char
        r'➜\s*$',            # oh-my-zsh
        r'λ\s*$',            # haskell-ish, some custom
    ]

    # Patterns indicating agent is waiting for user input — NOT ready for queue
    BUSY_PATTERNS = [
        r'\[y/n\]',                          # Yes/no prompt
        r'\[Y/n\]',                          # Default yes prompt
        r'\[y/N\]',                          # Default no prompt
        r'Do you want to proceed\?',         # Confirmation question
        r'Allow\s',                          # Permission prompt
        r'\? \(\d+ options?\)',              # Multi-choice question
        # Claude Code permission-selector lines — present even when the
        # "Do you want to proceed?" line has scrolled out of the capture
        # window. Matches "❯ 1. Yes", "  2. Yes, and …", etc.
        r'^\s*[❯>]\s*\d+\.\s+',
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

    # How long to remember the ids of items that were sent. Replay
    # protection for the case where the WS dropped between server-side
    # send and client-side queue_sent receipt — without this, the
    # client's reconcileQueue on reconnect re-enqueues the item by id
    # and the agent re-executes a prompt the user thought was done.
    #
    # Bumped 600s → 86400s (24h) to cover cross-device replay: when
    # device A sends an item but device B is offline, B's localStorage
    # still has the item as 'queued'. When B comes online >10 min
    # later, the old short TTL had let the cache expire and the item
    # got re-enqueued (and re-executed) on B's reconcile. 24h matches
    # the TOMBSTONE_TTL_SECONDS for X-deleted items — same problem
    # shape, same retention window.
    SENT_ID_TTL_SECONDS = 86400

    # How long sent items stay in the visible queue (the "Previous"
    # section on the client). After this they're dropped from
    # _queues[key] and the queue file on disk. Independent from
    # SENT_ID_TTL_SECONDS — the replay-protection cache (also TTL'd)
    # remains the source of truth for "did we already send id X" long
    # after the visible item has scrolled off. 5 minutes matches typical
    # "did I send that one?" recheck behavior; otherwise the queue file
    # grows indefinitely and every reconcile re-renders all history.
    SENT_VISIBLE_TTL_SECONDS = 300

    # Tombstone TTL for explicitly-removed item ids. Generous because
    # mobile clients can stay backgrounded for a long time before
    # reconnecting and trying to re-upload their stale localStorage.
    # 24h covers normal phone-in-pocket gaps without unbounded growth.
    TOMBSTONE_TTL_SECONDS = 86400

    def __init__(self):
        self._queues: Dict[str, List[QueueItem]] = {}
        self._paused: Dict[str, bool] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._processor_task = None
        self._app = None  # Set during startup
        self._loaded_sessions: set = set()  # Track which sessions loaded from disk
        # Recently-sent item ids (per queue key) with the historical
        # QueueItem snapshot. Lets us tell a duplicate enqueue ("the
        # client missed our queue_sent broadcast and is re-asking")
        # apart from a genuinely new item. Snapshot returned to the
        # client with is_new=False so the client can update its local
        # state to 'sent' without us re-running anything.
        self._recently_sent: Dict[str, Dict[str, tuple]] = {}  # key → {id → (item, ts)}
        # Tombstones: ids that were explicitly removed. Used to reject
        # reconcileQueue-driven resurrection from another device's
        # stale localStorage. Loaded from disk on first access.
        self._tombstones: Dict[str, Dict[str, float]] = {}  # key → {id → removed_at}
        # Wakeup signal for the processor loop. Set by enqueue() so the
        # processor doesn't have to wait up to its idle timeout to notice
        # that there's work to do. Created lazily to avoid binding to a
        # specific event loop at __init__ time (CommandQueue is constructed
        # before uvicorn starts the loop).
        self._wakeup_event: Optional[asyncio.Event] = None

    def set_app(self, app):
        """Set the FastAPI app reference for accessing state."""
        self._app = app

    def _wake(self) -> None:
        """Signal the processor loop to run a pass immediately."""
        if self._wakeup_event is not None:
            try:
                self._wakeup_event.set()
            except Exception:
                # Different event loop or shutting down — silent ignore is
                # fine because the processor's polling fallback will catch
                # the work on its next tick.
                pass

    @staticmethod
    def _parse_key(key: str) -> tuple:
        """Inverse of _queue_key. Returns (session, scope) where scope
        is either a pane_id (legacy `session:pane_id` form) or a
        repo-keyed string (new `session__sanitized_cwd` form). The
        caller usually only cares about the session for filtering
        in _process_loop."""
        if "__" in key:
            session, _ = key.split("__", 1)
            return (session, None)  # repo-keyed; pane unknown without lookup
        if ":" in key:
            session, pane_id = key.split(":", 1)
            return (session, pane_id)
        return (key, None)

    def _resolve_repo_key(self, session: str, pane_id: Optional[str]) -> Optional[str]:
        """Resolve (session, pane_id) → repo_key by looking up the pane's
        cwd. Returns None if cwd resolution fails (caller should fall
        back to the legacy pane-id key). Cached via helpers'
        _PANE_CWD_CACHE so repeated calls are cheap."""
        if not pane_id:
            return None
        from mobile_terminal.helpers import _get_pane_cwd_sync
        cwd = _get_pane_cwd_sync(session, pane_id)
        if not cwd:
            return None
        return repo_key_for_cwd(session, cwd)

    def _queue_key_resolved(self, session: str, pane_id: Optional[str] = None) -> str:
        """Like _queue_key, but tries to resolve to a repo-keyed scope
        first. Used by all the storage-touching public methods so the
        queue follows the cwd, not the ephemeral pane index. Falls
        back to the legacy pane-keyed form when cwd is unresolvable."""
        repo_key = self._resolve_repo_key(session, pane_id)
        if repo_key:
            return repo_key
        return self._queue_key(session, pane_id)

    @staticmethod
    def _queue_key(session: str, pane_id: Optional[str] = None) -> str:
        """Build internal dict key: 'session:pane_id' when pane_id given, else 'session'."""
        if pane_id:
            return f"{session}:{pane_id}"
        return session

    def _get_queue(self, session: str, pane_id: Optional[str] = None) -> List[QueueItem]:
        """Get or create queue for session+pane, loading from disk if
        needed. Pivots to repo-keyed scope if pane's cwd is resolvable;
        triggers a one-time pane→repo file migration on first access
        so existing pane-keyed jsonl files move over without losing data.
        """
        repo_key = self._resolve_repo_key(session, pane_id)
        key = repo_key or self._queue_key_resolved(session, pane_id)
        if key not in self._queues:
            self._queues[key] = []
            # First access — migrate legacy pane-keyed file to the new
            # repo-keyed name (no-op if already migrated, or if cwd
            # couldn't be resolved). Then load.
            if key not in self._loaded_sessions:
                if repo_key and pane_id:
                    try:
                        migrate_pane_to_repo(session, pane_id, repo_key)
                    except Exception as e:
                        logger.warning(f"queue migrate failed for {pane_id} -> {repo_key}: {e}")
                self._load_from_disk(session, pane_id, repo_key=repo_key)
                self._loaded_sessions.add(key)
        return self._queues[key]

    def _load_from_disk(self, session: str, pane_id: Optional[str] = None,
                        repo_key: Optional[str] = None):
        """Load queue + recently-sent ids from disk for a session+pane.

        Sent ids survive the restart so reconcileQueue from a client
        that missed the queue_sent broadcast can still be replay-blocked.
        Without disk persistence the in-memory _recently_sent dict is
        wiped on every restart, which is exactly when the bug bites
        (server restart between drain and client reconnect).
        """
        # Internal dict-key matches the storage scope: repo_key when
        # the pane resolved to one, otherwise legacy pane-keyed.
        key = repo_key or self._queue_key_resolved(session, pane_id)
        items_data = load_queue_from_disk(session, pane_id, repo_key=repo_key)
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
                    auto_eligible=bool(data.get("auto_eligible", False)),
                )
                # Only load items that are still queued (not sent/failed)
                if item.status in ("queued", "pending"):
                    items.append(item)
            except Exception as e:
                logger.warning(f"Skipping invalid queue item: {e}")
        if items:
            self._queues[key] = items
            logger.info(f"Loaded {len(items)} queued items for {key}")

        # Restore the recently-sent cache, applying TTL on read so
        # entries from a long-stopped server don't leak in.
        sent_data = load_sent_ids_from_disk(session, pane_id, repo_key=repo_key)
        if sent_data:
            now = time.time()
            cache = {}
            for entry in sent_data:
                try:
                    sid = entry["id"]
                    sent_at = float(entry.get("sent_at", 0))
                    if now - sent_at > self.SENT_ID_TTL_SECONDS:
                        continue  # past TTL, skip
                    item_dict = entry.get("item") or {}
                    item = QueueItem(
                        id=item_dict.get("id", sid),
                        text=item_dict.get("text", ""),
                        policy=item_dict.get("policy", "safe"),
                        status=item_dict.get("status", "sent"),
                        created_at=item_dict.get("created_at", sent_at),
                        sent_at=sent_at,
                        error=item_dict.get("error"),
                        backlog_id=item_dict.get("backlog_id"),
                    )
                    cache[sid] = (item, sent_at)
                except Exception as e:
                    logger.warning(f"Skipping invalid sent-id entry: {e}")
            if cache:
                self._recently_sent[key] = cache
                logger.info(f"Loaded {len(cache)} replay-protect entries for {key}")

        # Restore tombstones, applying TTL on read.
        tomb_data = load_tombs_from_disk(session, pane_id, repo_key=repo_key)
        if tomb_data:
            now = time.time()
            tombs = {tid: ts for tid, ts in tomb_data.items()
                     if now - ts <= self.TOMBSTONE_TTL_SECONDS}
            if tombs:
                self._tombstones[key] = tombs
                logger.info(f"Loaded {len(tombs)} tombstones for {key}")

    def _save_to_disk(self, session: str, pane_id: Optional[str] = None):
        """Save queue + recently-sent ids to disk for a session+pane.

        The two files share the same lifecycle: every queue mutation
        rewrites both. Cheap (small files, written rarely), and keeps
        the on-disk state self-consistent after any restart.
        """
        repo_key = self._resolve_repo_key(session, pane_id)
        key = repo_key or self._queue_key_resolved(session, pane_id)
        queue = self._queues.get(key, [])
        items_data = [asdict(item) for item in queue]

        # Diagnostic: catch any save that drops previously-persisted
        # ids without going through dequeue/mark_sent. Narrows down
        # "item vanished from disk without a trace" bugs by logging
        # exactly which save path shrank the queue and which ids were
        # lost. Compares the in-memory queue we're about to write
        # against whatever's currently on disk.
        try:
            prev_on_disk = {d.get("id") for d in load_queue_from_disk(session, pane_id, repo_key=repo_key)}
            new_ids = {d.get("id") for d in items_data}
            disappeared = prev_on_disk - new_ids
            if disappeared:
                import inspect
                caller = inspect.stack()[1].function if len(inspect.stack()) > 1 else "?"
                logger.warning(
                    f"[QUEUE-DIAG] save dropped {len(disappeared)} id(s) from "
                    f"{key} (caller={caller}): {[d[:8] for d in disappeared]}"
                )
        except Exception as e:
            logger.debug(f"queue diag check failed: {e}")

        save_queue_to_disk(session, items_data, pane_id)

        # Persist the recently-sent cache too. Prune past-TTL entries
        # before writing so the file stays bounded over time.
        sent_cache = self._recently_sent.get(key, {})
        if sent_cache or get_sent_ids_file(session, pane_id, repo_key=repo_key).exists():
            now = time.time()
            entries = []
            for sid, (item, sent_at) in sent_cache.items():
                if now - sent_at > self.SENT_ID_TTL_SECONDS:
                    continue
                entries.append({
                    "id": sid,
                    "sent_at": sent_at,
                    "item": asdict(item),
                })
            save_sent_ids_to_disk(session, entries, pane_id, repo_key=repo_key)

        # Persist tombstones, prune past-TTL entries.
        tomb_cache = self._tombstones.get(key, {})
        if tomb_cache or get_tomb_file(session, pane_id, repo_key=repo_key).exists():
            now = time.time()
            kept = {tid: ts for tid, ts in tomb_cache.items()
                    if now - ts <= self.TOMBSTONE_TTL_SECONDS}
            self._tombstones[key] = kept
            save_tombs_to_disk(session, kept, pane_id, repo_key=repo_key)

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

        Replay protection: if the id was sent within the last
        SENT_ID_TTL_SECONDS but the client is asking again (typically
        because the queue_sent WS broadcast was missed during a
        reconnect), return the historical sent item with is_new=False
        instead of re-creating and re-running it. Without this guard,
        reconcileQueue on a flaky-network reconnect would re-execute
        the prompt the user thought was done.
        """
        queue = self._get_queue(session, pane_id)
        key = self._queue_key_resolved(session, pane_id)

        # Idempotency check: if ID provided and exists, return existing
        if item_id:
            for existing in queue:
                if existing.id == item_id:
                    return (existing, False)  # Already exists
            # Tombstone check: this id was explicitly removed within
            # TTL. Returning a synthetic "removed" snapshot tells the
            # client to drop it from localStorage rather than keep
            # re-uploading it on every reconcile.
            tombs_for_key = self._tombstones.get(key, {})
            if tombs_for_key:
                now = time.time()
                expired = [
                    tid for tid, ts in tombs_for_key.items()
                    if now - ts > self.TOMBSTONE_TTL_SECONDS
                ]
                for tid in expired:
                    del tombs_for_key[tid]
                if item_id in tombs_for_key:
                    logger.info(
                        f"Tombstone block: enqueue with id={item_id[:8]}.. "
                        f"was removed at ts={tombs_for_key[item_id]:.0f}"
                    )
                    removed_at = tombs_for_key[item_id]
                    removed = QueueItem(
                        id=item_id, text=text, policy="safe",
                        status="removed", created_at=removed_at,
                    )
                    return (removed, False)
            # Replay-protection check: was this id recently sent? Prune
            # expired entries lazily on every check so the dict stays
            # bounded even if no fresh sends arrive.
            sent_for_key = self._recently_sent.get(key, {})
            if sent_for_key:
                now = time.time()
                expired = [
                    sid for sid, (_, ts) in sent_for_key.items()
                    if now - ts > self.SENT_ID_TTL_SECONDS
                ]
                for sid in expired:
                    del sent_for_key[sid]
                if item_id in sent_for_key:
                    historical, _ = sent_for_key[item_id]
                    logger.info(
                        f"Replay-protect: enqueue with id={item_id[:8]}.. "
                        f"matches recently-sent item, returning sent snapshot "
                        f"(no re-execution)"
                    )
                    return (historical, False)

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
        # Kick the processor so a freshly-queued safe item doesn't have to
        # wait for the next idle poll to be drained.
        self._wake()
        return (item, True)

    def dequeue(self, session: str, item_id: str, pane_id: Optional[str] = None) -> bool:
        """Remove an item from the queue. Records a tombstone so other
        clients with a stale localStorage copy can't resurrect the id
        via reconcileQueue."""
        queue = self._get_queue(session, pane_id)
        for i, item in enumerate(queue):
            if item.id == item_id:
                queue.pop(i)
                key = self._queue_key_resolved(session, pane_id)
                self._tombstones.setdefault(key, {})[item_id] = time.time()
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
        """Get all items in the queue. Prunes expired sent items first
        so the visible queue stays bounded even when a long-idle client
        reconnects."""
        if self._prune_old_sent(session, pane_id):
            self._save_to_disk(session, pane_id)
        return self._get_queue(session, pane_id).copy()

    def set_auto_eligible(self, session: str, item_id: str, value: bool, pane_id: Optional[str] = None) -> Optional[QueueItem]:
        """Toggle the auto-drain opt-in flag on an item. Returns the
        updated item, or None if not found / not in 'queued' status
        (sent items can't be flagged — they're done)."""
        queue = self._get_queue(session, pane_id)
        for item in queue:
            if item.id != item_id:
                continue
            if item.status != "queued":
                return None
            item.auto_eligible = bool(value)
            self._save_to_disk(session, pane_id)
            # Wake processor — flipping to True means the next idle
            # cycle should consider this item immediately.
            if value:
                self._wake()
            return item
        return None

    def mark_sent(self, session: str, item_id: str, pane_id: Optional[str] = None) -> Optional[QueueItem]:
        """Mark an item as sent without firing the PTY drain path.

        Used when the client manually delivers the text itself (e.g.
        the per-row Send button or the Run button while the queue is
        paused). Without this, the server still sees the item as
        ``queued`` — next reconcile re-renders it in the active section
        and the processor would re-drain it on resume (double send).

        Behavior mirrors the relevant bookkeeping in _send_item: status,
        sent_at, replay-protection cache, and on-disk persistence with
        prune. Returns the updated item or None if not found.
        """
        queue = self._get_queue(session, pane_id)
        for item in queue:
            if item.id != item_id:
                continue
            if item.status == "sent":
                return item  # idempotent — already marked
            item.status = "sent"
            item.sent_at = time.time()
            key = self._queue_key_resolved(session, pane_id)
            self._recently_sent.setdefault(key, {})[item.id] = (item, item.sent_at)
            self._prune_old_sent(session, pane_id)
            self._save_to_disk(session, pane_id)
            return item
        return None

    def _prune_old_sent(self, session: str, pane_id: Optional[str] = None) -> bool:
        """Drop sent items past SENT_VISIBLE_TTL_SECONDS from the queue.

        Replay protection (``_recently_sent``) is independent and keeps
        a separate TTL — so dropping the visible item here does NOT let
        a duplicate enqueue re-run the prompt.

        Returns True if anything was dropped (so callers can persist).
        """
        key = self._queue_key_resolved(session, pane_id)
        queue = self._queues.get(key)
        if not queue:
            return False
        now = time.time()
        kept = [
            item for item in queue
            if not (
                item.status == "sent"
                and (now - (item.sent_at or 0)) > self.SENT_VISIBLE_TTL_SECONDS
            )
        ]
        if len(kept) == len(queue):
            return False
        self._queues[key] = kept
        return True

    def pause(self, session: str, pane_id: Optional[str] = None) -> None:
        """Pause queue processing for a session+pane."""
        key = self._queue_key_resolved(session, pane_id)
        self._paused[key] = True

    def resume(self, session: str, pane_id: Optional[str] = None) -> None:
        """Resume queue processing for a session+pane."""
        key = self._queue_key_resolved(session, pane_id)
        self._paused[key] = False
        self._wake()

    def is_paused(self, session: str, pane_id: Optional[str] = None) -> bool:
        """Check if queue is paused."""
        key = self._queue_key_resolved(session, pane_id)
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

    # Min seconds since last client input (any text from any connected
    # WS/SSE client) before auto-send is allowed to fire. Stops the
    # queue from racing with the user's own typing — symptom seen in
    # the wild: auto-sent "test1234" arrived in Claude's input box at
    # the same instant the user was typing the next message, and got
    # concatenated into one merged turn instead of submitting alone.
    USER_TYPING_QUIET_SECONDS = 3.0

    async def _check_ready(self, session: str, pane_id: Optional[str] = None) -> bool:
        """
        Check if terminal is ready to receive input:
        1. User hasn't typed in the last USER_TYPING_QUIET_SECONDS
           (avoid racing with their own input into Claude's TUI box)
        2. Driver says agent is in 'idle' phase (semantic — protects
           against firing mid-tool-call when output briefly quiets)
        3. Output has been quiet for QUIET_MS (defence in depth)
        4. No confirmation prompt visible ([y/n] etc.)
        5. Prompt is visible
        """
        from mobile_terminal.helpers import run_subprocess, get_tmux_target

        if not self._app:
            return False

        # ── User-typing quiet check ────────────────────────────────
        # last_ws_input_time is updated by every text/bytes message
        # the WS handler receives. If the user typed within the last
        # ~3s, there's a good chance their next Enter is about to
        # fire — auto-sending right now would land in Claude Code's
        # input box and merge with whatever they're composing.
        last_input = getattr(self._app.state, "last_ws_input_time", 0)
        if last_input and time.time() - last_input < self.USER_TYPING_QUIET_SECONDS:
            return False

        # ── Driver phase check ─────────────────────────────────────
        # The driver knows whether the agent is actively running a
        # tool call vs. waiting at its idle prompt. The capture-pane
        # heuristics below can false-positive during a brief streaming
        # gap; the phase check is the authoritative "agent is done"
        # signal. Skip if the build_observe_context helper isn't
        # registered (test/standalone CommandQueue use).
        build_ctx = getattr(self._app.state, "build_observe_context", None)
        driver = getattr(self._app.state, "driver", None)
        if build_ctx is not None and driver is not None and pane_id:
            try:
                ctx = await build_ctx(pane_id)
                if ctx is not None:
                    loop = asyncio.get_event_loop()
                    obs = await loop.run_in_executor(None, driver.observe, ctx)
                    # Only auto-fire when the agent reports idle. Other
                    # phases (working/planning/running_task/waiting)
                    # mean the user's command would land mid-effort.
                    if obs.phase != "idle":
                        return False
            except Exception as e:
                # Driver is configured but the check itself failed. Refuse
                # to fire — landing a queued command into a working pane
                # is worse than the queue stalling for a poll cycle. The
                # heuristic below would happily false-positive in exactly
                # the conditions that broke the driver check.
                logger.warning(f"driver phase check failed; skipping fire: {e}")
                return False

        # Check quiet period
        input_queue = self._app.state.input_queue
        since_output = (time.time() - input_queue._last_output_ts) * 1000
        if since_output < self.QUIET_MS:
            return False

        # Build proper tmux target for the active pane
        tmux_target = get_tmux_target(session, pane_id) if pane_id else session

        # Check for prompt via tmux capture-pane. -S -20 because Claude
        # Code's permission UI (question + selector + input box) easily
        # exceeds 5 lines, and BUSY_PATTERNS need to see the whole box.
        try:
            result = await run_subprocess(
                ["tmux", "capture-pane", "-t", tmux_target, "-p", "-S", "-20"],
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
                ["tmux", "display-message", "-p", "-t", tmux_target, "#{pane_title}"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            pane_title = title_result.stdout or ""
            if "Signal Detection Pending" in pane_title:
                return True
            # Claude Code idle prompt: title starts with ❯ or ✳
            if pane_title.strip().startswith("❯") or pane_title.strip().startswith("✳"):
                return True

        except Exception as e:
            logger.warning(f"Ready check failed: {e}")

        return False

    async def _send_item(self, session: str, item: QueueItem, pane_id: Optional[str] = None) -> bool:
        """Send a single item to the terminal.

        Uses ``send_text_to_pane`` (tmux send-keys -l with bracketed-paste
        wrapping for multiline text) followed by an explicit Enter, instead
        of writing raw bytes to the PTY. This matches how the manual /text
        endpoints send so the queue benefits from the same multi-line and
        atomicity guarantees.
        """
        from mobile_terminal.helpers import send_text_to_pane, get_tmux_target

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

            tmux_t = get_tmux_target(session, pane_id) if pane_id else session

            # Instrumentation: bracket each send step so we can tell where
            # an auto-sent item stalls when the resulting JSONL user turn
            # doesn't appear (bug: text visible in tail but no turn submit).
            # On the next repro we'll see exactly whether Enter was sent
            # and whether the subprocess call returned cleanly.
            logger.info(
                f"[QUEUE-SEND] id={item.id[:8]} pane={tmux_t} "
                f"text_len={len(item.text)} text={item.text[:60]!r}"
            )
            try:
                if item.text:
                    await send_text_to_pane(runtime, tmux_t, item.text)
                    logger.info(f"[QUEUE-SEND] id={item.id[:8]} text sent → pressing Enter")
                else:
                    logger.info(f"[QUEUE-SEND] id={item.id[:8]} empty text → pressing Enter")
                # Always send Enter — empty text + Enter is a valid "submit
                # current prompt" action and matches sendTextAtomic(text, true).
                await runtime.send_keys(tmux_t, "Enter")
                logger.info(f"[QUEUE-SEND] id={item.id[:8]} Enter done")
            except Exception as send_err:
                logger.warning(f"[QUEUE-SEND] id={item.id[:8]} FAILED: {send_err}")
                item.status = "failed"
                item.error = f"send-keys failed: {send_err}"
                self._save_to_disk(session, pane_id)
                return False

            item.status = "sent"
            item.sent_at = time.time()
            # Prune visibly-stale sent items from the queue while we're
            # writing anyway — keeps the disk file bounded and stops
            # reconcile from re-rendering ancient history.
            self._prune_old_sent(session, pane_id)
            self._save_to_disk(session, pane_id)  # Persist sent status + prune

            # Record the id in the recently-sent cache for replay
            # protection. If a flaky-WS client misses the queue_sent
            # broadcast and re-asks via reconcileQueue, the next enqueue
            # with this id will return is_new=False instead of running
            # the prompt a second time.
            key = self._queue_key_resolved(session, pane_id)
            self._recently_sent.setdefault(key, {})[item.id] = (item, item.sent_at)

            # Update PTY-input bookkeeping so other code (e.g. desktop
            # activity detector) doesn't think this came from outside MTO.
            try:
                self._app.state.last_ws_input_time = time.time()
            except Exception:
                pass

            # Notify client. Include session+pane so views for other panes
            # can ignore it (prevents queue list cross-pollination).
            if websocket:
                try:
                    await websocket.send_json({
                        "type": "queue_sent",
                        "id": item.id,
                        "sent_at": item.sent_at,
                        "backlog_id": item.backlog_id,
                        "session": session,
                        "pane_id": pane_id,
                    })
                except Exception:
                    pass

            return True

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
        """Main processing loop — drains every pane of the current session.

        Wakes immediately on enqueue/resume via ``_wakeup_event``; otherwise
        falls back to a 2s idle poll so transient terminal state changes
        (e.g. an agent finishing a tool call) get noticed.

        Per pass: iterate every queue belonging to the current tmux session,
        and for each non-paused queue, send the first ``queued`` item with
        policy ``safe`` *if* the ready gate passes. Items targeted at a
        different session are skipped — the runtime is bound to one tmux
        session at a time, so we can't drive panes that aren't there.
        """
        self._running = True
        # Bind the event to the running loop now that we're inside it.
        self._wakeup_event = asyncio.Event()

        while self._running:
            # Wait until either someone signals work, or our 2s idle poll
            # ticks (so prompt transitions outside our control are caught).
            try:
                await asyncio.wait_for(self._wakeup_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            self._wakeup_event.clear()

            if not self._app:
                continue

            current_session = self._app.state.current_session
            if not current_session:
                continue

            # Snapshot keys — _send_item may mutate _queues during the pass
            # (saves, status updates), and we don't want to iterate a moving
            # dict.
            keys = list(self._queues.keys())

            # For repo-keyed scopes we need to map back to a live pane
            # in the current session. Build {repo_key -> pane_id} once
            # per pass so we don't re-shell out per key.
            from mobile_terminal.helpers import _list_session_windows
            repo_to_pane: Dict[str, str] = {}
            try:
                wins = _list_session_windows(current_session)
                for w in wins:
                    pane = f"{w.get('window_index')}:0"
                    cwd = w.get("cwd")
                    if cwd:
                        rk = repo_key_for_cwd(current_session, cwd)
                        # Prefer the active_target's pane when multiple
                        # panes share the same cwd.
                        active = getattr(self._app.state, "active_target", None)
                        if rk not in repo_to_pane or pane == active:
                            repo_to_pane[rk] = pane
            except Exception as e:
                logger.debug(f"_process_loop pane-map failed: {e}")

            for key in keys:
                if not self._running:
                    break
                session, parsed_pane = self._parse_key(key)
                # Runtime is bound to one tmux session — skip foreign queues.
                if session != current_session:
                    continue
                # Resolve pane to deliver to: legacy keys carry it inline;
                # repo-keyed scopes look it up in repo_to_pane.
                pane_id = parsed_pane or repo_to_pane.get(key)
                if not pane_id:
                    # No live pane currently maps to this repo — skip.
                    # The queue is preserved on disk for whenever a
                    # pane in that cwd next exists.
                    continue
                if self.is_paused(session, pane_id):
                    continue

                queue = self._queues.get(key) or []
                if not queue:
                    continue

                # First queued item the user opted into auto-send for.
                # The ⚡ flag IS the safety gate — by tapping it the user
                # has taken explicit responsibility for that command, so
                # the picker no longer enforces policy=='safe'. Without
                # ⚡ nothing auto-fires regardless of policy.
                item = next(
                    (i for i in queue
                     if i.status == "queued"
                     and i.auto_eligible),
                    None,
                )
                if not item:
                    continue

                # Ready gate per-pane (different panes can be busy
                # independently; e.g. one waiting at a [y/n], another idle).
                if not await self._check_ready(session, pane_id):
                    continue

                await self._send_item(session, item, pane_id)
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
