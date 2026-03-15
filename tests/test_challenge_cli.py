"""Tests for CLI-based challenge review providers."""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from mobile_terminal.challenge import (
    get_available_models,
    call_cli,
    _parse_cli_output,
)


# ── Model Detection ──────────────────────────────────────────────────────


class TestCLIModelDetection:

    def test_codex_available_when_on_path(self):
        with patch("mobile_terminal.challenge.shutil.which",
                    side_effect=lambda x: "/usr/bin/codex" if x == "codex" else None):
            models = get_available_models()
            keys = [m["key"] for m in models]
            assert "codex-local" in keys

    def test_codex_unavailable_when_not_on_path(self):
        with patch("mobile_terminal.challenge.shutil.which", return_value=None):
            with patch("mobile_terminal.challenge.get_api_key", return_value=None):
                models = get_available_models()
                keys = [m["key"] for m in models]
                assert "codex-local" not in keys

    def test_gemini_available_when_on_path(self):
        with patch("mobile_terminal.challenge.shutil.which",
                    side_effect=lambda x: "/usr/bin/gemini" if x == "gemini" else None):
            models = get_available_models()
            keys = [m["key"] for m in models]
            assert "gemini-local" in keys

    def test_model_metadata_includes_local_flag(self):
        with patch("mobile_terminal.challenge.shutil.which",
                    side_effect=lambda x: "/usr/bin/codex" if x == "codex" else None):
            models = get_available_models()
            codex = next((m for m in models if m["key"] == "codex-local"), None)
            assert codex is not None
            assert codex["local"] is True
            assert codex["mode"] == "prompt"
            assert codex["provider"] == "codex_cli"

    def test_api_models_have_local_false(self):
        with patch("mobile_terminal.challenge.shutil.which", return_value=None):
            with patch("mobile_terminal.challenge.get_api_key", return_value="sk-test-key-1234567890abcdef"):
                with patch("mobile_terminal.challenge.validate_api_key", return_value=(True, "")):
                    models = get_available_models()
                    api_models = [m for m in models if not m.get("local")]
                    if api_models:
                        assert api_models[0]["local"] is False
                        assert api_models[0]["mode"] == "api"


# ── Output Parsing ───────────────────────────────────────────────────────


class TestParseCLIOutput:

    def test_parse_codex_jsonl_content_field(self):
        output = '{"type":"progress"}\n{"content":"Risk: missing null check"}\n'
        result = _parse_cli_output("codex_cli", output)
        assert "missing null check" in result

    def test_parse_codex_jsonl_message_field(self):
        output = '{"message":"Found race condition"}\n'
        result = _parse_cli_output("codex_cli", output)
        assert "race condition" in result

    def test_parse_codex_jsonl_text_field(self):
        output = '{"text":"Review complete"}\n'
        result = _parse_cli_output("codex_cli", output)
        assert "Review complete" in result

    def test_parse_codex_fallback_raw(self):
        result = _parse_cli_output("codex_cli", "just plain text output")
        assert result == "just plain text output"

    def test_parse_codex_empty_returns_none(self):
        result = _parse_cli_output("codex_cli", "")
        assert result is None

    def test_parse_gemini_json(self):
        output = json.dumps({"response": "Risk: race condition", "stats": {}, "error": None})
        result = _parse_cli_output("gemini_cli", output)
        assert "race condition" in result

    def test_parse_gemini_error_returns_none(self):
        output = json.dumps({"response": "", "error": "API quota exceeded"})
        result = _parse_cli_output("gemini_cli", output)
        assert result is None

    def test_parse_unknown_provider_returns_raw(self):
        result = _parse_cli_output("unknown_cli", "raw output here")
        assert result == "raw output here"


# ── call_cli ─────────────────────────────────────────────────────────────


class TestCallCLI:

    @pytest.mark.asyncio
    async def test_binary_not_found(self):
        with patch("mobile_terminal.challenge.shutil.which", return_value=None):
            result = await call_cli("codex_cli", "test prompt", Path("/tmp"))
            assert result["success"] is False
            assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_unknown_provider(self):
        result = await call_cli("nonexistent_provider", "test", Path("/tmp"))
        assert result["success"] is False
        assert "Unknown provider" in result["error"]

    @pytest.mark.asyncio
    async def test_success_codex(self):
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(b'{"content":"Looks good, no issues found"}\n', b"")
        )

        with patch("mobile_terminal.challenge.shutil.which", return_value="/usr/bin/codex"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                result = await call_cli("codex_cli", "test prompt", Path("/tmp"), "codex-local")
                assert result["success"] is True
                assert "Looks good" in result["content"]
                assert result["local"] is True
                assert result["provider"] == "codex_cli"

    @pytest.mark.asyncio
    async def test_success_gemini(self):
        response = json.dumps({"response": "Code looks correct", "error": None})
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(response.encode(), b"")
        )

        with patch("mobile_terminal.challenge.shutil.which", return_value="/usr/bin/gemini"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                result = await call_cli("gemini_cli", "test prompt", Path("/tmp"), "gemini-local")
                assert result["success"] is True
                assert "looks correct" in result["content"]

    @pytest.mark.asyncio
    async def test_nonzero_exit(self):
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(
            return_value=(b"", b"Error: auth failed")
        )

        with patch("mobile_terminal.challenge.shutil.which", return_value="/usr/bin/codex"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                result = await call_cli("codex_cli", "test", Path("/tmp"))
                assert result["success"] is False
                assert "auth failed" in result["error"]

    @pytest.mark.asyncio
    async def test_timeout(self):
        mock_proc = AsyncMock()
        mock_proc.kill = AsyncMock()
        mock_proc.wait = AsyncMock()

        async def slow_communicate(input=None):
            await asyncio.sleep(999)

        mock_proc.communicate = slow_communicate

        with patch("mobile_terminal.challenge.shutil.which", return_value="/usr/bin/codex"):
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                with patch("mobile_terminal.challenge.CLI_OVERALL_TIMEOUT", 0.01):
                    result = await call_cli("codex_cli", "test", Path("/tmp"))
                    assert result["success"] is False
                    assert "timed out" in result["error"]
