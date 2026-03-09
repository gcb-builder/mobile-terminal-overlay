"""Tests for tool output compression (Wave 1).

Covers _summarize_tool_result(), JSONL parser tool_use/tool_result pairing,
and the /api/log/tool-output endpoint.
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from mobile_terminal.routers.logs import _summarize_tool_result


# ---------------------------------------------------------------------------
# _summarize_tool_result
# ---------------------------------------------------------------------------

class TestSummarizeToolResult:
    def test_bash_ok_with_output(self):
        assert _summarize_tool_result("Bash", "line1\nline2\nline3", False) == "OK 3L"

    def test_bash_ok_empty(self):
        assert _summarize_tool_result("Bash", "", False) == "OK"

    def test_bash_error(self):
        result = _summarize_tool_result("Bash", "Error: command not found\nmore details", True)
        assert result.startswith("ERR: ")
        assert "command not found" in result

    def test_bash_error_empty(self):
        assert _summarize_tool_result("Bash", "", True) == "ERR"

    def test_read_line_count(self):
        content = "\n".join(f"line {i}" for i in range(100))
        assert _summarize_tool_result("Read", content, False) == "100L"

    def test_read_empty(self):
        assert _summarize_tool_result("Read", "", False) == "0L"

    def test_edit_ok(self):
        assert _summarize_tool_result("Edit", "The file was updated", False) == "OK"

    def test_write_ok(self):
        assert _summarize_tool_result("Write", "File created", False) == "OK"

    def test_glob_files(self):
        content = "src/a.py\nsrc/b.py\nsrc/c.py"
        assert _summarize_tool_result("Glob", content, False) == "3 files"

    def test_glob_single(self):
        assert _summarize_tool_result("Glob", "src/a.py", False) == "1 file"

    def test_glob_empty(self):
        assert _summarize_tool_result("Glob", "", False) == "0 files"

    def test_grep_with_matches(self):
        content = "file1.py\n  line1\n  line2\nfile2.py\n  line3"
        result = _summarize_tool_result("Grep", content, False)
        assert "F" in result
        assert "L" in result

    def test_grep_no_matches(self):
        assert _summarize_tool_result("Grep", "", False) == "0 matches"

    def test_error_with_ansi(self):
        """ANSI escape sequences should be stripped from error summaries."""
        ansi_text = "\x1b[31mError: test failed\x1b[0m\nmore"
        result = _summarize_tool_result("Bash", ansi_text, True)
        assert "\x1b" not in result
        assert "test failed" in result

    def test_error_skips_empty_lines(self):
        result = _summarize_tool_result("Bash", "\n\n  \nActual error here", True)
        assert result == "ERR: Actual error here"

    def test_list_content(self):
        """tool_result content can be a list of {type: text, text: ...}."""
        content = [{"type": "text", "text": "line1\nline2"}]
        assert _summarize_tool_result("Bash", content, False) == "OK 2L"

    def test_unknown_tool(self):
        assert _summarize_tool_result("Agent", "some\noutput", False) == "2L"

    def test_unknown_tool_empty(self):
        assert _summarize_tool_result("Agent", "", False) == "OK"


# ---------------------------------------------------------------------------
# JSONL parser: tool_use / tool_result pairing
# ---------------------------------------------------------------------------

def _make_jsonl(*entries):
    """Build a JSONL string from dicts."""
    return "\n".join(json.dumps(e) for e in entries)


def _make_tool_use_entry(tool_name, tool_id, tool_input):
    """Make an assistant message with a tool_use block."""
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": tool_name,
                    "input": tool_input,
                }
            ]
        },
    }


def _make_tool_result_entry(tool_use_id, content, is_error=False):
    """Make a user message with a tool_result block."""
    return {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                    "is_error": is_error,
                }
            ]
        },
    }


class TestJsonlParserPairing:
    """Test that the JSONL parser pairs tool_use with tool_result into structured messages."""

    def _parse_messages(self, jsonl_content):
        """Parse JSONL content through the same logic as the /api/log endpoint.

        Extracts the core parsing loop from logs.py to test in isolation.
        """
        import re
        lines_list = jsonl_content.strip().split("\n")
        conversation = []
        pending_tool_uses = {}

        for line in lines_list:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                msg_type = entry.get("type")
                message = entry.get("message", {})

                if msg_type == "user":
                    content = message.get("content", "")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_result":
                                tool_use_id = block.get("tool_use_id", "")
                                if tool_use_id and tool_use_id in pending_tool_uses:
                                    conv_idx, tool_name, tool_detail = pending_tool_uses.pop(tool_use_id)
                                    result_content = block.get("content", "")
                                    is_error = block.get("is_error", False)
                                    summary = _summarize_tool_result(tool_name, result_content, is_error)
                                    orig = conversation[conv_idx]
                                    text = orig["text"] if isinstance(orig, dict) else orig
                                    conversation[conv_idx] = {
                                        "text": text,
                                        "tool": {
                                            "name": tool_name,
                                            "detail": tool_detail,
                                            "tool_use_id": tool_use_id,
                                            "result_summary": summary,
                                            "result_status": "error" if is_error else "ok",
                                        },
                                    }
                    elif isinstance(content, str) and content.strip():
                        cleaned = re.sub(
                            r"<(?:system-reminder|task-notification)[^>]*>[\s\S]*?</(?:system-reminder|task-notification)>",
                            "",
                            content,
                        ).strip()
                        if cleaned:
                            conversation.append(f"$ {cleaned}")

                elif msg_type == "assistant":
                    content = message.get("content", [])
                    if isinstance(content, str):
                        conversation.append(content)
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                if block.get("type") == "text":
                                    text = block.get("text", "")
                                    if text.strip():
                                        conversation.append(text)
                                elif block.get("type") == "tool_use":
                                    tool_name = block.get("name", "tool")
                                    tool_id = block.get("id", "")
                                    tool_input = block.get("input", {})
                                    tool_detail = ""
                                    if tool_name == "Bash":
                                        tool_detail = tool_input.get("command", "")[:200]
                                        conversation.append(f"• Bash: {tool_detail}")
                                    elif tool_name in ("Read", "Edit", "Write", "Glob", "Grep"):
                                        tool_detail = (
                                            tool_input.get("file_path")
                                            or tool_input.get("path")
                                            or tool_input.get("pattern", "")
                                        )[:100]
                                        conversation.append(f"• {tool_name}: {tool_detail}")
                                    else:
                                        conversation.append(f"• {tool_name}")
                                    if tool_id:
                                        pending_tool_uses[tool_id] = (len(conversation) - 1, tool_name, tool_detail)
            except json.JSONDecodeError:
                continue

        return conversation

    def test_bash_paired(self):
        jsonl = _make_jsonl(
            _make_tool_use_entry("Bash", "toolu_001", {"command": "ls -la"}),
            _make_tool_result_entry("toolu_001", "file1.txt\nfile2.txt"),
        )
        msgs = self._parse_messages(jsonl)
        assert len(msgs) == 1
        assert isinstance(msgs[0], dict)
        assert msgs[0]["text"] == "• Bash: ls -la"
        assert msgs[0]["tool"]["name"] == "Bash"
        assert msgs[0]["tool"]["tool_use_id"] == "toolu_001"
        assert msgs[0]["tool"]["result_summary"] == "OK 2L"
        assert msgs[0]["tool"]["result_status"] == "ok"

    def test_read_paired(self):
        jsonl = _make_jsonl(
            _make_tool_use_entry("Read", "toolu_002", {"file_path": "/tmp/test.py"}),
            _make_tool_result_entry("toolu_002", "\n".join(f"line{i}" for i in range(50))),
        )
        msgs = self._parse_messages(jsonl)
        assert isinstance(msgs[0], dict)
        assert msgs[0]["tool"]["result_summary"] == "50L"

    def test_error_result(self):
        jsonl = _make_jsonl(
            _make_tool_use_entry("Bash", "toolu_003", {"command": "npm test"}),
            _make_tool_result_entry("toolu_003", "FAIL: 3 tests failed", is_error=True),
        )
        msgs = self._parse_messages(jsonl)
        assert msgs[0]["tool"]["result_status"] == "error"
        assert msgs[0]["tool"]["result_summary"].startswith("ERR:")

    def test_no_result_stays_string(self):
        """tool_use without matching tool_result stays as plain string."""
        jsonl = _make_jsonl(
            _make_tool_use_entry("Bash", "toolu_004", {"command": "echo hello"}),
        )
        msgs = self._parse_messages(jsonl)
        assert len(msgs) == 1
        assert isinstance(msgs[0], str)
        assert msgs[0] == "• Bash: echo hello"

    def test_multiple_tools_paired(self):
        jsonl = _make_jsonl(
            _make_tool_use_entry("Bash", "toolu_010", {"command": "ls"}),
            _make_tool_use_entry("Read", "toolu_011", {"file_path": "/tmp/a.py"}),
            _make_tool_result_entry("toolu_010", "file1\nfile2"),
            _make_tool_result_entry("toolu_011", "content here"),
        )
        msgs = self._parse_messages(jsonl)
        assert len(msgs) == 2
        assert isinstance(msgs[0], dict)
        assert msgs[0]["tool"]["name"] == "Bash"
        assert isinstance(msgs[1], dict)
        assert msgs[1]["tool"]["name"] == "Read"

    def test_interleaved_text_and_tools(self):
        jsonl = _make_jsonl(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Let me check."}]}},
            _make_tool_use_entry("Bash", "toolu_020", {"command": "pwd"}),
            _make_tool_result_entry("toolu_020", "/home/user"),
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}},
        )
        msgs = self._parse_messages(jsonl)
        assert msgs[0] == "Let me check."
        assert isinstance(msgs[1], dict)
        assert msgs[1]["tool"]["result_summary"] == "OK 1L"
        assert msgs[2] == "Done."

    def test_duplicate_result_ignored(self):
        """Second tool_result for same id is ignored (already popped from pending)."""
        jsonl = _make_jsonl(
            _make_tool_use_entry("Bash", "toolu_030", {"command": "ls"}),
            _make_tool_result_entry("toolu_030", "file1"),
            _make_tool_result_entry("toolu_030", "file2"),  # duplicate
        )
        msgs = self._parse_messages(jsonl)
        assert len(msgs) == 1
        assert msgs[0]["tool"]["result_summary"] == "OK 1L"  # first result kept


# ---------------------------------------------------------------------------
# Tool output cache (helpers.py)
# ---------------------------------------------------------------------------

from mobile_terminal.helpers import (
    get_cached_tool_output,
    set_cached_tool_output,
    _tool_output_cache,
    _tool_output_order,
)


class TestToolOutputCache:
    def setup_method(self):
        _tool_output_cache.clear()
        _tool_output_order.clear()

    def test_set_and_get(self):
        result = {"content": "hello", "is_error": False}
        set_cached_tool_output("/tmp/log.jsonl", 1000.0, "toolu_abc", result)
        got = get_cached_tool_output("/tmp/log.jsonl", 1000.0, "toolu_abc")
        assert got == result

    def test_miss_on_different_mtime(self):
        result = {"content": "hello"}
        set_cached_tool_output("/tmp/log.jsonl", 1000.0, "toolu_abc", result)
        got = get_cached_tool_output("/tmp/log.jsonl", 1001.0, "toolu_abc")
        assert got is None

    def test_miss_on_different_id(self):
        result = {"content": "hello"}
        set_cached_tool_output("/tmp/log.jsonl", 1000.0, "toolu_abc", result)
        got = get_cached_tool_output("/tmp/log.jsonl", 1000.0, "toolu_xyz")
        assert got is None

    def test_lru_eviction(self):
        from mobile_terminal.helpers import TOOL_OUTPUT_CACHE_MAX
        for i in range(TOOL_OUTPUT_CACHE_MAX + 10):
            set_cached_tool_output("/tmp/log.jsonl", 1000.0, f"toolu_{i}", {"i": i})
        # First entries should be evicted
        assert get_cached_tool_output("/tmp/log.jsonl", 1000.0, "toolu_0") is None
        # Last entries should still be there
        last_id = f"toolu_{TOOL_OUTPUT_CACHE_MAX + 9}"
        assert get_cached_tool_output("/tmp/log.jsonl", 1000.0, last_id) is not None
