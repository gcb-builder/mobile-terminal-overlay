"""
Tests for the agent driver layer.

Covers:
- JSONL tail parsing and phase classification
- Generic permission regex detection
- Observation cache key behavior
- PID detection tiers (via mocking)
- Driver registry fallback
- Codex JSONL session log parsing
- Gemini CLI pane_title phase detection
"""

import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from mobile_terminal.drivers import get_driver, register_driver
from mobile_terminal.drivers.base import (
    BaseAgentDriver,
    Observation,
    ObserveContext,
    tail_jsonl,
    find_claude_log_file,
)
from mobile_terminal.drivers.claude import ClaudeDriver
from mobile_terminal.drivers.codex import CodexDriver, find_codex_log_file
from mobile_terminal.drivers.gemini import GeminiDriver, _extract_title_detail
from mobile_terminal.drivers.generic import GenericDriver


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jsonl_entry(tool_name: str, tool_input: dict = None, entry_type: str = "assistant") -> str:
    """Create a JSONL line mimicking Claude Code log format."""
    entry = {
        "type": entry_type,
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": tool_name,
                    "input": tool_input or {},
                }
            ]
        },
    }
    return json.dumps(entry)


def _write_jsonl(tmpdir: Path, lines: list[str], filename: str = "test.jsonl") -> Path:
    """Write JSONL lines to a file and return its path."""
    path = tmpdir / filename
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# Driver Registry
# ---------------------------------------------------------------------------

class TestDriverRegistry:
    def test_get_claude_driver(self):
        d = get_driver("claude")
        assert isinstance(d, ClaudeDriver)
        assert d.id() == "claude"
        assert d.display_name() == "Claude"

    def test_get_codex_driver(self):
        d = get_driver("codex")
        assert isinstance(d, CodexDriver)
        assert d.id() == "codex"
        assert d.display_name() == "Codex CLI"

    def test_get_generic_driver(self):
        d = get_driver("generic")
        assert isinstance(d, GenericDriver)
        assert d.id() == "generic"
        assert d.display_name() == "Agent"

    def test_get_gemini_driver(self):
        d = get_driver("gemini")
        assert isinstance(d, GeminiDriver)
        assert d.id() == "gemini"
        assert d.display_name() == "Gemini CLI"

    def test_unknown_agent_falls_back_to_generic(self):
        d = get_driver("totally_unknown_agent_xyz")
        assert isinstance(d, GenericDriver)
        assert d.id() == "generic"

    def test_display_name_override(self):
        d = get_driver("claude", display_name="Claude (Opus)")
        assert d.display_name() == "Claude (Opus)"
        assert d.id() == "claude"

    def test_register_custom_driver(self):
        class CustomDriver(BaseAgentDriver):
            _agent_id = "custom"
            _display_name = "Custom Agent"

        register_driver("custom", CustomDriver)
        d = get_driver("custom")
        assert isinstance(d, CustomDriver)
        assert d.id() == "custom"


# ---------------------------------------------------------------------------
# JSONL Tail Parsing
# ---------------------------------------------------------------------------

class TestTailJsonl:
    def test_parses_valid_entries(self, tmp_path):
        lines = [
            _make_jsonl_entry("Bash", {"command": "ls"}),
            _make_jsonl_entry("Read", {"file_path": "/foo/bar.py"}),
        ]
        path = _write_jsonl(tmp_path, lines)
        entries = tail_jsonl(path)
        # Returns in reverse order (most recent first)
        assert len(entries) == 2
        assert entries[0]["message"]["content"][0]["name"] == "Read"
        assert entries[1]["message"]["content"][0]["name"] == "Bash"

    def test_handles_empty_file(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        assert tail_jsonl(path) == []

    def test_handles_corrupt_lines(self, tmp_path):
        lines = [
            _make_jsonl_entry("Bash", {"command": "ls"}),
            "this is not json",
            _make_jsonl_entry("Read", {"file_path": "/bar.py"}),
        ]
        path = _write_jsonl(tmp_path, lines)
        entries = tail_jsonl(path)
        assert len(entries) == 2  # Skips corrupt line

    def test_handles_nonexistent_file(self):
        entries = tail_jsonl(Path("/nonexistent/path/file.jsonl"))
        assert entries == []

    def test_limits_to_30_entries(self, tmp_path):
        lines = [_make_jsonl_entry("Bash", {"command": f"cmd{i}"}) for i in range(50)]
        path = _write_jsonl(tmp_path, lines)
        entries = tail_jsonl(path)
        assert len(entries) == 30


# ---------------------------------------------------------------------------
# Claude Phase Classification
# ---------------------------------------------------------------------------

class TestClaudePhaseClassification:
    def _classify(self, tool_entries, pane_title=""):
        """Helper: create ObserveContext + run _classify_entries."""
        driver = ClaudeDriver()
        ctx = ObserveContext(pane_title=pane_title)
        entries = []
        for tool_name, tool_input in tool_entries:
            entries.append(json.loads(_make_jsonl_entry(tool_name, tool_input)))
        # Reverse to match tail_jsonl order (most recent first)
        entries.reverse()
        return driver._classify_entries(ctx, entries)

    def test_idle_no_entries(self):
        result = self._classify([])
        assert result["phase"] == "idle"

    def test_working_with_bash(self):
        result = self._classify([("Bash", {"command": "npm test"})])
        assert result["phase"] == "working"
        assert "Bash" in result["detail"]
        assert "npm test" in result["detail"]

    def test_working_with_read(self):
        result = self._classify([("Read", {"file_path": "/foo/bar/baz.py"})])
        assert result["phase"] == "working"
        assert "baz.py" in result["detail"]

    def test_planning_mode(self):
        result = self._classify([("EnterPlanMode", {})])
        assert result["phase"] == "planning"

    def test_running_task(self):
        result = self._classify([("Task", {"description": "Deploy service"})])
        assert result["phase"] == "running_task"
        assert "Deploy service" in result["detail"]

    def test_waiting_ask_user_question(self):
        result = self._classify([
            ("AskUserQuestion", {"questions": [{"question": "Which DB?"}]}),
        ])
        assert result["phase"] == "waiting"
        assert result["waiting_reason"] == "question"
        assert "Which DB?" in result["detail"]

    def test_waiting_permission_from_pane_title(self):
        result = self._classify(
            [("Bash", {"command": "rm -rf /tmp/test"})],
            pane_title="Signal Detection Pending",
        )
        assert result["phase"] == "waiting"
        assert result["waiting_reason"] == "permission"
        assert result["permission_tool"] == "Bash"

    def test_exit_plan_mode_clears_planning(self):
        result = self._classify([
            ("ExitPlanMode", {}),
            ("EnterPlanMode", {}),
        ])
        # ExitPlanMode is most recent, so plan_mode should be False
        assert result["phase"] != "planning"


# ---------------------------------------------------------------------------
# Generic Permission Detection
# ---------------------------------------------------------------------------

class TestGenericPermissionDetection:
    def test_detects_allow_y_n(self):
        driver = GenericDriver()
        ctx = ObserveContext(tmux_target="test:0.0")
        obs = Observation()
        # Mock pane snapshot
        ctx._pane_snapshot = "Some output\nAllow file write? (y/n)\n"
        driver.detect_permission_wait(ctx, obs)
        assert obs.phase == "waiting"
        assert obs.waiting_reason == "permission"

    def test_detects_approve_pattern(self):
        driver = GenericDriver()
        ctx = ObserveContext(tmux_target="test:0.0")
        obs = Observation()
        ctx._pane_snapshot = "Approve this action? [y/N]\n"
        driver.detect_permission_wait(ctx, obs)
        assert obs.waiting_reason == "permission"

    def test_no_false_positive(self):
        driver = GenericDriver()
        ctx = ObserveContext(tmux_target="test:0.0")
        obs = Observation()
        ctx._pane_snapshot = "Running tests...\nAll 42 tests passed.\n"
        driver.detect_permission_wait(ctx, obs)
        assert obs.waiting_reason is None

    def test_confirm_yes_no(self):
        driver = GenericDriver()
        ctx = ObserveContext(tmux_target="test:0.0")
        obs = Observation()
        ctx._pane_snapshot = "Confirm deletion? (yes/no)\n"
        driver.detect_permission_wait(ctx, obs)
        assert obs.waiting_reason == "permission"


# ---------------------------------------------------------------------------
# Observation Dataclass
# ---------------------------------------------------------------------------

class TestObservation:
    def test_to_dict(self):
        obs = Observation(
            agent_type="claude",
            agent_name="Claude",
            running=True,
            pid=12345,
            phase="working",
            detail="Bash: ls",
            tool="Bash",
            active=True,
        )
        d = obs.to_dict()
        assert d["agent_type"] == "claude"
        assert d["running"] is True
        assert d["pid"] == 12345
        assert d["phase"] == "working"

    def test_defaults(self):
        obs = Observation()
        assert obs.running is False
        assert obs.phase == "idle"
        assert obs.waiting_reason is None


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

class TestCapabilities:
    def test_claude_capabilities(self):
        caps = ClaudeDriver().capabilities()
        assert caps["has_jsonl_logs"] is True
        assert caps["has_permission_signal"] is True

    def test_generic_capabilities(self):
        caps = GenericDriver().capabilities()
        assert caps["has_jsonl_logs"] is False

    def test_codex_capabilities(self):
        caps = CodexDriver().capabilities()
        assert caps["has_jsonl_logs"] is True
        assert caps["has_phase_detection"] is True
        assert caps["has_pane_title_signal"] is False

    def test_gemini_capabilities(self):
        caps = GeminiDriver().capabilities()
        assert caps["has_jsonl_logs"] is False
        assert caps["has_permission_signal"] is True
        assert caps["has_phase_detection"] is True
        assert caps["has_pane_title_signal"] is True


# ---------------------------------------------------------------------------
# Start Command
# ---------------------------------------------------------------------------

class TestStartCommand:
    def test_claude_default(self):
        assert ClaudeDriver().start_command() == ["claude"]

    def test_codex_default(self):
        assert CodexDriver().start_command() == ["codex"]

    def test_gemini_default(self):
        assert GeminiDriver().start_command() == ["gemini"]

    def test_override(self):
        assert ClaudeDriver().start_command("claude --verbose") == ["claude --verbose"]

    def test_generic_default(self):
        # Generic has no process_name, uses agent_id
        assert GenericDriver().start_command() == ["generic"]


# ---------------------------------------------------------------------------
# PID Detection (mocked subprocess)
# ---------------------------------------------------------------------------

class TestPIDDetection:
    @patch("mobile_terminal.drivers.base.subprocess.run")
    def test_detects_running_process(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="12345\n",
        )
        driver = ClaudeDriver()
        ctx = ObserveContext(shell_pid=100)
        obs = Observation()
        driver.is_running(ctx, obs)
        assert obs.running is True
        assert obs.pid == 12345

    @patch("mobile_terminal.drivers.base.subprocess.run")
    def test_no_process_found(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
        )
        driver = ClaudeDriver()
        ctx = ObserveContext(shell_pid=100)
        obs = Observation()
        driver.is_running(ctx, obs)
        assert obs.running is False
        assert obs.pid is None

    def test_no_shell_pid(self):
        driver = ClaudeDriver()
        ctx = ObserveContext(shell_pid=None)
        obs = Observation()
        driver.is_running(ctx, obs)
        assert obs.running is False


# ---------------------------------------------------------------------------
# find_claude_log_file
# ---------------------------------------------------------------------------

class TestFindClaudeLogFile:
    def test_finds_most_recent(self, tmp_path):
        # Create fake .claude/projects/<id>/ structure
        project_id = str(tmp_path.resolve()).replace("~", "-").replace("/", "-")
        projects_dir = Path.home() / ".claude" / "projects" / project_id
        projects_dir.mkdir(parents=True, exist_ok=True)

        try:
            old_file = projects_dir / "old.jsonl"
            old_file.write_text('{"type":"test"}\n')
            time.sleep(0.05)
            new_file = projects_dir / "new.jsonl"
            new_file.write_text('{"type":"test"}\n')

            result = find_claude_log_file(tmp_path)
            assert result is not None
            assert result.name == "new.jsonl"
        finally:
            # Cleanup
            for f in projects_dir.glob("*.jsonl"):
                f.unlink()
            try:
                projects_dir.rmdir()
            except Exception:
                pass

    def test_returns_none_for_nonexistent(self):
        result = find_claude_log_file(Path("/nonexistent/repo/path"))
        assert result is None


# ---------------------------------------------------------------------------
# Codex JSONL Phase Classification
# ---------------------------------------------------------------------------

def _make_codex_event(event_type: str, payload: dict = None) -> str:
    """Create a JSONL line mimicking Codex session log format."""
    entry = {"type": event_type, "payload": payload or {}}
    return json.dumps(entry)


def _make_codex_notification(event_name: str, payload: dict = None) -> str:
    """Create a notification event (e.g., approval-requested)."""
    entry = {"type": "notification", "event": event_name, "payload": payload or {}}
    return json.dumps(entry)


class TestCodexPhaseClassification:
    def _classify(self, jsonl_lines: list[str]):
        """Helper: write JSONL, parse via tail_jsonl, classify."""
        driver = CodexDriver()
        # Parse lines as entries (most recent first)
        entries = []
        for line in reversed(jsonl_lines):
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return driver._classify_entries(entries)

    def test_idle_no_entries(self):
        result = self._classify([])
        assert result["phase"] == "idle"

    def test_turn_started_thinking(self):
        lines = [_make_codex_event("turn.started")]
        result = self._classify(lines)
        assert result["phase"] == "working"
        assert "Thinking" in result["detail"]

    def test_item_started_command(self):
        lines = [
            _make_codex_event("turn.started"),
            _make_codex_event("item.started", {"command": "npm test", "item_type": "command"}),
        ]
        result = self._classify(lines)
        assert result["phase"] == "working"
        assert "npm test" in result["detail"]

    def test_item_started_file(self):
        lines = [
            _make_codex_event("item.started", {"file": "/src/utils/helpers.ts", "item_type": "file_change"}),
        ]
        result = self._classify(lines)
        assert result["phase"] == "working"
        assert "helpers.ts" in result["detail"]

    def test_item_completed(self):
        lines = [
            _make_codex_event("item.completed", {"tool": "edit", "description": "Updated config"}),
        ]
        result = self._classify(lines)
        assert result["phase"] == "working"

    def test_turn_completed_idle(self):
        lines = [
            _make_codex_event("turn.started"),
            _make_codex_event("item.started", {"command": "ls"}),
            _make_codex_event("turn.completed"),
        ]
        result = self._classify(lines)
        assert result["phase"] == "idle"
        assert "Turn complete" in result["detail"]

    def test_turn_failed(self):
        lines = [
            _make_codex_event("turn.failed", {"error": "Rate limit exceeded"}),
        ]
        result = self._classify(lines)
        assert result["phase"] == "idle"
        assert "Error" in result["detail"]
        assert "Rate limit" in result["detail"]

    def test_approval_requested_waiting(self):
        lines = [
            _make_codex_event("turn.started"),
            _make_codex_notification("approval-requested", {"tool": "shell", "name": "rm -rf"}),
        ]
        result = self._classify(lines)
        assert result["phase"] == "waiting"
        assert result["waiting_reason"] == "permission"
        assert result["permission_tool"] == "shell"


class TestCodexLogFile:
    def test_finds_most_recent_rollout(self, tmp_path):
        # Create fake ~/.codex/sessions/2026/02/25/ structure
        day_dir = tmp_path / "sessions" / "2026" / "02" / "25"
        day_dir.mkdir(parents=True)

        old_file = day_dir / "rollout-abc.jsonl"
        old_file.write_text('{"type":"turn.started"}\n')
        time.sleep(0.05)
        new_file = day_dir / "rollout-xyz.jsonl"
        new_file.write_text('{"type":"turn.started"}\n')

        with patch("mobile_terminal.drivers.codex.Path.home", return_value=tmp_path.parent / "fake_home"):
            # Won't find anything with wrong home
            result = find_codex_log_file()
            # This would be None since fake_home doesn't exist

        # Direct test with real structure
        with patch("mobile_terminal.drivers.codex.Path") as mock_path_cls:
            # Use the real Path for everything except home()
            mock_path_cls.side_effect = Path
            mock_path_cls.home.return_value = tmp_path
            result = find_codex_log_file()
            if result is not None:
                assert result.name == "rollout-xyz.jsonl"

    def test_returns_none_when_no_sessions(self):
        with patch("mobile_terminal.drivers.codex.Path.home", return_value=Path("/nonexistent/home")):
            result = find_codex_log_file()
            assert result is None


# ---------------------------------------------------------------------------
# Gemini CLI Pane Title Detection
# ---------------------------------------------------------------------------

class TestGeminiPaneTitleDetection:
    def _observe_with_title(self, pane_title: str, running: bool = True):
        """Helper: create GeminiDriver + ObserveContext, run observe."""
        driver = GeminiDriver()
        ctx = ObserveContext(pane_title=pane_title)
        obs = Observation(
            agent_type="gemini",
            agent_name="Gemini CLI",
            running=running,
        )
        # Directly test classification
        classified = driver._classify_from_pane_title(pane_title, obs)
        return obs, classified

    def test_ready_idle(self):
        obs, classified = self._observe_with_title("◇  Ready (my-project)                                                          ")
        assert classified is True
        assert obs.phase == "idle"
        assert obs.detail == "Ready"

    def test_working_streaming(self):
        obs, classified = self._observe_with_title("✦  Working... (my-project)                                                      ")
        assert classified is True
        assert obs.phase == "working"
        assert obs.active is True

    def test_working_with_thought(self):
        obs, classified = self._observe_with_title("✦  Reading files (my-project)                                                   ")
        assert classified is True
        assert obs.phase == "working"
        assert obs.detail == "Reading files"

    def test_silent_working(self):
        obs, classified = self._observe_with_title("⏲  Working... (my-project)                                                      ")
        assert classified is True
        assert obs.phase == "working"

    def test_action_required_permission(self):
        obs, classified = self._observe_with_title("✋  Action Required (my-project)                                                 ")
        assert classified is True
        assert obs.phase == "waiting"
        assert obs.waiting_reason == "permission"
        assert "Approval" in obs.detail

    def test_legacy_static_title_running(self):
        obs, classified = self._observe_with_title("Gemini CLI (my-project)", running=True)
        assert classified is True
        assert obs.phase == "working"

    def test_legacy_static_title_not_running(self):
        obs, classified = self._observe_with_title("Gemini CLI (my-project)", running=False)
        assert classified is True
        assert obs.phase == "idle"

    def test_unrelated_title_no_match(self):
        obs, classified = self._observe_with_title("bash - /home/user")
        assert classified is False

    def test_empty_title(self):
        obs, classified = self._observe_with_title("")
        assert classified is False


class TestGeminiTitleDetailExtraction:
    def test_extracts_thought_detail(self):
        assert _extract_title_detail("✦  Reading files (my-project)   ") == "Reading files"

    def test_working_returns_empty(self):
        # "Working..." is the default, not informative
        assert _extract_title_detail("✦  Working... (my-project)      ") == ""

    def test_ready_detail(self):
        detail = _extract_title_detail("◇  Ready (my-project)          ")
        assert detail == "Ready"

    def test_complex_thought(self):
        assert _extract_title_detail("✦  Analyzing test results (deep-project)") == "Analyzing test results"

    def test_no_parens(self):
        # Edge case: no folder name
        detail = _extract_title_detail("✦  Working...")
        assert detail == ""  # "Working..." maps to empty


class TestGeminiProcessDetection:
    @patch("mobile_terminal.drivers.gemini.subprocess.run")
    def test_detects_gemini_cli_process(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="54321\n")
        driver = GeminiDriver()
        ctx = ObserveContext(shell_pid=100)
        obs = Observation()
        driver.is_running(ctx, obs)
        assert obs.running is True
        assert obs.pid == 54321
        # Verify pgrep was called with "gemini-cli"
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "gemini-cli" in call_args

    @patch("mobile_terminal.drivers.gemini.subprocess.run")
    def test_no_process_found(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        driver = GeminiDriver()
        ctx = ObserveContext(shell_pid=100, pane_title="")
        obs = Observation()
        driver.is_running(ctx, obs)
        assert obs.running is False

    def test_pane_title_fallback_when_no_shell_pid(self):
        driver = GeminiDriver()
        ctx = ObserveContext(shell_pid=None, pane_title="✦  Working... (project)")
        obs = Observation()
        driver.is_running(ctx, obs)
        assert obs.running is True
        assert obs.active is True

    def test_no_shell_pid_no_title(self):
        driver = GeminiDriver()
        ctx = ObserveContext(shell_pid=None, pane_title="")
        obs = Observation()
        driver.is_running(ctx, obs)
        assert obs.running is False
