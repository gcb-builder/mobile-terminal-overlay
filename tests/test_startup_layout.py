"""Tests for ensure_startup_layout — idempotent window restoration.

Each test mocks the small set of helpers used by the function:
- ``_tmux_session_exists`` (the session must exist for the function to act)
- ``_list_session_windows`` (controls which windows appear "already there")
- ``run_subprocess`` (captures the tmux new-window / send-keys calls)

Plain asyncio.run-based tests so they work without pytest-asyncio.
"""

import asyncio
from unittest.mock import patch, AsyncMock, MagicMock

from mobile_terminal.config import Config, StartupWindow
from mobile_terminal import helpers


def _config_with_layout(layout):
    c = Config()
    c.session_name = "test-session"
    c.startup_layout = layout
    return c


def _proc_ok(returncode=0, stderr=""):
    p = MagicMock()
    p.returncode = returncode
    p.stderr = stderr
    return p


def _run(coro):
    return asyncio.run(coro)


def test_no_layout_is_noop():
    c = _config_with_layout([])
    result = _run(helpers.ensure_startup_layout(c))
    assert result == {"created": [], "skipped": [], "resumed": [], "errors": []}


def test_creates_missing_windows():
    c = _config_with_layout([
        StartupWindow(window_name="alpha", path="/tmp", auto_resume=False),
        StartupWindow(window_name="beta", path="/tmp", auto_resume=False),
    ])
    with patch.object(helpers, "_tmux_session_exists", return_value=True), \
         patch.object(helpers, "_list_session_windows", return_value=[]), \
         patch.object(helpers, "run_subprocess",
                      new_callable=AsyncMock, return_value=_proc_ok()):
        result = _run(helpers.ensure_startup_layout(c))
    assert result["created"] == ["alpha", "beta"]
    assert result["skipped"] == []
    assert result["resumed"] == []


def test_idempotent_skips_existing():
    c = _config_with_layout([
        StartupWindow(window_name="alpha", path="/tmp"),
        StartupWindow(window_name="beta", path="/tmp"),
    ])
    existing = [
        {"window_index": "0", "window_name": "alpha", "pane_id": "%0", "cwd": "/tmp"},
        {"window_index": "1", "window_name": "beta", "pane_id": "%1", "cwd": "/tmp"},
    ]
    with patch.object(helpers, "_tmux_session_exists", return_value=True), \
         patch.object(helpers, "_list_session_windows", return_value=existing), \
         patch.object(helpers, "run_subprocess",
                      new_callable=AsyncMock) as ruls:
        result = _run(helpers.ensure_startup_layout(c))
    assert result["created"] == []
    assert result["skipped"] == ["alpha", "beta"]
    ruls.assert_not_called()  # no tmux new-window invocations


def test_partial_create():
    c = _config_with_layout([
        StartupWindow(window_name="alpha", path="/tmp"),
        StartupWindow(window_name="beta", path="/tmp"),
        StartupWindow(window_name="gamma", path="/tmp"),
    ])
    existing = [
        {"window_index": "0", "window_name": "alpha", "pane_id": "%0", "cwd": "/tmp"},
    ]
    with patch.object(helpers, "_tmux_session_exists", return_value=True), \
         patch.object(helpers, "_list_session_windows", return_value=existing), \
         patch.object(helpers, "run_subprocess",
                      new_callable=AsyncMock, return_value=_proc_ok()):
        result = _run(helpers.ensure_startup_layout(c))
    assert result["created"] == ["beta", "gamma"]
    assert result["skipped"] == ["alpha"]


def test_auto_resume_only_for_new_windows():
    c = _config_with_layout([
        StartupWindow(window_name="alpha", path="/tmp", auto_resume=True),
        StartupWindow(window_name="beta", path="/tmp", auto_resume=True),
    ])
    existing = [
        {"window_index": "0", "window_name": "alpha", "pane_id": "%0", "cwd": "/tmp"},
    ]
    with patch.object(helpers, "_tmux_session_exists", return_value=True), \
         patch.object(helpers, "_list_session_windows", return_value=existing), \
         patch.object(helpers, "run_subprocess",
                      new_callable=AsyncMock, return_value=_proc_ok()):
        result = _run(helpers.ensure_startup_layout(c))
    # Only the newly-created beta should be in the resumed list;
    # alpha was already running and is left alone.
    assert result["created"] == ["beta"]
    assert result["resumed"] == ["beta"]


def test_missing_path_is_error_not_create():
    c = _config_with_layout([
        StartupWindow(window_name="alpha", path="/this/path/definitely/does/not/exist"),
    ])
    with patch.object(helpers, "_tmux_session_exists", return_value=True), \
         patch.object(helpers, "_list_session_windows", return_value=[]), \
         patch.object(helpers, "run_subprocess",
                      new_callable=AsyncMock, return_value=_proc_ok()) as ruls:
        result = _run(helpers.ensure_startup_layout(c))
    assert result["created"] == []
    assert any("path missing" in e for e in result["errors"])
    ruls.assert_not_called()


def test_session_missing_is_error():
    c = _config_with_layout([StartupWindow(window_name="a", path="/tmp")])
    with patch.object(helpers, "_tmux_session_exists", return_value=False):
        result = _run(helpers.ensure_startup_layout(c))
    assert result["created"] == []
    assert any("does not exist" in e for e in result["errors"])
