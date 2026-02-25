"""
Codex CLI agent driver.

Detects Codex CLI state via:
- JSONL session logs at ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
- Event types: turn.started, item.started/completed, turn.completed/failed
- approval-requested events for permission detection
- Process name: "codex" (native Rust binary)

Codex uses a Ratatui full-screen TUI (alternate screen buffer), so terminal
scrollback scraping is unreliable. JSONL logs are the primary signal.
"""

import logging
import time
from pathlib import Path
from typing import Optional

from .base import BaseAgentDriver, Observation, ObserveContext, tail_jsonl

logger = logging.getLogger(__name__)

# Phase cache: per-pane keyed by (log_file, mtime, size)
_codex_phase_cache: dict = {}


def find_codex_log_file() -> Optional[Path]:
    """Find the most recent Codex session JSONL log.

    Codex writes session logs to ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl.
    Scans date directories in reverse order for efficiency.
    """
    sessions_dir = Path.home() / ".codex" / "sessions"
    if not sessions_dir.exists():
        return None

    # Walk YYYY/MM/DD dirs in reverse chronological order
    try:
        year_dirs = sorted(sessions_dir.iterdir(), reverse=True)
    except Exception:
        return []

    for year_dir in year_dirs:
        if not year_dir.is_dir():
            continue
        try:
            month_dirs = sorted(year_dir.iterdir(), reverse=True)
        except Exception:
            continue
        for month_dir in month_dirs:
            if not month_dir.is_dir():
                continue
            try:
                day_dirs = sorted(month_dir.iterdir(), reverse=True)
            except Exception:
                continue
            for day_dir in day_dirs:
                if not day_dir.is_dir():
                    continue
                jsonl_files = list(day_dir.glob("rollout-*.jsonl"))
                if jsonl_files:
                    return max(jsonl_files, key=lambda f: f.stat().st_mtime)

    return None


class CodexDriver(BaseAgentDriver):
    """Driver for OpenAI Codex CLI agent.

    Phase detection via JSONL session logs. Permission detection via
    approval-requested events in JSONL. No pane_title support (Ratatui TUI).
    """

    _agent_id = "codex"
    _display_name = "Codex CLI"
    _process_name = "codex"

    def capabilities(self) -> dict:
        return {
            "has_jsonl_logs": True,
            "has_permission_signal": True,
            "has_phase_detection": True,
            "has_pane_title_signal": False,
        }

    def observe(self, ctx: ObserveContext) -> Observation:
        """Full Codex observation: PID + JSONL phase detection."""
        obs = Observation(
            agent_type=self.id(),
            agent_name=self.display_name(),
        )

        # 1. PID detection
        self.is_running(ctx, obs)

        # 2. Find session log
        log_file = find_codex_log_file()
        if log_file:
            obs.log_paths = [log_file]

        # 3. Check log activity
        if log_file:
            try:
                age = time.time() - log_file.stat().st_mtime
                obs.active = age < 30
            except Exception:
                pass

        # 4. Early exit if nothing happening
        if not obs.running and not obs.active:
            obs.phase = "idle"
            return obs

        # 5. Parse JSONL for phase
        if log_file:
            self._parse_phase(ctx, obs, log_file)
        elif obs.running:
            obs.phase = "working"
            obs.detail = "Working..."

        return obs

    def _parse_phase(
        self, ctx: ObserveContext, obs: Observation, log_file: Path
    ) -> None:
        """Parse Codex session JSONL for phase classification."""
        try:
            st = log_file.stat()
        except Exception:
            return

        # Check cache
        cache_key = f"codex:{ctx.session_name}:{ctx.target}:{log_file}"
        cached = _codex_phase_cache.get(cache_key)
        if (cached
                and cached["mtime"] == st.st_mtime
                and cached["size"] == st.st_size
                and cached["result"] is not None):
            self._apply_result(obs, cached["result"])
            return

        # Cache miss — parse
        entries = tail_jsonl(log_file)
        result = self._classify_entries(entries)

        # Update cache (evict oldest if over 50)
        _codex_phase_cache[cache_key] = {
            "mtime": st.st_mtime, "size": st.st_size, "result": result,
        }
        if len(_codex_phase_cache) > 50:
            oldest_key = next(iter(_codex_phase_cache))
            del _codex_phase_cache[oldest_key]

        self._apply_result(obs, result)

    def _classify_entries(self, entries: list) -> dict:
        """Classify Codex JSONL entries into phase/detail.

        Codex JSONL event types:
        - turn.started: agent turn begins
        - item.started: individual action (command, file change, etc.)
        - item.completed: action finished
        - turn.completed: turn finished (includes token usage)
        - turn.failed: error during turn
        - approval-requested: needs user permission
        - agent-turn-complete: notification event
        """
        result = {
            "phase": "idle",
            "detail": "",
            "tool": "",
            "waiting_reason": None,
            "permission_tool": None,
            "permission_target": None,
        }

        if not entries:
            return result

        # Scan entries (most recent first, as returned by tail_jsonl)
        for entry in entries:
            event_type = entry.get("type", "")

            # Check for notification events
            if event_type == "notification":
                event_name = entry.get("event", "")
                if event_name == "approval-requested":
                    result["phase"] = "waiting"
                    result["waiting_reason"] = "permission"
                    result["detail"] = "Approval needed"
                    # Try to extract what needs approval
                    payload = entry.get("payload", {})
                    tool_name = payload.get("tool", "") or payload.get("name", "")
                    if tool_name:
                        result["permission_tool"] = tool_name
                        result["detail"] = f"Approve: {tool_name}"
                    return result

            # Codex uses various event structures — check common patterns
            payload_type = ""
            payload = entry.get("payload", {})
            if isinstance(payload, dict):
                payload_type = payload.get("type", "")

            # Direct event type matching
            if event_type == "turn.started" or payload_type == "turn.started":
                result["phase"] = "working"
                result["detail"] = "Thinking..."
                result["tool"] = ""
                # Don't return — keep scanning for more specific events
                continue

            if event_type == "item.started" or payload_type == "item.started":
                result["phase"] = "working"
                detail = self._extract_item_detail(entry)
                result["detail"] = detail or "Executing..."
                result["tool"] = payload.get("item_type", "")
                return result

            if event_type == "item.completed" or payload_type == "item.completed":
                result["phase"] = "working"
                detail = self._extract_item_detail(entry)
                result["detail"] = detail or "Working..."
                result["tool"] = payload.get("item_type", "")
                return result

            if event_type == "turn.completed" or payload_type == "turn.completed":
                result["phase"] = "idle"
                result["detail"] = "Turn complete"
                return result

            if event_type == "turn.failed" or payload_type == "turn.failed":
                result["phase"] = "idle"
                error = payload.get("error", "")
                result["detail"] = f"Error: {str(error)[:60]}" if error else "Error"
                return result

            # Fallback: if there's a message content with tool calls
            msg = entry.get("message", {})
            if isinstance(msg, dict):
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tool_name = block.get("name", "")
                            result["phase"] = "working"
                            result["tool"] = tool_name
                            result["detail"] = f"Using {tool_name}"
                            return result

        return result

    @staticmethod
    def _extract_item_detail(entry: dict) -> str:
        """Extract human-readable detail from a Codex item event."""
        payload = entry.get("payload", {})
        if not isinstance(payload, dict):
            return ""

        # Command execution
        command = payload.get("command", "")
        if command:
            return f"Running: {str(command)[:60]}"

        # File operations
        file_path = payload.get("file", "") or payload.get("path", "")
        if file_path:
            name = str(file_path).split("/")[-1] if "/" in str(file_path) else str(file_path)
            item_type = payload.get("item_type", "")
            if item_type:
                return f"{item_type}: {name[:60]}"
            return name[:60]

        # Tool name fallback
        tool = payload.get("tool", "") or payload.get("name", "")
        if tool:
            return f"Using {tool}"

        # Description
        desc = payload.get("description", "")
        if desc:
            return str(desc)[:60]

        return ""

    @staticmethod
    def _apply_result(obs: Observation, result: dict) -> None:
        """Apply cached result dict onto an Observation."""
        obs.phase = result.get("phase", "idle")
        obs.detail = result.get("detail", "")
        obs.tool = result.get("tool", "")
        obs.waiting_reason = result.get("waiting_reason")
        obs.permission_tool = result.get("permission_tool")
        obs.permission_target = result.get("permission_target")
