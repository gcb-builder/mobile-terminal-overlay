"""
Tests for the agent driver layer.

Covers:
- JSONL tail parsing and phase classification
- Generic permission regex detection
- Observation cache key behavior
- PID detection tiers (via mocking)
- Driver registry fallback
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
from mobile_terminal.drivers.codex import CodexDriver
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
        assert caps["has_jsonl_logs"] is False


# ---------------------------------------------------------------------------
# Start Command
# ---------------------------------------------------------------------------

class TestStartCommand:
    def test_claude_default(self):
        assert ClaudeDriver().start_command() == ["claude"]

    def test_codex_default(self):
        assert CodexDriver().start_command() == ["codex"]

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
