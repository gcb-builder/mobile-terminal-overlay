"""Tests for ProcessRuntime Protocol and TmuxRuntime implementation."""

import asyncio
import os
import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mobile_terminal.runtime import ProcessRuntime, TmuxRuntime


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocolConformance:
    def test_tmux_runtime_satisfies_protocol(self):
        rt = TmuxRuntime()
        assert isinstance(rt, ProcessRuntime)


# ---------------------------------------------------------------------------
# PTY operations
# ---------------------------------------------------------------------------

class TestPtyOps:
    def test_pty_write_delegates_to_os_write(self):
        rt = TmuxRuntime()
        rt._master_fd = 42
        with patch("os.write") as mock_write:
            rt.pty_write(b"hello")
            mock_write.assert_called_once_with(42, b"hello")

    def test_pty_write_raises_without_fd(self):
        rt = TmuxRuntime()
        with pytest.raises(RuntimeError, match="No PTY fd"):
            rt.pty_write(b"hello")

    def test_pty_read_delegates_to_os_read(self):
        rt = TmuxRuntime()
        rt._master_fd = 42
        with patch("os.read", return_value=b"data") as mock_read:
            result = rt.pty_read(1024)
            mock_read.assert_called_once_with(42, 1024)
            assert result == b"data"

    def test_pty_read_raises_without_fd(self):
        rt = TmuxRuntime()
        with pytest.raises(RuntimeError, match="No PTY fd"):
            rt.pty_read()

    def test_write_command_appends_cr(self):
        rt = TmuxRuntime()
        rt._master_fd = 42
        with patch("os.write") as mock_write:
            rt.write_command("ls -la")
            mock_write.assert_called_once_with(42, b"ls -la\r")

    def test_write_command_empty_string(self):
        """write_command('') should still send just CR."""
        rt = TmuxRuntime()
        rt._master_fd = 42
        with patch("os.write") as mock_write:
            rt.write_command("")
            mock_write.assert_called_once_with(42, b"\r")

    def test_close_fd_clears_state(self):
        rt = TmuxRuntime()
        rt._master_fd = 42
        rt._child_pid = 123
        with patch("os.close") as mock_close:
            rt.close_fd()
            mock_close.assert_called_once_with(42)
        assert rt._master_fd is None
        assert rt._child_pid is None

    def test_close_fd_noop_when_no_fd(self):
        rt = TmuxRuntime()
        rt.close_fd()  # Should not raise
        assert rt._master_fd is None

    def test_has_fd_property(self):
        rt = TmuxRuntime()
        assert rt.has_fd is False
        rt._master_fd = 42
        assert rt.has_fd is True


# ---------------------------------------------------------------------------
# send-keys
# ---------------------------------------------------------------------------

class TestSendKeys:
    @pytest.mark.asyncio
    async def test_send_keys_basic(self):
        rt = TmuxRuntime()
        with patch("mobile_terminal.runtime.run_subprocess", new_callable=AsyncMock) as mock_sub:
            await rt.send_keys("main:0", "ls", "Enter")
            mock_sub.assert_called_once()
            cmd = mock_sub.call_args[0][0]
            assert cmd == ["tmux", "send-keys", "-t", "main:0", "ls", "Enter"]

    @pytest.mark.asyncio
    async def test_send_keys_literal_flag(self):
        rt = TmuxRuntime()
        with patch("mobile_terminal.runtime.run_subprocess", new_callable=AsyncMock) as mock_sub:
            await rt.send_keys("main:0", "hello world", literal=True)
            cmd = mock_sub.call_args[0][0]
            assert "-l" in cmd
            assert cmd == ["tmux", "send-keys", "-t", "main:0", "-l", "hello world"]

    @pytest.mark.asyncio
    async def test_send_keys_special_key(self):
        rt = TmuxRuntime()
        with patch("mobile_terminal.runtime.run_subprocess", new_callable=AsyncMock) as mock_sub:
            await rt.send_keys("main:0.1", "C-c")
            cmd = mock_sub.call_args[0][0]
            assert cmd == ["tmux", "send-keys", "-t", "main:0.1", "C-c"]

    @pytest.mark.asyncio
    async def test_send_keys_multiple_args(self):
        rt = TmuxRuntime()
        with patch("mobile_terminal.runtime.run_subprocess", new_callable=AsyncMock) as mock_sub:
            await rt.send_keys("s:0", "echo test", "Enter")
            cmd = mock_sub.call_args[0][0]
            assert cmd == ["tmux", "send-keys", "-t", "s:0", "echo test", "Enter"]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_terminate_sends_sigterm(self):
        rt = TmuxRuntime()
        rt._child_pid = 999

        with patch("os.kill") as mock_kill, \
             patch("time.sleep"):
            # After SIGTERM, process is dead (os.kill(0) raises)
            mock_kill.side_effect = [None, OSError("No such process")]
            method = rt.terminate(force=False)
            assert method == "SIGTERM"
            mock_kill.assert_any_call(999, signal.SIGTERM)

    def test_terminate_force_sends_sigkill(self):
        rt = TmuxRuntime()
        rt._child_pid = 999

        with patch("os.kill") as mock_kill, \
             patch("time.sleep"):
            # After SIGTERM, process still alive, then SIGKILL
            mock_kill.side_effect = [None, None, None]
            method = rt.terminate(force=True)
            assert method == "SIGKILL"
            mock_kill.assert_any_call(999, signal.SIGKILL)

    def test_terminate_already_dead(self):
        rt = TmuxRuntime()
        rt._child_pid = 999

        with patch("os.kill", side_effect=ProcessLookupError):
            method = rt.terminate()
            assert method == "already_dead"

    def test_terminate_no_pid(self):
        rt = TmuxRuntime()
        method = rt.terminate()
        assert method == "already_dead"


# ---------------------------------------------------------------------------
# Window operations
# ---------------------------------------------------------------------------

class TestWindowOps:
    @pytest.mark.asyncio
    async def test_new_window_calls_tmux(self):
        rt = TmuxRuntime()
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("mobile_terminal.runtime.run_subprocess",
                    new_callable=AsyncMock, return_value=mock_result) as mock_sub:
            target = await rt.new_window("main", "worker", cwd="/tmp")
            cmd = mock_sub.call_args[0][0]
            assert cmd == ["tmux", "new-window", "-a", "-t", "main", "-n", "worker", "-c", "/tmp"]
            assert target == "main:worker"

    @pytest.mark.asyncio
    async def test_new_window_no_cwd(self):
        rt = TmuxRuntime()
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("mobile_terminal.runtime.run_subprocess",
                    new_callable=AsyncMock, return_value=mock_result):
            await rt.new_window("main", "test")

    @pytest.mark.asyncio
    async def test_new_window_failure_raises(self):
        rt = TmuxRuntime()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error creating window"
        with patch("mobile_terminal.runtime.run_subprocess",
                    new_callable=AsyncMock, return_value=mock_result):
            with pytest.raises(RuntimeError, match="error creating window"):
                await rt.new_window("main", "fail")

    @pytest.mark.asyncio
    async def test_kill_window_calls_tmux(self):
        rt = TmuxRuntime()
        with patch("mobile_terminal.runtime.run_subprocess", new_callable=AsyncMock) as mock_sub:
            await rt.kill_window("main:worker")
            cmd = mock_sub.call_args[0][0]
            assert cmd == ["tmux", "kill-window", "-t", "main:worker"]

    @pytest.mark.asyncio
    async def test_capture_pane_basic(self):
        rt = TmuxRuntime()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "line1\nline2\n"
        with patch("mobile_terminal.runtime.run_subprocess",
                    new_callable=AsyncMock, return_value=mock_result) as mock_sub:
            content = await rt.capture_pane("main:0", lines=30)
            cmd = mock_sub.call_args[0][0]
            assert cmd == ["tmux", "capture-pane", "-p", "-S", "-30", "-t", "main:0"]
            assert content == "line1\nline2\n"

    @pytest.mark.asyncio
    async def test_capture_pane_with_ansi(self):
        rt = TmuxRuntime()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "\x1b[32mgreen\x1b[0m\n"
        with patch("mobile_terminal.runtime.run_subprocess",
                    new_callable=AsyncMock, return_value=mock_result) as mock_sub:
            content = await rt.capture_pane("main:0", lines=50, ansi=True)
            cmd = mock_sub.call_args[0][0]
            assert "-e" in cmd
            assert content == "\x1b[32mgreen\x1b[0m\n"

    @pytest.mark.asyncio
    async def test_display_message_returns_stdout(self):
        rt = TmuxRuntime()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "  some title  \n"
        with patch("mobile_terminal.runtime.run_subprocess",
                    new_callable=AsyncMock, return_value=mock_result) as mock_sub:
            result = await rt.display_message("main:0", "#{pane_title}")
            assert result == "some title"
            cmd = mock_sub.call_args[0][0]
            assert cmd == ["tmux", "display-message", "-t", "main:0", "-p", "#{pane_title}"]

    @pytest.mark.asyncio
    async def test_list_panes_with_format(self):
        rt = TmuxRuntime()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "0:0\n1:0\n"
        with patch("mobile_terminal.runtime.run_subprocess",
                    new_callable=AsyncMock, return_value=mock_result) as mock_sub:
            raw = await rt.list_panes("main", fmt="#{window_index}:#{pane_index}")
            cmd = mock_sub.call_args[0][0]
            assert cmd == ["tmux", "list-panes", "-s", "-t", "main",
                           "-F", "#{window_index}:#{pane_index}"]
            assert "0:0" in raw

    @pytest.mark.asyncio
    async def test_pipe_pane_start_stop(self):
        rt = TmuxRuntime()
        with patch("mobile_terminal.runtime.run_subprocess", new_callable=AsyncMock) as mock_sub:
            # Start piping
            await rt.pipe_pane("main:0", "cat > /tmp/log")
            start_cmd = mock_sub.call_args_list[0][0][0]
            assert start_cmd == ["tmux", "pipe-pane", "-t", "main:0", "cat > /tmp/log"]

            # Stop piping
            await rt.pipe_pane("main:0")
            stop_cmd = mock_sub.call_args_list[1][0][0]
            assert stop_cmd == ["tmux", "pipe-pane", "-t", "main:0"]


# ---------------------------------------------------------------------------
# Terminal size
# ---------------------------------------------------------------------------

class TestSetSize:
    def test_set_size_calls_ioctl_and_sigwinch(self):
        rt = TmuxRuntime()
        rt._master_fd = 42
        rt._child_pid = 999

        with patch("fcntl.ioctl") as mock_ioctl, \
             patch("os.kill") as mock_kill:
            rt.set_size(120, 40)
            mock_ioctl.assert_called_once()
            fd_arg = mock_ioctl.call_args[0][0]
            assert fd_arg == 42
            mock_kill.assert_called_once_with(999, signal.SIGWINCH)

    def test_set_size_raises_without_fd(self):
        rt = TmuxRuntime()
        with pytest.raises(RuntimeError, match="No PTY fd"):
            rt.set_size(80, 24)


# ---------------------------------------------------------------------------
# InputQueue integration
# ---------------------------------------------------------------------------

class TestInputQueueIntegration:
    def test_input_queue_accepts_runtime(self):
        from mobile_terminal.models import InputQueue
        rt = TmuxRuntime()
        iq = InputQueue(runtime=rt)
        assert iq._runtime is rt

    @pytest.mark.asyncio
    async def test_writer_loop_uses_runtime_pty_write(self):
        """Verify InputQueue._writer_loop calls runtime.pty_write instead of os.write."""
        from mobile_terminal.models import InputQueue
        rt = TmuxRuntime()
        rt._master_fd = 42  # Pretend we have a fd

        iq = InputQueue(runtime=rt)

        with patch.object(rt, "pty_write") as mock_write:
            # Put an item directly into the queue
            await iq._queue.put(("msg1", b"test data", None))

            # Run one iteration of the writer loop manually
            iq._running = True

            # Get the item
            msg_id, data, websocket = await asyncio.wait_for(
                iq._queue.get(), timeout=1.0
            )

            # Simulate what _writer_loop does
            rt.pty_write(data)
            mock_write.assert_called_once_with(b"test data")

    def test_send_signature_no_master_fd(self):
        """Verify InputQueue.send() no longer takes master_fd parameter."""
        from mobile_terminal.models import InputQueue
        import inspect
        sig = inspect.signature(InputQueue.send)
        param_names = list(sig.parameters.keys())
        assert "master_fd" not in param_names
        assert "msg_id" in param_names
        assert "data" in param_names
        assert "websocket" in param_names
