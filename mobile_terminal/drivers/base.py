"""
Base agent driver protocol and shared utilities.

AgentDriver is the abstraction that separates terminal/session orchestration
(tmux, xterm.js, websocket, push) from agent semantics (process detection,
permission signals, log parsing, start commands).
"""

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Observation:
    """Flat, UI-ready snapshot of agent state.

    Same JSON shape for /api/health/agent and /api/team/state.
    """
    # Identity
    agent_type: str = "generic"
    agent_name: str = "Agent"
    # Process state
    running: bool = False
    pid: Optional[int] = None
    # Phase
    phase: str = "idle"          # idle, working, planning, running_task, waiting
    detail: str = ""
    tool: str = ""
    active: bool = False         # log/output activity within last 30s
    # Permission
    waiting_reason: Optional[str] = None   # "permission", "question", None
    permission_tool: Optional[str] = None
    permission_target: Optional[str] = None
    # Log hints
    log_paths: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "agent_type": self.agent_type,
            "agent_name": self.agent_name,
            "running": self.running,
            "pid": self.pid,
            "phase": self.phase,
            "detail": self.detail,
            "tool": self.tool,
            "active": self.active,
            "waiting_reason": self.waiting_reason,
            "permission_tool": self.permission_tool,
            "permission_target": self.permission_target,
            "log_paths": [str(p) for p in self.log_paths],
        }


def _capture_pane(tmux_target: str, lines: int = 30) -> str:
    """Capture visible pane content via tmux."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", tmux_target, "-p", "-S", f"-{lines}"],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


@dataclass
class ObserveContext:
    """Context passed to driver.observe(). Lazy pane_snapshot."""
    session_name: str = ""
    target: str = ""
    tmux_target: str = ""
    shell_pid: Optional[int] = None
    pane_title: str = ""
    repo_path: Optional[Path] = None
    _pane_snapshot: Optional[str] = None

    def get_pane_snapshot(self, lines: int = 30) -> str:
        """Lazy capture-pane, cached per request."""
        if self._pane_snapshot is None:
            self._pane_snapshot = _capture_pane(self.tmux_target, lines)
        return self._pane_snapshot


# ---------------------------------------------------------------------------
# JSONL parsing utilities (shared by Claude + any JSONL-based driver)
# ---------------------------------------------------------------------------

def tail_jsonl(log_file: Path, read_bytes: int = 8192) -> list:
    """Read the last N bytes of a JSONL file, return parsed entries (most recent first)."""
    try:
        st = log_file.stat()
        file_size = st.st_size
        actual_read = min(file_size, read_bytes)
        with open(log_file, 'rb') as f:
            if file_size > actual_read:
                f.seek(file_size - actual_read)
            tail_bytes = f.read(actual_read)
        tail_text = tail_bytes.decode('utf-8', errors='replace')

        lines = tail_text.split('\n')
        if file_size > actual_read:
            lines = lines[1:]  # Skip partial first line

        entries = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(entries) >= 30:
                break
        return entries
    except Exception:
        return []


def find_claude_log_file(repo_path: Path) -> Optional[Path]:
    """Find the most recent Claude JSONL log for a repo path."""
    project_id = str(repo_path.resolve()).replace("~", "-").replace("/", "-")
    claude_projects_dir = Path.home() / ".claude" / "projects" / project_id
    if not claude_projects_dir.exists():
        return None
    jsonl_files = list(claude_projects_dir.glob("*.jsonl"))
    if not jsonl_files:
        return None
    return max(jsonl_files, key=lambda f: f.stat().st_mtime)


# ---------------------------------------------------------------------------
# AgentDriver protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class AgentDriver(Protocol):
    def id(self) -> str: ...
    def display_name(self) -> str: ...
    def start_command(self, startup_command: Optional[str] = None) -> list: ...
    def observe(self, ctx: ObserveContext) -> Observation: ...
    def capabilities(self) -> dict: ...


# ---------------------------------------------------------------------------
# BaseAgentDriver — default implementations
# ---------------------------------------------------------------------------

class BaseAgentDriver:
    """Base with default observe() that calls is_running(), detect_permission_wait(),
    parse_progress() in sequence. Subclasses override individual methods."""

    _display_name: str = "Agent"
    _agent_id: str = "generic"
    _process_name: str = ""  # e.g. "claude", "codex"

    def id(self) -> str:
        return self._agent_id

    def display_name(self) -> str:
        return self._display_name

    def start_command(self, startup_command: Optional[str] = None) -> list:
        if startup_command:
            return [startup_command]
        return [self._process_name or self._agent_id]

    def capabilities(self) -> dict:
        """Stable JSON for UI to decide what to render."""
        return {
            "has_jsonl_logs": False,
            "has_permission_signal": False,
            "has_phase_detection": False,
            "has_pane_title_signal": False,
        }

    def observe(self, ctx: ObserveContext) -> Observation:
        """Default observe: is_running → detect_permission → parse_progress."""
        obs = Observation(
            agent_type=self.id(),
            agent_name=self.display_name(),
        )

        # 1. PID detection (tiered)
        self.is_running(ctx, obs)

        # 2. Permission detection
        self.detect_permission_wait(ctx, obs)

        # 3. Phase / progress detection
        if obs.waiting_reason is None:
            self.parse_progress(ctx, obs)

        return obs

    def is_running(self, ctx: ObserveContext, obs: Observation) -> None:
        """Tiered PID detection. Sets obs.running and obs.pid."""
        if not ctx.shell_pid:
            return

        process_name = self._process_name or self._agent_id
        try:
            # Check children of shell_pid for agent process
            proc_check = subprocess.run(
                ["pgrep", "-f", process_name, "-P", str(ctx.shell_pid)],
                capture_output=True, text=True, timeout=2,
            )
            if proc_check.returncode == 0 and proc_check.stdout.strip():
                pids = proc_check.stdout.strip().split("\n")
                if pids and pids[0].isdigit():
                    obs.running = True
                    obs.pid = int(pids[0])
                    return
        except Exception:
            pass

        # Activity fallback: log recency
        if ctx.repo_path:
            log_file = find_claude_log_file(ctx.repo_path)
            if log_file:
                try:
                    age = time.time() - log_file.stat().st_mtime
                    if age < 30:
                        obs.active = True
                except Exception:
                    pass

    def detect_permission_wait(self, ctx: ObserveContext, obs: Observation) -> None:
        """Override in subclasses. Default: no-op."""
        pass

    def parse_progress(self, ctx: ObserveContext, obs: Observation) -> None:
        """Override in subclasses. Default: activity-based idle/working."""
        if obs.running or obs.active:
            obs.phase = "working"
            obs.detail = "Working..."
        else:
            obs.phase = "idle"
