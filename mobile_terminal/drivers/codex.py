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
import json
import time
from pathlib import Path
from typing import Optional

from .base import (
    BaseAgentDriver,
    DriverCapabilities,
    Observation,
    ObserveContext,
    normalize_tool_content,
    summarize_tool_result,
    tail_jsonl,
)

logger = logging.getLogger(__name__)

# Phase cache: per-pane keyed by (log_file, mtime, size)
_codex_phase_cache: dict = {}


def find_codex_log_file(repo_path: Optional[Path] = None) -> Optional[Path]:
    """Find the most recent Codex session JSONL log.

    Codex writes session logs to ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl.
    Scans date directories in reverse order for efficiency.
    """
    sessions_dir = Path.home() / ".codex" / "sessions"
    if not sessions_dir.exists():
        return None

    files = list_codex_log_files(repo_path)
    return files[0] if files else None


def list_codex_log_files(repo_path: Optional[Path] = None) -> list[Path]:
    """List Codex rollout logs newest-first."""
    sessions_dir = Path.home() / ".codex" / "sessions"
    if not sessions_dir.exists():
        return []

    # Walk YYYY/MM/DD dirs in reverse chronological order
    found = []
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
                found.extend(day_dir.glob("rollout-*.jsonl"))

    files = sorted(found, key=lambda f: f.stat().st_mtime, reverse=True)
    if repo_path is None:
        return files
    return [f for f in files if _codex_log_matches_repo(f, repo_path)]


def _codex_log_matches_repo(log_file: Path, repo_path: Path) -> bool:
    """Return True when rollout session_meta cwd belongs to repo_path."""
    try:
        repo = repo_path.resolve()
    except Exception:
        repo = repo_path
    try:
        with log_file.open("r", encoding="utf-8", errors="replace") as f:
            for _ in range(20):
                line = f.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "session_meta":
                    continue
                cwd = entry.get("payload", {}).get("cwd", "")
                if not cwd:
                    return False
                try:
                    cwd_path = Path(cwd).resolve()
                except Exception:
                    cwd_path = Path(cwd)
                return cwd_path == repo or repo in cwd_path.parents
    except Exception:
        return False
    return False


class CodexDriver(BaseAgentDriver):
    """Driver for OpenAI Codex CLI agent.

    Phase detection via JSONL session logs. Permission detection via
    approval-requested events in JSONL. No pane_title support (Ratatui TUI).
    """

    _agent_id = "codex"
    _display_name = "Codex CLI"
    _process_name = "codex"
    _context_limit = 200_000

    def find_log_file(self, repo_path: Path) -> Optional[Path]:
        return find_codex_log_file(repo_path)

    def ready_patterns(self) -> list[str]:
        return ["codex", "Codex", " > "]

    def config_dir_name(self) -> str:
        return ".codex"

    def capabilities(self) -> DriverCapabilities:
        return DriverCapabilities(
            structured_logs=True,
            permission_detection=True,
            phase_detection=True,
            pane_title_signal=False,
        )

    def parse_log_messages(self, log_file: Path, limit: int = 200) -> list:
        """Parse Codex rollout JSONL into the frontend's log message shape."""
        raw_content = log_file.read_text(errors="replace")
        conversation = []
        pending_tool_uses = {}

        for line in raw_content.strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = entry.get("type", "")
            payload = entry.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}

            if event_type == "notification" and entry.get("event") == "approval-requested":
                tool_name = payload.get("tool", "") or payload.get("name", "") or "tool"
                conversation.append(f"{self.display_name()} ⚠ Approval needed: {tool_name}")
                continue

            if event_type != "response_item":
                continue

            payload_type = payload.get("type", "")

            if payload_type == "message":
                role = payload.get("role", "")
                if role not in ("user", "assistant"):
                    continue
                text = self._extract_content_text(payload.get("content", []))
                if not text:
                    continue
                if role == "user":
                    conversation.append(f"$ {text}")
                else:
                    conversation.append(f"{self.display_name()}: {text}")
                continue

            if payload_type == "function_call":
                tool_name = payload.get("name", "") or "tool"
                tool_id = payload.get("call_id", "") or payload.get("id", "")
                detail = self._function_call_detail(tool_name, payload.get("arguments", ""))
                label = f"{self.display_name()} • {tool_name}"
                if detail:
                    label += f": {detail}"
                conversation.append(label)
                if tool_id:
                    pending_tool_uses[tool_id] = (len(conversation) - 1, tool_name, detail)
                continue

            if payload_type == "function_call_output":
                tool_id = payload.get("call_id", "")
                if not tool_id or tool_id not in pending_tool_uses:
                    continue
                conv_idx, tool_name, tool_detail = pending_tool_uses.pop(tool_id)
                result_content = normalize_tool_content(payload.get("output", ""))
                is_error = self._tool_output_is_error(result_content)
                summary = summarize_tool_result(tool_name, result_content, is_error)
                orig = conversation[conv_idx]
                text = orig["text"] if isinstance(orig, dict) else orig
                conversation[conv_idx] = {
                    "text": text,
                    "tool": {
                        "name": tool_name,
                        "detail": tool_detail,
                        "tool_use_id": tool_id,
                        "result_summary": summary,
                        "result_status": "error" if is_error else "ok",
                    },
                }

        return conversation[-limit:] if len(conversation) > limit else conversation

    def get_tool_output(self, log_file: Path, tool_use_id: str) -> Optional[dict]:
        """Return a Codex function_call_output payload by call_id."""
        raw = log_file.read_text(errors="replace")
        for line in reversed(raw.strip().split("\n")):
            if not line.strip() or tool_use_id not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = entry.get("payload", {})
            if (
                entry.get("type") != "response_item"
                or not isinstance(payload, dict)
                or payload.get("type") != "function_call_output"
                or payload.get("call_id") != tool_use_id
            ):
                continue
            result_content = normalize_tool_content(payload.get("output", ""))
            return {
                "content": result_content,
                "is_error": self._tool_output_is_error(result_content),
            }
        return None

    def observe(self, ctx: ObserveContext) -> Observation:
        """Full Codex observation: PID + JSONL phase detection."""
        obs = Observation(
            agent_type=self.id(),
            agent_name=self.display_name(),
        )

        # 1. PID detection
        self.is_running(ctx, obs)

        # 2. Find session log
        log_file = find_codex_log_file(ctx.repo_path)
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

        # 6. Compute context percentage
        if obs.context_used is not None:
            limit = self._context_limit
            if obs.context_used > limit * 0.95:
                limit = 1_000_000
            obs.context_limit = limit
            obs.context_pct = min(round((obs.context_used / limit) * 100, 1), 100.0)

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
            "context_used": None,
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
                # Extract token usage from turn.completed payload
                usage = payload.get("usage", {})
                if usage:
                    input_tok = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                    output_tok = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
                    total = input_tok + output_tok
                    if total > 0:
                        result["context_used"] = total
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
    def _extract_content_text(content) -> str:
        """Extract display text from Codex message content blocks."""
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text") or block.get("input_text") or block.get("output_text")
            if text:
                parts.append(str(text))
        return "\n".join(parts).strip()

    @staticmethod
    def _function_call_detail(tool_name: str, arguments) -> str:
        """Best-effort readable detail for a Codex function_call."""
        if isinstance(arguments, str):
            arg_text = arguments
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                return arg_text[:200]
        if not isinstance(arguments, dict):
            return ""
        if tool_name == "exec_command":
            return str(arguments.get("cmd", ""))[:200]
        if tool_name in ("apply_patch",):
            return "patch"
        for key in ("path", "ref_id", "target", "pattern", "query", "message"):
            if arguments.get(key):
                return str(arguments[key])[:200]
        return ""

    @staticmethod
    def _tool_output_is_error(output: str) -> bool:
        """Infer failed command output from Codex's command summary text."""
        if "Process exited with code " not in output:
            return False
        return "Process exited with code 0" not in output

    @staticmethod
    def _apply_result(obs: Observation, result: dict) -> None:
        """Apply cached result dict onto an Observation."""
        obs.phase = result.get("phase", "idle")
        obs.detail = result.get("detail", "")
        obs.tool = result.get("tool", "")
        obs.waiting_reason = result.get("waiting_reason")
        obs.permission_tool = result.get("permission_tool")
        obs.permission_target = result.get("permission_target")
        obs.context_used = result.get("context_used")
