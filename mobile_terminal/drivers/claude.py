"""
Claude Code agent driver.

Extracts Claude-specific logic from server.py:
- JSONL log parsing for phase/tool detection
- pane_title "Signal Detection Pending" for permission detection
- Permission payload extraction from tool_use blocks
- PID detection via pgrep -f claude
"""

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

from .base import (
    BaseAgentDriver,
    DriverCapabilities,
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
    _context_limit = 200_000

    def find_log_file(self, repo_path: Path) -> Optional[Path]:
        return find_claude_log_file(repo_path)

    def ready_patterns(self) -> list[str]:
        return ["claude-code", "Claude Code", " > ",
                "What would you like to do?", "How can I help"]

    def config_dir_name(self) -> str:
        return ".claude"

    def capabilities(self) -> DriverCapabilities:
        return DriverCapabilities(
            structured_logs=True,
            permission_detection=True,
            phase_detection=True,
            pane_title_signal=True,
        )

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

        # 4. If Claude is not running in this pane, phase is idle.
        # Don't use log activity as a gate — find_claude_log_file picks the
        # most recent log by repo, which may belong to a different Claude
        # instance (e.g. a team agent in another pane).
        if not obs.running:
            obs.phase = "idle"
            return obs

        # 5. Parse JSONL + pane_title for phase + permission
        if log_file:
            self._parse_phase_and_permission(ctx, obs, log_file)

        # 6. Compute context percentage
        # Claude Code can extend context to 1M; bump limit when usage exceeds 200k
        if obs.context_used is not None:
            limit = self._context_limit
            if obs.context_used > limit * 0.95:
                limit = 1_000_000
            obs.context_limit = limit
            obs.context_pct = min(round((obs.context_used / limit) * 100, 1), 100.0)

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
            "context_used": None,
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

            # Extract token usage from most recent assistant entry
            if result["context_used"] is None:
                usage = msg.get("usage", {})
                if usage:
                    total = (usage.get("input_tokens", 0)
                             + usage.get("cache_creation_input_tokens", 0)
                             + usage.get("cache_read_input_tokens", 0)
                             + usage.get("output_tokens", 0))
                    if total > 0:
                        result["context_used"] = total

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
        obs.context_used = result.get("context_used")


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
        # last_log_size kept for backward-compat with callers that still
        # poke at it; the new check_sync ignores it. State-query semantics
        # mean we always re-scan the recent tail rather than chase deltas.
        self.last_log_size = 0

    def check_sync(self, session: str, target: str, tmux_target: str) -> Optional[dict]:
        """Return the currently-pending permission as {tool, target, id}, or None.

        State query (NOT incremental): walks the recent JSONL tail, finds
        the most recent assistant tool_use whose tool_use_id has no matching
        user tool_result, and confirms a permission prompt is visible in
        tmux. The dedup key is the real tool_use_id, so the same prompt
        only fires once even across many polls until it's resolved.
        """
        if not self.log_file or not self.log_file.exists():
            return None

        # mtime/size cache — re-parse only when file changed
        try:
            st = self.log_file.stat()
        except Exception:
            return None
        cache_key = (st.st_mtime, st.st_size)
        cached = getattr(self, "_unresolved_cache", None)
        if cached and cached[0] == cache_key:
            tool_info = cached[1]
        else:
            tool_info = _find_unresolved_tool_use(self.log_file)
            self._unresolved_cache = (cache_key, tool_info)

        if not tool_info:
            logger.debug(f"[detector] no unresolved tool_use in {self.log_file.name} (size={st.st_size})")
            return None

        # Confirm Claude is visibly waiting (defends against parser bugs
        # or a tool_result that hasn't landed in JSONL yet).
        if not _has_visible_permission_prompt(tmux_target):
            logger.info(f"[detector] unresolved tool_use {tool_info['id'][:25]} ({tool_info['name']}) but no visible prompt in {tmux_target}")
            return None

        payload_id = tool_info["id"]
        if payload_id == self.last_sent_id:
            logger.info(f"[detector] dedup: tool_use {payload_id[:25]} already fired")
            return None
        logger.info(f"[detector] returning perm: id={payload_id[:25]} name={tool_info['name']} target={tool_info['target'][:60]}")
        self.last_sent_id = payload_id

        return {
            "tool": tool_info["name"],
            "target": tool_info.get("target", ""),
            "context": "",
            "id": payload_id,
        }

    def clear(self):
        self.last_sent_id = None
        self._unresolved_cache = None


def _find_unresolved_tool_use(log_file: Path, max_bytes: int = 1_048_576) -> Optional[dict]:
    """Scan the last `max_bytes` of a Claude JSONL session log; return the
    most recent assistant tool_use whose tool_use_id does NOT have a
    corresponding user tool_result. Returns None if all tool_uses in the
    window are resolved (or none exist).

    Reads the *tail* of the file (default 1MB). For session logs that
    routinely fit within this window — i.e., every realistic case — the
    answer is exact. For pathologically long logs, may miss a tool_use
    older than the window; in that case, no auto-fire happens, the user
    gets a push notification, and they tap Allow.
    """
    try:
        fsize = log_file.stat().st_size
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            if fsize > max_bytes:
                f.seek(fsize - max_bytes)
                f.readline()  # drop the partial first line
            text = f.read()
    except Exception:
        return None

    tool_uses: dict = {}      # id → {id, name, target, order}
    resolved: set = set()     # tool_use_ids that have a tool_result
    order = 0

    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        order += 1
        msg_type = entry.get("type", "")
        content = entry.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        if msg_type == "assistant":
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tid = block.get("id", "")
                if not tid:
                    continue
                inp = block.get("input", {}) if isinstance(block.get("input"), dict) else {}
                target = (
                    inp.get("command")
                    or inp.get("file_path")
                    or inp.get("pattern")
                    or ""
                )
                tool_uses[tid] = {
                    "id": tid,
                    "name": block.get("name", ""),
                    "target": str(target)[:200],
                    "order": order,
                }
        elif msg_type == "user":
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                rid = block.get("tool_use_id", "")
                if rid:
                    resolved.add(rid)

    pending = [info for tid, info in tool_uses.items() if tid not in resolved]
    if not pending:
        return None
    pending.sort(key=lambda i: i["order"], reverse=True)
    return pending[0]


def _has_visible_permission_prompt(tmux_target: str) -> bool:
    """Confirm via tmux that a permission prompt is currently rendered.

    The capture window must be generous: Claude's permission box for Bash
    can run 15+ lines (header + command + question + options + footer) and
    the agent's status indicator above ("Embellishing… 3m 27s…") pushes
    the question line further up. -S -50 keeps the question in view even
    on slower-rendering hardware.

    Either of these visual signals counts as a permission prompt:
    - the literal "do you want to proceed?" question (Bash/Edit/Write
      style 3-option boxes)
    - a `❯ N.` numbered selector (also matches 2-option Yes/No boxes
      and AskUserQuestion variants where the question text differs).
    The session-feedback nag uses `1:` not `1.`, so the period in the
    regex keeps that out.
    """
    try:
        import subprocess
        title_result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", tmux_target, "#{pane_title}"],
            capture_output=True, text=True, timeout=2,
        )
        if "Signal Detection Pending" in (title_result.stdout or ""):
            return True
        capture_result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", tmux_target, "-S", "-50"],
            capture_output=True, text=True, timeout=2,
        )
        pane_text = capture_result.stdout or ""
        has_prompt = "do you want to proceed?" in pane_text.lower()
        # Selector must be on a single line: leading ws, then ❯/>, then
        # space, then digit-period-space. Without this, the input-box ❯
        # on one line + a numbered list on the next (Claude prose with
        # "2. Cache..." etc) falsely matches when joined by \n.
        has_selector = re.search(r"^[ \t]*[❯>][ \t]+\d+\.[ \t]+", pane_text, re.MULTILINE) is not None
        return has_prompt or has_selector
    except Exception:
        return False


class BacklogCandidateDetector:
    """Disabled by default — scanning produced more noise than signal.

    History:
    - v1 scanned ``TodoWrite`` and ``TaskCreate``. ``TodoWrite`` flooded
      the Suggestions tray (Claude rewrites it many times per session,
      5–15 items per call) so it was removed.
    - v2 scanned only ``TaskCreate``. In modern Claude Code (2026)
      ``TaskCreate`` has effectively replaced ``TodoWrite`` for ordinary
      planning — a single SecondBrain orchestration session was observed
      to invoke ``TaskCreate`` 413 times with 411 unique subjects, all
      sub-agent spawns that were *currently being executed*, not "things
      to remember later". The Suggestions tray filled up immediately.

    The detector is now a no-op: ``check_sync`` always returns ``[]``.
    The class, the JSONL position bookkeeping, and the public API are
    kept so callers (logs router, tail_sender) don't have to change, and
    so a future smarter signal can plug in here. ``CandidateStore`` is
    likewise kept in case it gets repurposed.

    To bring detection back, restore the body of ``_extract_candidates``
    below — start narrow (e.g. only items the agent explicitly tags as
    follow-up) rather than blanket tool scanning.
    """

    def __init__(self):
        self.last_log_size: int = 0
        self.log_file: Optional[Path] = None
        self._seen_hashes: set = set()

    def set_log_file(self, path: Optional[Path]):
        """Update log path. Reset position and seen hashes for new session."""
        self.log_file = Path(path) if path else None
        try:
            self.last_log_size = (
                self.log_file.stat().st_size
                if self.log_file and self.log_file.exists() else 0
            )
        except Exception:
            self.last_log_size = 0
        self._seen_hashes.clear()

    def check_sync(self, session: str, pane_id: str) -> list:
        """No-op. Detection is disabled — see class docstring.

        We still advance ``last_log_size`` so that if detection is ever
        re-enabled we don't suddenly process the entire backfill from
        whenever the server started.
        """
        if not self.log_file or not self.log_file.exists():
            return []
        try:
            self.last_log_size = self.log_file.stat().st_size
        except Exception:
            pass
        return []

    def _read_new(self, current_size: int) -> str:
        try:
            with open(self.log_file, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self.last_log_size)
                return f.read()
        except Exception:
            return ""

    def _extract_candidates(self, new_text: str) -> list:
        candidates = []
        for line in new_text.strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") != "assistant":
                    continue
                content = entry.get("message", {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "")
                    inp = block.get("input", {})

                    if name == "TaskCreate":
                        self._try_add(candidates, name, inp)
                    # TodoWrite intentionally skipped — see class docstring.
            except json.JSONDecodeError:
                continue
        return candidates

    def _try_add(self, candidates: list, source_tool: str, data: dict):
        """Extract summary/prompt from tool data and add if unseen."""
        subject = (data.get("subject") or "").strip()
        description = (data.get("description") or "").strip()
        if not subject:
            return

        summary = subject[:120]
        prompt = description or subject
        content_hash = hashlib.md5(summary.lower().encode()).hexdigest()

        if content_hash in self._seen_hashes:
            return
        self._seen_hashes.add(content_hash)

        candidates.append({
            "summary": summary,
            "prompt": prompt,
            "source_tool": source_tool,
            "hash": content_hash,
        })
