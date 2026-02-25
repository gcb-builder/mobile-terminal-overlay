"""
Claude Code agent driver.

Extracts Claude-specific logic from server.py:
- JSONL log parsing for phase/tool detection
- pane_title "Signal Detection Pending" for permission detection
- Permission payload extraction from tool_use blocks
- PID detection via pgrep -f claude
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

from .base import (
    BaseAgentDriver,
    Observation,
    ObserveContext,
    find_claude_log_file,
    tail_jsonl,
)

logger = logging.getLogger(__name__)

# Phase cache: {log_path, mtime, size, result} — single global entry
_phase_cache: dict = {"log_path": "", "mtime": 0.0, "size": 0, "result": None}

# Team phase cache: key → {mtime, size, result} — per-pane caching
_team_phase_cache: dict = {}


class ClaudeDriver(BaseAgentDriver):
    """Driver for Claude Code CLI agent."""

    _agent_id = "claude"
    _display_name = "Claude"
    _process_name = "claude"

    def capabilities(self) -> dict:
        return {
            "has_jsonl_logs": True,
            "has_permission_signal": True,
            "has_phase_detection": True,
            "has_pane_title_signal": True,
        }

    def observe(self, ctx: ObserveContext) -> Observation:
        """Full Claude observation: PID + JSONL phase + permission detection."""
        obs = Observation(
            agent_type=self.id(),
            agent_name=self.display_name(),
        )

        # 1. PID detection
        self.is_running(ctx, obs)

        # 2. Find log file
        log_file = None
        if ctx.repo_path:
            log_file = find_claude_log_file(ctx.repo_path)
            if log_file:
                obs.log_paths = [log_file]

        # 3. Check log activity (informational, NOT a gate)
        if log_file:
            try:
                age = time.time() - log_file.stat().st_mtime
                obs.active = age < 30
            except Exception:
                pass

        # 4. If not running and no activity, return early idle
        if not obs.running and not obs.active:
            obs.phase = "idle"
            return obs

        # 5. Parse JSONL + pane_title for phase + permission
        if log_file:
            self._parse_phase_and_permission(ctx, obs, log_file)

        return obs

    def _parse_phase_and_permission(
        self, ctx: ObserveContext, obs: Observation, log_file: Path
    ) -> None:
        """Parse JSONL tail + pane_title for phase, tool, permission info."""
        try:
            st = log_file.stat()
        except Exception:
            return

        # Check cache
        cache_key = f"{ctx.session_name}:{ctx.target}:{ctx.repo_path}:{log_file}"
        cached = _team_phase_cache.get(cache_key)
        if (cached
                and cached["mtime"] == st.st_mtime
                and cached["size"] == st.st_size
                and cached["result"] is not None):
            # Apply cached result to obs
            self._apply_phase_result(obs, cached["result"])
            return

        # Cache miss — parse
        entries = tail_jsonl(log_file)
        result = self._classify_entries(ctx, entries)

        # Update cache (evict oldest if over 50)
        _team_phase_cache[cache_key] = {
            "mtime": st.st_mtime, "size": st.st_size, "result": result,
        }
        if len(_team_phase_cache) > 50:
            oldest_key = next(iter(_team_phase_cache))
            del _team_phase_cache[oldest_key]

        self._apply_phase_result(obs, result)

    def _classify_entries(self, ctx: ObserveContext, entries: list) -> dict:
        """Classify JSONL entries into phase/tool/permission result dict."""
        result = {
            "phase": "idle",
            "detail": "",
            "tool": "",
            "waiting_reason": None,
            "permission_tool": None,
            "permission_target": None,
        }

        # Check pane_title for signal detection
        if ctx.pane_title and "Signal Detection Pending" in ctx.pane_title:
            result["phase"] = "waiting"
            result["detail"] = "Signal Detection Pending"
            result["waiting_reason"] = "permission"
            # Extract permission info from last tool_use
            self._extract_permission_info(entries, result)
            return result

        # Scan entries (reverse chronological)
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
            if isinstance(content, str) or not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue

                tool_name = block.get("name", "")
                tool_input = block.get("input", {})

                if tool_name == "AskUserQuestion":
                    result["phase"] = "waiting"
                    result["waiting_reason"] = "question"
                    questions = tool_input.get("questions", [])
                    if questions:
                        result["detail"] = questions[0].get("question", "Needs input")[:80]
                    else:
                        result["detail"] = "Needs input"
                    result["tool"] = tool_name
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
                    last_tool_detail = self._extract_tool_detail(tool_name, tool_input)

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
            result["detail"] = active_form or (f"{last_tool}: {last_tool_detail}" if last_tool_detail else "Working...")
            result["tool"] = last_tool
        elif last_tool:
            result["phase"] = "working"
            result["detail"] = active_form or last_tool_detail or f"Using {last_tool}"
            result["tool"] = last_tool
        else:
            result["phase"] = "idle"
            result["detail"] = ""

        return result

    @staticmethod
    def _extract_tool_detail(tool_name: str, tool_input: dict) -> str:
        """Extract human-readable detail from tool input."""
        if tool_name == "Bash":
            return tool_input.get("command", "")[:60]
        elif tool_name in ("Read", "Edit", "Write", "Glob", "Grep"):
            path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("pattern", "")
            return path.split("/")[-1][:60] if "/" in str(path) else str(path)[:60]
        elif tool_name == "Task":
            return tool_input.get("description", "")[:60]
        elif tool_name == "EnterPlanMode":
            return "Planning..."
        elif tool_name == "ExitPlanMode":
            return "Plan ready"
        return ""

    @staticmethod
    def _extract_permission_info(entries: list, result: dict) -> None:
        """Extract permission_tool and permission_target from JSONL entries."""
        for entry in entries:
            msg = entry.get("message", {})
            if entry.get("type") != "assistant":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})
                    if tool_name == "Bash":
                        result["permission_tool"] = "Bash"
                        result["permission_target"] = tool_input.get("command", "")[:80]
                    elif tool_name in ("Edit", "Write", "Read"):
                        result["permission_tool"] = tool_name
                        result["permission_target"] = tool_input.get("file_path", "")[:80]
                    else:
                        result["permission_tool"] = tool_name
                        result["permission_target"] = ""
                    return

    @staticmethod
    def _apply_phase_result(obs: Observation, result: dict) -> None:
        """Apply a cached result dict onto an Observation."""
        obs.phase = result.get("phase", "idle")
        obs.detail = result.get("detail", "")
        obs.tool = result.get("tool", "")
        obs.waiting_reason = result.get("waiting_reason")
        obs.permission_tool = result.get("permission_tool")
        obs.permission_target = result.get("permission_target")


class ClaudePermissionDetector:
    """Stateful permission detector for push notifications.

    Reads only new JSONL bytes since last check (incremental).
    Used by push_monitor() — NOT by observe() (which is stateless).
    """

    def __init__(self):
        self.last_log_size = 0
        self.last_sent_id = None
        self.log_file = None

    def set_log_file(self, path: Path):
        self.log_file = Path(path) if path else None
        try:
            self.last_log_size = self.log_file.stat().st_size if self.log_file and self.log_file.exists() else 0
        except Exception:
            self.last_log_size = 0

    def check_sync(self, session: str, target: str, tmux_target: str) -> Optional[dict]:
        """Check for pending permission request. Returns payload or None."""
        if not self.log_file or not self.log_file.exists():
            return None

        try:
            current_size = self.log_file.stat().st_size
        except Exception:
            return None
        if current_size <= self.last_log_size:
            return None

        new_text = self._read_new_entries(current_size)
        self.last_log_size = current_size

        if not new_text:
            return None

        tool_info = self._extract_last_tool_use(new_text)
        if not tool_info:
            return None

        # Confirm Claude is actually waiting (pane_title check)
        try:
            import subprocess
            title_result = subprocess.run(
                ["tmux", "display-message", "-p", "-t", tmux_target, "#{pane_title}"],
                capture_output=True, text=True, timeout=2,
            )
            if "Signal Detection Pending" not in (title_result.stdout or ""):
                return None
        except Exception:
            return None

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
        try:
            with open(self.log_file, 'r') as f:
                f.seek(self.last_log_size)
                return f.read()
        except Exception:
            return ""

    def _extract_last_tool_use(self, new_text):
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
        self.last_sent_id = None
